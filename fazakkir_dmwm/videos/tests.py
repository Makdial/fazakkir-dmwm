from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile

from . import audio_tasks
from . import transcription
from .audio import AudioExtractionError
from .audio_tasks import generate_audio, schedule_audio_generation
from .models import Comment, Playlist, PlaylistItem, SubtitleTrack, Video
from .r2_storage import StorageError


def make_user(username, password="pw12345678", **kwargs):
    return User.objects.create_user(username, password=password, **kwargs)


def make_video(user, title="Video", **kwargs):
    defaults = {
        "file_id": "videos/sample.mp4",
        "video_url": "https://example.com/sample.mp4",
    }
    defaults.update(kwargs)
    return Video.objects.create(user=user, title=title, **defaults)


class VideoPathTests(TestCase):
    def setUp(self):
        self.user = make_user("uploader")
        self.video = make_video(self.user)

    def test_download_urls_do_not_raise(self):
        # Regression test for the missing ``pathlib.Path`` import.
        self.assertTrue(self.video.video_download_url or True)
        self.assertEqual(self.video.audio_download_url, "")


class PlaylistTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.other = make_user("bob")
        self.video = make_video(self.user)
        self.client.login(username="alice", password="pw12345678")

    def test_list_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("videos:playlists"))
        self.assertEqual(response.status_code, 302)

    def test_list_renders(self):
        response = self.client.get(reverse("videos:playlists"))
        self.assertEqual(response.status_code, 200)

    def test_create_playlist(self):
        response = self.client.post(
            reverse("videos:playlist_create"), {"title": "Watch later"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertTrue(
            Playlist.objects.filter(user=self.user, title="Watch later").exists()
        )

    def test_create_playlist_with_video_adds_item(self):
        response = self.client.post(
            reverse("videos:playlist_create"),
            {"title": "Favourites", "video_id": self.video.id},
        )
        playlist_id = response.json()["playlist"]["id"]
        self.assertTrue(
            PlaylistItem.objects.filter(playlist_id=playlist_id, video=self.video).exists()
        )

    def test_toggle_adds_and_removes(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        url = reverse("videos:playlist_toggle", args=[playlist.id])

        first = self.client.post(url, {"video_id": self.video.id}).json()
        self.assertTrue(first["added"])
        self.assertEqual(first["count"], 1)

        second = self.client.post(url, {"video_id": self.video.id}).json()
        self.assertFalse(second["added"])
        self.assertEqual(second["count"], 0)

    def test_only_owner_can_toggle(self):
        other_playlist = Playlist.objects.create(user=self.other, title="Theirs")
        response = self.client.post(
            reverse("videos:playlist_toggle", args=[other_playlist.id]),
            {"video_id": self.video.id},
        )
        self.assertEqual(response.status_code, 404)

    def test_private_playlist_hidden_from_others(self):
        playlist = Playlist.objects.create(user=self.other, title="Private")
        self.client.logout()
        self.client.login(username="bob", password="pw12345678")
        # bob owns it
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            200,
        )
        # alice does not
        self.client.logout()
        self.client.login(username="alice", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            403,
        )

    def test_public_playlist_visible_to_others(self):
        playlist = Playlist.objects.create(user=self.other, title="Public", is_public=True)
        self.client.logout()
        self.client.login(username="alice", password="pw12345678")
        self.assertEqual(
            self.client.get(reverse("videos:playlist_detail", args=[playlist.id])).status_code,
            200,
        )

    def test_delete_playlist(self):
        playlist = Playlist.objects.create(user=self.user, title="P")
        response = self.client.post(
            reverse("videos:playlist_delete", args=[playlist.id])
        )
        self.assertTrue(response.json()["success"])
        self.assertFalse(Playlist.objects.filter(id=playlist.id).exists())


class CommentTests(TestCase):
    def setUp(self):
        self.author = make_user("carol")
        self.other = make_user("dave")
        self.video = make_video(self.author)

    def test_comment_requires_login(self):
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "Hi"}
        )
        self.assertEqual(response.status_code, 403)

    def test_add_comment(self):
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "Nice video"}
        )
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["comment"]["body"], "Nice video")
        self.assertEqual(Comment.objects.count(), 1)

    def test_empty_comment_rejected(self):
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(
            reverse("videos:comment_add", args=[self.video.id]), {"body": "   "}
        )
        self.assertEqual(response.status_code, 400)

    def test_author_can_delete_own_comment(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        self.client.login(username="carol", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertTrue(response.json()["success"])
        self.assertFalse(Comment.objects.filter(id=comment.id).exists())

    def test_other_user_cannot_delete(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        self.client.login(username="dave", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_delete_any_comment(self):
        comment = Comment.objects.create(video=self.video, user=self.author, body="hi")
        moderator = make_user("mod", is_staff=True)
        moderator.profile.access_level = Profile.Access.WRITE
        moderator.profile.save(update_fields=["access_level"])
        self.client.login(username="mod", password="pw12345678")
        response = self.client.post(reverse("videos:comment_delete", args=[comment.id]))
        self.assertTrue(response.json()["success"])


class DetailPageTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(self.user)
        self.playlist = Playlist.objects.create(user=self.user, title="My list")
        PlaylistItem.objects.create(playlist=self.playlist, video=self.video)
        Comment.objects.create(video=self.video, user=self.user, body="Great clip")

    @mock.patch("videos.views.schedule_audio_generation")
    def test_detail_renders_comments_and_save_panel(self, _schedule):
        self.client.login(username="viewer", password="pw12345678")
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Great clip", content)
        self.assertIn("My list", content)
        self.assertIn('id="savePanel"', content)

    @mock.patch("videos.views.schedule_audio_generation")
    def test_detail_anonymous_hides_comment_form(self, _schedule):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("to add a comment", content)
        self.assertNotIn('id="savePanel"', content)


class VideoDeletionTests(TestCase):
    def setUp(self):
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(self.admin)
        Comment.objects.create(video=self.video, user=self.admin, body="c")
        PlaylistItem.objects.create(
            playlist=Playlist.objects.create(user=self.admin, title="P"), video=self.video
        )

    @mock.patch("videos.r2_storage.delete_file")
    def test_delete_video_cascades(self, _delete):
        self.client.login(username="root", password="pw12345678")
        response = self.client.post(reverse("videos:delete", args=[self.video.id]))
        self.assertTrue(response.json()["success"])
        self.assertFalse(Video.objects.filter(id=self.video.id).exists())
        # Database cascades: comments and playlist entries are gone too.
        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(PlaylistItem.objects.count(), 0)

    def test_member_cannot_delete_video(self):
        member = make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.post(reverse("videos:delete", args=[self.video.id]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Video.objects.filter(id=self.video.id).exists())


class FakeBody:
    """Minimal stand-in for a botocore ``StreamingBody``."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def iter_chunks(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class AudioStreamTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")
        self.video = make_video(
            self.user,
            title="With audio",
            audio_file_id="audio/track.mp3",
            audio_url="",
        )

    def test_no_audio_returns_404(self):
        silent = make_video(self.user, title="Silent")
        response = self.client.get(reverse("videos:audio_stream", args=[silent.id]))
        self.assertEqual(response.status_code, 404)

    def test_legacy_audio_redirects_to_stored_url(self):
        legacy = make_video(
            self.user,
            title="Legacy",
            audio_file_id="legacy/track.mp3",
            audio_url="https://cdn.example.com/legacy.mp3",
        )
        response = self.client.get(reverse("videos:audio_stream", args=[legacy.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://cdn.example.com/legacy.mp3")

    @mock.patch("videos.views.get_object")
    def test_streams_audio_same_origin(self, get_object):
        body = FakeBody([b"abc", b"def"])
        get_object.return_value = {
            "Body": body,
            "ContentType": "audio/mpeg",
            "ContentLength": 6,
        }

        response = self.client.get(reverse("videos:audio_stream", args=[self.video.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(b"".join(response.streaming_content), b"abcdef")
        self.assertTrue(body.closed)
        get_object.assert_called_once_with(range_header=None, path="audio/track.mp3")

    @mock.patch("videos.views.get_object")
    def test_range_request_is_passed_through(self, get_object):
        get_object.return_value = {
            "Body": FakeBody([b"abc"]),
            "ContentType": "audio/mpeg",
            "ContentLength": 3,
            "ContentRange": "bytes 0-2/6",
        }

        response = self.client.get(
            reverse("videos:audio_stream", args=[self.video.id]),
            HTTP_RANGE="bytes=0-2",
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 0-2/6")
        self.assertEqual(response["Content-Length"], "3")

    @mock.patch("videos.r2_storage.resolve_url")
    @mock.patch("videos.views.get_object", side_effect=StorageError("boom"))
    def test_falls_back_to_presigned_url(self, _get_object, resolve_url):
        resolve_url.return_value = "https://r2.example.com/signed.mp3"
        response = self.client.get(reverse("videos:audio_stream", args=[self.video.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://r2.example.com/signed.mp3")


class VideoCardDownloadTests(TestCase):
    def setUp(self):
        self.user = make_user("uploader")
        self.audio_video = make_video(
            self.user, title="Has audio", audio_file_id="audio/a.mp3"
        )

    def test_list_card_offers_mp3(self):
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertIn("card-download", content)
        self.assertIn("Download MP3", content)

    def test_channel_card_offers_mp3(self):
        content = self.client.get(
            reverse("videos:channel", args=[self.user.username])
        ).content.decode()
        self.assertIn("card-download", content)

    def test_silent_video_has_no_mp3_button(self):
        self.audio_video.delete()
        make_video(self.user, title="Silent")
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertNotIn("card-download", content)


class LanguageToggleTests(TestCase):
    """The four languages offered by the in-app switcher."""

    LANGUAGES = ("en", "fr", "ar", "wo")

    def setUp(self):
        self.user = make_user("viewer")
        # A video so the page exercises the card markup (the MP3 button) as well
        # as the navbar.
        self.video = make_video(
            self.user, title="Listen", audio_file_id="audio/track.mp3"
        )

    def _page(self, language):
        self.client.cookies["django_language"] = language
        return self.client.get(reverse("videos:list")).content.decode()

    def test_all_four_languages_are_offered(self):
        content = self._page("en")
        for code in self.LANGUAGES:
            self.assertIn(f'value="{code}"', content)

    def test_language_is_marked_active(self):
        # The button attributes wrap across lines in the template, so match with
        # a whitespace-tolerant pattern rather than one literal string.
        self.assertRegex(
            self._page("ar"),
            r'value="ar"\s+class="lang-option active"',
        )

    def test_arabic_renders_right_to_left(self):
        self.assertIn('<html lang="ar" dir="rtl">', self._page("ar"))

    def test_other_languages_render_left_to_right(self):
        for code in ("en", "fr", "wo"):
            self.assertIn(f'<html lang="{code}" dir="ltr">', self._page(code))

    def test_french_translations_are_applied(self):
        self.assertIn("Télécharger le MP3", self._page("fr"))

    def test_arabic_translations_are_applied(self):
        self.assertIn("تحميل MP3", self._page("ar"))

    def test_wolof_translations_are_applied(self):
        self.assertIn("Yees MP3", self._page("wo"))

    def test_arabic_text_is_not_left_as_english(self):
        # Guards against a catalogue that exists but is never loaded.
        self.assertNotIn("Download MP3", self._page("ar"))

    def test_each_language_has_a_compiled_catalog(self):
        from django.utils import translation

        samples = {
            "fr": "Télécharger le MP3",
            "ar": "تحميل MP3",
            "wo": "Yees MP3",
        }
        for code, expected in samples.items():
            with translation.override(code):
                self.assertEqual(translation.gettext("Download MP3"), expected)

    def test_no_template_comment_is_rendered_as_text(self):
        # Django's {# ... #} syntax cannot span several lines: the comment
        # regex does not match newlines, so a multi-line comment is printed to
        # visitors instead of being stripped. Guard against that regressing.
        self.client.cookies["django_language"] = "ar"
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertNotIn("{#", content)
        self.assertNotIn("#}", content)
        self.assertTrue(content.lstrip().startswith("<!DOCTYPE html>"))


class OfflineSupportTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(
            self.user, title="Listen", audio_file_id="audio/track.mp3"
        )

    def test_manifest_is_served(self):
        response = self.client.get(reverse("manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("manifest+json", response["Content-Type"])
        self.assertIn("Fazakkir", response.content.decode())

    def test_health_check_reports_ok(self):
        # The hosting platform polls this to decide whether the container is
        # serving traffic.
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": True})

    def test_health_check_fails_when_the_database_is_down(self):
        import django.db

        with mock.patch.object(
            django.db.connection, "cursor", side_effect=Exception("db down")
        ):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], False)

    def test_health_check_needs_no_authentication(self):
        # It must answer before anyone can sign in, so no login involved.
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)

    def test_service_worker_is_served_from_root(self):
        response = self.client.get(reverse("service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn("fazakkir-media-v1", response.content.decode())

    def test_offline_page_renders(self):
        response = self.client.get(reverse("videos:offline"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="offlineList"', response.content.decode())

    def test_detail_page_has_offline_button_and_stream_src(self):
        content = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        self.assertIn('data-offline-url="/%d/audio-stream/"' % self.video.id, content)
        self.assertIn('audioSrc = "/%d/audio-stream/"' % self.video.id, content)

    def test_base_page_links_manifest(self):
        content = self.client.get(reverse("videos:list")).content.decode()
        self.assertIn('rel="manifest"', content)
        self.assertIn("offline.js", content)


class AudioGenerationTests(TestCase):
    """The automatic MP3 extraction that runs on upload / play."""

    def setUp(self):
        self.user = make_user("owner")

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_marks_processing_and_starts_worker(self, thread):
        video = make_video(self.user, title="Needs audio")
        self.assertTrue(schedule_audio_generation(video.id))
        video.refresh_from_db()
        self.assertEqual(video.audio_status, "processing")
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_is_a_noop_while_processing(self, thread):
        video = make_video(self.user, title="Busy", audio_status="processing")
        self.assertFalse(schedule_audio_generation(video.id))
        thread.assert_not_called()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_schedule_skips_videos_known_to_have_no_audio(self, thread):
        video = make_video(self.user, title="Silent", audio_status="none")
        self.assertFalse(schedule_audio_generation(video.id))
        thread.assert_not_called()

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_retry_allows_a_previously_failed_video(self, thread):
        video = make_video(self.user, title="Failed", audio_status="failed")
        self.assertTrue(schedule_audio_generation(video.id, retry=True))
        video.refresh_from_db()
        self.assertEqual(video.audio_status, "processing")

    @mock.patch("videos.audio_tasks.upload_audio")
    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_stores_the_mp3(self, extract, upload):
        extract.return_value = True
        upload.return_value = {"path": "audio/track.mp3", "url": ""}
        video = make_video(self.user, title="Needs audio")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        # Pin the resolved URL: presigned URLs embed a timestamp, so comparing
        # against a freshly generated one would be flaky across a second
        # boundary.
        source = "https://r2.example.com/videos/sample.mp4?signed=1"
        with mock.patch.object(Video, "playback_url", source):
            self.assertTrue(generate_audio(video.id))

        video.refresh_from_db()
        self.assertTrue(video.has_audio)
        self.assertEqual(video.audio_file_id, "audio/track.mp3")
        self.assertEqual(video.audio_status, "ready")
        # ffmpeg streams the video straight from its (presigned) URL.
        self.assertEqual(extract.call_args.args[0], source)

    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_records_videos_without_an_audio_stream(self, extract):
        extract.return_value = False
        video = make_video(self.user, title="Silent")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "none")
        self.assertFalse(video.has_audio)

    @mock.patch("videos.audio_tasks.extract_mp3_from_url")
    def test_generate_audio_records_failures(self, extract):
        extract.side_effect = AudioExtractionError("ffmpeg boom")
        video = make_video(self.user, title="Broken")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        with self.assertLogs("videos.audio_tasks", level="WARNING"):
            self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "failed")
        self.assertIn("ffmpeg boom", video.audio_error)

    def test_generate_audio_without_a_source_url_fails(self):
        video = make_video(self.user, title="Source-less", file_id="", video_url="")
        Video.objects.filter(id=video.id).update(audio_status="processing")

        self.assertFalse(generate_audio(video.id))

        video.refresh_from_db()
        self.assertEqual(video.audio_status, "failed")


class AudioStatusEndpointTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    def test_reports_ready_audio(self):
        video = make_video(self.user, title="Has audio", audio_file_id="audio/a.mp3")
        data = self.client.get(
            reverse("videos:audio_status", args=[video.id])
        ).json()
        self.assertTrue(data["has_audio"])
        self.assertFalse(data["pending"])
        self.assertEqual(data["audio_url"], f"/{video.id}/audio-stream/")

    def test_reports_pending_for_an_untouched_video(self):
        video = make_video(self.user, title="Silent")
        data = self.client.get(
            reverse("videos:audio_status", args=[video.id])
        ).json()
        self.assertFalse(data["has_audio"])
        self.assertTrue(data["pending"])

    @mock.patch("videos.views.schedule_audio_generation", return_value=True)
    def test_retry_endpoint_requests_generation(self, schedule):
        video = make_video(self.user, title="Silent")
        response = self.client.post(
            reverse("videos:audio_generate", args=[video.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        schedule.assert_called_once_with(video.id, retry=True)


class DetailAudioGenerationTests(TestCase):
    def setUp(self):
        self.user = make_user("viewer")

    @mock.patch("videos.views.schedule_audio_generation", return_value=True)
    def test_playing_an_idle_video_starts_generation(self, schedule):
        video = make_video(self.user, title="Silent")
        content = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        schedule.assert_called_once_with(video.id)
        self.assertIn("Preparing the audio", content)

    @mock.patch("videos.views.schedule_audio_generation")
    def test_playing_a_video_with_audio_does_not_regenerate(self, schedule):
        video = make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")
        self.client.get(reverse("videos:detail", args=[video.id]))
        schedule.assert_not_called()

    @mock.patch("videos.views.schedule_audio_generation")
    def test_playing_a_silent_video_does_not_loop(self, schedule):
        video = make_video(self.user, title="Silent", audio_status="none")
        content = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        schedule.assert_not_called()
        self.assertIn("This video has no audio track.", content)


class GenerateAudioCommandTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    def test_skips_videos_known_to_have_no_audio(self):
        make_video(self.user, title="Silent", audio_status="none")
        out = StringIO()
        call_command("generateaudio", stdout=out)
        self.assertIn("No videos need an MP3", out.getvalue())

    @mock.patch(
        "videos.management.commands.generateaudio.generate_audio",
        return_value=True,
    )
    def test_processes_videos_missing_audio(self, generate):
        video = make_video(self.user, title="Needs audio")
        out = StringIO()
        call_command("generateaudio", stdout=out)
        generate.assert_called_once_with(video.id)
        self.assertIn("1/1", out.getvalue())


class SubtitleRenderingTests(TestCase):
    """WebVTT output and timestamp formatting."""

    def test_format_timestamp(self):
        self.assertEqual(transcription.format_timestamp(0), "00:00:00.000")
        self.assertEqual(transcription.format_timestamp(61.5), "00:01:01.500")
        self.assertEqual(transcription.format_timestamp(3725.25), "01:02:05.250")

    def test_negative_timestamps_are_clamped(self):
        self.assertEqual(transcription.format_timestamp(-5), "00:00:00.000")

    def test_build_vtt_has_header_and_cues(self):
        vtt = transcription.build_vtt(
            [
                transcription.Segment(0, 2.5, "مرحبا"),
                transcription.Segment(2.5, 4, "Hello"),
            ]
        )
        self.assertTrue(vtt.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:02.500", vtt)
        self.assertIn("00:00:02.500 --> 00:00:04.000", vtt)
        self.assertIn("مرحبا", vtt)

    def test_build_vtt_drops_empty_cues(self):
        vtt = transcription.build_vtt(
            [transcription.Segment(0, 1, "   "), transcription.Segment(1, 2, "kept")]
        )
        self.assertEqual(vtt.count("-->"), 1)
        self.assertIn("00:00:01.000 --> 00:00:02.000", vtt)
        self.assertNotIn("00:00:00.000", vtt)

    def test_empty_list_still_yields_valid_header(self):
        self.assertEqual(transcription.build_vtt([]), "WEBVTT\n")


class FakeProvider(transcription.SubtitleProvider):
    """Deterministic stand-in for a real speech-to-text back end."""

    name = "fake"

    def __init__(self):
        self.transcribe_calls = []
        self.translate_calls = []

    def transcribe(self, path, language):
        self.transcribe_calls.append((path, language))
        return [transcription.Segment(0, 1.5, "one"), transcription.Segment(1.5, 3, "two")]

    def translate(self, segments, target_language):
        self.translate_calls.append((len(segments), target_language))
        return [
            transcription.Segment(s.start, s.end, f"[{target_language}] {s.text}")
            for s in segments
        ]


class TranscriptionPipelineTests(TestCase):
    """The transcribe -> translate -> WebVTT pipeline."""

    def setUp(self):
        self.user = make_user("owner")
        self.provider = FakeProvider()

    def _patch_pipeline(self, chunks):
        return (
            mock.patch("videos.transcription.get_provider", return_value=self.provider),
            mock.patch("videos.transcription.download_audio", return_value="audio.mp3"),
            mock.patch("videos.transcription.chunk_audio", return_value=chunks),
        )

    def test_creates_source_translation_and_lyrics_tracks(self):
        video = make_video(self.user, title="Song", audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            result = transcription.generate_subtitles(video.id)

        self.assertEqual(sorted(result["languages"]), ["ar", "en", "fr"])
        languages = set(
            video.subtitle_tracks.values_list("language_code", flat=True)
        )
        self.assertEqual(languages, {"ar", "fr", "en", "lyrics"})

    def test_source_track_holds_the_transcription(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        track = video.subtitle_tracks.get(language_code="ar")
        self.assertTrue(track.content.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", track.content)

    def test_translations_are_saved_per_language(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        french = video.subtitle_tracks.get(language_code="fr")
        english = video.subtitle_tracks.get(language_code="en")
        self.assertIn("[fr] one", french.content)
        self.assertIn("[en] one", english.content)
        self.assertEqual(self.provider.translate_calls, [(2, "fr"), (2, "en")])

    def test_lyrics_track_is_marked_as_lyrics(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        self.assertTrue(video.subtitle_tracks.get(language_code="lyrics").is_lyrics)

    def test_chunk_offsets_shift_timestamps(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0), ("c1.mp3", 480.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        track = video.subtitle_tracks.get(language_code="ar")
        # Second chunk starts at 480s, so its cues land at 08:00 onwards.
        self.assertIn("00:08:00.000 --> 00:08:01.500", track.content)

    def test_missing_audio_is_reported(self):
        video = make_video(self.user, audio_file_id="")
        with self.assertRaises(transcription.TranscriptionError):
            transcription.generate_subtitles(video.id)

    def test_no_speech_is_reported(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        self.provider.transcribe = lambda path, language: []
        with provider_patch, audio_patch, chunk_patch:
            with self.assertRaises(transcription.TranscriptionError):
                transcription.generate_subtitles(video.id)

    def test_existing_lyrics_are_not_overwritten(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code="lyrics",
            label="Lyrics",
            content="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nmine",
            is_lyrics=True,
        )
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        self.assertIn("mine", video.subtitle_tracks.get(language_code="lyrics").content)

    def test_regenerating_replaces_existing_tracks(self):
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video,
            language_code="fr",
            label="French",
            content="WEBVTT\n\nold",
        )
        provider_patch, audio_patch, chunk_patch = self._patch_pipeline(
            [("c0.mp3", 0.0)]
        )
        with provider_patch, audio_patch, chunk_patch:
            transcription.generate_subtitles(video.id)

        french = video.subtitle_tracks.filter(language_code="fr")
        self.assertEqual(french.count(), 1)
        self.assertIn("[fr] one", french.first().content)


class SubtitleStatusWorkflowTests(TestCase):
    """Status flags, background scheduling and the views around them."""

    def setUp(self):
        self.user = make_user("owner")
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.video = make_video(
            self.user, title="Song", audio_file_id="audio/a.mp3"
        )
        # The sweep progress is module level, so reset it around each test.
        self._reset_sweep()
        self.addCleanup(self._reset_sweep)

    def _reset_sweep(self):
        transcription._SWEEP_STATE.update(
            running=False, total=0, done=0, succeeded=0, failed=0
        )

    def test_generate_and_record_marks_success(self):
        with mock.patch.object(transcription, "generate_subtitles") as generate:
            generate.return_value = {"languages": ["fr"], "cues": 3}
            self.assertTrue(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "ready")
        self.assertEqual(self.video.auto_subtitle_error, "")

    def test_generate_and_record_captures_errors(self):
        with mock.patch.object(
            transcription, "generate_subtitles", side_effect=transcription.TranscriptionError("no key")
        ):
            self.assertFalse(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")
        self.assertIn("no key", self.video.auto_subtitle_error)

    def test_generate_and_record_survives_unexpected_errors(self):
        with mock.patch.object(
            transcription, "generate_subtitles", side_effect=RuntimeError("boom")
        ):
            # The traceback is logged; capture it so the test output stays clean.
            with self.assertLogs("videos.transcription", level="ERROR"):
                self.assertFalse(transcription.generate_and_record(self.video.id))

        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")

    @mock.patch("videos.transcription.threading.Thread")
    def test_scheduling_marks_processing_and_starts_a_worker(self, thread):
        self.assertTrue(transcription.schedule_subtitle_generation(self.video.id))
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "processing")
        thread.assert_called_once()

    @mock.patch("videos.transcription.threading.Thread")
    def test_scheduling_is_a_noop_while_processing(self, thread):
        Video.objects.filter(id=self.video.id).update(auto_subtitle_status="processing")
        self.assertFalse(transcription.schedule_subtitle_generation(self.video.id))
        thread.assert_not_called()

    @mock.patch("videos.transcription.threading.Thread")
    def test_retry_allows_a_failed_video(self, thread):
        Video.objects.filter(id=self.video.id).update(auto_subtitle_status="failed")
        self.assertTrue(
            transcription.schedule_subtitle_generation(self.video.id, retry=True)
        )
        thread.assert_called_once()

    @mock.patch("videos.transcription.generate_and_record", return_value=True)
    def test_bulk_sweep_skips_videos_without_audio(self, record):
        make_video(self.user, title="Other", audio_file_id="audio/b.mp3")
        make_video(self.user, title="No audio", audio_file_id="")
        transcription._sweep_videos()
        # Only the two videos that actually have an MP3 to transcribe.
        self.assertEqual(record.call_count, 2)
        for call in record.call_args_list:
            self.assertNotEqual(call.args[0], None)

    @mock.patch("videos.transcription.threading.Thread")
    def test_start_sweep_runs_once(self, thread):
        self.assertTrue(transcription.start_subtitle_sweep())
        self.assertFalse(transcription.start_subtitle_sweep())
        thread.assert_called_once()


class SubtitleEndpointsTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.admin = make_user("root", is_staff=True, is_superuser=True)
        self.member = make_user("member")
        self.video = make_video(self.owner, audio_file_id="audio/a.mp3")
        self.silent = make_video(self.owner, title="Silent", audio_file_id="")

    def _login_admin(self):
        self.client.login(username="root", password="pw12345678")

    def test_member_cannot_generate(self):
        self.client.login(username="member", password="pw12345678")
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_redirected(self):
        response = self.client.get(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 302)

    @mock.patch("videos.views.subtitles_configured", return_value=False)
    def test_unconfigured_provider_is_reported(self, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("OPENAI_API_KEY", response.json()["error"])

    @mock.patch("videos.views.subtitles_configured", return_value=True)
    @mock.patch("videos.views.schedule_subtitle_generation", return_value=True)
    def test_admin_starts_generation(self, schedule, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        schedule.assert_called_once_with(self.video.id, retry=True)

    @mock.patch("videos.views.subtitles_configured", return_value=True)
    def test_video_without_audio_is_refused(self, _configured):
        self._login_admin()
        response = self.client.post(
            reverse("videos:subtitles_generate", args=[self.silent.id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("audio", response.json()["error"])

    def test_status_endpoint_is_public(self):
        response = self.client.get(
            reverse("videos:subtitles_status", args=[self.video.id])
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "")
        self.assertTrue(data["pending"])

    def test_status_lists_existing_languages(self):
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="fr",
            label="French",
            content="WEBVTT",
        )
        data = self.client.get(
            reverse("videos:subtitles_status", args=[self.video.id])
        ).json()
        self.assertEqual(data["languages"], ["fr"])

    def test_status_of_missing_video_is_404(self):
        response = self.client.get(reverse("videos:subtitles_status", args=[9999]))
        self.assertEqual(response.status_code, 404)


class GenerateSubtitlesCommandTests(TestCase):
    def setUp(self):
        self.user = make_user("owner")

    @mock.patch("videos.transcription.is_configured", return_value=False)
    def test_refuses_to_run_without_a_provider(self, _configured):
        out = StringIO()
        call_command("generatesubtitles", stdout=out, stderr=out)
        self.assertIn("No subtitle provider is configured", out.getvalue())

    @mock.patch("videos.transcription.generate_and_record", return_value=True)
    @mock.patch("videos.transcription.is_configured", return_value=True)
    @mock.patch("videos.transcription.get_provider")
    def test_processes_videos_missing_subtitles(self, provider, _configured, record):
        provider.return_value = FakeProvider()
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        make_video(self.user, title="Silent", audio_file_id="")

        out = StringIO()
        call_command("generatesubtitles", stdout=out)

        record.assert_called_once_with(video.id)
        self.assertIn("1/1", out.getvalue())

    @mock.patch("videos.transcription.is_configured", return_value=True)
    @mock.patch("videos.transcription.get_provider")
    def test_skip_existing(self, provider, _configured):
        provider.return_value = FakeProvider()
        video = make_video(self.user, audio_file_id="audio/a.mp3")
        SubtitleTrack.objects.create(
            video=video, language_code="fr", label="French", content="WEBVTT"
        )
        out = StringIO()
        with mock.patch("videos.transcription.generate_and_record") as record:
            call_command("generatesubtitles", "--skip-existing", stdout=out)
        record.assert_not_called()
        self.assertIn("No videos need subtitles", out.getvalue())


class BackgroundPlaybackTests(TestCase):
    """Media Session wiring so playback survives the screen locking."""

    def setUp(self):
        self.user = make_user("viewer")
        self.video = make_video(
            self.user,
            title="Listen",
            audio_file_id="audio/track.mp3",
            thumbnail_file_id="thumbnails/a.jpg",
        )

    def _detail(self):
        return self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_video_element_plays_inline(self):
        # Required on iOS: without it Safari forces fullscreen and background
        # audio stops when the tab is hidden.
        self.assertRegex(self._detail(), r"<video[^>]*\splaysinline")

    def test_media_session_is_registered(self):
        content = self._detail()
        self.assertIn("navigator.mediaSession", content)
        self.assertIn("new MediaMetadata", content)
        self.assertIn("mediaSession.metadata", content)

    def test_offline_capable_handlers_are_registered(self):
        content = self._detail()
        for action in ("play", "pause", "seekbackward", "seekforward", "stop"):
            self.assertIn(f"        {action}:", content)

    def test_unsupported_actions_are_guarded(self):
        # setActionHandler throws for actions a browser does not implement.
        self.assertIn("try { mediaSession.setActionHandler", self._detail())

    def test_playback_state_follows_the_active_player(self):
        content = self._detail()
        self.assertIn("function setActivePlayer", content)
        self.assertIn("function syncPlaybackState", content)
        self.assertIn("el.addEventListener(name, syncPlaybackState)", content)

    def test_mode_switch_changes_the_active_player(self):
        # The OS controls must follow whichever mode the listener is in.
        content = self._detail()
        self.assertIn("setActivePlayer(audioPlayer)", content)
        self.assertIn("setActivePlayer(videoPlayer)", content)

    def test_metadata_uses_the_video_details(self):
        content = self._detail()
        self.assertIn('title: "Listen"', content)
        self.assertIn('album: "Fazakkir"', content)


class AudioSweepTests(TestCase):
    """The bulk "Generate missing MP3s" run behind the dashboard button."""

    def setUp(self):
        self.user = make_user("owner")
        # The sweep state is module level, so reset it between tests.
        audio_tasks._SWEEP_STATE.update(
            running=False, total=0, done=0, succeeded=0, failed=0
        )
        self.addCleanup(
            lambda: audio_tasks._SWEEP_STATE.update(
                running=False, total=0, done=0, succeeded=0, failed=0
            )
        )

    def test_eligible_skips_silent_videos(self):
        make_video(self.user, title="Untouched")
        make_video(self.user, title="Failed", audio_status="failed")
        make_video(self.user, title="Processing", audio_status="processing")
        make_video(self.user, title="Silent", audio_status="none")
        make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")

        titles = set(
            audio_tasks.eligible_audio_queryset().values_list("title", flat=True)
        )
        self.assertEqual(titles, {"Untouched", "Failed", "Processing"})

    @mock.patch("videos.audio_tasks.generate_audio", return_value=True)
    def test_sweep_converts_every_eligible_video(self, generate):
        wanted = make_video(self.user, title="A")
        also_wanted = make_video(self.user, title="B", audio_status="failed")
        make_video(self.user, title="Silent", audio_status="none")
        make_video(self.user, title="Ready", audio_file_id="audio/a.mp3")

        audio_tasks._sweep_videos()

        self.assertEqual(
            sorted(call.args[0] for call in generate.call_args_list),
            sorted([wanted.id, also_wanted.id]),
        )
        state = audio_tasks.audio_sweep_status()
        self.assertTrue(state["running"])
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["succeeded"], 2)
        self.assertEqual(state["failed"], 0)

    @mock.patch("videos.audio_tasks.generate_audio", return_value=False)
    def test_sweep_counts_failures(self, generate):
        make_video(self.user, title="Broken")
        make_video(self.user, title="AlsoBroken")

        audio_tasks._sweep_videos()

        state = audio_tasks.audio_sweep_status()
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["failed"], 2)
        self.assertEqual(state["succeeded"], 0)

    @mock.patch("videos.audio_tasks.generate_audio", return_value=True)
    def test_sweep_marks_each_video_processing(self, generate):
        video = make_video(self.user, title="Needs audio")

        seen = []

        def record(video_id, *_args, **_kwargs):
            seen.append(
                Video.objects.filter(id=video_id)
                .values_list("audio_status", flat=True)
                .first()
            )
            return True

        generate.side_effect = record
        audio_tasks._sweep_videos()

        # The video is flagged "processing" before the conversion runs, so the
        # on-play path cannot start a second worker for it.
        self.assertEqual(seen, ["processing"])

    @mock.patch("videos.audio_tasks.threading.Thread")
    def test_start_sweep_runs_once(self, thread):
        self.assertTrue(audio_tasks.start_audio_sweep())
        # The thread is faked, so the slot stays taken and a second call is refused.
        self.assertFalse(audio_tasks.start_audio_sweep())
        thread.assert_called_once()
        self.assertTrue(audio_tasks.audio_sweep_status()["running"])


class StaleJobTests(TestCase):
    """A host that sleeps can kill a worker, leaving a job stuck 'processing'."""

    def setUp(self):
        self.user = make_user("owner")
        # A video mid-conversion has no MP3 yet.
        self.video = make_video(self.user, title="Needs audio")

    def _stuck(self, minutes):
        Video.objects.filter(id=self.video.id).update(
            audio_status="processing",
            job_started_at=timezone.now() - timedelta(minutes=minutes),
        )

    def test_recent_job_is_left_alone(self):
        self._stuck(1)
        self.assertEqual(audio_tasks.clear_stale_jobs(), 0)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "processing")

    def test_abandoned_job_is_released(self):
        self._stuck(audio_tasks.stale_minutes() + 10)
        self.assertEqual(audio_tasks.clear_stale_jobs(), 1)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "failed")
        self.assertIsNone(self.video.job_started_at)
        # ...and it can now be retried.
        with mock.patch("videos.audio_tasks.threading.Thread") as thread:
            self.assertTrue(
                audio_tasks.schedule_audio_generation(self.video.id, retry=True)
            )
        thread.assert_called_once()

    def test_finished_subtitles_are_not_reset_by_a_stuck_audio_job(self):
        Video.objects.filter(id=self.video.id).update(
            auto_subtitle_status="ready",
        )
        self._stuck(audio_tasks.stale_minutes() + 10)
        audio_tasks.clear_stale_jobs()
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "ready")
        self.assertEqual(self.video.audio_status, "failed")

    def test_stuck_subtitle_job_is_released(self):
        Video.objects.filter(id=self.video.id).update(
            audio_file_id="audio/a.mp3",
            auto_subtitle_status="processing",
            job_started_at=timezone.now()
            - timedelta(minutes=audio_tasks.stale_minutes() + 10),
        )
        self.assertEqual(audio_tasks.clear_stale_jobs(), 1)
        self.video.refresh_from_db()
        self.assertEqual(self.video.auto_subtitle_status, "failed")

    def test_status_endpoint_clears_stuck_jobs(self):
        self._stuck(audio_tasks.stale_minutes() + 10)
        self.client.get(reverse("videos:audio_status", args=[self.video.id]))
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_status, "failed")
