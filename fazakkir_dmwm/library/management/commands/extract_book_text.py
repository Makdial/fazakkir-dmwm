"""Index the text of library PDFs for Ask DMWM.

Usage:
    python manage.py extract_book_text --all
    python manage.py extract_book_text --slug the-book-slug --force
"""

from django.core.management.base import BaseCommand

from library.content import extract_book_text
from library.models import Book


class Command(BaseCommand):
    help = "Extract searchable text from library PDFs so Ask DMWM can use the books."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only extract this book slug.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-check every book, including ones that already have text.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace text that was already extracted.",
        )
        parser.add_argument(
            "--pages",
            type=int,
            default=80,
            help="Maximum number of pages to read per book (default: 80).",
        )

    def handle(self, *args, **options):
        books = Book.objects.exclude(pdf_file_id="")
        if options.get("slug"):
            books = books.filter(slug=options["slug"])
        elif not options.get("all"):
            books = books.filter(content_indexed=False)

        books = books.order_by("title")
        total = books.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("No books need indexing."))
            return

        for index, book in enumerate(books, start=1):
            self.stdout.write(f"[{index}/{total}] {book.title}")
            text = extract_book_text(
                book,
                force=options.get("force", False),
                max_pages=options.get("pages", 80),
            )
            if text:
                self.stdout.write(
                    self.style.SUCCESS(f"    indexed {len(text):,} characters")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "    no text found (the PDF may be scanned); "
                        "the description will still be used"
                    )
                )
