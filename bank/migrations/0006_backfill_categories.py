from django.db import migrations


def backfill(apps, schema_editor):
    from bank.categories import categorize
    Payment = apps.get_model("bank", "Payment")
    for p in Payment.objects.select_related("contact").iterator():
        cat = categorize(p.recipient_name, p.contact.kind if p.contact_id else None)
        if p.category != cat:
            Payment.objects.filter(pk=p.pk).update(category=cat)


class Migration(migrations.Migration):
    dependencies = [("bank", "0005_payment_category_analysis")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
