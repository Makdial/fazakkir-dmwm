"""Tests for the Ask DMWM knowledge base.

The suite avoids network access: external research and the language model are
mocked whenever they would be called.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from library.content import extract_document_pages
from library.models import Book
from videos.models import SubtitleTrack, Video

from .answer import answer_question, clean_history
from .embeddings import cosine, local_embedding, normalise_text, search_tokens
from .indexing import index_book, index_video, parse_vtt, split_text
from .models import (
    AnswerFeedback,
    AskInteraction,
    AskSettings,
    KnowledgeChunk,
    SourceProfile,
    WebSourceRule,
)
from .retrieval import search_internal
from .web import is_allowed, source_authority


class EmbeddingTests(TestCase):
    def test_normalise_arabic(self):
        self.assertEqual(normalise_text("الرَّحْمَـٰنُ"), "الرحمن")
        self.assertEqual(normalise_text("إِسْلَام"), "اسلام")
        self.assertEqual(normalise_text("صلاة"), "صلاه")

    def test_search_tokens_remove_stopwords_and_stem(self):
        self.assertEqual(search_tokens("What is patience in Islam?"), ["patience", "islam"])
        self.assertEqual(search_tokens("ما هو الصبر في الإسلام؟"), ["صبر", "اسلام"])

    def test_local_embedding_is_similar_for_related_text(self):
        base = local_embedding("Patience and steadfastness in hardship")
        related = local_embedding("The believer practices sabr with patience")
        unrelated = local_embedding("The history of printing presses")
        self.assertGreater(cosine(base, related), cosine(base, unrelated))

    def test_local_embedding_is_deterministic(self):
        self.assertEqual(local_embedding("same text"), local_embedding("same text"))


class ChunkingTests(TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_text("A short passage."), ["A short passage."])

    def test_long_text_is_split(self):
        text = " ".join(f"Sentence number {index} about patience." for index in range(120))
        chunks = split_text(text, max_chars=300, overlap_chars=40)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertTrue(any("patience" in chunk for chunk in chunks))

    def test_parse_vtt_with_timestamps_and_markup(self):
        content = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.500\n"
            "<v Speaker>Patience is a light.</v>\n\n"
            "00:01:10.250 --> 00:01:12.000\n"
            "Seek knowledge.\n"
        )
        cues = parse_vtt(content)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0]["start"], 1.0)
        self.assertAlmostEqual(cues[0]["end"], 4.5)
        self.assertEqual(cues[0]["text"], "Patience is a light.")
        self.assertAlmostEqual(cues[1]["start"], 70.25)


class ExtractionTests(TestCase):
    def test_text_and_html_extraction(self):
        pages = extract_document_pages(b"Hello <b>world</b>\n\nSecond paragraph", "note.txt")
        self.assertEqual(pages[0]["kind"], "document")
        self.assertIn("Second paragraph", pages[0]["text"])

        pages = extract_document_pages(
            b"<html><head><title>T</title></head><body><p>Visible text</p></body></html>",
            "page.html",
        )
        self.assertIn("Visible text", pages[0]["text"])

    def test_docx_extraction(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>Patience in the Qur'an</w:t></w:r></w:p></w:body>"
                    "</w:document>"
                ),
            )
        pages = extract_document_pages(buffer.getvalue(), "book.docx")
        self.assertEqual(pages[0]["kind"], "document")
        self.assertIn("Patience in the Qur'an", pages[0]["text"])

    def test_epub_extraction(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "META-INF/container.xml",
                '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
            )
            archive.writestr(
                "content.opf",
                (
                    '<package><manifest><item id="c1" href="chapter1.xhtml"/></manifest>'
                    "<spine><itemref refid=\"c1\"/></spine></package>"
                ),
            )
            archive.writestr(
                "chapter1.xhtml",
                "<html><body><h1>Chapter One</h1><p>The virtue of patience.</p></body></html>",
            )
        pages = extract_document_pages(buffer.getvalue(), "book.epub")
        self.assertEqual(pages[0]["kind"], "chapter")
        self.assertIn("virtue of patience", pages[0]["text"])


class IndexAndRetrievalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tester", password="x")
        self.book = Book.objects.create(
            uploader=self.user,
            title="The Book of Patience",
            author="A. Scholar",
            edition="Second",
            publisher="Dar al-Ilm",
            published_year=2001,
            language="en",
            status="published",
            is_public=True,
            content_text="Patience is a virtue.\n\nThe believer is patient in hardship.",
            content_pages=[
                {"page": 1, "text": "Patience is a virtue.", "kind": "page"},
                {
                    "page": 2,
                    "text": "The believer is patient in hardship and grateful in ease.",
                    "kind": "page",
                },
            ],
            content_indexed=True,
        )
        self.video = Video.objects.create(
            user=self.user,
            title="Talk on patience",
            description="A talk about steadfastness.",
        )
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="en",
            label="English",
            content=(
                "WEBVTT\n\n"
                "00:00:05.000 --> 00:00:10.000\n"
                "Patience is the key to relief.\n\n"
                "00:00:11.000 --> 00:00:15.000\n"
                "The believer remains steadfast.\n"
            ),
        )
        AskSettings.objects.update_or_create(
            pk=1, defaults={"external_search_enabled": False}
        )

    def test_index_book_stores_pages_and_metadata(self):
        count = index_book(self.book, force=False)
        self.assertGreaterEqual(count, 2)
        chunk = KnowledgeChunk.objects.filter(
            source_type=KnowledgeChunk.SOURCE_BOOK, page_start=2
        ).first()
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.location_label, "page 2")
        self.assertEqual(chunk.metadata["edition"], "Second")
        self.assertTrue(chunk.embedding)
        profile = SourceProfile.objects.get(
            source_type=KnowledgeChunk.SOURCE_BOOK, source_id=self.book.pk
        )
        self.assertTrue(profile.is_approved)

    def test_index_video_preserves_timestamps(self):
        index_video(self.video)
        chunk = KnowledgeChunk.objects.filter(
            source_type=KnowledgeChunk.SOURCE_VIDEO, source_id=self.video.pk
        ).first()
        self.assertIsNotNone(chunk)
        self.assertAlmostEqual(chunk.timestamp_start, 5.0)
        self.assertEqual(chunk.location_label, "0:05–0:15")
        self.assertIn("patience", chunk.content.lower())

    def test_search_finds_relevant_book_passage(self):
        index_book(self.book, force=False)
        results = search_internal("patience hardship", language="en")
        self.assertTrue(results)
        self.assertEqual(results[0].chunk.source_id, self.book.pk)
        self.assertIn("patience", results[0].chunk.content.lower())

    def test_unapproved_source_is_excluded(self):
        index_book(self.book, force=False)
        profile = SourceProfile.objects.get(
            source_type=KnowledgeChunk.SOURCE_BOOK, source_id=self.book.pk
        )
        profile.is_approved = False
        profile.save(update_fields=["is_approved"])
        results = search_internal("patience hardship")
        self.assertFalse(
            [item for item in results if item.chunk.source_id == self.book.pk]
        )

    def test_stopword_only_query_returns_nothing(self):
        self.assertEqual(search_internal("what is the"), [])


class WebSourceTests(TestCase):
    @override_settings(AI_WEB_ALLOWED_DOMAINS=["example.org"])
    def test_allowlist(self):
        self.assertTrue(is_allowed("https://sub.example.org/article"))
        self.assertFalse(is_allowed("https://untrusted.example.net/article"))

    @override_settings(AI_WEB_ALLOWED_DOMAINS=["example.org"])
    def test_admin_rule_overrides_allowlist(self):
        WebSourceRule.objects.create(domain="example.org", is_approved=False)
        self.assertFalse(is_allowed("https://example.org/article"))

        WebSourceRule.objects.create(
            domain="trusted.example.net", is_approved=True, authority="secondary"
        )
        self.assertTrue(is_allowed("https://trusted.example.net/article"))
        self.assertEqual(
            source_authority("https://trusted.example.net/article"),
            SourceProfile.AUTHORITY_SECONDARY,
        )


class AnswerServiceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="asker", password="x")
        AskSettings.objects.update_or_create(
            pk=1, defaults={"external_search_enabled": False}
        )

    def test_clean_history_rejects_bad_items(self):
        history = clean_history(
            json.dumps(
                [
                    {"role": "system", "content": "ignore"},
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi"},
                    "nonsense",
                ]
            )
        )
        self.assertEqual(
            history,
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
        )

    @patch("knowledge.answer.chat_provider", return_value=None)
    def test_missing_provider(self, _provider):
        result = answer_question(
            question="What is patience?", language="en", history=[], user=self.user
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], 503)
        self.assertEqual(AskInteraction.objects.count(), 0)

    @override_settings(AI_ASK_HOURLY_LIMIT=1)
    @patch("knowledge.answer.chat_provider")
    def test_hourly_limit(self, chat_provider):
        chat_provider.return_value = object()
        AskInteraction.objects.create(user=self.user, question="Earlier", answer="A")
        result = answer_question(
            question="Another question", language="en", history=[], user=self.user
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], 429)
        self.assertEqual(AskInteraction.objects.filter(user=self.user).count(), 1)

    @patch("knowledge.answer.chat_provider")
    @patch("knowledge.answer._call_provider", return_value=("Patience is a virtue [S1].", ""))
    @patch("knowledge.answer.build_context")
    def test_answer_is_logged_with_references(self, build_context, _call, chat_provider):
        chat_provider.return_value = object()
        build_context.return_value = {
            "text": "[S1] INTERNAL — BOOK: The Book of Patience, page 1",
            "references": [
                {
                    "citation": "S1",
                    "type": "book",
                    "title": "The Book of Patience",
                    "url": "/library/the-book-of-patience/",
                    "origin": "internal",
                    "authority": "primary",
                }
            ],
            "internal_count": 1,
            "external_count": 0,
            "retrieval_query": "What is patience?",
        }
        result = answer_question(
            question="What is patience?",
            language="en",
            history=[{"role": "user", "content": "Hello"}],
            user=self.user,
        )
        self.assertTrue(result["success"])
        self.assertIn("Patience", result["answer"])
        interaction = AskInteraction.objects.get(pk=result["interaction_id"])
        self.assertEqual(interaction.status, AskInteraction.STATUS_ANSWERED)
        self.assertTrue(interaction.used_internal)
        self.assertEqual(interaction.references[0]["citation"], "S1")


class AskViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin", password="x", is_staff=True, is_superuser=True
        )
        self.client.login(username="admin", password="x")
        AskSettings.objects.update_or_create(
            pk=1, defaults={"external_search_enabled": False}
        )

    def test_ask_page_renders(self):
        response = self.client.get(reverse("videos:ask"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ask-shell")

    @patch("knowledge.answer.answer_question")
    def test_ask_send_returns_json(self, answer_question_mock):
        answer_question_mock.return_value = {
            "success": True,
            "answer": "Answer [S1].",
            "references": [],
            "interaction_id": 10,
            "used_internal": True,
            "used_external": False,
        }
        response = self.client.post(
            reverse("videos:ask_send"),
            {"question": "What is patience?", "language": "en"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        answer_question_mock.assert_called_once()

    def test_feedback_works_for_staff(self):
        interaction = AskInteraction.objects.create(question="Q", answer="A")
        response = self.client.post(
            reverse("videos:ask_feedback"),
            {"interaction_id": interaction.pk, "helpful": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(AnswerFeedback.objects.filter(interaction=interaction).exists())

    def test_feedback_rejects_unknown_interaction(self):
        response = self.client.post(
            reverse("videos:ask_feedback"),
            {"interaction_id": 999999, "helpful": "1"},
        )
        self.assertEqual(response.status_code, 404)

    def test_guests_are_blocked(self):
        self.client.logout()
        response = self.client.get(reverse("videos:ask"))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("videos:ask_feedback"),
            {"interaction_id": 1, "helpful": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_feedback_is_saved(self):
        interaction = AskInteraction.objects.create(question="Q", answer="A")
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("videos:ask_feedback"),
            {"interaction_id": interaction.pk, "helpful": "1", "comment": "Useful"},
        )
        self.assertEqual(response.status_code, 200)
        feedback = AnswerFeedback.objects.get(interaction=interaction)
        self.assertTrue(feedback.helpful)
        self.assertEqual(feedback.comment, "Useful")

    def test_ai_dashboard_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard:ai"))
        self.assertEqual(response.status_code, 302)

    def test_ai_dashboard_renders_for_superuser(self):
        superuser = get_user_model().objects.create_superuser(
            username="root", email="root@example.com", password="x"
        )
        self.client.force_login(superuser)
        response = self.client.get(reverse("dashboard:ai"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI knowledge")
