"""Hybrid retrieval: keyword matching, vector similarity and reranking."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q

from .embeddings import cosine, embed_query, search_tokens
from .models import AskSettings, KnowledgeChunk, SourceProfile, format_timestamp
from .web import research as web_research

logger = logging.getLogger(__name__)

AUTHORITY_WEIGHTS = {
    SourceProfile.AUTHORITY_PRIMARY: 1.0,
    SourceProfile.AUTHORITY_SECONDARY: 0.72,
    SourceProfile.AUTHORITY_REFERENCE: 0.45,
    SourceProfile.AUTHORITY_UNVERIFIED: 0.15,
}

_TIME_SENSITIVE = (
    "today",
    "latest",
    "news",
    "current",
    "currently",
    "recent",
    "recently",
    "now",
    "update",
    "2025",
    "2026",
    "aujourd",
    "actualité",
    "actuel",
    "récent",
    "maintenant",
    "اليوم",
    "الحالي",
    "الأخير",
)


@dataclass
class RetrievedChunk:
    chunk: KnowledgeChunk
    score: float
    keyword: float
    vector: float


def _normalise_keyword(count: int, token_count: int) -> float:
    if token_count == 0:
        return 0.0
    return min(1.0, count / max(3.0, token_count * 1.6))


def _profiles() -> dict[tuple[str, int], SourceProfile]:
    return {
        (profile.source_type, profile.source_id): profile
        for profile in SourceProfile.objects.all()
    }


def _candidate_chunks(tokens: list[str]) -> list[KnowledgeChunk]:
    scan_limit = int(getattr(settings, "KNOWLEDGE_VECTOR_SCAN_LIMIT", 4000))
    base = KnowledgeChunk.objects.select_related("book", "video", "short")

    candidates: list[KnowledgeChunk] = []
    if tokens:
        query = Q()
        for token in tokens[:8]:
            query |= Q(search_text__icontains=token)
        candidates = list(base.filter(query).order_by("-updated_at")[:scan_limit])

    if len(candidates) < 25:
        seen = {chunk.pk for chunk in candidates}
        for chunk in base.order_by("-updated_at")[: min(scan_limit, 1200)]:
            if chunk.pk not in seen:
                candidates.append(chunk)
                seen.add(chunk.pk)

    return candidates


def search_internal(
    query: str, *, language: str = "", limit: int = 8
) -> list[RetrievedChunk]:
    tokens = search_tokens(query)[:12]
    if not tokens:
        return []

    chunks = _candidate_chunks(tokens)
    if not chunks:
        return []

    profiles = _profiles()
    allowed: list[KnowledgeChunk] = []
    for chunk in chunks:
        profile = profiles.get((chunk.source_type, chunk.source_id))
        if profile is not None and not profile.is_approved:
            continue
        allowed.append(chunk)

    # Embed the query once per provider/model pair used by the index.  Mixing
    # vectors from different providers would produce meaningless similarities.
    groups: dict[tuple[str, str], list[float] | None] = {}
    for chunk in allowed:
        key = (chunk.embedding_provider or "local", chunk.embedding_model or "local-hash-384")
        if key not in groups:
            groups[key] = embed_query(query, key[0], key[1])

    ranked: list[RetrievedChunk] = []
    for chunk in allowed:
        lower = chunk.search_text or chunk.content.lower()
        keyword_count = sum(lower.count(token) for token in tokens)
        keyword = _normalise_keyword(keyword_count, len(tokens))

        vector = 0.0
        key = (chunk.embedding_provider or "local", chunk.embedding_model or "local-hash-384")
        query_vector = groups.get(key)
        if query_vector and chunk.embedding:
            vector = max(0.0, (cosine(query_vector, chunk.embedding) + 1.0) / 2.0)

        title_lower = (chunk.title or "").lower()
        title_hits = sum(1 for token in tokens if token in title_lower)
        title_score = title_hits / len(tokens) if tokens else 0.0

        profile = profiles.get((chunk.source_type, chunk.source_id))
        authority = (
            profile.authority if profile else SourceProfile.AUTHORITY_REFERENCE
        )
        authority_score = AUTHORITY_WEIGHTS.get(authority, 0.45)
        language_score = 1.0 if language and chunk.language.startswith(language) else 0.0

        score = (
            0.40 * keyword
            + 0.42 * vector
            + 0.09 * authority_score
            + 0.06 * title_score
            + 0.03 * language_score
        )
        ranked.append(
            RetrievedChunk(
                chunk=chunk,
                score=score,
                keyword=keyword,
                vector=vector,
            )
        )

    # Rerank the best candidates with signals a bag-of-words score misses:
    # exact phrase presence, source authority, and whether the title matches.
    ranked.sort(key=lambda item: item.score, reverse=True)
    reranked = ranked[:40]
    phrase = " ".join(query.lower().split())
    if len(phrase) >= 8:
        for item in reranked:
            if phrase in (item.chunk.content or "").lower():
                item.score += 0.08
    for item in reranked:
        profile = profiles.get((item.chunk.source_type, item.chunk.source_id))
        if profile and profile.authority == SourceProfile.AUTHORITY_UNVERIFIED:
            item.score -= 0.05
    reranked.sort(key=lambda item: item.score, reverse=True)

    # Keep the evidence varied: at most three passages from one book/video.
    selected: list[RetrievedChunk] = []
    per_source: dict[tuple[str, int], int] = {}
    for item in reranked:
        key = (item.chunk.source_type, item.chunk.source_id)
        if per_source.get(key, 0) >= 3:
            continue
        selected.append(item)
        per_source[key] = per_source.get(key, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def catalog_context(limit_books: int = 40, limit_videos: int = 30) -> str:
    """A compact catalogue so questions about the collections are answered."""
    from library.models import Book
    from videos.models import Video

    books = list(
        Book.objects.filter(status="published", is_public=True)
        .select_related("category")
        .order_by("-is_featured", "-created_at")[:limit_books]
    )
    videos = list(Video.objects.order_by("-created_at")[:limit_videos])

    lines = []
    if books:
        lines.append("BOOKS IN THE LIBRARY")
        for book in books:
            parts = [f"- {book.title}"]
            if book.author:
                parts.append(f"by {book.author}")
            if book.published_year:
                parts.append(f"({book.published_year})")
            if book.language and book.language != "auto":
                parts.append(f"[{book.language}]")
            if book.description:
                parts.append(f"— {book.description[:180]}")
            lines.append(" ".join(parts))
    if videos:
        lines.append("\nVIDEOS AND SPEECHES")
        for video in videos:
            description = (video.description or "").strip()
            lines.append(
                f"- {video.title}" + (f" — {description[:160]}" if description else "")
            )
    return "\n".join(lines)


def _wants_external(question: str, results: list[RetrievedChunk]) -> bool:
    lower = question.lower()
    if any(marker in lower for marker in _TIME_SENSITIVE):
        return True
    if len(results) < 2:
        return True
    best = max((item.score for item in results), default=0.0)
    return best < 0.30


def _format_source(item: RetrievedChunk, citation: str) -> tuple[str, dict]:
    chunk = item.chunk
    profile = SourceProfile.objects.filter(
        source_type=chunk.source_type, source_id=chunk.source_id
    ).first()
    authority = profile.authority if profile else SourceProfile.AUTHORITY_REFERENCE

    if chunk.source_type == KnowledgeChunk.SOURCE_BOOK:
        kind = "BOOK"
        byline = f" by {chunk.author}" if chunk.author else ""
        location = f", {chunk.location_label}" if chunk.location_label else ""
        header = f"[{citation}] INTERNAL — {kind}: {chunk.title}{byline}{location}"
    elif chunk.source_type == KnowledgeChunk.SOURCE_VIDEO:
        location = f", {chunk.location_label}" if chunk.location_label else ""
        header = f"[{citation}] INTERNAL — VIDEO: {chunk.title}{location}"
    else:
        header = f"[{citation}] INTERNAL — SHORT: {chunk.title}"

    text = f"{header}\nURL: {chunk.display_url}\nText: {chunk.content}"
    reference = {
        "citation": citation,
        "type": chunk.source_type,
        "title": chunk.title,
        "author": chunk.author,
        "url": chunk.display_url,
        "page": chunk.page_start
        if (chunk.metadata or {}).get("location_kind", "page") == "page"
        else None,
        "timestamp": format_timestamp(chunk.timestamp_start)
        if chunk.timestamp_start is not None
        else "",
        "location": chunk.location_label,
        "origin": "internal",
        "authority": authority,
        "edition": (chunk.metadata or {}).get("edition", ""),
        "snippet": (chunk.content or "")[:700],
    }
    return text, reference


def _format_web(result: dict, citation: str) -> tuple[str, dict]:
    text = (
        f"[{citation}] EXTERNAL WEB SOURCE: {result['title']}\n"
        f"URL: {result['url']}\nSnippet: {result['snippet']}"
    )
    reference = {
        "citation": citation,
        "type": "web",
        "title": result["title"],
        "url": result["url"],
        "page": None,
        "timestamp": "",
        "location": "",
        "origin": "external",
        "authority": result.get("authority", SourceProfile.AUTHORITY_REFERENCE),
        "snippet": result.get("snippet", "")[:700],
    }
    return text, reference


def build_context(
    question: str,
    *,
    language: str = "",
    history: list[dict[str, str]] | None = None,
) -> dict:
    """Retrieve evidence and build the citation-numbered grounding block."""
    history = history or []
    previous_questions = [
        message.get("content", "")
        for message in history[-4:]
        if message.get("role") == "user" and message.get("content")
    ]
    retrieval_query = "\n".join(previous_questions + [question])

    results = search_internal(retrieval_query, language=language)
    ask_settings = AskSettings.load()

    external_results: list[dict] = []
    if (
        ask_settings.external_search_enabled
        and _wants_external(question, results)
    ):
        try:
            external_results = web_research(question, max_results=5)
        except Exception:  # noqa: BLE001
            logger.warning("External research failed", exc_info=True)

    blocks: list[str] = []
    references: list[dict] = []
    citation_number = 1

    for item in results:
        text, reference = _format_source(item, f"S{citation_number}")
        blocks.append(text)
        references.append(reference)
        citation_number += 1

    for result in external_results:
        text, reference = _format_web(result, f"S{citation_number}")
        blocks.append(text)
        references.append(reference)
        citation_number += 1

    catalog = catalog_context()
    if catalog:
        blocks.append("CATALOGUE OF THE COLLECTION\n" + catalog)

    text = "\n\n".join(blocks)
    max_chars = max(4000, int(ask_settings.max_context_chars))
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0]

    return {
        "text": text,
        "references": references,
        "internal_count": len(results),
        "external_count": len(external_results),
        "retrieval_query": retrieval_query,
    }
