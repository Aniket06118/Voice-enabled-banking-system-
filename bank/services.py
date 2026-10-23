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
import re
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from .categories import categorize
from .models import Account, AuditLog, Payment

PIN_THRESHOLD = Decimal("500")      # below this: voice confirm; at/above: PIN
VOICE_ONLY_HOURLY_CAP = Decimal("1500")  # more than this in "yes"-only payments within an hour -> PIN
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


def _voice_only_last_hour(account) -> Decimal:
    """Money sent in the last hour with only a spoken/tapped 'yes' (no PIN)."""
    return account.payments.filter(
        status=Payment.Status.SUCCESS, auth_used=Payment.Auth.VOICE_CONFIRM,
        completed_at__gte=timezone.now() - timedelta(hours=1),
    ).aggregate(t=Sum("amount"))["t"] or Decimal("0")


def _audit(account, event, **detail):
    AuditLog.objects.create(account=account, event=event, detail=detail)


def _spent_today(account) -> Decimal:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    total = account.payments.filter(
        status=Payment.Status.SUCCESS, completed_at__gte=start
    ).aggregate(t=Sum("amount"))["t"]
    return total or Decimal("0")


# Common phonetic mappings for Indic scripts to English contact names (from Aniket)
_INDIC_NAME_MAP = {
    # Hindi / Devanagari
    "राहुल": "rahul", "वर्मा": "verma", "वर्र्मा": "verma", "शर्मा": "sharma",
    "अमन": "aman", "मॉम": "mom", "माँ": "mom", "मां": "mom", "माता": "mom", "अम्मी": "mom", "मम्मी": "mom",
    # Tamil
    "ராகுல்": "rahul", "வர்மா": "verma", "சர்மா": "sharma", "அமன்": "aman", "அம்மா": "mom",
    # Telugu
    "రాహుల్": "rahul", "వర్మ": "verma", "వర్మా": "verma", "శర్మ": "sharma", "అమన్": "aman", "అమ్మ": "mom",
    # Bengali
    "রাহুল": "rahul", "বর্মা": "verma", "ভার্মা": "verma", "শর্মা": "sharma", "আমান": "aman", "মা": "mom",
    # Marathi / Gujarati
    "વર્મા": "verma", "શર્મા": "sharma", "રાહુલ": "rahul",
}


def _normalize_name(name: str) -> str:
    """Normalize input name, translating any known Indic script words into Latin equivalents."""
    cleaned = " ".join(str(name or "").lower().split())
    return " ".join(_INDIC_NAME_MAP.get(t, t) for t in cleaned.split())


def resolve_contact(account: Account, name: str) -> list:
    """Find contacts matching a spoken name (English or Indian scripts).
    0 items = not found, 1 = match, 2+ = ambiguous ("which Rahul?")."""
    raw_wanted = " ".join(str(name or "").lower().split())
    norm_wanted = _normalize_name(name)
    contacts = list(account.contacts.all())

    for target in (norm_wanted, raw_wanted):          # 1. exact match
        exact = [c for c in contacts if c.name.lower() == target]
        if len(exact) == 1:
            return exact

    def match(c, target):                              # 2. word / sub-token match
        if not target:
            return False
        c_words = c.name.lower().split()
        t_words = target.split()
        return (target in c.name.lower() or all(w in c_words for w in t_words)
                or all(w in c.name.lower() for w in t_words))

    return [c for c in contacts if match(c, norm_wanted) or match(c, raw_wanted)]


# ------------------------------------------------------------- step 1: initiate

