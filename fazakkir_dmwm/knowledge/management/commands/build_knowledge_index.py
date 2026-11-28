"""Build the Ask DMWM knowledge index from books, videos and shorts.

Examples:
    python manage.py build_knowledge_index
    python manage.py build_knowledge_index --all --force
    python manage.py build_knowledge_index --book-slug the-book
    python manage.py build_knowledge_index --video-id 42 --force
"""

from django.core.management.base import BaseCommand, CommandError

from knowledge.indexing import index_book, index_short, index_video
from knowledge.models import KnowledgeChunk


class Command(BaseCommand):
    help = "Extract and embed books, video transcripts and shorts for Ask DMWM."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Include sources that already have chunks.")
        parser.add_argument("--force", action="store_true", help="Rebuild chunks that already exist.")
        parser.add_argument("--books", action="store_true", help="Only index books.")
        parser.add_argument("--videos", action="store_true", help="Only index videos.")
        parser.add_argument("--shorts", action="store_true", help="Only index shorts.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum sources per type (0 = no limit).")
        parser.add_argument("--book-slug", help="Index one book by slug.")
        parser.add_argument("--video-id", type=int, help="Index one video by id.")
        parser.add_argument("--max-pages", type=int, default=120, help="Maximum pages per book.")

    def handle(self, *args, **options):
        only_books = options["books"]
        only_videos = options["videos"]
        only_shorts = options["shorts"]
        any_specific = any((only_books, only_videos, only_shorts, options["book_slug"], options["video_id"]))
        do_books = not any_specific or only_books or bool(options["book_slug"])
        do_videos = not any_specific or only_videos or bool(options["video_id"])
        do_shorts = not any_specific or only_shorts
        limit = options["limit"] or None

        if options["book_slug"]:
            from library.models import Book

            book = Book.objects.filter(slug=options["book_slug"]).first()
            if book is None:
                raise CommandError("No book with that slug.")
            count = index_book(book, force=True, max_pages=options["max_pages"])
            self.stdout.write(self.style.SUCCESS(f"{book.title}: {count} chunks"))
            return

        if options["video_id"]:
            from videos.models import Video

            video = Video.objects.filter(pk=options["video_id"]).first()
            if video is None:
                raise CommandError("No video with that id.")
            count = index_video(video, force=True)
            self.stdout.write(self.style.SUCCESS(f"{video.title}: {count} chunks"))
            return

        total = 0
        if do_books:
            from library.models import Book

            books = Book.objects.filter(status="published", is_public=True)
            if not options["all"] and not options["force"]:
                books = books.filter(knowledge_chunks__isnull=True)
            books = books.distinct().order_by("title")
            if limit:
                books = books[:limit]
            for index, book in enumerate(books, start=1):
                self.stdout.write(f"[book {index}] {book.title}")
                try:
                    count = index_book(book, force=options["force"], max_pages=options["max_pages"])
                    total += count
                    self.stdout.write(self.style.SUCCESS(f"    {count} chunks"))
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(self.style.ERROR(f"    failed: {exc}"))

        if do_videos:
            from videos.models import Video

            videos = Video.objects.all()
            if not options["all"] and not options["force"]:
                videos = videos.filter(knowledge_chunks__isnull=True)
            videos = videos.distinct().order_by("-created_at")
            if limit:
                videos = videos[:limit]
            for index, video in enumerate(videos, start=1):
                self.stdout.write(f"[video {index}] {video.title}")
                try:
                    count = index_video(video, force=options["force"])
                    total += count
                    self.stdout.write(self.style.SUCCESS(f"    {count} chunks"))
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(self.style.ERROR(f"    failed: {exc}"))

        if do_shorts:
            from shorts.models import Short

            shorts = Short.objects.all()
            if not options["all"] and not options["force"]:
                shorts = shorts.filter(knowledge_chunks__isnull=True)
            if limit:
                shorts = shorts[:limit]
            for index, short in enumerate(shorts, start=1):
                self.stdout.write(f"[short {index}] {short.title}")
                try:
                    total += index_short(short, force=options["force"])
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(self.style.ERROR(f"    failed: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Done. {total} chunks indexed."))
        self.stdout.write(f"Total chunks: {KnowledgeChunk.objects.count()}")
