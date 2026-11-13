from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    Book, Category, Bookmark, Favorite, ReadingProgress, Review,
    TranslationHistory, SavedHighlight, ReadingSession, PersonalCollection,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name",)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "language", "status", "is_featured", "views", "downloads", "created_at")
    list_filter = ("status", "language", "is_featured", "allow_download", "category")
    search_fields = ("title", "author", "tags", "isbn")
    readonly_fields = ("slug", "views", "downloads", "rating_sum", "rating_count", "created_at", "updated_at", "file_size")
    fieldsets = (
        (None, {"fields": ("title", "slug", "author", "description", "category", "language", "tags")}),
        (_("Publishing"), {"fields": ("status", "is_featured", "allow_download", "is_public", "isbn", "publisher", "published_year", "page_count")}),
        (_("Storage"), {"fields": ("pdf_file_id", "pdf_url", "cover_file_id", "cover_url", "file_size")}),
        (_("Stats"), {"fields": ("views", "downloads", "rating_sum", "rating_count", "created_at", "updated_at")}),
    )


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("book", "user", "rating", "created_at")
    list_filter = ("rating",)


admin.site.register(Bookmark)
admin.site.register(Favorite)
admin.site.register(ReadingProgress)
admin.site.register(TranslationHistory)
admin.site.register(SavedHighlight)
admin.site.register(ReadingSession)
admin.site.register(PersonalCollection)