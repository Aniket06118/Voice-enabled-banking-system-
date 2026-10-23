from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

import random
from datetime import timedelta

from django.utils import timezone

from bank.categories import categorize
from bank.models import Account, Contact, Payment

DEMO_PIN = "1234"


class Command(BaseCommand):
    help = ("Create the demo customer (Aniket) with contacts and 6 months of payments. Safe to re-run. "
            "Use --phone 98XXXXXXXX to move the demo account to another mobile number.")

    def add_arguments(self, parser):
        parser.add_argument("--phone", help="Move the demo account to this 10-digit mobile number")

    def handle(self, *args, **options):
        from django.conf import settings
        from bank.services import normalize_phone
        DEMO_PHONE, DEMO_PASSWORD = settings.DEMO_PHONE, settings.DEMO_PASSWORD
        User = get_user_model()
        new_phone = normalize_phone(options.get("phone")) if options.get("phone") else None
        if new_phone:
            existing = Account.objects.filter(phone=DEMO_PHONE).first() or Account.objects.filter(account_number="123456789").first()
            if Account.objects.filter(phone=new_phone).exclude(pk=getattr(existing, "pk", None)).exists():
                self.stderr.write(f"Another account already uses {new_phone}.")
                return
            if existing:
                existing.phone = new_phone
                existing.save(update_fields=["phone"])
                existing.user.username = new_phone
                existing.user.save(update_fields=["username"])
                self.stdout.write(self.style.SUCCESS(f"Demo account moved to {new_phone}. Put DEMO_PHONE={new_phone} in .env."))
            DEMO_PHONE = new_phone
        user, created = User.objects.get_or_create(username=DEMO_PHONE)
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()

        account, acct_created = Account.objects.get_or_create(
            user=user,
            defaults=dict(
                full_name="Aniket Sharma", phone=DEMO_PHONE,
                account_number="123456789", bank_name="Lotus Bank",
                balance=Decimal("10000.00"),
            ),
        )
        if acct_created:
            account.set_pin(DEMO_PIN)
            account.save()

        for name, upi, kind in [
            ("Rahul Sharma", "rahul.sharma@upi", "person"),
            ("Rahul Verma", "rahul.verma@upi", "person"),
            ("Mom", "mom@upi", "person"),
            ("Aman", "aman@upi", "person"),
            # bills, recharges and gold are paid like any payee, so the voice agent can
            # handle "pay my electricity bill" with the same safety rules
            ("Adani Electricity", "adanielectricity@billpay", "biller"),
            ("Delhi Jal Board", "djb@billpay", "biller"),
            ("Indraprastha Gas", "igl@billpay", "biller"),
            ("Airtel Broadband", "airtelbroadband@billpay", "biller"),
            ("Jio Prepaid", "jio@recharge", "recharge"),
            ("Airtel Prepaid", "airtel@recharge", "recharge"),
            ("Vi Prepaid", "vi@recharge", "recharge"),
            ("EchoMint Gold", "gold@echomint", "gold"),
        ]:
            c, _ = Contact.objects.get_or_create(account=account, name=name,
                                                 defaults={"upi_id": upi, "kind": kind})
            if c.kind != kind:
                c.kind = kind
                c.save(update_fields=["kind"])

        made = seed_history(account)
        if made:
            self.stdout.write(f"Added {made} past payments over the last 6 months for the analytics page.")

        self.stdout.write(self.style.SUCCESS(
            "Demo user ready ->  phone: %s   password: %s   PIN: %s   balance: Rs 10,000"
            % (DEMO_PHONE, DEMO_PASSWORD, DEMO_PIN)))


# Shops and services the demo user pays, with a typical amount range and how often.
MERCHANTS = [
    ("Swiggy", 180, 650, 14), ("Zomato", 200, 700, 10), ("Chai Point", 40, 160, 6),
    ("Uber", 120, 480, 9), ("Ola", 100, 400, 5), ("IRCTC", 450, 2200, 3),
    ("Amazon", 300, 2800, 6), ("Flipkart", 400, 2400, 3),
    ("BigBasket", 600, 2100, 7), ("Blinkit", 150, 700, 8),
    ("Apollo Pharmacy", 120, 950, 4), ("PVR Cinemas", 350, 900, 2), ("Netflix", 199, 199, 1),
]
PEOPLE = [("Mom", 1000, 5000, 4), ("Aman", 100, 800, 4), ("Rahul Sharma", 200, 1500, 3)]
MONTHLY_BILLS = [("Adani Electricity", 900, 2400), ("Delhi Jal Board", 250, 600),
                 ("Airtel Broadband", 799, 799), ("Jio Prepaid", 299, 299)]


def seed_history(account) -> int:
    """~85 completed payments over the last 6 months. Deterministic and idempotent:
    it does nothing if the demo history is already there."""
    if account.payments.filter(idempotency_key__startswith="seed-").exists():
        return 0
    rng = random.Random(42)
    today = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
    plan = []
    for name, lo, hi, per_month in MERCHANTS + PEOPLE:
        for month in range(6):
            for _ in range(per_month if rng.random() < 0.9 else per_month - 1):
                if rng.random() < 0.87:          # thin it out to a realistic ~90 total
                    continue
                days_ago = month * 30 + rng.randint(1, 29)
                amount = lo if lo == hi else rng.randrange(lo, hi + 1, 1)
                plan.append((name, amount, days_ago))
    for month in range(6):
        for name, lo, hi in MONTHLY_BILLS:
            plan.append((name, lo if lo == hi else rng.randrange(lo, hi + 1, 1), month * 30 + rng.randint(3, 8)))

    kinds = {c.name: c for c in account.contacts.all()}
    made = 0
    for i, (name, amount, days_ago) in enumerate(plan):
        contact = kinds.get(name)
        if contact is None:
            contact, _ = Contact.objects.get_or_create(
                account=account, name=name,
                defaults={"upi_id": name.lower().replace(" ", "") + "@upi", "kind": "merchant"})
            kinds[name] = contact
        when = today - timedelta(days=days_ago, hours=rng.randint(0, 10), minutes=rng.randint(0, 59))
        auth = Payment.Auth.PIN if amount >= 500 else Payment.Auth.VOICE_CONFIRM
        p = Payment.objects.create(
            account=account, contact=contact, recipient_name=contact.name, upi_id=contact.upi_id,
            amount=amount, status=Payment.Status.SUCCESS, auth_required=auth, auth_used=auth,
            idempotency_key=f"seed-{i}", category=categorize(contact.name, contact.kind))
        Payment.objects.filter(pk=p.pk).update(created_at=when, completed_at=when)
        made += 1
    return made
