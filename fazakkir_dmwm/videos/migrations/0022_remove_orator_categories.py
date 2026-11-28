from django.db import migrations

NAMES = [
    "Serigne Cheikh Ahmad Tidiane Sy Al Maktoum",
    "Serigne Cheikh Ahmad Tidiane Sy Capitaine",
    "Serigne Moustapha Sy Al Mourchid",
    "Serigne Moustapha Sy Al Maktoum",
    "Serigne Moustapha Sy",
]


def forwards(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    for name in NAMES:
        Category.objects.filter(name__iexact=name).delete()


def backwards(apps, schema_editor):
    # No-op: categories are user data.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("videos", "0021_remove_category_parent"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
