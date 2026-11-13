"""Views for the digital library."""

from __future__ import annotations

import json
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Q, Avg
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core.permissions import write_required, read_required
from videos import r2_storage

from .forms import BookForm, CategoryForm, ReviewForm
from .models import (
    Book, Category, Bookmark, Favorite, ReadingProgress, Review,
    TranslationHistory, SavedHighlight, ReadingSession, PersonalCollection,
)

logger = logging.getLogger(__name__)


def _public_books():
    return Book.objects.filter(status="published", is_public=True).select_related("category", "uploader")


def book_list(request: HttpRequest) -> HttpResponse:
    qs = _public_books()
    q = (request.GET.get("q") or "").strip()
    category = (request.GET.get("category") or "").strip()
    language = (request.GET.get("language") or "").strip()
    sort = (request.GET.get("sort") or "newest").strip()

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(author__icontains=q)
            | Q(description__icontains=q)
            | Q(tags__icontains=q)
        )
    if category:
        qs = qs.filter(category__slug=category)
    if language:
        qs = qs.filter(language=language)

    if sort == "popular":
        qs = qs.order_by("-views", "-created_at")
    elif sort == "rating":
        qs = qs.order_by("-rating_count", "-created_at")
    elif sort == "title":
        qs = qs.order_by("title")
    elif sort == "oldest":
        qs = qs.order_by("created_at")
    else:
        qs = qs.order_by("-is_featured", "-created_at")

    paginator = Paginator(qs, 24)
    page = paginator.get_page(request.GET.get("page"))

    categories = Category.objects.all()
    featured = _public_books().filter(is_featured=True)[:8]

    return render(
        request,
        "library/book_list.html",
        {
            "page": page,
            "books": page.object_list,
            "categories": categories,
            "featured": featured,
            "q": q,
            "category": category,
            "language": language,
            "sort": sort,
            "languages": Book.LANGUAGE_CHOICES,
        },
    )


def book_detail(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    if not (book.status == "published" and book.is_public) and not (
        request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, "profile", None) and request.user.profile.can_write)
    ):
        return redirect("library:book_list")

    # count a view once per session
    viewed = request.session.get("viewed_books", [])
    if book.id not in viewed:
        Book.objects.filter(pk=book.pk).update(views=models.F("views") + 1)
        book.views += 1
        viewed.append(book.id)
        request.session["viewed_books"] = viewed[-200:]

    progress = None
    is_favorite = False
    user_review = None
    if request.user.is_authenticated:
        progress = ReadingProgress.objects.filter(user=request.user, book=book).first()
        is_favorite = Favorite.objects.filter(user=request.user, book=book).exists()
        user_review = Review.objects.filter(user=request.user, book=book).first()

    reviews = book.reviews.select_related("user").all()[:20]
    bookmarks = []
    if request.user.is_authenticated:
        bookmarks = book.bookmarks.filter(user=request.user).order_by("page")

    related = _public_books().filter(category=book.category).exclude(pk=book.pk)[:6] if book.category else _public_books().exclude(pk=book.pk)[:6]

    return render(
        request,
        "library/book_detail.html",
        {
            "book": book,
            "progress": progress,
            "is_favorite": is_favorite,
            "user_review": user_review,
            "reviews": reviews,
            "bookmarks": bookmarks,
            "related": related,
            "review_form": ReviewForm(instance=user_review) if user_review else ReviewForm(),
        },
    )


