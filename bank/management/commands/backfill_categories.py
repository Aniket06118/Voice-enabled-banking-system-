from django.core.management.base import BaseCommand

from bank.categories import categorize
from bank.models import Payment


class Command(BaseCommand):
    help = "Re-assign spending categories to every payment using the rules in bank/categories.py."

    def handle(self, *args, **options):
        changed = 0
        for p in Payment.objects.select_related("contact").iterator():
            cat = categorize(p.recipient_name, p.contact.kind if p.contact_id else None)
            if p.category != cat:
                Payment.objects.filter(pk=p.pk).update(category=cat)
                changed += 1
        self.stdout.write(self.style.SUCCESS(f"Categorised {changed} payment(s)."))
