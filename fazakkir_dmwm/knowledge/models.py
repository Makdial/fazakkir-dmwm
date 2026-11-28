"""Models for the Ask DMWM knowledge base.

The index stores page-aware book chunks and timestamp-aware video transcript
chunks.  Vectors are kept next to each chunk, so the same rows work on SQLite
during development and on Postgres in production without requiring a separate
vector service.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .prompts import effective_system_prompt


class KnowledgeChunk(models.Model):
    """One searchable passage from a book, video or short."""

    SOURCE_BOOK = "book"
    SOURCE_VIDEO = "video"
    SOURCE_SHORT = "short"
    SOURCE_CHOICES = [
        (SOURCE_BOOK, "Book"),
        (SOURCE_VIDEO, "Video"),
        (SOURCE_SHORT, "Short"),
    ]

    source_type = models.CharField(max_length=12, choices=SOURCE_CHOICES, db_index=True)
    source_id = models.PositiveIntegerField(db_index=True)
    book = models.ForeignKey(
        "library.Book",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="knowledge_chunks",
    )
    video = models.ForeignKey(
        "videos.Video",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="knowledge_chunks",
    )
    short = models.ForeignKey(
        "shorts.Short",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="knowledge_chunks",
    )

    title = models.CharField(max_length=250)
    author = models.CharField(max_length=200, blank=True, default="")
    url = models.CharField(max_length=500, blank=True, default="")
    language = models.CharField(max_length=12, blank=True, default="")

    page_start = models.PositiveIntegerField(null=True, blank=True)
    page_end = models.PositiveIntegerField(null=True, blank=True)
    timestamp_start = models.FloatField(null=True, blank=True)
    timestamp_end = models.FloatField(null=True, blank=True)

    chunk_index = models.PositiveIntegerField(default=0)
    content = models.TextField()
    search_text = models.TextField(db_index=False)
    embedding = models.JSONField(default=list, blank=True)
    embedding_provider = models.CharField(max_length=20, blank=True, default="")
    embedding_model = models.CharField(max_length=80, blank=True, default="")
    token_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_type", "source_id", "chunk_index"]
        verbose_name = "Knowledge chunk"
        verbose_name_plural = "Knowledge chunks"
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id", "chunk_index"],
                name="unique_chunk_per_source",
            )
        ]
        indexes = [
            models.Index(fields=["source_type", "source_id"]),
            models.Index(fields=["book"]),
            models.Index(fields=["video"]),
        ]

    def __str__(self) -> str:
        location = ""
        if self.page_start and (self.metadata or {}).get("location_kind", "page") == "page":
            location = f", p. {self.page_start}"
        elif self.location_label:
            location = f", {self.location_label}"
        return f"{self.title}{location}"

    @property
    def location_label(self) -> str:
        kind = (self.metadata or {}).get("location_kind", "page")
        if kind == "chapter" and self.page_start:
            return f"section {self.page_start}"
        if kind == "document":
            return "document"
        if self.page_start:
            if self.page_end and self.page_end != self.page_start:
                return f"pages {self.page_start}–{self.page_end}"
            return f"page {self.page_start}"
        if self.timestamp_start is not None:
            start = format_timestamp(self.timestamp_start)
            if self.timestamp_end is not None:
                return f"{start}–{format_timestamp(self.timestamp_end)}"
            return start
        return ""

    @property
    def display_url(self) -> str:
        if self.url:
            return self.url
        if self.book_id:
            return f"/library/{self.book.slug}/" if self.book.slug else "/library/"
        if self.video_id:
            return f"/{self.video_id}"
        if self.short_id:
            return f"/shorts/{self.short_id}/"
        return ""


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class SourceProfile(models.Model):
    """Editorial review of a book, video or short used by the assistant."""

    SOURCE_CHOICES = KnowledgeChunk.SOURCE_CHOICES
    AUTHORITY_PRIMARY = "primary"
    AUTHORITY_SECONDARY = "secondary"
    AUTHORITY_REFERENCE = "reference"
    AUTHORITY_UNVERIFIED = "unverified"
    AUTHORITY_CHOICES = [
        (AUTHORITY_PRIMARY, "Primary — Qur'an, authentic hadith, orator's own words"),
        (AUTHORITY_SECONDARY, "Secondary — recognised scholarly explanation"),
        (AUTHORITY_REFERENCE, "Reference — general or historical material"),
        (AUTHORITY_UNVERIFIED, "Unverified — needs editorial review"),
    ]

    source_type = models.CharField(max_length=12, choices=SOURCE_CHOICES)
    source_id = models.PositiveIntegerField()
    title = models.CharField(max_length=250, blank=True, default="")
    authority = models.CharField(
        max_length=16, choices=AUTHORITY_CHOICES, default=AUTHORITY_REFERENCE
    )
    perspective = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Scholarly school or perspective, when relevant.",
    )
    notes = models.TextField(blank=True, default="")
    is_approved = models.BooleanField(
        default=True,
        help_text="Unapproved sources are excluded from Ask DMWM retrieval.",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_type", "title"]
        unique_together = ("source_type", "source_id")
        verbose_name = "Source review"
        verbose_name_plural = "Source reviews"

    def __str__(self) -> str:
        return f"{self.title or self.source_type} ({self.get_authority_display()})"


class WebSourceRule(models.Model):
    """An administrator decision about one external web domain."""

    domain = models.CharField(max_length=200, unique=True)
    is_approved = models.BooleanField(default=True)
    authority = models.CharField(
        max_length=16,
        choices=SourceProfile.AUTHORITY_CHOICES,
        default=SourceProfile.AUTHORITY_REFERENCE,
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain"]
        verbose_name = "External source rule"
        verbose_name_plural = "External source rules"

    def __str__(self) -> str:
        state = "allowed" if self.is_approved else "blocked"
        return f"{self.domain} ({state})"


class AskSettings(models.Model):
    """Singleton configuration edited from the AI dashboard."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    system_prompt = models.TextField(
        blank=True,
        default="",
        help_text="Leave blank to use the built-in, safety-reviewed prompt.",
    )
    temperature = models.FloatField(default=0.1)
    max_context_chars = models.PositiveIntegerField(default=24000)
    external_search_enabled = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ask_settings_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ask DMWM settings"
        verbose_name_plural = "Ask DMWM settings"

    def __str__(self) -> str:
        return "Ask DMWM settings"

    @classmethod
    def load(cls) -> "AskSettings":
        settings_obj, _created = cls.objects.get_or_create(pk=1)
        return settings_obj

    @property
    def effective_prompt(self) -> str:
        return effective_system_prompt(self.system_prompt)


