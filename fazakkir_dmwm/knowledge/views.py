"""Visitor feedback and the AI administration dashboard."""

from __future__ import annotations

import logging

from django.contrib import messages
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.permissions import read_required, write_required

from .answer import chat_provider
from .indexing import index_missing, index_stats, reindex_source
from .models import (
    AnswerFeedback,
    AskInteraction,
    AskSettings,
    KnowledgeChunk,
    SourceProfile,
    WebSourceRule,
)
from .prompts import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

VALID_SOURCE_TYPES = {choice[0] for choice in KnowledgeChunk.SOURCE_CHOICES}


@require_POST
def feedback(request):
    """Record a visitor's helpful/not-helpful verdict on one answer."""
    interaction = get_object_or_404(
        AskInteraction, pk=request.POST.get("interaction_id") or 0
    )
    helpful = (request.POST.get("helpful") or "").lower() in ("1", "true", "yes", "helpful")
    comment = (request.POST.get("comment") or "").strip()[:2000]

    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": _("Please sign in.")}, status=403)

    feedback_obj, _created = AnswerFeedback.objects.update_or_create(
        interaction=interaction,
        user=request.user,
        defaults={"helpful": helpful, "comment": comment},
    )
    return JsonResponse(
        {
            "success": True,
            "helpful": feedback_obj.helpful,
            "message": _("Thank you for the feedback."),
        }
    )


def _source_rows(limit: int = 30):
    from library.models import Book
    from videos.models import Video

    profiles = {
        (profile.source_type, profile.source_id): profile
        for profile in SourceProfile.objects.all()
    }
    books = (
        Book.objects.annotate(chunk_count=Count("knowledge_chunks"))
        .order_by("chunk_count", "-created_at")
        .distinct()[:limit]
    )
    rows = []
    for book in books:
        rows.append(
            {
                "source_type": "book",
                "source_id": book.pk,
                "kind": _("Book"),
                "title": book.title,
                "profile": profiles.get(("book", book.pk)),
                "chunks": getattr(book, "chunk_count", 0),
                "url": f"/library/{book.slug}/",
            }
        )

    videos = (
        Video.objects.annotate(chunk_count=Count("knowledge_chunks"))
        .order_by("chunk_count", "-created_at")
        .distinct()[:limit]
    )
    for video in videos:
        rows.append(
            {
                "source_type": "video",
                "source_id": video.pk,
                "kind": _("Video"),
                "title": video.title,
                "profile": profiles.get(("video", video.pk)),
                "chunks": getattr(video, "chunk_count", 0),
                "url": f"/{video.pk}",
            }
        )
    return rows


@read_required
def manage(request):
    ask_settings = AskSettings.load()
    provider = chat_provider()
    stats = index_stats()
    rows = _source_rows()
    feedback_items = (
        AnswerFeedback.objects.select_related("interaction", "user")
        .order_by("-created_at")[:20]
    )
    questions = (
        AskInteraction.objects.exclude(status=AskInteraction.STATUS_ANSWERED)
        .order_by("-created_at")[:25]
    )
    recent_answers = AskInteraction.objects.order_by("-created_at")[:10]
    web_rules = WebSourceRule.objects.all()

    return render(
        request,
        "knowledge/manage.html",
        {
            "active_tab": "ai",
            "ask_settings": ask_settings,
            "provider": provider,
            "stats": stats,
            "source_rows": rows,
            "feedback_items": feedback_items,
            "questions": questions,
            "recent_answers": recent_answers,
            "web_rules": web_rules,
            "authority_choices": SourceProfile.AUTHORITY_CHOICES,
            "default_prompt": DEFAULT_SYSTEM_PROMPT,
        },
    )


@write_required
@require_POST
def save_prompt(request):
    ask_settings = AskSettings.load()
    ask_settings.system_prompt = (request.POST.get("system_prompt") or "").strip()
    try:
        ask_settings.temperature = max(0.0, min(1.0, float(request.POST.get("temperature", 0.2))))
    except (TypeError, ValueError):
        ask_settings.temperature = 0.2
    try:
        ask_settings.max_context_chars = max(
            4000, min(60000, int(request.POST.get("max_context_chars", 24000)))
        )
    except (TypeError, ValueError):
        ask_settings.max_context_chars = 24000
    ask_settings.external_search_enabled = (
        request.POST.get("external_search_enabled") == "on"
    )
    ask_settings.is_enabled = request.POST.get("is_enabled") == "on"
    ask_settings.updated_by = request.user
    ask_settings.save()
    messages.success(request, _("Ask DMWM settings saved."))
    return redirect("dashboard:ai")