def initiate_payment(account: Account, recipient_name, amount, idempotency_key=None, reference="") -> dict:
    """Validate a payment request and create it as PENDING (no money moves yet).
    `reference` is what the customer typed for a bill or recharge (consumer number,
    mobile number). It is saved with the payment and shown on the statement."""
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
    reference = re.sub(r"[^0-9A-Za-z \-/]", "", str(reference or "")).strip()[:40]
    if contact.kind == "recharge" and reference and not re.fullmatch(r"[6-9]\d{9}", reference):
        return _result(False, "bad_reference", "Please enter a 10-digit mobile number to recharge.")

    # A frozen card blocks shop and service payments (the card's job), never UPI to people or bills.
    if contact.kind == "merchant" and account.card_frozen:
        return _result(False, "card_frozen",
                       f"Your card is frozen, so payments to {contact.name} are blocked. "
                       "You can unfreeze it on the Cards page.")

    if amount > account.balance:
        return _result(False, "insufficient_balance",
                       f"Insufficient balance. Your current balance is {fmt(account.balance)}.")
    if _spent_today(account) + amount > account.daily_limit:
        return _result(False, "daily_limit_exceeded",
                       f"This would go over your daily limit of {fmt(account.daily_limit)}.")

    auth = required_auth(amount)
    if auth == Payment.Auth.VOICE_CONFIRM and _voice_only_last_hour(account) + amount > VOICE_ONLY_HOURLY_CAP:
        # Many small "yes" payments add up: past the hourly cap, the PIN is needed again.
        auth = Payment.Auth.PIN
    try:
        payment = Payment.objects.create(
            account=account, contact=contact, recipient_name=contact.name,
            upi_id=contact.upi_id, amount=amount, auth_required=auth,
            idempotency_key=idempotency_key or None,
            category=categorize(contact.name, contact.kind),
            reference=reference,
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

def gold_holding(account: Account) -> dict:
    """Digital gold = every completed payment to the account's gold contact, at the
    bank's gold price (settings.GOLD_PRICE_PER_GRAM)."""
    from django.conf import settings
    price = Decimal(str(settings.GOLD_PRICE_PER_GRAM))
    invested = account.payments.filter(status=Payment.Status.SUCCESS, contact__kind="gold") \
        .aggregate(t=Sum("amount"))["t"] or Decimal("0")
    invested = _money(invested)
    return {"price_per_gram": str(price), "invested": str(invested),
            "grams": str((invested / price).quantize(Decimal("0.0001"))) if price else "0"}


def card_details(account: Account) -> dict:
    """The virtual debit card, derived from the account itself (nothing made up)."""
    issued = timezone.localtime(account.created_at)
    return {"last4": account.account_number[-4:],
            "valid_till": f"{issued.month:02d}/{(issued.year + 5) % 100:02d}",
            "frozen": bool(account.card_frozen)}


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
        "member_since": timezone.localtime(account.created_at).date().isoformat(),
        "card": card_details(account),
        "gold": gold_holding(account),
        "contacts": [{"id": c.id, "name": c.name, "upi_id": c.upi_id, "kind": c.kind} for c in account.contacts.all()],
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
            "category": p.category,
            "reference": p.reference or "",
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
    "contact_added": lambda d: f"{d.get('name', 'A contact')} was added to your contacts",
    "contact_removed": lambda d: f"{d.get('name', 'A contact')} was removed from your contacts",
    "signed_up": lambda d: "Welcome to EchoMint! Your account is ready",
    "password_reset": lambda d: "Your password was reset using your PIN",
    "card_frozen": lambda d: "Your card was frozen",
    "card_unfrozen": lambda d: "Your card was unfrozen",
}
NOTICE_LEVEL = {"contact_added": "info", "contact_removed": "info", "signed_up": "info", "wrong_pin": "warn", "pin_locked": "warn", "voice_failed": "warn",
                "pin_changed": "info", "daily_limit_changed": "info", "password_reset": "warn",
                "card_frozen": "info", "card_unfrozen": "info"}


def notifications(acct, limit=8) -> list:
    rows = AuditLog.objects.filter(account=acct, event__in=list(NOTICE_TEXT)).order_by("-created_at")[:limit]
    return [{"text": NOTICE_TEXT[r.event](r.detail or {}), "level": NOTICE_LEVEL.get(r.event, "ok"),
             "timestamp": r.created_at.isoformat()} for r in rows]