class AskInteraction(models.Model):
    """One question/answer turn, including failures and unanswered questions."""

    STATUS_ANSWERED = "answered"
    STATUS_NO_CONTEXT = "no_context"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_ANSWERED, "Answered"),
        (STATUS_NO_CONTEXT, "Answered without matching sources"),
        (STATUS_ERROR, "Error"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ask_interactions",
    )
    question = models.TextField()
    answer = models.TextField(blank=True, default="")
    language = models.CharField(max_length=12, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ANSWERED, db_index=True
    )
    error = models.TextField(blank=True, default="")
    references = models.JSONField(default=list, blank=True)
    used_internal = models.BooleanField(default=False)
    used_external = models.BooleanField(default=False)
    is_resolved = models.BooleanField(default=False)
    admin_notes = models.TextField(blank=True, default="")
    # A corrected, editor-approved answer. When ``use_as_example`` is set the
    # pair (question, corrected_answer) is injected into the prompt as a
    # few-shot example so the assistant learns the approved wording.
    corrected_answer = models.TextField(blank=True, default="")
    use_as_example = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Ask DMWM interaction"
        verbose_name_plural = "Ask DMWM interactions"

    def __str__(self) -> str:
        return self.question[:80]

    @property
    def helpful_feedback(self):
        return self.feedback.filter(helpful=True).count()


class AnswerFeedback(models.Model):
    """Visitor feedback attached to one assistant answer."""

    interaction = models.ForeignKey(
        AskInteraction, on_delete=models.CASCADE, related_name="feedback"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_feedback",
    )
    helpful = models.BooleanField()
    comment = models.TextField(blank=True, default="")
    # An optional "right answer" supplied by the visitor. When promoted by an
    # administrator it becomes a training example for the assistant.
    suggested_answer = models.TextField(blank=True, default="")
    is_reviewed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("interaction", "user")
        verbose_name = "Answer feedback"
        verbose_name_plural = "Answer feedback"

    def __str__(self) -> str:
        return f"{'Helpful' if self.helpful else 'Not helpful'}: {self.interaction.question[:60]}"