@write_required
@require_POST
def reindex(request):
    source_type = (request.POST.get("source_type") or "").strip()
    source_id = (request.POST.get("source_id") or "").strip()
    scope = (request.POST.get("scope") or "missing").strip()

    if source_type in VALID_SOURCE_TYPES and source_id.isdigit():
        try:
            count = reindex_source(source_type, int(source_id))
            messages.success(
                request, _("Reindexed %(count)s passages from this source.") % {"count": count}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Manual reindex failed", exc_info=True)
            messages.error(request, _("Reindex failed: %(error)s") % {"error": exc})
        return redirect("dashboard:ai")

    if scope == "all":
        summary = index_missing(books_limit=5, videos_limit=10, shorts_limit=10)
        messages.success(
            request,
            _("Indexed %(books)s books, %(videos)s videos and %(shorts)s shorts.")
            % {
                "books": summary["books"],
                "videos": summary["videos"],
                "shorts": summary["shorts"],
            },
        )
        if summary["errors"]:
            messages.warning(request, " · ".join(summary["errors"][:3]))
        return redirect("dashboard:ai")

    messages.info(request, _("Nothing to index."))
    return redirect("dashboard:ai")


@write_required
@require_POST
def review_source(request):
    source_type = (request.POST.get("source_type") or "").strip()
    source_id = (request.POST.get("source_id") or "").strip()
    if source_type not in VALID_SOURCE_TYPES or not source_id.isdigit():
        messages.error(request, _("Unknown source."))
        return redirect("dashboard:ai")

    title = (request.POST.get("title") or "").strip()[:250]
    profile, _created = SourceProfile.objects.update_or_create(
        source_type=source_type,
        source_id=int(source_id),
        defaults={
            "title": title,
            "authority": request.POST.get("authority")
            or SourceProfile.AUTHORITY_REFERENCE,
            "perspective": (request.POST.get("perspective") or "").strip()[:200],
            "notes": (request.POST.get("notes") or "").strip(),
            "is_approved": request.POST.get("is_approved") == "on",
            "reviewed_by": request.user,
            "reviewed_at": timezone.now(),
        },
    )
    messages.success(request, _("Review saved for %(title)s.") % {"title": profile.title})
    return redirect("dashboard:ai")


@write_required
@require_POST
def review_web_source(request):
    domain = (request.POST.get("domain") or "").strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://")
    domain = domain.split("/")[0].removeprefix("www.")
    if not domain:
        messages.error(request, _("Enter a domain name."))
        return redirect("dashboard:ai")

    WebSourceRule.objects.update_or_create(
        domain=domain,
        defaults={
            "is_approved": request.POST.get("is_approved") == "on",
            "authority": request.POST.get("authority")
            or SourceProfile.AUTHORITY_REFERENCE,
            "notes": (request.POST.get("notes") or "").strip(),
        },
    )
    messages.success(request, _("External source rule saved."))
    return redirect("dashboard:ai")


@write_required
@require_POST
def review_feedback(request, feedback_id: int):
    item = get_object_or_404(AnswerFeedback, pk=feedback_id)
    item.is_reviewed = True
    item.save(update_fields=["is_reviewed"])
    messages.success(request, _("Feedback marked as reviewed."))
    return redirect("dashboard:ai")


@write_required
@require_POST
def review_interaction(request, interaction_id: int):
    item = get_object_or_404(AskInteraction, pk=interaction_id)
    item.is_resolved = request.POST.get("is_resolved") == "on"
    item.admin_notes = (request.POST.get("admin_notes") or "").strip()
    item.save(update_fields=["is_resolved", "admin_notes"])
    messages.success(request, _("Question updated."))
    return redirect("dashboard:ai")
