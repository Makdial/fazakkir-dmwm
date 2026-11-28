from django.db import migrations

NAMES = [
    "Serigne Cheikh Ahmad Tidiane Sy Al Maktoum",
    "Serigne Moustapha Sy Al Mourchid",
    "Serigne Cheikh Ahmad Tidiane Sy Capitaine",
]


def forwards(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    Category.objects.filter(name__in=NAMES).delete()
    Category.objects.filter(name="Conferences").delete()


def backwards(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    parent, _ = Category.objects.get_or_create(name="Conferences", defaults={"slug": "conferences", "order": 0})
    for name in NAMES:
        Category.objects.get_or_create(name=name, defaults={"slug": name.lower().replace(" ", "-"), "parent": parent, "order": 0})


class Migration(migrations.Migration):
    dependencies = [
        ("videos", "0020_seed_conference_categories"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(
            model_name="category",
            name="parent",
        ),
    ]
