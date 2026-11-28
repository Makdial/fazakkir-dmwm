"""Turn library books and video transcripts into searchable chunks."""

from __future__ import annotations

import html
import logging
import re

from django.conf import settings
from django.db import transaction

from library.content import extract_book_pages

from .embeddings import embed_texts, normalise_text, search_tokens
from .models import KnowledgeChunk, SourceProfile

logger = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 1300
CHUNK_OVERLAP_CHARS = 180

_SENTENCE_RE = re.compile(r"(?<=[.!?؟。！？;])\s+")
_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def split_text(
    text: str, max_chars: int = MAX_CHUNK_CHARS, overlap_chars: int = CHUNK_OVERLAP_CHARS
) -> list[str]:
    """Split text into sentence-aligned chunks with a small overlap."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    sentences = [sentence.strip() for sentence in _SENTENCE_RE.split(text) if sentence.strip()]
    if len(sentences) == 1:
        # A PDF without punctuation: slice on word boundaries.
        words = text.split()
        chunks: list[str] = []
        current: list[str] = []
        length = 0
        for word in words:
            if current and length + len(word) + 1 > max_chars:
                chunks.append(" ".join(current))
                current = []
                length = 0
            current.append(word)
            length += len(word) + 1
        if current:
            chunks.append(" ".join(current))
        return chunks

    chunks = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        if current and length + len(sentence) + 1 > max_chars:
            chunks.append(" ".join(current))
            carry = [current[-1]] if len(current[-1]) <= overlap_chars else []
            current = list(carry)
            length = sum(len(item) + 1 for item in current)
        current.append(sentence)
        length += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _parse_timestamp(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return (
        (int(hours[:-1]) if hours else 0) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def parse_vtt(content: str) -> list[dict]:
    """Return ``[{start, end, text}]`` cues from a WebVTT track."""
    cues: list[dict] = []
    if not content:
        return cues

    for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n")):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        for index, line in enumerate(lines):
            match = _TIMESTAMP_RE.search(line)
            if not match:
                continue
            text = " ".join(lines[index + 1 :])
            text = html.unescape(_TAG_RE.sub(" ", text))
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                break
            cues.append(
                {
                    "start": _parse_timestamp(*match.groups()[:4]),
                    "end": _parse_timestamp(*match.groups()[4:]),
                    "text": text,
                }
            )
            break
    return cues


def _choose_track(video):
    tracks = [
        track
        for track in video.subtitle_tracks.all()
        if track.content and not track.is_lyrics
    ]
    if not tracks:
        return None
    preferred = (getattr(settings, "SUBTITLE_SOURCE_LANGUAGE", "") or "ar").lower()
    for language in (preferred, "ar", "en", "fr"):
        for track in tracks:
            if track.language_code.lower().startswith(language):
                return track
    return tracks[0]


def _default_profile(source_type: str, source_id: int, title: str) -> None:
    authority = (
        SourceProfile.AUTHORITY_PRIMARY
        if source_type == KnowledgeChunk.SOURCE_VIDEO
        else SourceProfile.AUTHORITY_REFERENCE
    )
    SourceProfile.objects.get_or_create(
        source_type=source_type,
        source_id=source_id,
        defaults={"title": title, "authority": authority},
    )


def _save_chunks(chunks: list[dict]) -> int:
    if not chunks:
        return 0
    texts = [chunk["content"] for chunk in chunks]
    vectors, provider, model = embed_texts(texts)
    if len(vectors) != len(chunks):
        raise RuntimeError("The embedding provider returned the wrong number of vectors")

    objects = []
    for chunk, vector in zip(chunks, vectors):
        objects.append(
            KnowledgeChunk(
                source_type=chunk["source_type"],
                source_id=chunk["source_id"],
                book=chunk.get("book"),
                video=chunk.get("video"),
                short=chunk.get("short"),
                title=chunk["title"],
                author=chunk.get("author", ""),
                url=chunk.get("url", ""),
                language=chunk.get("language", ""),
                page_start=chunk.get("page_start"),
                page_end=chunk.get("page_end"),
                timestamp_start=chunk.get("timestamp_start"),
                timestamp_end=chunk.get("timestamp_end"),
                chunk_index=chunk["chunk_index"],
                content=chunk["content"],
                search_text=normalise_text(chunk["content"]),
                embedding=vector,
                embedding_provider=provider,
                embedding_model=model,
                token_count=len(search_tokens(chunk["content"])),
                metadata=chunk.get("metadata", {}),
            )
        )
    KnowledgeChunk.objects.bulk_create(objects, batch_size=200)
    return len(objects)


def index_book(book, *, force: bool = True, max_pages: int = 120) -> int:
    """Extract a book, chunk its pages and store the embeddings."""
    pages = extract_book_pages(book, force=force, max_pages=max_pages)
    chunks: list[dict] = []
    title = book.title

    for page in pages:
        page_number = page.get("page")
        location_kind = page.get("kind", "page")
        for text in split_text(page.get("text", "")):
            chunks.append(
                {
                    "source_type": KnowledgeChunk.SOURCE_BOOK,
                    "source_id": book.pk,
                    "book": book,
                    "title": title,
                    "author": book.author,
                    "url": f"/library/{book.slug}/" if book.slug else "/library/",
                    "language": (book.language or "").split("-")[0],
                    "page_start": page_number,
                    "page_end": page_number,
                    "content": text,
                    "chunk_index": len(chunks),
                    "metadata": {
                        "category": book.category.name if book.category else "",
                        "publisher": book.publisher,
                        "edition": book.edition,
                        "year": book.published_year,
                        "isbn": book.isbn,
                        "location_kind": location_kind,
                    },
                }
            )

    if not chunks and (book.description or "").strip():
        chunks.append(
            {
                "source_type": KnowledgeChunk.SOURCE_BOOK,
                "source_id": book.pk,
                "book": book,
                "title": title,
                "author": book.author,
                "url": f"/library/{book.slug}/" if book.slug else "/library/",
                "language": (book.language or "").split("-")[0],
                "content": f"{title}. {book.description}".strip(),
                "chunk_index": 0,
                "metadata": {
                    "category": book.category.name if book.category else "",
                    "kind": "description",
                },
            }
        )

    with transaction.atomic():
        KnowledgeChunk.objects.filter(
            source_type=KnowledgeChunk.SOURCE_BOOK, source_id=book.pk
        ).delete()
        count = _save_chunks(chunks)
        _default_profile(KnowledgeChunk.SOURCE_BOOK, book.pk, title)
    return count


def index_video(video, *, force: bool = True) -> int:
    """Index the best available transcript for a video, with timestamps."""
    track = _choose_track(video)
    chunks: list[dict] = []
    base = {
        "source_type": KnowledgeChunk.SOURCE_VIDEO,
        "source_id": video.pk,
        "video": video,
        "title": video.title,
        "author": getattr(video.user, "username", ""),
        "url": f"/{video.pk}",
        "language": track.language_code if track else "",
    }

    cues = parse_vtt(track.content) if track else []
    if cues:
        current: list[dict] = []
        length = 0
        for cue in cues:
            if current and length + len(cue["text"]) + 1 > MAX_CHUNK_CHARS:
                chunks.append(
                    {
                        **base,
                        "timestamp_start": current[0]["start"],
                        "timestamp_end": current[-1]["end"],
                        "content": " ".join(item["text"] for item in current),
                        "chunk_index": len(chunks),
                        "metadata": {
                            "kind": "transcript",
                            "track": track.label,
                            "language": track.language_code,
                        },
                    }
                )
                carry = current[-1:]
                current = list(carry)
                length = sum(len(item["text"]) + 1 for item in current)
            current.append(cue)
            length += len(cue["text"]) + 1
        if current:
            chunks.append(
                {
                    **base,
                    "timestamp_start": current[0]["start"],
                    "timestamp_end": current[-1]["end"],
                    "content": " ".join(item["text"] for item in current),
                    "chunk_index": len(chunks),
                    "metadata": {
                        "kind": "transcript",
                        "track": track.label,
                        "language": track.language_code,
                    },
                }
            )

    if not chunks:
        description = " ".join(filter(None, [video.title, video.description]))
        if description.strip():
            chunks.append(
                {
                    **base,
                    "content": description.strip(),
                    "chunk_index": 0,
                    "metadata": {"kind": "metadata"},
                }
            )

    with transaction.atomic():
        KnowledgeChunk.objects.filter(
            source_type=KnowledgeChunk.SOURCE_VIDEO, source_id=video.pk
        ).delete()
        count = _save_chunks(chunks)
        _default_profile(KnowledgeChunk.SOURCE_VIDEO, video.pk, video.title)
    return count


def index_short(short, *, force: bool = True) -> int:
    text = " ".join(filter(None, [short.title, short.description])).strip()
    if not text:
        return 0
    chunks = [
        {
            "source_type": KnowledgeChunk.SOURCE_SHORT,
            "source_id": short.pk,
            "short": short,
            "title": short.title,
            "url": f"/shorts/{short.pk}/",
            "content": text,
            "chunk_index": 0,
            "metadata": {"kind": "metadata"},
        }
    ]
    with transaction.atomic():
        KnowledgeChunk.objects.filter(
            source_type=KnowledgeChunk.SOURCE_SHORT, source_id=short.pk
        ).delete()
        count = _save_chunks(chunks)
        _default_profile(KnowledgeChunk.SOURCE_SHORT, short.pk, short.title)
    return count


def index_missing(*, books_limit: int = 3, videos_limit: int = 6, shorts_limit: int = 6) -> dict:
    """Index recent sources that have no chunks yet.  Used by the dashboard."""
    from library.models import Book
    from shorts.models import Short
    from videos.models import Video

    summary = {"books": 0, "videos": 0, "shorts": 0, "errors": []}

    book_qs = (
        Book.objects.filter(status="published", is_public=True)
        .exclude(pdf_file_id="")
        .filter(knowledge_chunks__isnull=True)
        .distinct()[:books_limit]
    )
    for book in book_qs:
        try:
            index_book(book, force=False)
            summary["books"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not index book %r", book.title, exc_info=True)
            summary["errors"].append(f"Book: {book.title}: {exc}")

    video_qs = Video.objects.filter(knowledge_chunks__isnull=True).distinct()[:videos_limit]
    for video in video_qs:
        try:
            index_video(video)
            summary["videos"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not index video %r", video.title, exc_info=True)
            summary["errors"].append(f"Video: {video.title}: {exc}")

    short_qs = Short.objects.filter(knowledge_chunks__isnull=True).distinct()[:shorts_limit]
    for short in short_qs:
        try:
            index_short(short)
            summary["shorts"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not index short %r", short.title, exc_info=True)
            summary["errors"].append(f"Short: {short.title}: {exc}")

    return summary


def reindex_source(source_type: str, source_id: int) -> int:
    if source_type == KnowledgeChunk.SOURCE_BOOK:
        from library.models import Book

        return index_book(Book.objects.get(pk=source_id), force=True)
    if source_type == KnowledgeChunk.SOURCE_VIDEO:
        from videos.models import Video

        return index_video(Video.objects.get(pk=source_id), force=True)
    if source_type == KnowledgeChunk.SOURCE_SHORT:
        from shorts.models import Short

        return index_short(Short.objects.get(pk=source_id), force=True)
    raise ValueError(f"Unknown source type: {source_type}")


def index_stats() -> dict:
    from library.models import Book
    from videos.models import Video

    total_books = Book.objects.filter(status="published", is_public=True).count()
    indexed_books = (
        Book.objects.filter(
            status="published", is_public=True, knowledge_chunks__isnull=False
        )
        .distinct()
        .count()
    )
    total_videos = Video.objects.count()
    indexed_videos = (
        Video.objects.filter(knowledge_chunks__isnull=False).distinct().count()
    )
    providers = list(
        KnowledgeChunk.objects.order_by()
        .values_list("embedding_provider", "embedding_model")
        .distinct()[:5]
    )
    return {
        "chunks": KnowledgeChunk.objects.count(),
        "books_total": total_books,
        "books_indexed": indexed_books,
        "videos_total": total_videos,
        "videos_indexed": indexed_videos,
        "providers": providers,
        "last_indexed": KnowledgeChunk.objects.order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first(),
    }
