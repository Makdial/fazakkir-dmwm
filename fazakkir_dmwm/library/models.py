"""Data models for the digital library (books in PDF format)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=40, blank=True, default="book")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name) or "category"
        super().save(*args, **kwargs)


class Book(models.Model):
    """A book published in PDF format."""

    LANGUAGE_CHOICES = [
        ("auto", _("Auto-detect")),
        ("en", _("English")),
        ("fr", _("French")),
        ("ar", _("Arabic")),
        ("es", _("Spanish")),
        ("de", _("German")),
        ("it", _("Italian")),
        ("pt", _("Portuguese")),
        ("ru", _("Russian")),
        ("zh", _("Chinese (Simplified)")),
        ("zh-TW", _("Chinese (Traditional)")),
        ("ja", _("Japanese")),
        ("ko", _("Korean")),
        ("tr", _("Turkish")),
        ("nl", _("Dutch")),
        ("pl", _("Polish")),
        ("hi", _("Hindi")),
        ("ur", _("Urdu")),
        ("wo", _("Wolof")),
        ("bn", _("Bengali")),
        ("fa", _("Persian")),
        ("he", _("Hebrew")),
        ("id", _("Indonesian")),
        ("ms", _("Malay")),
        ("th", _("Thai")),
        ("vi", _("Vietnamese")),
        ("tl", _("Tagalog")),
        ("sw", _("Swahili")),
        ("am", _("Amharic")),
        ("ha", _("Hausa")),
        ("yo", _("Yoruba")),
        ("ig", _("Igbo")),
        ("zu", _("Zulu")),
        ("xh", _("Xhosa")),
        ("af", _("Afrikaans")),
        ("sq", _("Albanian")),
        ("hy", _("Armenian")),
        ("az", _("Azerbaijani")),
        ("eu", _("Basque")),
        ("be", _("Belarusian")),
        ("bs", _("Bosnian")),
        ("bg", _("Bulgarian")),
        ("ca", _("Catalan")),
        ("hr", _("Croatian")),
        ("cs", _("Czech")),
        ("da", _("Danish")),
        ("et", _("Estonian")),
        ("fi", _("Finnish")),
        ("gl", _("Galician")),
        ("ka", _("Georgian")),
        ("el", _("Greek")),
        ("gu", _("Gujarati")),
        ("hu", _("Hungarian")),
        ("is", _("Icelandic")),
        ("kn", _("Kannada")),
        ("kk", _("Kazakh")),
        ("km", _("Khmer")),
        ("ky", _("Kyrgyz")),
        ("lo", _("Lao")),
        ("lv", _("Latvian")),
        ("lt", _("Lithuanian")),
        ("mk", _("Macedonian")),
        ("ml", _("Malayalam")),
        ("mt", _("Maltese")),
        ("mr", _("Marathi")),
        ("mn", _("Mongolian")),
        ("ne", _("Nepali")),
        ("no", _("Norwegian")),
        ("pa", _("Punjabi")),
        ("ro", _("Romanian")),
        ("sr", _("Serbian")),
        ("si", _("Sinhala")),
        ("sk", _("Slovak")),
        ("sl", _("Slovenian")),
        ("so", _("Somali")),
        ("su", _("Sundanese")),
        ("sv", _("Swedish")),
        ("ta", _("Tamil")),
        ("te", _("Telugu")),
        ("uk", _("Ukrainian")),
        ("uz", _("Uzbek")),
        ("cy", _("Welsh")),
    ]

    STATUS_CHOICES = [
        ("draft", _("Draft")),
        ("published", _("Published")),
        ("archived", _("Archived")),
    ]

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="uploaded_books",
    )
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    author = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="books"
    )
    language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default="en")
    tags = models.CharField(max_length=300, blank=True, help_text=_("Comma separated"))
    isbn = models.CharField(max_length=20, blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    published_year = models.PositiveIntegerField(null=True, blank=True)
    page_count = models.PositiveIntegerField(default=0)

    # R2 storage fields (mirroring the Video model).
    pdf_file_id = models.CharField(max_length=200, blank=True, default="")
    pdf_url = models.URLField(max_length=500, blank=True, default="")
    cover_file_id = models.CharField(max_length=200, blank=True, default="")
    cover_url = models.URLField(max_length=500, blank=True, default="")

    file_size = models.PositiveBigIntegerField(default=0)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="published")
    is_featured = models.BooleanField(default=False, db_index=True)
    allow_download = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)

    views = models.PositiveIntegerField(default=0)
    downloads = models.PositiveIntegerField(default=0)
    rating_sum = models.PositiveIntegerField(default=0)
    rating_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Book")
        verbose_name_plural = _("Books")

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "book"
            slug = base
            i = 1
            while Book.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def rating(self) -> float:
        if not self.rating_count:
            return 0.0
        return round(self.rating_sum / self.rating_count, 1)

    @property
    def pdf_download_url(self):
        from videos import r2_storage

        return r2_storage.download_url(self.pdf_file_id, f"{self.slug or 'book'}.pdf", self.pdf_url)

    @property
    def pdf_view_url(self):
        from videos import r2_storage

        return r2_storage.resolve_url(self.pdf_file_id, self.pdf_url)

    @property
    def cover_image_url(self):
        from videos import r2_storage

        return r2_storage.resolve_url(self.cover_file_id, self.cover_url)

    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class ReadingProgress(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reading_progress")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="progress")
    page = models.PositiveIntegerField(default=1)
    percent = models.FloatField(default=0.0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "book")
        verbose_name = _("Reading progress")

    def __str__(self) -> str:
        return f"{self.user} - {self.book} (p.{self.page})"


class Bookmark(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookmarks")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="bookmarks")
    page = models.PositiveIntegerField(default=1)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Bookmark")

    def __str__(self) -> str:
        return f"{self.user} - {self.book} p.{self.page}"


class Review(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="book_reviews")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(default=5)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "book")
        ordering = ["-created_at"]
        verbose_name = _("Review")

    def __str__(self) -> str:
        return f"{self.user} - {self.book} ({self.rating})"


class Favorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorite_books")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "book")
        verbose_name = _("Favorite")


class TranslationHistory(models.Model):
    """A saved translation performed by a user."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="translation_history")
    book = models.ForeignKey(Book, on_delete=models.SET_NULL, null=True, blank=True, related_name="translations")
    original_text = models.TextField()
    translated_text = models.TextField()
    source_lang = models.CharField(max_length=5, default="auto")
    target_lang = models.CharField(max_length=5)
    page = models.PositiveIntegerField(default=0)
    context_text = models.TextField(blank=True)
    is_saved = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Translation history")

    def __str__(self) -> str:
        return f"{self.user}: {self.original_text[:40]}"


class SavedHighlight(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_highlights")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="highlights")
    page = models.PositiveIntegerField(default=1)
    text = models.TextField()
    note = models.CharField(max_length=300, blank=True)
    color = models.CharField(max_length=20, default="yellow")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Saved highlight")


class ReadingSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reading_sessions")
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reading_sessions")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    pages_read = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = _("Reading session")


class PersonalCollection(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="collections")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    books = models.ManyToManyField(Book, blank=True, related_name="in_collections")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Personal collection")
        unique_together = ("user", "name")

    def __str__(self) -> str:
        return self.name