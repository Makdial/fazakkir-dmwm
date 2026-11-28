from django.contrib import admin

from .models import (
    AnswerFeedback,
    AskInteraction,
    AskSettings,
    KnowledgeChunk,
    SourceProfile,
    WebSourceRule,
)


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "location_label", "language", "embedding_provider", "updated_at")
    list_filter = ("source_type", "embedding_provider", "language")
    search_fields = ("title", "content", "search_text")
    readonly_fields = ("search_text", "embedding", "created_at", "updated_at")

    @admin.display(description="Location")
    def location_label(self, obj):
        return obj.location_label or "—"


@admin.register(SourceProfile)
class SourceProfileAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "authority", "is_approved", "reviewed_by", "reviewed_at")
    list_filter = ("source_type", "authority", "is_approved")
    search_fields = ("title", "perspective", "notes")
    list_editable = ("authority", "is_approved")


@admin.register(WebSourceRule)
class WebSourceRuleAdmin(admin.ModelAdmin):
    list_display = ("domain", "is_approved", "authority", "updated_at")
    list_filter = ("is_approved", "authority")
    search_fields = ("domain", "notes")
    list_editable = ("is_approved", "authority")


@admin.register(AskSettings)
class AskSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_enabled", "external_search_enabled", "temperature", "updated_at")


@admin.register(AskInteraction)
class AskInteractionAdmin(admin.ModelAdmin):
    list_display = ("question", "status", "language", "used_internal", "used_external", "is_resolved", "created_at")
    list_filter = ("status", "used_internal", "used_external", "is_resolved", "created_at")
    search_fields = ("question", "answer", "error")
    readonly_fields = ("question", "answer", "references", "created_at")
    date_hierarchy = "created_at"


@admin.register(AnswerFeedback)
class AnswerFeedbackAdmin(admin.ModelAdmin):
    list_display = ("interaction", "helpful", "user", "is_reviewed", "created_at")
    list_filter = ("helpful", "is_reviewed", "created_at")
    search_fields = ("comment", "interaction__question")
