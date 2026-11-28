from django.db import migrations, models


def forwards(apps, schema_editor):
    conn = schema_editor.connection
    vendor = conn.vendor
    with conn.cursor() as cursor:
        if vendor == "postgresql":
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='videos_category' AND column_name='parent_id'"
            )
            exists = cursor.fetchone() is not None
        else:
            cursor.execute("PRAGMA table_info(videos_category)")
            exists = any(row[1] == "parent_id" for row in cursor.fetchall())
        if not exists:
            if vendor == "postgresql":
                cursor.execute(
                    "ALTER TABLE videos_category ADD COLUMN parent_id bigint "
                    "REFERENCES videos_category(id) ON DELETE CASCADE"
                )
            else:
                cursor.execute(
                    "ALTER TABLE videos_category ADD COLUMN parent_id integer "
                    "REFERENCES videos_category(id) ON DELETE CASCADE"
                )


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("videos", "0022_remove_orator_categories"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
