from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import Book
from videos import r2_storage


@receiver(pre_delete, sender=Book)
def _delete_book_files(sender, instance, **kwargs):
    for path in (instance.pdf_file_id, instance.cover_file_id):
        try:
            r2_storage.delete_file(path)
        except Exception:
            pass
