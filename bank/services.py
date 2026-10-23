"""
All banking rules live here. The website API *and* the voice agent's tools call
these functions, so there is exactly one place where money moves.

Payment flow (two steps, so the user always gets a chance to confirm):

    1. initiate_payment()  -> creates a PENDING payment, validates everything
    2. confirm_payment()   -> moves the money, atomically

Authentication rule (decided by the team):
    amount <  500  -> user just says "yes" (voice confirmation)
    amount >= 500  -> user must enter their 4-digit PIN on screen (like UPI)
"""
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from .models import Account, AuditLog, Payment

PIN_THRESHOLD = Decimal("500")      # below this: voice confirm; at/above: PIN
PENDING_TTL = timedelta(minutes=5)  # an unconfirmed payment expires
MAX_PIN_ATTEMPTS = 3
PIN_LOCK_DURATION = timedelta(minutes=15)


# ---------------------------------------------------------------- helpers

def _result(ok: bool, code: str, message: str, **extra) -> dict:
    """Every service returns this shape. `message` is safe to speak aloud."""
    return {"ok": ok, "code": code, "message": message, **extra}


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt(amount) -> str:
    amount = _money(amount)
    return f"₹{amount:,.0f}" if amount == amount.to_integral_value() else f"₹{amount:,.2f}"


def required_auth(amount: Decimal) -> str:
    return Payment.Auth.PIN if amount >= PIN_THRESHOLD else Payment.Auth.VOICE_CONFIRM


def _audit(account, event, **detail):
    AuditLog.objects.create(account=account, event=event, detail=detail)


def _spent_today(account) -> Decimal:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    total = account.payments.filter(
        status=Payment.Status.SUCCESS, completed_at__gte=start
    ).aggregate(t=Sum("amount"))["t"]
    return total or Decimal("0")


def resolve_contact(account: Account, name: str) -> list:
    """Find contacts matching a spoken name. Returns a list:
    0 items = not found, 1 = match, 2+ = ambiguous ("which Rahul?")."""
    wanted = " ".join(name.lower().split())
    contacts = list(account.contacts.all())

    exact = [c for c in contacts if c.name.lower() == wanted]
    if len(exact) == 1:
        return exact

    def word_match(c):
        words = c.name.lower().split()
        return wanted in c.name.lower() or all(w in words for w in wanted.split())

    return [c for c in contacts if word_match(c)]


# ------------------------------------------------------------- step 1: initiate

def initiate_payment(account: Account, recipient_name, amount, idempotency_key=None) -> dict:
    """Validate a payment request and create it as PENDING (no money moves yet)."""
    if not recipient_name or not str(recipient_name).strip():
        return _result(False, "missing_recipient",
                       "I need to know who to pay. Please tell me the recipient's name.")
    if amount is None:
        return _result(False, "missing_amount",
                       "I need to know how much to pay. Please tell me the amount.")
    try:
        amount = _money(amount)
    except (InvalidOperation, ValueError):
        return _result(False, "invalid_amount", "That amount doesn't look right.")
    if amount <= 0:
        return _result(False, "invalid_amount", "The amount must be greater than zero.")

    # Same request sent twice (retry, double tap) -> return the original payment.
    if idempotency_key:
        existing = account.payments.filter(idempotency_key=idempotency_key).first()
        if existing:
            return _describe_existing(existing)

    matches = resolve_contact(account, str(recipient_name))
    if not matches:
        return _result(False, "recipient_not_found",
                       f"'{recipient_name}' was not found in your contacts.")
    if len(matches) > 1:
        names = [c.name for c in matches]
        return _result(False, "ambiguous_recipient",
                       f"I found more than one match: {', '.join(names)}. Which one do you mean?",
                       options=names)
    contact = matches[0]

    if amount > account.balance:
        return _result(False, "insufficient_balance",
                       f"Insufficient balance. Your current balance is {fmt(account.balance)}.")
    if _spent_today(account) + amount > account.daily_limit:
        return _result(False, "daily_limit_exceeded",
                       f"This would go over your daily limit of {fmt(account.daily_limit)}.")

    auth = required_auth(amount)
    try:
        payment = Payment.objects.create(
            account=account, contact=contact, recipient_name=contact.name,
            upi_id=contact.upi_id, amount=amount, auth_required=auth,
            idempotency_key=idempotency_key or None,
        )
    except IntegrityError:  # two identical requests raced each other
        existing = account.payments.filter(idempotency_key=idempotency_key).first()
        if existing:
            return _describe_existing(existing)
        raise

    _audit(account, "payment_initiated", payment_id=str(payment.id),
           amount=str(amount), recipient=contact.name, auth=auth)
    return _pending_result(payment)


