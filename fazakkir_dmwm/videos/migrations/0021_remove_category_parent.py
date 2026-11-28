from django.db import migrations, models


def _set_constraints_immediate(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _delete_orator_categories(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    for name in [
        "Serigne Cheikh Ahmad Tidiane Sy Al Maktoum",
        "Serigne Cheikh Ahmad Tidiane Sy Capitaine",
        "Serigne Moustapha Sy Al Mourchid",
        "Serigne Moustapha Sy Al Maktoum",
        "Serigne Moustapha Sy",
        "Conferences",
    ]:
        Category.objects.filter(name__iexact=name).delete()


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("videos", "0020_seed_conference_categories"),
    ]

    operations = [
        migrations.RunPython(_delete_orator_categories, migrations.RunPython.noop),
        migrations.RunPython(_set_constraints_immediate, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="category",
            name="parent",
        ),
    ]
