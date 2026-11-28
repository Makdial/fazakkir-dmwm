from django.db import migrations
from django.utils.text import slugify

NAMES = [
    "Serigne Cheikh Ahmad Tidiane Sy Al Maktoum",
    "Serigne Moustapha Sy Al Mourchid",
    "Serigne Cheikh Ahmad Tidiane Sy Capitaine",
]


def forwards(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    parent, _ = Category.objects.get_or_create(
        name="Conferences",
        defaults={"slug": "conferences", "order": 0},
    )
    for name in NAMES:
        Category.objects.get_or_create(
            name=name,
            defaults={"slug": slugify(name), "parent": parent, "order": 0},
        )


def backwards(apps, schema_editor):
    Category = apps.get_model("videos", "Category")
    Category.objects.filter(name__in=NAMES).delete()
    Category.objects.filter(name="Conferences").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("videos", "0019_category_parent"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