def _pending_result(payment: Payment) -> dict:
    base = dict(payment_id=str(payment.id), amount=str(payment.amount),
                recipient=payment.recipient_name)
    if payment.auth_required == Payment.Auth.PIN:
        return _result(True, "needs_pin",
                       f"To send {fmt(payment.amount)} to {payment.recipient_name}, "
                       "please enter your 4-digit PIN on the screen.", **base)
    return _result(True, "needs_confirmation",
                   f"Confirm sending {fmt(payment.amount)} to {payment.recipient_name}? "
                   "Say yes to confirm.", **base)


def _describe_existing(payment: Payment) -> dict:
    base = dict(payment_id=str(payment.id), amount=str(payment.amount),
                recipient=payment.recipient_name)
    if payment.status == Payment.Status.PENDING:
        return _pending_result(payment)
    if payment.status == Payment.Status.SUCCESS:
        return _result(True, "already_completed",
                       f"{fmt(payment.amount)} was already sent to {payment.recipient_name}.", **base)
    return _result(False, f"already_{payment.status.lower()}",
                   f"That payment was {payment.status.lower()}.", **base)


# -------------------------------------------------------------- step 2: confirm

def verify_pin(acct, pin) -> dict:
    """Check the account PIN with the same wrong-attempt lockout the payments use.
    Used as the backup way in when the voice check can't be passed."""
    now = timezone.now()
    with transaction.atomic():
        acct = type(acct).objects.select_for_update().get(pk=acct.pk)
        if acct.pin_locked_until and acct.pin_locked_until > now:
            mins = int((acct.pin_locked_until - now).total_seconds() // 60) + 1
            return _result(False, "pin_locked", f"Too many wrong PINs. Try again in {mins} minutes.")
        if not pin or not acct.check_pin(str(pin)):
            acct.pin_failed_attempts += 1
            _audit(acct, "wrong_pin", context="voice_fallback", attempts=acct.pin_failed_attempts)
            if acct.pin_failed_attempts >= MAX_PIN_ATTEMPTS:
                acct.pin_locked_until = now + PIN_LOCK_DURATION
                acct.pin_failed_attempts = 0
                acct.save()
                _audit(acct, "pin_locked")
                return _result(False, "pin_locked", "Wrong PIN too many times. Locked for 15 minutes.")
            acct.save()
            left = MAX_PIN_ATTEMPTS - acct.pin_failed_attempts
            return _result(False, "wrong_pin",
                           f"Wrong PIN. You have {left} attempt{'s' if left != 1 else ''} left.")
        acct.pin_failed_attempts = 0
        acct.pin_locked_until = None
        acct.save()
    return _result(True, "pin_ok", "PIN accepted.")


def confirm_payment(account: Account, payment_id, pin=None) -> dict:
    """Move the money. One database transaction: it all happens or none of it."""
    now = timezone.now()
    with transaction.atomic():
        # Lock the rows so two requests can never spend the same balance twice.
        acct = Account.objects.select_for_update().get(pk=account.pk)
        payment = (
            Payment.objects.select_for_update().filter(pk=payment_id, account=acct).first()
            if _valid_uuid(payment_id) else None
        )
        if payment is None:
            return _result(False, "payment_not_found", "I couldn't find that payment.")

        if payment.status != Payment.Status.PENDING:
            return _describe_existing(payment)  # confirming twice never double-charges

        if now - payment.created_at > PENDING_TTL:
            payment.status = Payment.Status.FAILED
            payment.failure_reason = "Expired before confirmation"
            payment.completed_at = now
            payment.save()
            _audit(acct, "payment_expired", payment_id=str(payment.id))
            return _result(False, "expired",
                           "That payment request expired. Please start again.")

        if payment.auth_required == Payment.Auth.PIN:
            if acct.pin_locked_until and acct.pin_locked_until > now:
                mins = int((acct.pin_locked_until - now).total_seconds() // 60) + 1
                return _result(False, "pin_locked",
                               f"Too many wrong PINs. Try again in {mins} minutes.")
            if not pin:
                return _result(False, "pin_required",
                               "Please enter your 4-digit PIN to continue.")
            if not acct.check_pin(pin):
                acct.pin_failed_attempts += 1
                _audit(acct, "wrong_pin", payment_id=str(payment.id),
                       attempts=acct.pin_failed_attempts)
                if acct.pin_failed_attempts >= MAX_PIN_ATTEMPTS:
                    acct.pin_locked_until = now + PIN_LOCK_DURATION
                    acct.pin_failed_attempts = 0
                    payment.status = Payment.Status.FAILED
                    payment.failure_reason = "PIN locked after too many wrong attempts"
                    payment.completed_at = now
                    payment.save()
                    acct.save()
                    _audit(acct, "pin_locked")
                    return _result(False, "pin_locked",
                                   "Wrong PIN too many times. Payments needing a PIN "
                                   "are locked for 15 minutes.")
                acct.save()
                left = MAX_PIN_ATTEMPTS - acct.pin_failed_attempts
                return _result(False, "wrong_pin",
                               f"Wrong PIN. You have {left} attempt{'s' if left != 1 else ''} left.",
                               attempts_left=left, payment_id=str(payment.id))
            acct.pin_failed_attempts = 0
            acct.pin_locked_until = None

        # Re-check the money rules at the moment of payment (not just at the start).
        if payment.amount > acct.balance:
            payment.status = Payment.Status.FAILED
            payment.failure_reason = "Insufficient balance"
            payment.completed_at = now
            payment.save()
            acct.save()
            return _result(False, "insufficient_balance",
                           f"Insufficient balance. Your current balance is {fmt(acct.balance)}.")
        if _spent_today(acct) + payment.amount > acct.daily_limit:
            payment.status = Payment.Status.FAILED
            payment.failure_reason = "Daily limit exceeded"
            payment.completed_at = now
            payment.save()
            acct.save()
            return _result(False, "daily_limit_exceeded",
                           f"This would go over your daily limit of {fmt(acct.daily_limit)}.")

        acct.balance -= payment.amount
        acct.save()
        payment.status = Payment.Status.SUCCESS
        payment.auth_used = payment.auth_required
        payment.completed_at = now
        payment.save()
        _audit(acct, "payment_success", payment_id=str(payment.id),
               amount=str(payment.amount), recipient=payment.recipient_name,
               auth=payment.auth_used)

    account.refresh_from_db()
    return _result(True, "success",
                   f"{fmt(payment.amount)} has been successfully transferred to "
                   f"{payment.recipient_name}. Your remaining balance is {fmt(account.balance)}.",
                   payment_id=str(payment.id), new_balance=str(account.balance))


def cancel_payment(account: Account, payment_id) -> dict:
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(
            pk=payment_id, account=account).first() if _valid_uuid(payment_id) else None
        if payment is None:
            return _result(False, "payment_not_found", "I couldn't find that payment.")
        if payment.status != Payment.Status.PENDING:
            return _describe_existing(payment)
        payment.status = Payment.Status.CANCELLED
        payment.completed_at = timezone.now()
        payment.save()
        _audit(account, "payment_cancelled", payment_id=str(payment.id))
    return _result(True, "cancelled", "Okay, I've cancelled that payment.",
                   payment_id=str(payment.id))


def latest_pending(account: Account):
    """The newest payment still waiting for confirmation (and not expired)."""
    return account.payments.filter(
        status=Payment.Status.PENDING,
        created_at__gte=timezone.now() - PENDING_TTL,
    ).order_by("-created_at").first()


def supersede_older_pending(account: Account, keep_id) -> None:
    """Starting a new payment cancels any older unconfirmed one, so a spoken
    'yes' can only ever refer to the most recent request."""
    account.payments.filter(status=Payment.Status.PENDING).exclude(pk=keep_id).update(
        status=Payment.Status.CANCELLED, completed_at=timezone.now())


def _valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


# ------------------------------------------------------------- read-only queries

def get_user_details(account: Account) -> dict:
    return {
        "name": account.full_name,
        "phone": account.phone,
        "account_number": account.account_number,
        "bank_name": account.bank_name,
        "balance": str(account.balance),
        "currency": account.currency,
        "daily_limit": str(account.daily_limit),
        "spent_today": str(_spent_today(account)),
        "contacts": [{"name": c.name, "upi_id": c.upi_id, "kind": c.kind} for c in account.contacts.all()],
    }


def get_history(account: Account, limit=20, recipient=None, days=None) -> list:
    """Completed payments (success + failed), newest first."""
    qs = account.payments.exclude(
        status__in=[Payment.Status.PENDING, Payment.Status.CANCELLED]
    )
    if recipient:
        qs = qs.filter(recipient_name__icontains=recipient)
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=int(days)))
    return [
        {
            "recipient": p.recipient_name,
            "amount": float(p.amount),
            "status": p.get_status_display(),
            "timestamp": p.created_at.isoformat(),
            "verified_with": p.auth_used or None,
        }
        for p in qs[: int(limit)]
    ]