# ------------------------------------------------------------- sign up + contacts

STARTER_BALANCE = Decimal("10000")   # demo money for new accounts in this prototype
DEFAULT_PAYEES = [  # every new account can pay these bills from day one
    ("Adani Electricity", "adanielectricity@billpay", "biller"),
    ("Delhi Jal Board", "djb@billpay", "biller"),
    ("Indraprastha Gas", "igl@billpay", "biller"),
    ("Airtel Broadband", "airtelbroadband@billpay", "biller"),
    ("Jio Prepaid", "jio@recharge", "recharge"),
    ("Airtel Prepaid", "airtel@recharge", "recharge"),
    ("Vi Prepaid", "vi@recharge", "recharge"),
    ("EchoMint Gold", "gold@echomint", "gold"),
]
WEAK_PINS = {"0000", "1111", "1234", "4321", "1212", "0123", "9999", "2580"}


def normalize_phone(raw) -> str:
    """'+91 98111-22233', '09811122233', '919811122233' -> '9811122233'."""
    return re.sub(r"\D", "", str(raw or ""))[-10:]


def masked_phone(phone: str) -> str:
    return "+91 ••••• •" + phone[-4:]


def password_problem(password: str):
    """The password rule (same at sign-up and reset). Returns an error result or None."""
    if len(str(password or "")) < 6:
        return _result(False, "bad_password", "Your password needs at least 6 characters.")
    return None