@login_required
def read_book(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    if not (book.status == "published" and book.is_public) and not (
        request.user.is_superuser or (hasattr(request.user, "profile") and request.user.profile.can_write)
    ):
        return redirect("library:book_list")

    progress = ReadingProgress.objects.filter(user=request.user, book=book).first()
    start_page = progress.page if progress else 1

    return render(
        request,
        "library/read.html",
        {
            "book": book,
            "pdf_url": book.pdf_view_url,
            "start_page": start_page,
            "allow_download": book.allow_download,
            "languages_list": Book.LANGUAGE_CHOICES,
        },
    )


@write_required
def upload_book(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = BookForm(request.POST)
        pdf = request.FILES.get("pdf_file")
        cover = request.FILES.get("cover_file")
        if form.is_valid():
            book = form.save(commit=False)
            book.uploader = request.user
            if not pdf:
                form.add_error("pdf_file", _("A PDF file is required."))
                categories = Category.objects.all()
                return render(request, "library/upload.html", {"form": form, "categories": categories})
            if pdf:
                import uuid
                from pathlib import Path
                ext = Path(pdf.name).suffix.lower() or ".pdf"
                key = f"books/{uuid.uuid4().hex}{ext}"
                r2_storage.upload_object(key, pdf, content_type="application/pdf")
                book.pdf_file_id = key
                book.pdf_url = r2_storage.stored_url(key)
                book.file_size = getattr(pdf, "size", 0) or 0
            if cover:
                import uuid
                from pathlib import Path
                ext = Path(cover.name).suffix.lower()
                key = f"covers/{uuid.uuid4().hex}{ext}"
                r2_storage.upload_object(key, cover, content_type=cover.content_type or "image/jpeg")
                book.cover_file_id = key
                book.cover_url = r2_storage.stored_url(key)
            book.save()
            messages.success(request, _("Book uploaded successfully."))
            return redirect("library:book_detail", slug=book.slug)
        # attach file errors
        if not pdf and not book.pdf_file_id:
            form.add_error("pdf_file", _("A PDF file is required."))
    else:
        form = BookForm(initial={"status": "published", "is_public": True, "allow_download": True, "language": "en"})

    categories = Category.objects.all()
    return render(request, "library/upload.html", {"form": form, "categories": categories})


@write_required
def edit_book(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    if request.method == "POST":
        form = BookForm(request.POST, instance=book)
        pdf = request.FILES.get("pdf_file")
        cover = request.FILES.get("cover_file")
        if form.is_valid():
            book = form.save(commit=False)
            if pdf:
                import uuid
                from pathlib import Path
                # delete old
                if book.pdf_file_id:
                    r2_storage.delete_file(book.pdf_file_id)
                ext = Path(pdf.name).suffix.lower() or ".pdf"
                key = f"books/{uuid.uuid4().hex}{ext}"
                r2_storage.upload_object(key, pdf, content_type="application/pdf")
                book.pdf_file_id = key
                book.pdf_url = r2_storage.stored_url(key)
                book.file_size = getattr(pdf, "size", 0) or book.file_size
            if cover:
                import uuid
                from pathlib import Path
                if book.cover_file_id:
                    r2_storage.delete_file(book.cover_file_id)
                ext = Path(cover.name).suffix.lower()
                key = f"covers/{uuid.uuid4().hex}{ext}"
                r2_storage.upload_object(key, cover, content_type=cover.content_type or "image/jpeg")
                book.cover_file_id = key
                book.cover_url = r2_storage.stored_url(key)
            book.save()
            messages.success(request, _("Book updated."))
            return redirect("library:book_detail", slug=book.slug)
    else:
        form = BookForm(instance=book)
    categories = Category.objects.all()
    return render(request, "library/upload.html", {"form": form, "categories": categories, "book": book, "editing": True})


@write_required
def delete_book(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    if request.method == "POST":
        if book.pdf_file_id:
            r2_storage.delete_file(book.pdf_file_id)
        if book.cover_file_id:
            r2_storage.delete_file(book.cover_file_id)
        book.delete()
        messages.success(request, _("Book deleted."))
        return redirect("library:manage_books")
    return render(request, "library/confirm_delete.html", {"book": book})


@read_required
def manage_books(request: HttpRequest) -> HttpResponse:
    qs = Book.objects.all().select_related("category", "uploader")
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(author__icontains=q))
    if status:
        qs = qs.filter(status=status)
    qs = qs.order_by("-created_at")
    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "library/manage_books.html", {"page": page, "books": page.object_list, "q": q, "status": status})


@write_required
def manage_categories(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("Category added."))
            return redirect("library:manage_categories")
    else:
        form = CategoryForm()
    categories = Category.objects.all()
    return render(request, "library/manage_categories.html", {"categories": categories, "form": form})


@write_required
def delete_category(request: HttpRequest, pk: int) -> HttpResponse:
    cat = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        cat.delete()
        messages.success(request, _("Category deleted."))
    return redirect("library:manage_categories")


@require_POST
@login_required
def toggle_favorite(request: HttpRequest, slug: str) -> JsonResponse:
    book = get_object_or_404(Book, slug=slug)
    fav, created = Favorite.objects.get_or_create(user=request.user, book=book)
    if not created:
        fav.delete()
        return JsonResponse({"favorited": False})
    return JsonResponse({"favorited": True})


@require_POST
@login_required
def save_progress(request: HttpRequest, slug: str) -> JsonResponse:
    book = get_object_or_404(Book, slug=slug)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    page = int(data.get("page", 1))
    percent = float(data.get("percent", 0.0))
    prog, _ = ReadingProgress.objects.update_or_create(
        user=request.user, book=book, defaults={"page": page, "percent": percent}
    )
    return JsonResponse({"ok": True, "page": prog.page, "percent": prog.percent})


@require_POST
@login_required
def add_bookmark(request: HttpRequest, slug: str) -> JsonResponse:
    book = get_object_or_404(Book, slug=slug)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    page = int(data.get("page", 1))
    note = (data.get("note") or "").strip()[:300]
    bm = Bookmark.objects.create(user=request.user, book=book, page=page, note=note)
    return JsonResponse({"ok": True, "id": bm.id, "page": bm.page})


@require_POST
@login_required
def delete_bookmark(request: HttpRequest, pk: int) -> JsonResponse:
    bm = get_object_or_404(Bookmark, pk=pk, user=request.user)
    bm.delete()
    return JsonResponse({"ok": True})


@login_required
def my_library(request: HttpRequest) -> HttpResponse:
    favorites = Favorite.objects.filter(user=request.user).select_related("book")
    progress = ReadingProgress.objects.filter(user=request.user).select_related("book").order_by("-updated_at")
    return render(request, "library/my_library.html", {"favorites": favorites, "progress": progress})


@require_POST
@login_required
def submit_review(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    instance = Review.objects.filter(user=request.user, book=book).first()
    form = ReviewForm(request.POST, instance=instance)
    if form.is_valid():
        review = form.save(commit=False)
        review.user = request.user
        review.book = book
        # adjust aggregates
        if instance:
            book.rating_sum = max(0, book.rating_sum - instance.rating)
            book.rating_count = max(0, book.rating_count - 1)
        review.save()
        book.rating_sum += review.rating
        book.rating_count += 1
        book.save(update_fields=["rating_sum", "rating_count"])
        messages.success(request, _("Review saved."))
    else:
        messages.error(request, _("Invalid review."))
    return redirect("library:book_detail", slug=slug)


def download_book(request: HttpRequest, slug: str) -> HttpResponse:
    book = get_object_or_404(Book, slug=slug)
    if not book.allow_download:
        messages.error(request, _("Download is not permitted for this book."))
        return redirect("library:book_detail", slug=slug)
    if not (book.status == "published" and book.is_public) and not (
        request.user.is_authenticated and (request.user.is_superuser or (hasattr(request.user, "profile") and request.user.profile.can_write))
    ):
        return redirect("library:book_list")
    Book.objects.filter(pk=book.pk).update(downloads=models.F("downloads") + 1)
    url = book.pdf_download_url
    return redirect(url)


def translate_text(request: HttpRequest) -> JsonResponse:
    """Sentence/paragraph level translation endpoint.

    Accepts POST with JSON {text, source, target}. Returns {translated}.
    Provider chain: OpenAI -> Google Cloud Translate -> LibreTranslate (public) -> MyMemory.
    """
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    text = (data.get("text") or "").strip()
    source = (data.get("source") or "auto").strip()
    target = (data.get("target") or "").strip()
    context = (data.get("context") or "").strip()
    book_slug = (data.get("book") or "").strip()
    page = int(data.get("page") or 0)
    if not text or not target:
        return JsonResponse({"error": "missing text or target"}, status=400)
    # cap size
    if len(text) > 5000:
        text = text[:5000]
    if len(context) > 8000:
        context = context[:8000]

    translated = _translate(text, source, target, context)
    if translated is None:
        return JsonResponse({"error": "translation unavailable"}, status=503)

    # Save to history for authenticated users
    if request.user.is_authenticated:
        try:
            book = None
            if book_slug:
                book = Book.objects.filter(slug=book_slug).first()
            TranslationHistory.objects.create(
                user=request.user,
                book=book,
                original_text=text,
                translated_text=translated,
                source_lang=source,
                target_lang=target,
                page=page,
                context_text=context,
            )
        except Exception as exc:
            logger.warning("Could not save translation history: %s", exc)

    return JsonResponse({"translated": translated})


def _translate(text: str, source: str, target: str, context: str = "") -> str | None:
    # 1. OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        try:
            import httpx

            system = (
                "You are a professional, context-aware translator. "
                f"Translate the selected text from {source} to {target}. "
                "Use the surrounding context only to disambiguate meaning, tone and terminology; "
                "translate ONLY the selected text, not the context. "
                "Produce a concise, natural, accurate translation that preserves meaning, "
                "context, tone and specialized terminology. Avoid word-for-word distortion. "
                "Return only the translation, no explanations."
            )
            user_content = text
            if context:
                user_content = f"Surrounding context:\n{context}\n\nSelected text to translate:\n{text}"
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={
                    "model": os.environ.get("OPENAI_TRANSLATE_MODEL", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.2,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.warning("OpenAI translate failed: %s", exc)

    # 2. Google Cloud Translate
    google_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if google_key:
        try:
            import httpx

            resp = httpx.post(
                f"https://translation.googleapis.com/language/translate/v2?key={google_key}",
                json={"q": text, "source": source if source != "auto" else None, "target": target, "format": "text"},
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["data"]["translations"][0]["translatedText"]
        except Exception as exc:
            logger.warning("Google translate failed: %s", exc)

    # 3. LibreTranslate public instance
    try:
        import httpx

        payload = {"q": text, "source": source if source != "auto" else "auto", "target": target, "format": "text"}
        api_url = os.environ.get("LIBRETRANSLATE_URL", "https://libretranslate.com/translate").strip()
        resp = httpx.post(api_url, json=payload, timeout=30.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "translatedText" in data:
                candidate = data["translatedText"]
                if candidate and candidate.strip().lower() not in ("", text.lower()) and "PLAINTEST" not in candidate.upper():
                    return candidate
    except Exception as exc:
        logger.warning("LibreTranslate failed: %s", exc)

    # 4. MyMemory (free, no key)
    try:
        import httpx
        from urllib.parse import quote

        langpair = f"{source if source != 'auto' else 'en'}|{target}"
        url = f"https://api.mymemory.translated.net/get?q={quote(text[:500])}&langpair={langpair}"
        resp = httpx.get(url, timeout=20.0)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("responseStatus") == 200:
                return data["responseData"]["translatedText"]
    except Exception as exc:
        logger.warning("MyMemory failed: %s", exc)

    return None


# ---------------- User dashboard ----------------

@login_required
def user_dashboard(request: HttpRequest) -> HttpResponse:
    user = request.user
    progress = ReadingProgress.objects.filter(user=user).select_related("book").order_by("-updated_at")[:10]
    favorites = Favorite.objects.filter(user=user).select_related("book").order_by("-created_at")[:10]
    history = TranslationHistory.objects.filter(user=user).select_related("book").order_by("-created_at")[:10]
    saved_translations = TranslationHistory.objects.filter(user=user, is_saved=True).select_related("book").order_by("-created_at")[:10]
    highlights = SavedHighlight.objects.filter(user=user).select_related("book").order_by("-created_at")[:10]
    sessions = ReadingSession.objects.filter(user=user).select_related("book").order_by("-started_at")[:10]
    collections = PersonalCollection.objects.filter(user=user).prefetch_related("books")
    # stats
    total_books_read = ReadingProgress.objects.filter(user=user).count()
    total_translations = TranslationHistory.objects.filter(user=user).count()
    total_highlights = SavedHighlight.objects.filter(user=user).count()
    # recommended: books in same categories as favorites/progress
    cat_ids = set()
    for p in progress:
        if p.book.category_id:
            cat_ids.add(p.book.category_id)
    for f in favorites:
        if f.book.category_id:
            cat_ids.add(f.book.category_id)
    recommended = _public_books().filter(category_id__in=cat_ids).exclude(
        id__in=[p.book_id for p in progress]
    ).distinct()[:8] if cat_ids else _public_books().order_by("-views")[:8]

    return render(request, "library/user_dashboard.html", {
        "progress": progress,
        "favorites": favorites,
        "history": history,
        "saved_translations": saved_translations,
        "highlights": highlights,
        "sessions": sessions,
        "collections": collections,
        "total_books_read": total_books_read,
        "total_translations": total_translations,
        "total_highlights": total_highlights,
        "recommended": recommended,
    })


# ---------------- Admin dashboard ----------------

@read_required
def admin_dashboard(request: HttpRequest) -> HttpResponse:
    from django.contrib.auth import get_user_model
    from django.db.models import Count
    User = get_user_model()
    total_books = Book.objects.count()
    published_books = Book.objects.filter(status="published").count()
    try:
        total_users = User.objects.filter(profile__is_guest=False).count()
    except Exception:
        total_users = User.objects.count()
    total_sessions = ReadingSession.objects.count()
    total_translations = TranslationHistory.objects.count()
    popular_books = Book.objects.order_by("-views")[:8]
    recent_books = Book.objects.order_by("-created_at")[:8]
    # most active readers
    active_readers = (
        ReadingProgress.objects.values("user__username")
        .annotate(c=Count("id"))
        .order_by("-c")[:8]
    )
    return render(request, "library/admin_dashboard.html", {
        "total_books": total_books,
        "published_books": published_books,
        "total_users": total_users,
        "total_sessions": total_sessions,
        "total_translations": total_translations,
        "popular_books": popular_books,
        "recent_books": recent_books,
        "active_readers": active_readers,
    })


# ---------------- Translation history ----------------

@login_required
def translation_history(request: HttpRequest) -> HttpResponse:
    qs = TranslationHistory.objects.filter(user=request.user).select_related("book").order_by("-created_at")
    book_slug = (request.GET.get("book") or "").strip()
    saved_only = request.GET.get("saved") == "1"
    if book_slug:
        qs = qs.filter(book__slug=book_slug)
    if saved_only:
        qs = qs.filter(is_saved=True)
    paginator = Paginator(qs, 30)
    page = paginator.get_page(request.GET.get("page"))
    books = Book.objects.filter(translations__user=request.user).distinct()
    return render(request, "library/translation_history.html", {
        "page": page,
        "translations": page.object_list,
        "books": books,
        "book_slug": book_slug,
        "saved_only": saved_only,
    })


@require_POST
@login_required
def toggle_saved_translation(request: HttpRequest, pk: int) -> JsonResponse:
    t = get_object_or_404(TranslationHistory, pk=pk, user=request.user)
    t.is_saved = not t.is_saved
    t.save(update_fields=["is_saved"])
    return JsonResponse({"ok": True, "saved": t.is_saved})


@require_POST
@login_required
def delete_translation(request: HttpRequest, pk: int) -> JsonResponse:
    t = get_object_or_404(TranslationHistory, pk=pk, user=request.user)
    t.delete()
    return JsonResponse({"ok": True})


@login_required
def export_translations(request: HttpRequest) -> HttpResponse:
    import csv
    qs = TranslationHistory.objects.filter(user=request.user, is_saved=True).select_related("book").order_by("-created_at")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="translations.csv"'
    writer = csv.writer(response)
    writer.writerow(["Book", "Original", "Translated", "Source", "Target", "Page", "Date"])
    for t in qs:
        writer.writerow([
            t.book.title if t.book else "",
            t.original_text,
            t.translated_text,
            t.source_lang,
            t.target_lang,
            t.page,
            t.created_at.isoformat(),
        ])
    return response


# ---------------- Highlights ----------------

@login_required
def highlights_list(request: HttpRequest) -> HttpResponse:
    qs = SavedHighlight.objects.filter(user=request.user).select_related("book").order_by("-created_at")
    return render(request, "library/highlights.html", {"highlights": qs})


@require_POST
@login_required
def add_highlight(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    book = get_object_or_404(Book, slug=data.get("book"))
    h = SavedHighlight.objects.create(
        user=request.user,
        book=book,
        page=int(data.get("page", 1)),
        text=(data.get("text") or "").strip()[:5000],
        note=(data.get("note") or "").strip()[:300],
        color=(data.get("color") or "yellow").strip()[:20],
    )
    return JsonResponse({"ok": True, "id": h.id})


@require_POST
@login_required
def delete_highlight(request: HttpRequest, pk: int) -> JsonResponse:
    h = get_object_or_404(SavedHighlight, pk=pk, user=request.user)
    h.delete()
    return JsonResponse({"ok": True})


# ---------------- Personal collections ----------------

@login_required
def collections_list(request: HttpRequest) -> HttpResponse:
    collections = PersonalCollection.objects.filter(user=request.user).prefetch_related("books")
    return render(request, "library/collections.html", {"collections": collections})


@require_POST
@login_required
def create_collection(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name required"}, status=400)
    col, created = PersonalCollection.objects.get_or_create(user=request.user, name=name)
    if not created:
        return JsonResponse({"error": "already exists"}, status=400)
    return JsonResponse({"ok": True, "id": col.id, "name": col.name})


@require_POST
@login_required
def collection_add_book(request: HttpRequest, pk: int) -> JsonResponse:
    col = get_object_or_404(PersonalCollection, pk=pk, user=request.user)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    book = get_object_or_404(Book, slug=data.get("book"))
    col.books.add(book)
    return JsonResponse({"ok": True})


@require_POST
@login_required
def collection_remove_book(request: HttpRequest, pk: int) -> JsonResponse:
    col = get_object_or_404(PersonalCollection, pk=pk, user=request.user)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST
    book = get_object_or_404(Book, slug=data.get("book"))
    col.books.remove(book)
    return JsonResponse({"ok": True})


@require_POST
@login_required
def delete_collection(request: HttpRequest, pk: int) -> JsonResponse:
    col = get_object_or_404(PersonalCollection, pk=pk, user=request.user)
    col.delete()
    return JsonResponse({"ok": True})


# ---------------- Side-by-side translation helper (context) ----------------
# Already handled via translate endpoint with context.