def spending_summary(account: Account, recipient=None, days=None) -> dict:
    """Totals computed by the database (exact), so the AI never does the maths."""
    qs = account.payments.filter(status=Payment.Status.SUCCESS)
    if recipient:
        qs = qs.filter(recipient_name__icontains=recipient)
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=int(days)))
    total = qs.aggregate(total=Sum("amount"), count=Count("id"))
    by_recipient = (
        qs.values("recipient_name").annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    return {
        "total_spent": float(total["total"] or 0),
        "payments": total["count"],
        "by_recipient": [
            {"recipient": r["recipient_name"], "total": float(r["total"]), "count": r["count"]}
            for r in by_recipient
        ],
    }


# ------------------------------------------------------------- account settings

MIN_DAILY_LIMIT = Decimal("500")
MAX_DAILY_LIMIT = Decimal("100000")


def change_pin(acct, old_pin, new_pin) -> dict:
    new_pin = str(new_pin or "")
    if not (len(new_pin) == 4 and new_pin.isdigit()):
        return _result(False, "bad_pin", "Your new PIN must be exactly 4 digits.")
    if new_pin in ("0000", "1111", "1234", "4321") or len(set(new_pin)) == 1:
        return _result(False, "weak_pin", "That PIN is too easy to guess. Please choose another.")
    checked = verify_pin(acct, old_pin)
    if not checked["ok"]:
        return checked
    acct = type(acct).objects.get(pk=acct.pk)
    acct.set_pin(new_pin)
    acct.save()
    _audit(acct, "pin_changed")
    return _result(True, "pin_changed", "Your PIN has been changed.")


def set_daily_limit(acct, limit, pin) -> dict:
    try:
        limit = _money(limit)
    except Exception:  # noqa: BLE001
        return _result(False, "invalid_amount", "Please enter the limit as a number.")
    if not (MIN_DAILY_LIMIT <= limit <= MAX_DAILY_LIMIT):
        return _result(False, "invalid_amount",
                       f"The daily limit must be between {fmt(MIN_DAILY_LIMIT)} and {fmt(MAX_DAILY_LIMIT)}.")
    checked = verify_pin(acct, pin)
    if not checked["ok"]:
        return checked
    acct = type(acct).objects.get(pk=acct.pk)
    old = acct.daily_limit
    acct.daily_limit = limit
    acct.save()
    _audit(acct, "daily_limit_changed", old=str(old), new=str(limit))
    return _result(True, "limit_changed", f"Your daily limit is now {fmt(limit)}.", daily_limit=str(limit))


def _fmt_str(v) -> str:
    try:
        return fmt(Decimal(str(v)))
    except Exception:  # noqa: BLE001
        return f"₹{v}"


NOTICE_TEXT = {
    "payment_success": lambda d: f"Paid {_fmt_str(d.get('amount'))} to {d.get('recipient', 'a contact')}",
    "wrong_pin": lambda d: "A wrong PIN was entered",
    "pin_locked": lambda d: "PIN payments were locked after too many wrong PINs",
    "pin_changed": lambda d: "Your PIN was changed",
    "daily_limit_changed": lambda d: f"Daily limit changed to {_fmt_str(d.get('new'))}",
    "voice_failed": lambda d: "A voice check did not match",
    "voice_enrolled": lambda d: "Your voice was saved",
    "voice_fallback_pin": lambda d: "You signed in with your PIN instead of your voice",
    "payment_expired": lambda d: "A payment request expired before it was confirmed",
}
NOTICE_LEVEL = {"wrong_pin": "warn", "pin_locked": "warn", "voice_failed": "warn",
                "pin_changed": "info", "daily_limit_changed": "info"}


def notifications(acct, limit=8) -> list:
    rows = AuditLog.objects.filter(account=acct, event__in=list(NOTICE_TEXT)).order_by("-created_at")[:limit]
    return [{"text": NOTICE_TEXT[r.event](r.detail or {}), "level": NOTICE_LEVEL.get(r.event, "ok"),
             "timestamp": r.created_at.isoformat()} for r in rows]