def validate_signup(full_name, phone, password, pin) -> dict:
    """Check every sign-up field WITHOUT creating anything (the phone is verified first)."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    full_name = " ".join(str(full_name or "").split())
    phone = normalize_phone(phone)
    password = str(password or "")
    pin = str(pin or "")
    if not (2 <= len(full_name) <= 80):
        return _result(False, "bad_name", "Please enter your full name.")
    if not re.fullmatch(r"[6-9]\d{9}", phone):
        return _result(False, "bad_phone", "Please enter a 10-digit Indian mobile number.")
    problem = password_problem(password)
    if problem:
        return problem
    if not re.fullmatch(r"\d{4}", pin):
        return _result(False, "bad_pin", "Your PIN must be exactly 4 digits.")
    if pin in WEAK_PINS or len(set(pin)) == 1:
        return _result(False, "weak_pin", "That PIN is too easy to guess. Please choose another.")
    if User.objects.filter(username=phone).exists() or Account.objects.filter(phone=phone).exists():
        return _result(False, "phone_taken", "An account with this phone number already exists. Please log in.")
    return _result(True, "valid", "OK", full_name=full_name, phone=phone)


def open_account(full_name, phone, password_hash, pin_hash) -> dict:
    """Create the user + account from already-validated details. Only HASHES of the
    password and PIN are passed in."""
    import secrets as _secrets
    from django.contrib.auth import get_user_model
    User = get_user_model()
    with transaction.atomic():
        if User.objects.filter(username=phone).exists() or Account.objects.filter(phone=phone).exists():
            return _result(False, "phone_taken", "An account with this phone number already exists. Please log in.")
        user = User.objects.create_user(username=phone)
        user.password = password_hash
        user.save()
        number = None
        while number is None or Account.objects.filter(account_number=number).exists():
            number = str(_secrets.randbelow(9 * 10**11) + 10**11)
        acct = Account(user=user, full_name=full_name, phone=phone, account_number=number,
                       bank_name="Lotus Bank", balance=STARTER_BALANCE, pin_hash=pin_hash)
        acct.save()
        from .models import Contact
        for name, upi, kind in DEFAULT_PAYEES:
            Contact.objects.create(account=acct, name=name, upi_id=upi, kind=kind)
        _audit(acct, "signed_up", phone_verified=True)
    return _result(True, "signed_up", f"Welcome to EchoMint, {full_name.split()[0]}!", phone=phone)


def create_customer(full_name, phone, password, pin) -> dict:
    """Validate + open in one go (used when phone verification is switched off)."""
    from django.contrib.auth.hashers import make_password
    checked = validate_signup(full_name, phone, password, pin)
    if not checked["ok"]:
        return checked
    return open_account(checked["full_name"], checked["phone"], make_password(str(password)), make_password(str(pin)))


def reset_password(phone, pin, new_password) -> dict:
    """Forgot password: the registered mobile number + the account's 4-digit PIN prove
    it's the owner, then the password is replaced. Wrong PINs count towards the normal
    PIN lockout (3 tries, then 15 minutes), so the PIN can't be guessed. Changing the
    password also signs out every other session of this user (Django does this)."""
    problem = password_problem(new_password)
    if problem:
        return problem
    wrong = _result(False, "wrong_details", "That mobile number and PIN don't match an account.")
    acct = Account.objects.select_related("user").filter(phone=normalize_phone(phone)).first()
    if acct is None or not acct.pin_hash:
        return wrong                       # same answer as a wrong PIN: nobody can probe for accounts
    checked = verify_pin(acct, pin)
    if not checked["ok"]:
        return checked if checked["code"] == "pin_locked" else wrong
    acct.user.set_password(str(new_password))
    acct.user.save()
    _audit(acct, "password_reset")
    return _result(True, "password_reset", "Your password has been changed. Please log in with the new one.",
                   phone=acct.phone)


def set_card_frozen(acct, frozen: bool) -> dict:
    acct = type(acct).objects.get(pk=acct.pk)
    acct.card_frozen = bool(frozen)
    acct.save(update_fields=["card_frozen"])
    _audit(acct, "card_frozen" if acct.card_frozen else "card_unfrozen")
    return _result(True, "card_frozen" if acct.card_frozen else "card_unfrozen",
                   "Your card is frozen." if acct.card_frozen else "Your card is active again.",
                   frozen=acct.card_frozen)


def add_contact(acct, name, upi_id) -> dict:
    from .models import Contact
    name = " ".join(str(name or "").split())[:60]
    upi_id = str(upi_id or "").strip().lower()
    if len(name) < 2:
        return _result(False, "bad_name", "Please enter the person's name.")
    if not re.fullmatch(r"[a-z0-9._-]{2,64}@[a-z][a-z0-9.]{1,32}", upi_id):
        return _result(False, "bad_upi", "Please enter a valid UPI ID, like name@okbank.")
    if acct.contacts.filter(name__iexact=name).exists():
        return _result(False, "contact_exists", f"You already have a contact called {name}.")
    Contact.objects.create(account=acct, name=name, upi_id=upi_id, kind="person")
    _audit(acct, "contact_added", name=name)
    return _result(True, "contact_added", f"{name} has been added to your contacts.")


REMOVABLE_KINDS = ("person", "merchant")     # bills, recharges and gold are built-in payees


def remove_contact(acct, contact_id) -> dict:
    """Delete one of the customer's own contacts. Past payments keep their own copy of
    the name and UPI ID, so the statement and analytics don't change. A payment still
    waiting for a 'yes' or PIN to this contact is cancelled."""
    from .models import Contact
    try:
        contact = Contact.objects.get(pk=int(contact_id), account=acct)
    except (Contact.DoesNotExist, ValueError, TypeError):
        return _result(False, "contact_not_found", "That contact wasn't found.")
    if contact.kind not in REMOVABLE_KINDS:
        return _result(False, "contact_builtin", f"{contact.name} is a built-in payee and can't be removed.")
    name = contact.name
    with transaction.atomic():
        acct.payments.filter(contact=contact, status=Payment.Status.PENDING).update(
            status=Payment.Status.CANCELLED, failure_reason="Contact removed")
        contact.delete()
        _audit(acct, "contact_removed", name=name)
    return _result(True, "contact_removed", f"{name} has been removed from your contacts.")
