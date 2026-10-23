from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from bank.models import Account, Contact

DEMO_PHONE = "9876543210"
DEMO_PASSWORD = "demo1234"
DEMO_PIN = "1234"


class Command(BaseCommand):
    help = "Create the demo customer (Aniket) with contacts and a starting balance. Safe to re-run."

    def handle(self, *args, **options):
        User = get_user_model()
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

        for name, upi in [
            ("Rahul Sharma", "rahul.sharma@upi"),
            ("Rahul Verma", "rahul.verma@upi"),
            ("Mom", "mom@upi"),
            ("Aman", "aman@upi"),
        ]:
            Contact.objects.get_or_create(account=account, name=name, defaults={"upi_id": upi})

        self.stdout.write(self.style.SUCCESS(
            "Demo user ready ->  phone: %s   password: %s   PIN: %s   balance: Rs 10,000"
            % (DEMO_PHONE, DEMO_PASSWORD, DEMO_PIN)))
