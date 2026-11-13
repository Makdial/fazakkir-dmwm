from django.apps import AppConfig


class LibraryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "library"
    verbose_name = "Library"

    def ready(self):
        from . import signals  # noqa: F401
