"""Account.card_frozen + Payment.reference.

Written the same defensive way as 0005/0008: the Neon database is shared, so a
column may already exist (added by another copy of the project). Each column is
only added if it is missing, and Django's own record of the models is updated
either way, so "migrate" never fails with "column already exists".
"""
from django.db import migrations, models


def add_if_missing(apps, schema_editor):
    conn = schema_editor.connection
    for model_name, field_name in (("Account", "card_frozen"), ("Payment", "reference")):
        model = apps.get_model("bank", model_name)
        with conn.cursor() as cursor:
            columns = [c.name for c in conn.introspection.get_table_description(cursor, model._meta.db_table)]
        if field_name not in columns:
            schema_editor.add_field(model, model._meta.get_field(field_name))


class Migration(migrations.Migration):
    dependencies = [("bank", "0008_saved_analysis_table")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(model_name="account", name="card_frozen",
                                    field=models.BooleanField(default=False)),
                migrations.AddField(model_name="payment", name="reference",
                                    field=models.CharField(blank=True, default="", max_length=40)),
            ],
            database_operations=[],
        ),
        migrations.RunPython(add_if_missing, migrations.RunPython.noop),
    ]
