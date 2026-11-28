"""Extract and search the text inside library PDFs.

The Ask DMWM assistant grounds its answers in the books published in the
library.  This module turns a book's PDF into searchable text, stores it on the
book row (so it is only downloaded once) and picks the passages that are most
relevant to a visitor's question.
"""

from __future__ import annotations

import io
import logging
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from typing import Iterable
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Keep the first request after a deploy bounded: most PDFs are small, but a
# scanned 200 MB book should not hold a web worker open.  The extractor keeps
# the first pages, which normally contain the introduction and the core of the
# argument, and the catalog metadata still covers the rest.
MAX_PDF_BYTES = 35 * 1024 * 1024
MAX_PAGES = 80
MAX_TEXT_CHARS = 400_000

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_RE = re.compile(r"(?<=[.!?؟。！？;])\s+")

# Function words in the languages most used on the site.  They are removed
# from the query before scoring passages so a question such as "What does the
# book say about patience?" matches the passages about patience, not every
# passage containing "the".
_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because
    been before being below between both but by can could did do does doing
    down during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more most
    my myself no nor not now of off on once only or other our ours ourselves
    out over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up
    very was we were what when where which while who whom why will with would
    you your yours yourself yourselves
    au aux avec ce ces cet cette dans de des du elle elles en est et eux il ils
    je la le les leur leurs lui ma mais mes moi mon ne nos notre nous on ou par
    pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une vos votre
    vous c d j l m n s t y
    el la los las un una unos unas y o pero si en de del al por para con sin
    sobre entre cuando donde como que se su sus es son fue ser estar
    """.split()
)


def question_tokens(question: str) -> list[str]:
    """Return meaningful words from *question* for keyword scoring."""
    return [
        token
        for token in _WORD_RE.findall((question or "").lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


def _normalise(text: str) -> str:
    text = (text or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_pages(
    source: io.BufferedIOBase,
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> list[dict]:
    """Extract page-aware text from a PDF file-like object."""
    from pypdf import PdfReader

    reader = PdfReader(source)
    pages: list[dict] = []
    total = 0

    for index, page in enumerate(reader.pages):
        if index >= max_pages or total >= max_chars:
            break
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one broken page must not lose the book
            logger.debug("Could not extract page %s of a library PDF", index, exc_info=True)
            continue
        page_text = _normalise(page_text)
        if not page_text:
            continue
        pages.append({"page": index + 1, "text": page_text, "kind": "page"})
        total += len(page_text)

    return pages


def extract_pdf_text(
    source: io.BufferedIOBase,
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> str:
    pages = extract_pdf_pages(source, max_pages=max_pages, max_chars=max_chars)
    return "\n\n".join(page["text"] for page in pages)[:max_chars]


class _VisibleTextParser(HTMLParser):
    """Collect visible text from an XHTML/HTML document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "head"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "head"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip == 0:
            self.parts.append(data)


def _html_text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    parser = _VisibleTextParser()
    parser.feed(value)
    return _normalise(" ".join(parser.parts))


def extract_epub_pages(data: bytes, max_pages: int = MAX_PAGES) -> list[dict]:
    """Extract chapter-by-chapter text from an EPUB without extra packages."""
    pages: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            container = ElementTree.fromstring(
                archive.read("META-INF/container.xml")
            )
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return pages
            opf_path = rootfile.attrib.get("full-path", "")
            if not opf_path:
                return pages
            opf = ElementTree.fromstring(archive.read(opf_path))
            manifest = {
                item.attrib["id"]: item.attrib.get("href", "")
                for item in opf.findall(".//{*}manifest/{*}item")
                if item.attrib.get("id") and item.attrib.get("href")
            }
            base = posixpath.dirname(opf_path)
            for itemref in opf.findall(".//{*}spine/{*}itemref"):
                href = manifest.get(itemref.attrib.get("refid", ""))
                if not href:
                    continue
                path = posixpath.normpath(posixpath.join(base, href))
                try:
                    text = _html_text(archive.read(path))
                except KeyError:
                    continue
                if text:
                    pages.append({"page": len(pages) + 1, "text": text, "kind": "chapter"})
                if len(pages) >= max_pages:
                    break
    except Exception:  # noqa: BLE001
        logger.warning("Could not read an EPUB document", exc_info=True)
    return pages


