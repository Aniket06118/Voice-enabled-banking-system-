"""Move saved analyses to their own table, "bank_saved_analysis".

Why: on a shared database another version of the project had already created a
different "bank_analysis" table (without our "params" column), so inserts failed.
This migration never touches that old table. It simply makes sure our table
exists with the right columns, whatever state the database is in.
"""
from django.db import migrations


def create_if_missing(apps, schema_editor):
    Analysis = apps.get_model("bank", "Analysis")
    table = Analysis._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing = schema_editor.connection.introspection.table_names(cursor)
    if table not in existing:
        schema_editor.create_model(Analysis)


class Migration(migrations.Migration):
    dependencies = [("bank", "0007_contact_kind_merchant")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable(name="analysis", table="bank_saved_analysis")],
            database_operations=[],
        ),
        migrations.RunPython(create_if_missing, migrations.RunPython.noop),
    ]