def extract_docx_pages(data: bytes) -> list[dict]:
    """Extract paragraphs from a Word document using only the standard library."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except Exception:  # noqa: BLE001
        logger.warning("Could not read a DOCX document", exc_info=True)
        return []
    paragraphs = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        text = "".join(
            node.text or "" for node in paragraph.iter() if node.tag.endswith("}t")
        )
        if text.strip():
            paragraphs.append(text.strip())
    text = _normalise("\n".join(paragraphs))
    return [{"page": 1, "text": text, "kind": "document"}] if text else []


def extract_document_pages(
    data: bytes, filename: str = "", max_pages: int = MAX_PAGES
) -> list[dict]:
    """Extract text from a PDF, EPUB, DOCX, Markdown or plain-text file."""
    suffix = (filename or "").lower().rsplit(".", 1)[-1]
    if suffix == "pdf" or not suffix:
        return extract_pdf_pages(io.BytesIO(data), max_pages=max_pages)
    if suffix == "epub":
        return extract_epub_pages(data, max_pages=max_pages)
    if suffix in ("docx", "doc"):
        return extract_docx_pages(data)
    if suffix in ("txt", "md", "markdown", "html", "htm"):
        text = data.decode("utf-8", errors="ignore")
        if suffix in ("html", "htm"):
            text = _html_text(text)
        else:
            text = _normalise(text)
        return [{"page": 1, "text": text, "kind": "document"}] if text else []
    return []


def extract_book_pages(
    book, *, force: bool = False, max_pages: int = MAX_PAGES
) -> list[dict]:
    """Download a book from R2 and return page-aware text.

    The combined text is cached on the book row so a second question does not
    download the file again.  ``content_indexed`` records a completed attempt,
    including scanned PDFs that legitimately contain no extractable text.
    """
    from .models import Book

    if book.content_text and not force:
        # Older rows have the combined text but no page map.  They are still
        # useful as one fallback chunk; a reindex recreates the pages.
        cached_pages = list(getattr(book, "content_pages", None) or [])
        if cached_pages:
            return cached_pages
        return [{"page": None, "text": book.content_text, "kind": "document"}]
    if not book.pdf_file_id:
        return []

    from videos import r2_storage

    try:
        response = r2_storage.get_object(book.pdf_file_id)
        body = response.get("Body")
        if body is None:
            return []
        payload = body.read(MAX_PDF_BYTES + 1)
        if len(payload) > MAX_PDF_BYTES:
            logger.warning(
                "Skipping text extraction for %r: file is larger than %s bytes",
                book.title,
                MAX_PDF_BYTES,
            )
            return []
        pages = extract_document_pages(
            payload, getattr(book, "pdf_file_id", ""), max_pages=max_pages
        )
    except Exception:  # noqa: BLE001 - extraction is best effort
        logger.warning("Could not extract text from book %r", book.title, exc_info=True)
        return []

    text = "\n\n".join(page["text"] for page in pages)[:MAX_TEXT_CHARS]
    Book.objects.filter(pk=book.pk).update(
        content_text=text,
        content_pages=pages,
        content_indexed=True,
    )
    book.content_text = text
    book.content_pages = pages
    book.content_indexed = True
    return pages


def extract_book_text(book, *, force: bool = False, max_pages: int = MAX_PAGES) -> str:
    """Compatibility helper returning the combined text of a book."""
    if book.content_text and not force:
        return book.content_text
    pages = extract_book_pages(book, force=force, max_pages=max_pages)
    return "\n\n".join(page["text"] for page in pages)[:MAX_TEXT_CHARS]


def _chunks(text: str, size: int = 1100) -> list[str]:
    """Split text into sentence-aligned chunks of roughly *size* characters."""
    text = _normalise(text)
    if not text:
        return []

    sentences = _SENTENCE_RE.split(text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) + 1 > size:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)

    # A PDF without punctuation can produce one enormous sentence.  Slice it
    # on word boundaries so scoring still has manageable units.
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= size * 1.5:
            result.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            end = min(len(chunk), start + size)
            if end < len(chunk):
                space = chunk.rfind(" ", start + size // 2, end)
                if space > start:
                    end = space
            result.append(chunk[start:end].strip())
            start = end
    return [chunk for chunk in result if chunk]


def best_passages(
    text: str,
    question: str,
    *,
    limit: int = 3200,
    max_passages: int = 3,
    chunk_size: int = 1100,
) -> str:
    """Return the passages of *text* most related to *question*.

    When the question has no keyword in common with the text (for example a
    question in Wolof against an English book), the opening passages are used:
    they usually state the book's subject and are a better fallback than an
    arbitrary page.
    """
    text = _normalise(text)
    if not text:
        return ""

    tokens = question_tokens(question)
    chunks = _chunks(text, size=chunk_size)
    if not chunks:
        return text[:limit]

    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        lower = chunk.lower()
        hits = sum(lower.count(token) for token in tokens)
        scored.append((hits, index, chunk))

    matched = sorted(
        (item for item in scored if item[0] > 0),
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )
    chosen = matched[:max_passages]
    if not chosen:
        chosen = scored[: min(max_passages, len(scored))]

    # Keep the reading order inside the book; jumping between pages makes the
    # source harder for the model (and the visitor) to follow.
    chosen.sort(key=lambda item: item[1])

    parts: list[str] = []
    used = 0
    for _hits, _index, chunk in chosen:
        piece = chunk[: max(400, limit - used)]
        if not piece:
            break
        parts.append(piece)
        used += len(piece)
        if used >= limit:
            break
    return "\n…\n".join(parts)[:limit]


def book_excerpt(book, question: str, *, limit: int = 3000) -> str:
    """Return the best extract from a book, falling back to its description."""
    excerpt = best_passages(book.content_text or "", question, limit=limit)
    if excerpt:
        return excerpt
    return (book.description or "").strip()[:limit]


def count_matches(question: str, text: str) -> int:
    """Cheap relevance score used to rank catalog entries."""
    tokens: Iterable[str] = question_tokens(question)
    lower = (text or "").lower()
    return sum(lower.count(token) for token in tokens)
