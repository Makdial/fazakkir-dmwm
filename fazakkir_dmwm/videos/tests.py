from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
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


class VideoEditTests(TestCase):
    """Administrators can change every editable field of a stored video."""

    def setUp(self):
        self.admin = make_user("editor", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Old title",
            description="Old description",
            thumbnail_file_id="thumbnails/old.jpg",
            thumbnail_url="https://example.com/old.jpg",
            audio_file_id="audio/old.mp3",
            audio_url="https://example.com/old.mp3",
            audio_status="ready",
        )
        SubtitleTrack.objects.create(
            video=self.video,
            language_code="fr",
            label="French",
            content="WEBVTT",
        )
        self.client.login(username="editor", password="pw12345678")

    def test_edit_page_loads_with_current_values(self):
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old title")
        self.assertContains(response, "Old description")

    def test_member_cannot_reach_the_edit_page(self):
        make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("videos:edit", args=[self.video.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_title_and_description_are_saved(self):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "New title", "description": "New description"},
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.title, "New title")
        self.assertEqual(self.video.description, "New description")
        # Nothing else should have moved.
        self.assertEqual(self.video.file_id, "videos/sample.mp4")
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")
        self.assertEqual(self.video.audio_file_id, "audio/old.mp3")

    def test_empty_title_is_rejected(self):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "", "description": "x"},
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.title, "Old title")

    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_thumbnail")
    def test_replacing_the_thumbnail_swaps_and_deletes_the_old_object(
        self, upload_thumbnail, delete_file
    ):
        upload_thumbnail.return_value = {
            "path": "thumbnails/new.jpg",
            "url": "https://example.com/new.jpg",
        }
        image = SimpleUploadedFile(
            "new.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type="image/jpeg"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "Old title", "description": "Old description", "thumbnail_file": image},
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/new.jpg")
        # The previous object must not be left behind in the bucket.
        delete_file.assert_called_once_with("thumbnails/old.jpg")

    @mock.patch("videos.views.delete_file")
    def test_removing_the_thumbnail_clears_and_deletes(self, delete_file):
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Old title",
                "description": "Old description",
                "remove_thumbnail": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "")
        self.assertEqual(self.video.thumbnail_url, "")
        delete_file.assert_called_once_with("thumbnails/old.jpg")

    def test_new_thumbnail_and_removal_together_is_rejected(self):
        image = SimpleUploadedFile(
            "new.jpg", b"\xff\xd8\xff\xe0fakejpeg", content_type="image/jpeg"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Old title",
                "description": "Old description",
                "thumbnail_file": image,
                "remove_thumbnail": "on",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")

    def test_non_image_thumbnail_is_rejected(self):
        image = SimpleUploadedFile(
            "bad.txt", b"not an image", content_type="text/plain"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {"title": "Old title", "description": "Old description", "thumbnail_file": image},
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.thumbnail_file_id, "thumbnails/old.jpg")

    @mock.patch("videos.views.schedule_audio_generation")
    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_replacing_the_video_resets_derived_media(
        self, extract, upload_video, delete_file, schedule_audio
    ):
        extract.return_value = ("audio/new.mp3", "https://example.com/new.mp3", "ready")
        upload_video.return_value = {
            "path": "videos/new.mp4",
            "url": "https://example.com/new.mp4",
        }
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.file_id, "videos/new.mp4")
        self.assertEqual(self.video.audio_file_id, "audio/new.mp3")
        self.assertEqual(self.video.audio_status, "ready")
        # The stale objects and the subtitles of the old footage are gone.
        self.assertIn("videos/sample.mp4", delete_file.call_args_list[0][0])
        self.assertIn("audio/old.mp3", [call.args[0] for call in delete_file.call_args_list])
        self.assertEqual(SubtitleTrack.objects.filter(video=self.video).count(), 0)
        self.assertEqual(self.video.auto_subtitle_status, "")
        # The MP3 is already there, so no regeneration is scheduled.
        schedule_audio.assert_not_called()

    @mock.patch("videos.views.schedule_audio_generation")
    @mock.patch("videos.views.delete_file")
    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_replacing_the_video_schedules_mp3_when_extraction_is_deferred(
        self, extract, upload_video, delete_file, schedule_audio
    ):
        extract.return_value = ("", "", "")
        upload_video.return_value = {
            "path": "videos/new.mp4",
            "url": "https://example.com/new.mp4",
        }
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.video.refresh_from_db()
        self.assertEqual(self.video.audio_file_id, "")
        self.assertEqual(self.video.audio_status, "")
        schedule_audio.assert_called_once_with(self.video.id)

    @mock.patch("videos.views.upload_video")
    @mock.patch("videos.views._extract_audio_track")
    def test_failed_video_upload_keeps_the_original(
        self, extract, upload_video
    ):
        extract.return_value = ("", "", "")
        upload_video.side_effect = StorageError("bucket is full")
        replacement = SimpleUploadedFile(
            "new.mp4", b"\x00\x00\x00\x18ftypmp42fake", content_type="video/mp4"
        )
        response = self.client.post(
            reverse("videos:edit", args=[self.video.id]),
            {
                "title": "Renamed",
                "description": "d",
                "video_file": replacement,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.video.refresh_from_db()
        self.assertEqual(self.video.file_id, "videos/sample.mp4")
        self.assertEqual(self.video.title, "Old title")

    def test_detail_page_shows_edit_link_to_administrators(self):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertContains(response, reverse("videos:edit", args=[self.video.id]))

    def test_detail_page_hides_edit_link_from_members(self):
        make_user("regular")
        self.client.login(username="regular", password="pw12345678")
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertNotContains(response, reverse("videos:edit", args=[self.video.id]))


class VideoSharingTests(TestCase):
    """Visitors can share the video page and the MP3 as never-expiring links."""

    def setUp(self):
        self.admin = make_user("sharer", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.with_audio = make_video(
            self.admin,
            title="Shareable",
            audio_file_id="audio/share.mp3",
            audio_url="https://example.com/share.mp3",
            audio_status="ready",
        )
        self.silent = make_video(self.admin, title="No audio")

    def test_share_panel_is_visible_to_anonymous_visitors(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sharePanel"')
        self.assertContains(response, 'id="shareToggle"')

    def test_video_share_link_is_absolute(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        self.assertContains(response, "testserver" + reverse("videos:detail", args=[self.with_audio.id]))

    def test_mp3_share_link_uses_the_permanent_audio_route(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = self._share_links(response.content.decode())
        self.assertIn("testserver" + reverse("videos:audio_stream", args=[self.with_audio.id]), body)

    def test_mp3_share_link_is_never_the_expiring_presigned_url(self):
        # The download button legitimately signs a URL that dies after twelve
        # hours, but the link handed to a friend must never be one of those.
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        share_block = self._share_links(response.content.decode())
        self.assertNotIn("X-Amz-Signature", share_block)
        self.assertNotIn("share.mp3?", share_block)

    @staticmethod
    def _share_links(body: str) -> str:
        """Return just the SHARE_LINKS map, with JS escaping undone.

        ``escapejs`` turns ``/`` into ``\\/`` and ``-`` into ``\\u002D``. Both are
        valid inside a JavaScript string literal, so the browser decodes them
        back to the intended URL; the test just has to compare the decoded form.
        """
        import re

        start = body.index("const SHARE_LINKS")
        end = body.index("}", body.index("audio:", start))
        block = body[start:end].replace("\\/", "/")
        return re.sub(r"\\u00([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), block)

    def test_video_without_audio_hides_the_mp3_target(self):
        response = self.client.get(reverse("videos:detail", args=[self.silent.id]))
        self.assertContains(response, 'id="sharePanel"')
        self.assertNotContains(response, 'data-share="audio"')

    def test_share_networks_are_prefilled_with_the_link(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        self.assertIn("wa.me", body)
        self.assertIn("t.me/share/url", body)
        self.assertIn("facebook.com/sharer", body)
        self.assertIn("twitter.com/intent/tweet", body)

    def test_share_panel_javascript_is_present(self):
        response = self.client.get(reverse("videos:detail", args=[self.with_audio.id]))
        body = response.content.decode()
        self.assertIn("function shareKind", body)
        self.assertIn("function toggleShareMenu", body)
        # A dismissal must not be reported as a failure.
        self.assertIn("AbortError", body)


class CustomPlayerTests(TestCase):
    """The player owns its controls instead of delegating to the browser.

    The native control bar drew its own play button over the poster image, so
    the poster and the `controls` attribute were both dropped. Two things must
    survive that change: `playsinline`, without which iOS takes playback
    fullscreen and background playback stops, and the Media Session wiring,
    which is what keeps audio going when the screen locks.
    """

    def setUp(self):
        self.admin = make_user("player", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Played",
            audio_file_id="audio/p.mp3",
            audio_url="https://example.com/p.mp3",
            audio_status="ready",
        )

    def body(self):
        response = self.client.get(reverse("videos:detail", args=[self.video.id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_video_has_no_poster(self):
        self.assertNotIn("poster=", self.body())

    def test_media_elements_have_no_native_controls_attribute(self):
        body = self.body()
        self.assertNotIn('<video id="video-player" controls', body)
        self.assertNotIn('<audio id="audio-player" controls', body)

    def test_playsinline_is_kept_for_background_playback(self):
        body = self.body()
        video_tag = body[body.index("<video id="):body.index(">", body.index("<video id="))]
        self.assertIn("playsinline", video_tag)

    def test_custom_control_bar_is_rendered(self):
        body = self.body()
        for element_id in ("playerBar", "seekBar", "playPauseBtn", "volumeBar",
                           "rateSelect", "fullscreenBtn", "currentTime", "duration"):
            self.assertIn(f'id="{element_id}"', body)

    def test_media_session_wiring_survives(self):
        body = self.body()
        self.assertIn("navigator.mediaSession", body)
        self.assertIn("mediaSession.setActionHandler", body)
        # seekbackward / seekforward are what the lock screen buttons use.
        self.assertIn("seekbackward", body)
        self.assertIn("seekforward", body)
        self.assertIn("MediaMetadata", body)

    def test_control_bar_drives_the_active_player(self):
        body = self.body()
        # Switching to MP3 mode must not leave the bar bound to the video only.
        self.assertIn("activePlayer", body)
        self.assertIn("function refreshAllControls", body)
        self.assertIn("if (typeof refreshAllControls", body)

    def test_seek_and_volume_are_wired(self):
        body = self.body()
        self.assertIn("function seekBy", body)
        self.assertIn('seekBar.addEventListener("input"', body)
        self.assertIn('volumeBar.addEventListener("input"', body)

    def test_keyboard_transport_shortcuts_are_wired(self):
        body = self.body()
        self.assertIn('"keydown"', body)
        self.assertIn("togglePlayPause()", body)

    def test_subtitle_tracks_still_reach_the_video_element(self):
        SubtitleTrack.objects.create(
            video=self.video, language_code="fr", label="French", content="WEBVTT"
        )
        body = self.body()
        self.assertIn('kind="subtitles"', body)
        self.assertIn(reverse("videos:subtitle_file", args=[self.video.id, "fr"]), body)


class AudioModeCoverTests(TestCase):
    """Listening to the MP3 shows the thumbnail instead of a black rectangle.

    Switching to the audio element hides the <video>, which left the 16:9 stage
    as an empty black box.
    """

    def setUp(self):
        self.admin = make_user("listener", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Cover art",
            thumbnail_file_id="thumbnails/c.jpg",
            thumbnail_url="https://example.com/c.jpg",
            audio_file_id="audio/c.mp3",
            audio_url="https://example.com/c.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_cover_image_is_rendered(self):
        self.assertIn('id="audioCover"', self.body)
        # display_thumbnail_url may be a presigned R2 link, so match on the
        # object key rather than the stored URL.
        self.assertIn("thumbnails/c.jpg", self.body)
        self.assertIn('alt="Cover art"', self.body)

    def test_cover_is_hidden_until_audio_mode_is_entered(self):
        # The rules live in the stylesheet, not the markup.
        css = self._player_css()
        self.assertIn(".audio-cover", css)
        self.assertIn("video-player.audio-mode .audio-cover", css)
        # Hidden by default, shown only once the audio mode class is present.
        cover = css[css.index(".audio-cover {"):css.index("}", css.index(".audio-cover {"))]
        self.assertIn("display: none", cover)

    def test_switching_mode_toggles_the_cover_class(self):
        self.assertIn('classList.add("audio-mode")', self.body)
        self.assertIn('classList.remove("audio-mode")', self.body)

    def test_video_without_a_thumbnail_gets_no_cover_element(self):
        bare = make_video(self.admin, title="No art", audio_file_id="audio/n.mp3")
        body = self.client.get(reverse("videos:detail", args=[bare.id])).content.decode()
        self.assertNotIn('id="audioCover"', body)

    @staticmethod
    def _player_css() -> str:
        root = (
            Path(__file__).resolve().parent.parent
            / "static" / "css" / "videoplayer.css"
        )
        return root.read_text(encoding="utf-8")


class AutoHidingControlsTests(TestCase):
    """The control bar must not sit on top of the picture while it plays."""

    def setUp(self):
        self.admin = make_user("tapper", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Tapped",
            audio_file_id="audio/t.mp3",
            audio_url="https://example.com/t.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_bar_starts_hidden(self):
        self.assertIn("hideControls()", self.body)
        # The hidden state is the default, so the first frame is never covered.
        self.assertIn("visibility: hidden", self._player_css())

    def test_tap_zone_brings_the_controls_back(self):
        self.assertIn('id="tapZone"', self.body)

    def test_tapping_the_picture_plays_and_pauses(self):
        # Tapping used to only reveal the bar, which left no tap anywhere on
        # the video able to start playback.
        self.assertIn("tapZone.addEventListener", self.body)
        handler = self.body[
            self.body.index("tapZone.addEventListener"):self.body.index("if (playerStage)")
        ]
        self.assertIn("togglePlayPause()", handler)
        self.assertIn("showControls()", handler)

    def test_refused_playback_reveals_the_bar_instead_of_failing_silently(self):
        toggle = self.body[
            self.body.index("function togglePlayPause"):self.body.index("function seekBy")
        ]
        self.assertIn("showControls()", toggle)
        self.assertIn(".catch(", toggle)

    def test_play_hides_and_pause_shows(self):
        self.assertIn('el.addEventListener("play"', self.body)
        self.assertIn('el.addEventListener("pause"', self.body)
        self.assertIn("CONTROLS_IDLE_MS", self.body)

    def test_hover_reveals_the_bar_on_a_pointer_device(self):
        # Entering the picture must be enough, without needing to move.
        self.assertIn('addEventListener("mouseenter"', self.body)
        self.assertIn('addEventListener("mouseleave"', self.body)

    def test_hover_behaviour_is_limited_to_real_pointers(self):
        # A tablet reports hover: none even with a mouse attached; without this
        # guard a synthesised mouseenter would latch the bar open.
        self.assertIn("(hover: hover) and (pointer: fine)", self.body)
        self.assertIn("const canHover", self.body)

    def test_bar_never_hides_while_the_pointer_is_resting_on_it(self):
        guard = self.body[
            self.body.index("function hideControls()"):self.body.index("function toggleControls()")
        ]
        self.assertIn("if (pointerInsideStage && canHover) return", guard)

    def test_playing_hides_only_when_nobody_is_hovering(self):
        self.assertIn("function shouldAutoHide()", self.body)
        helper = self.body[
            self.body.index("function shouldAutoHide()"):self.body.index("function showControls()")
        ]
        self.assertIn("pointerInsideStage", helper)
        # A paused video must always keep its controls up.
        self.assertIn("return playing && !pointerInsideStage", helper)
        play = self.body[self.body.index('el.addEventListener("play"'):]
        self.assertIn("if (shouldAutoHide()) hideControls()", play[:400])

    def test_a_paused_player_is_never_left_inert(self):
        # Removing the poster means no picture loads until playback starts, so
        # the player used to open as a black rectangle with hidden controls.
        self.assertIn('id="bigPlay"', self.body)
        self.assertIn("function refreshBigPlay()", self.body)
        self.assertIn(".player-bigplay", self._player_css())

    def test_big_play_button_starts_playback(self):
        handler = self.body[
            self.body.index("if (bigPlay) {"):self.body.index("if (tapZone) {")
        ]
        self.assertIn("togglePlayPause()", handler)

    def test_big_play_is_hidden_while_playing(self):
        helper = self.body[
            self.body.index("function refreshBigPlay()"):self.body.index("if (playerStage)")
        ]
        self.assertIn("bigPlay.hidden = !paused", helper)

    def test_controls_start_visible_not_hidden(self):
        # A paused video must show its bar; only playback hides it.
        tail = self.body[self.body.index("wireControls()\n"):]
        self.assertIn("showControls()", tail[:200])
        self.assertNotIn("hideControls()", tail[:200])

    def test_mobile_puts_the_bar_below_the_picture(self):
        css = self._player_css()
        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("position: static", css)

    @staticmethod
    def _player_css() -> str:
        root = (
            Path(__file__).resolve().parent.parent
            / "static" / "css" / "videoplayer.css"
        )
        return root.read_text(encoding="utf-8")


class ThumbnailOverlayTests(TestCase):
    """The hover play overlay must never permanently cover a thumbnail.

    Touch devices report ``hover: none``, and the card is already one big tap
    target there, so the overlay is hidden rather than forced visible.
    """

    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "static" / "css" / "videos.css"
        self.css = root.read_text(encoding="utf-8")

    def test_touch_rule_hides_the_overlay_instead_of_showing_it(self):
        touch_block = self._hover_none_block()
        self.assertIn("display: none", touch_block)
        self.assertNotIn("opacity: 1", touch_block)

    def test_overlay_sits_on_the_thumbnail_edge_not_the_middle(self):
        block = self._block_for(".play-icon {")
        self.assertIn("left: 12px", block)
        self.assertIn("translateY(-50%)", block)
        # Centring with translate(-50%, -50%) is what put it over the subject.
        self.assertNotIn("left: 50%", block)
        self.assertNotIn("translate(-50%, -50%)", block)

    def test_overlay_still_appears_on_hover_capable_devices(self):
        self.assertIn(".video-card:hover .play-icon", self.css)

    def _block_for(self, opening: str) -> str:
        start = self.css.index(opening)
        end = self.css.index("}", start)
        return self.css[start:end]

    def _hover_none_block(self) -> str:
        start = self.css.index("@media (hover: none)")
        depth = 0
        for index in range(start, len(self.css)):
            if self.css[index] == "{":
                depth += 1
            elif self.css[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.css[start:index]
        raise AssertionError("unterminated @media (hover: none) block")


class FakeBody:

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


class ProductionStaticTests(TestCase):
    """Guard the production static-file setup the Docker build depends on."""

    def test_whitenoise_middleware_is_installed(self):
        from django.conf import settings

        self.assertIn(
            "whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE
        )

    def test_dockerfile_build_disables_debug_with_the_right_variable(self):
        # The build step must set DEBUG (which the settings read), not
        # DJANGO_DEBUG (which Django ignores). With the wrong name, collectstatic
        # runs with DEBUG=1, writes no staticfiles.json, and every page in the
        # deployed container fails with "Missing staticfiles manifest entry".
        import re

        import fazakkir_dmwm

        # The Dockerfile sits at the repository root, one level above the
        # Django project directory that holds this package.
        repo_root = Path(fazakkir_dmwm.__file__).resolve().parents[2]
        dockerfile = repo_root / "Dockerfile"
        self.assertTrue(
            dockerfile.exists(), f"Dockerfile not found at {dockerfile}"
        )
        text = dockerfile.read_text(encoding="utf-8")
        # The env var must be DEBUG, which the settings actually read. Checking
        # the whole file is fine: DJANGO_DEBUG must not appear anywhere, not
        # even in a comment, so nobody is tempted to rely on it.
        self.assertIn("DEBUG=0", text)
        self.assertNotIn("DJANGO_DEBUG", text)
        # The build fails loudly instead of shipping an image without a manifest.
        self.assertIn("staticfiles.json", text)

    def test_production_renders_every_page(self):
        response = self.client.get(reverse("videos:list"))
        self.assertEqual(response.status_code, 200)

    def test_settings_load_without_hsts_variable(self):
        # The container build runs with DEBUG=0 and no SECURE_HSTS_SECONDS set.
        # Indexing os.environ for an absent key used to raise KeyError and fail
        # the image build, so reload the settings with the variable removed.
        import importlib
        import os

        os.environ["DJANGO_SETTINGS_MODULE"] = "fazakkir_dmwm.settings"
        previous = os.environ.pop("SECURE_HSTS_SECONDS", None)
        module = importlib.import_module("fazakkir_dmwm.settings")
        try:
            importlib.reload(module)
            # Unset means HSTS is simply never configured, which leaves
            # Django's own default (0, disabled) in place.
            self.assertFalse(hasattr(module, "SECURE_HSTS_SECONDS"))
        finally:
            if previous is not None:
                os.environ["SECURE_HSTS_SECONDS"] = previous
            importlib.reload(module)


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

class VideoSearchTests(TestCase):
    """Search matches any word in the title or description, the uploader, or a year."""

    def setUp(self):
        self.alice = make_user("alice")
        self.bob = make_user("bob")
        self.a = make_video(
            self.alice,
            title="Mame Cheikh Capitaine",
            description="Recitation recorded in Diourbel",
        )
        self.b = make_video(self.bob, title="Serigne Moustapha Sy", description="Wedding")
        # created_at is auto_now_add, so pin the year directly on the row.
        Video.objects.filter(pk=self.b.pk).update(
            created_at=datetime(2000, 8, 19, tzinfo=dt_timezone.utc)
        )

    def found(self, query):
        response = self.client.get(reverse("videos:search"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return list(response.context["videos"])

    def test_search_page_renders_without_a_query(self):
        response = self.client.get(reverse("videos:search"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["videos"]), [])

    def test_matches_a_word_in_the_title(self):
        self.assertEqual(self.found("mame"), [self.a])

    def test_title_search_is_case_insensitive(self):
        self.assertEqual(self.found("MAME"), [self.a])

    def test_matches_a_word_in_the_description(self):
        self.assertEqual(self.found("diourbel"), [self.a])

    def test_matches_the_uploaders_name(self):
        self.assertEqual(self.found("bob"), [self.b])

    def test_matches_a_year_of_upload(self):
        # Nothing stores "2000" as text, so this only works via created_at.
        self.assertEqual(self.found("2000"), [self.b])

    def test_year_that_matches_nothing_returns_nothing(self):
        self.assertEqual(self.found("1999"), [])

    def test_words_are_combined_rather_than_matched_as_a_phrase(self):
        # "mame 2000" should return nothing: neither video satisfies both.
        self.assertEqual(self.found("mame 2000"), [])
        self.assertEqual(self.found("mame diourbel"), [self.a])

    def test_unknown_word_returns_nothing(self):
        self.assertEqual(self.found("zzzznotathing"), [])

    def test_query_is_echoed_back_for_the_search_box(self):
        response = self.client.get(reverse("videos:search"), {"q": "mame"})
        self.assertEqual(response.context["query"], "mame")

    def test_blank_query_returns_nothing_rather_than_everything(self):
        response = self.client.get(reverse("videos:search"), {"q": "   "})
        self.assertEqual(list(response.context["videos"]), [])

    def test_search_box_appears_in_the_navigation(self):
        response = self.client.get(reverse("videos:list"))
        self.assertContains(response, reverse("videos:search"))
        self.assertContains(response, 'name="q"')


class AudioOnlyUploadTests(TestCase):
    """An administrator can upload an MP3 with no video behind it."""

    def setUp(self):
        self.admin = make_user("uploader", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.client.login(username="uploader", password="pw12345678")

    def audio_post(self, **extra):
        payload = {
            "media_type": "audio",
            "title": "Recitation",
            "description": "",
            "audio_file": SimpleUploadedFile(
                "recitation.mp3", b"ID3\x03\x00fake", content_type="audio/mpeg"
            ),
        }
        payload.update(extra)
        # None cannot be encoded into multipart, so drop the key instead.
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(reverse("videos:upload_submit"), payload)

    @mock.patch("videos.views.upload_audio")
    def test_audio_upload_creates_an_audio_only_record(self, upload_audio):
        upload_audio.return_value = {
            "path": "audio/rec.mp3",
            "url": "https://example.com/rec.mp3",
        }
        response = self.audio_post()
        self.assertTrue(response.json()["success"])

        video = Video.objects.get()
        self.assertEqual(video.media_type, "audio")
        self.assertEqual(video.file_id, "")
        self.assertEqual(video.audio_file_id, "audio/rec.mp3")
        self.assertEqual(video.audio_status, "ready")
        self.assertTrue(video.is_audio_only)
        self.assertFalse(video.has_video)

    @mock.patch("videos.views.upload_audio")
    def test_audio_only_has_no_video_download_link(self, upload_audio):
        upload_audio.return_value = {"path": "audio/rec.mp3", "url": ""}
        self.audio_post()
        video = Video.objects.get()
        self.assertEqual(video.video_download_url, "")
        # The MP3 link is what people actually want.
        self.assertTrue(video.audio_download_url)

    def test_audio_upload_without_a_file_is_rejected(self):
        response = self.audio_post(audio_file=None)
        self.assertFalse(response.json()["success"])

    def test_audio_upload_rejects_a_video_file_too(self):
        response = self.audio_post(
            video_file=SimpleUploadedFile(
                "v.mp4", b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4"
            )
        )
        self.assertFalse(response.json()["success"])

    def test_video_upload_without_a_video_file_is_rejected(self):
        response = self.client.post(
            reverse("videos:upload_submit"),
            {"media_type": "video", "title": "No file", "description": ""},
        )
        self.assertFalse(response.json()["success"])

    @mock.patch("videos.views.upload_audio")
    def test_audio_page_shows_cover_and_no_mode_toggle(self, upload_audio):
        upload_audio.return_value = {
            "path": "audio/rec.mp3",
            "url": "https://example.com/rec.mp3",
        }
        self.audio_post()
        video = Video.objects.get()
        video.thumbnail_file_id = "thumbnails/t.jpg"
        video.thumbnail_url = "https://example.com/t.jpg"
        video.save()

        body = self.client.get(
            reverse("videos:detail", args=[video.id])
        ).content.decode()
        # No picture to switch back to, so the Video / Listen toggle is gone.
        self.assertNotIn('id="mode-video"', body)
        self.assertNotIn('id="mode-audio"', body)
        self.assertIn("audio-mode", body)

    def test_upload_page_offers_both_types(self):
        response = self.client.get(reverse("videos:upload"))
        self.assertContains(response, 'name="media_type"')
        self.assertContains(response, 'name="audio_file"')
        self.assertContains(response, 'name="video_file"')

class TranslationCoverageTests(TestCase):
    """Every string shown in the UI must exist in all three catalogues.

    GNU gettext is not available on this machine, so `makemessages` cannot run
    and a newly added string silently falls back to English on the French,
    Arabic and Wolof pages. This test fails the build when that happens, and
    points at exactly which strings are missing.
    """

    LANGUAGES = ("fr", "ar", "wo")
    SKIP_DIRS = {".venv", "staticfiles", "__pycache__", "migrations", "locale"}
    TRANS = re.compile(r"{%\s*trans(?:late)?\s+(['\"])(.*?)\1", re.S)
    BLOCKTRANS = re.compile(r"{%\s*blocktrans[^%]*%}(.*?){%\s*endblocktrans\s*%}", re.S)
    GETTEXT = re.compile(r"\bgettext\(\s*(['\"])(.*?)\1", re.S)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        project_root = Path(__file__).resolve().parent.parent
        cls.used: set[str] = set()
        for path in project_root.rglob("*"):
            if path.is_dir() or any(part in cls.SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in {".html", ".py", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in cls.TRANS.finditer(text):
                cls.used.add(cls._normalise(match.group(2)))
            for match in cls.BLOCKTRANS.finditer(text):
                cls.used.add(cls._normalise(cls._singular_form(match.group(1))))
            if path.suffix == ".py":
                for match in cls.GETTEXT.finditer(text):
                    cls.used.add(cls._normalise(match.group(2)))

    @staticmethod
    def _normalise(value: str) -> str:
        """Rewrite a template placeholder the way Django stores it.

        ``{{ email }}`` inside trans/blocktrans is written to the catalogue as
        ``%(email)s``, so the source text has to be converted before it can be
        compared with a msgid.
        """
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", " ".join(value.split()))

    @staticmethod
    def _singular_form(body: str) -> str:
        """The msgid of a blocktrans body.

        A pluralised blocktrans holds two halves separated by {% plural %},
        and only the first becomes the msgid; the second becomes msgid_plural.
        """
        head = re.split(r"{%\s*plural\s*%}", " ".join(body.split()), maxsplit=1)[0]
        return head.strip()

    def test_every_language_covers_every_string(self):
        root = Path(__file__).resolve().parent.parent
        for lang in self.LANGUAGES:
            po = root / "locale" / lang / "LC_MESSAGES" / "django.po"
            with self.subTest(language=lang):
                self.assertTrue(po.exists(), f"missing catalogue for {lang}")
                text = po.read_text(encoding="utf-8")
                have = set(re.findall(r'(?m)^msgid\s+"((?:[^"\\]|\\.)*)"', text))
                missing = sorted(self.used - have)
                self.assertEqual(
                    missing, [],
                    f"{lang} is missing {len(missing)} translations: "
                    f"{missing[:10]}",
                )

class MarkupBalanceTests(TestCase):
    """The player markup must close every element it opens.

    A single missing ``</div>`` left ``.video-player-container`` unclosed, so the
    download buttons and the entire comments section became its children. The
    control bar is absolutely positioned against that container, which therefore
    spanned the whole page and pushed the bar down to the bottom of the
    document. The page still rendered, so nothing caught it.
    """

    def setUp(self):
        self.user = User.objects.create_user("balanced", password="pw12345678")
        self.video = Video.objects.create(
            user=self.user,
            title="Nested",
            file_id="videos/n.mp4",
            video_url="https://example.com/n.mp4",
            audio_file_id="audio/n.mp3",
            audio_status="ready",
        )
        self.body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_no_unclosed_div_in_the_player_region(self):
        start = self.body.index('<div class="video-player-container"')
        end = self.body.index('<div class="video-details"')
        chunk = self.body[start:end]
        self.assertEqual(
            len(re.findall(r"<div\b", chunk)),
            len(re.findall(r"</div>", chunk)),
            "the player container is not closed, so the bar anchors to the page",
        )

    def test_download_buttons_are_not_inside_the_player_container(self):
        start = self.body.index('<div class="video-player-container"')
        end = self.body.index('<div class="player-controls"')
        self.assertGreater(
            end, start,
            "player-controls must follow the closed player container",
        )
        # The container's closing tag has to appear before the controls.
        tail = self.body[start:end]
        self.assertEqual(
            tail.count("<div"), tail.count("</div>"),
            "player-controls leaked inside .video-player-container",
        )

    def test_the_control_bar_is_a_sibling_of_the_picture(self):
        stage = self.body.index('id="playerStage"')
        bar = self.body.index('id="playerBar"')
        controls = self.body.index('class="player-controls"')
        self.assertLess(stage, bar)
        self.assertLess(bar, controls)


class ConfirmationDialogTests(TestCase):
    """Destructive actions must ask first, and cancelling must change nothing.

    window.confirm() was unusable here: the message is always English, it cannot
    be styled, and some mobile browsers suppress it outside a user gesture. The
    offline "Remove download" button in particular deleted the cached audio
    immediately, with no way back.
    """

    def setUp(self):
        self.admin = make_user("deleter", is_staff=True, is_superuser=True)
        Profile.objects.filter(user=self.admin).update(access_level=Profile.Access.WRITE)
        self.video = make_video(
            self.admin,
            title="Precious",
            audio_file_id="audio/p.mp3",
            audio_url="https://example.com/p.mp3",
            audio_status="ready",
        )
        self.client.login(username="deleter", password="pw12345678")

    @staticmethod
    def _asset(name: str) -> str:
        root = Path(__file__).resolve().parent.parent
        return (root / "static" / name).read_text(encoding="utf-8")

    def detail_page(self):
        return self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()

    def test_confirm_script_is_loaded_on_every_page(self):
        body = self.client.get(reverse("videos:list")).content.decode()
        # ManifestStaticFilesStorage appends a content hash in production, so
        # match the name rather than an exact unhashed path.
        self.assertRegex(body, r"/static/js/confirm\.[0-9a-f]+\.js|/static/js/confirm\.js")
        self.assertRegex(body, r"/static/css/confirm\.[0-9a-f]+\.css|/static/css/confirm\.css")

    def test_confirm_script_is_loaded_before_offline_js(self):
        # offline.js calls fazakkirConfirm when removing a download, so the
        # helper has to be defined first.
        import re as _re

        body = self.client.get(reverse("videos:list")).content.decode()
        confirm = _re.search(r"/static/js/confirm[^\"']*\.js", body)
        offline = _re.search(r"/static/js/offline[^\"']*\.js", body)
        self.assertIsNotNone(confirm, "confirm.js is not referenced")
        self.assertIsNotNone(offline, "offline.js is not referenced")
        self.assertLess(confirm.start(), offline.start())

    def test_confirm_script_exposes_a_promise_api(self):
        script = self._asset("js/confirm.js")
        self.assertIn("window.fazakkirConfirm", script)
        self.assertIn("Promise", script)

    def test_dialog_offers_cancel_and_a_destructive_default(self):
        script = self._asset("js/confirm.js")
        self.assertIn("confirm-cancel", script)
        self.assertIn("is-danger", script)

    def test_escape_cancels_rather_than_confirms(self):
        script = self._asset("js/confirm.js")
        self.assertIn('event.key === "Escape"', script)
        # The Escape branch must resolve false, not true.
        escape_line = script[script.index('event.key === "Escape"'):]
        self.assertIn("close(false)", escape_line[:200])

    def test_clicking_the_backdrop_cancels(self):
        script = self._asset("js/confirm.js")
        self.assertIn("if (event.target === parts.root) finish(false)", script)

    def test_video_delete_asks_before_calling_the_endpoint(self):
        body = self.detail_page()
        self.assertIn("function deleteVideo", body)
        self.assertIn("fazakkirConfirm({", body)
        # The old blocking confirm() must be gone.
        self.assertNotIn('confirm("', body)

    def test_no_raw_confirm_calls_remain_anywhere(self):
        for template in (
            "videos/templates/videos/detail.html",
            "videos/templates/videos/playlist_detail.html",
            "dashboard/templates/dashboard/home.html",
            "dashboard/templates/dashboard/comments.html",
        ):
            path = Path(__file__).resolve().parent.parent / template
            with self.subTest(template=template):
                self.assertNotIn("!confirm(", path.read_text(encoding="utf-8"))

    def test_offline_removal_asks_first(self):
        script = self._asset("js/offline.js")
        self.assertIn("fazakkirConfirm", script)
        # Cancelling must return before anything is dropped from the cache.
        self.assertIn("if (!ok) return", script)

    def test_offline_library_remove_asks_first(self):
        script = self._asset("js/offline.js")
        library = script[script.index("function renderLibrary"):]
        self.assertIn("fazakkirConfirm", library)

    def test_saving_offline_does_not_ask(self):
        # Saving is not destructive and must stay a single tap: the dialog is
        # only requested when the item is already saved.
        script = self._asset("js/offline.js")
        toggle = script[script.index("function setupToggle"):script.index("/* --- /offline/ page")]
        self.assertIn("var ask = saved", toggle)
        self.assertIn(": Promise.resolve(true)", toggle)
        self.assertLess(toggle.index("var ask = saved"), toggle.index("fazakkirConfirm"))


class PlayerLayoutTests(TestCase):
    """The control bar must stay inside the player on a desktop.

    The bar was absolutely positioned but its parent had no position, so it
    resolved against an ancestor further up the page: it stretched to the full
    window width and overlapped the download buttons underneath.
    """

    @staticmethod
    def _css(name: str) -> str:
        root = Path(__file__).resolve().parent.parent / "static" / "css"
        return (root / name).read_text(encoding="utf-8")

    def test_player_container_is_the_positioning_context(self):
        css = self._css("videos.css")
        start = css.index(".video-player-container {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: relative", block)

    def test_control_bar_is_positioned_against_its_container(self):
        css = self._css("videoplayer.css")
        start = css.index(".player-bar {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: absolute", block)
        self.assertIn("left: 0", block)
        self.assertIn("right: 0", block)
        # bottom: 0 against a container exactly as tall as the picture is what
        # keeps the bar attached to the video instead of the page.
        self.assertIn("bottom: 0", block)

    def test_positioning_context_is_declared_next_to_the_bar(self):
        # The bar lives in videoplayer.css but its positioning context was only
        # in videos.css. The two load separately, so the declaration is repeated
        # here; without it the bar anchors to the page and lands over the
        # comments, far below the video.
        css = self._css("videoplayer.css")
        self.assertIn(".video-player-container", css)
        start = css.index(".video-player-container {")
        block = css[start:css.index("}", start)]
        self.assertIn("position: relative", block)
        # The rule must appear before the bar's own absolute positioning.
        self.assertLess(start, css.index(".player-bar {"))

    def test_desktop_play_button_does_not_cover_the_thumbnail(self):
        css = self._css("videoplayer.css")
        desktop = css[css.index("@media (min-width: 769px)"):]
        block = desktop[:desktop.index("}")]
        self.assertIn("background: transparent", block)
        self.assertIn("box-shadow", block)

    def test_light_mode_controls_do_not_rely_on_white_on_white(self):
        css = self._css("videoplayer.css")
        mobile = css[css.index("@media (max-width: 768px)"):]
        self.assertIn("--bar-btn:", mobile)
        self.assertIn("var(--bg-tertiary)", mobile)
        self.assertIn("var(--text-primary)", mobile)

    def test_desktop_bar_stays_light_on_dark_over_the_picture(self):
        css = self._css("videoplayer.css")
        start = css.index(".player-bar {")
        block = css[start:css.index("}", start)]
        self.assertIn("--bar-fg: #fff", block)
        self.assertIn("rgba(0, 0, 0, 0.85)", block)


class PlayerScriptTests(TestCase):
    """Execute the rendered player script instead of only grepping it.

    Every earlier player test asserted that certain strings appeared in the
    HTML. That passed happily while the script threw a ReferenceError on the
    first line, because ``playerBar`` was used in six places and never
    declared, so the whole player was dead on arrival. String assertions cannot
    catch that; only running the code can.
    """

    def setUp(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("node is not installed on this machine")
        self.node = node

        user = User.objects.create_user(
            "runner", password="pw12345678", is_staff=True, is_superuser=True
        )
        Profile.objects.filter(user=user).update(access_level=Profile.Access.WRITE)
        self.video = Video.objects.create(
            user=user,
            title="Runnable",
            file_id="videos/run.mp4",
            video_url="https://example.com/run.mp4",
            audio_file_id="audio/run.mp3",
            audio_status="ready",
        )

    def _script(self) -> str:
        body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        # The page opens with the theme bootstrap inline, so pick the block that
        # actually holds the player rather than the first one on the page.
        for block in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                body, re.S):
            if "playerBar" in block:
                return block
        raise AssertionError("player script block not found in the page")

    def _run(self) -> str:
        project = Path(__file__).resolve().parent.parent
        harness = project / "static" / "js" / "tests" / "player_smoke.js"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "player.js"
            target.write_text(self._script(), encoding="utf-8")
            proc = subprocess.run(
                [self.node, str(harness), str(target)],
                capture_output=True, text=True, timeout=120, cwd=str(project),
            )
        return (proc.stdout or "") + (proc.stderr or "")

    def test_player_script_runs_without_throwing(self):
        output = self._run()
        self.assertIn(
            "script executed without throwing", output,
            "the player script failed to execute:\n" + output,
        )

    def test_tapping_the_picture_starts_playback(self):
        output = self._run()
        self.assertIn("video.play() called : true", output)
        self.assertIn("bar visible         : true", output)

    def test_every_control_is_wired_to_a_handler(self):
        output = self._run()
        for element, event in (
            ("tapZone", "click"),
            ("bigPlay", "click"),
            ("playPauseBtn", "click"),
            ("video-player", "play"),
            ("video-player", "pause"),
        ):
            self.assertNotIn(
                f"{element} {event} NOT BOUND", output,
                f"{element} has no {event} handler:\n{output}",
            )

    def test_every_referenced_element_variable_is_declared(self):
        # A variable used in the auto-hide code must also be declared.
        script = self._script()
        for name in ("playerBar", "playerStage", "tapZone", "bigPlay"):
            self.assertRegex(script, rf"const {name}\s*=", f"{name} is never declared")
            self.assertEqual(
                len(re.findall(rf"\bconst {name}\s*=", script)), 1,
                f"{name} is declared more than once",
            )

    def test_audio_only_flag_is_a_boolean_not_a_quoted_string(self):
        # "false" is a non-empty string and therefore truthy, which made every
        # control drive the audio element instead of the video.
        script = self._script()
        match = re.search(r"const audioOnly\s*=\s*(.+)", script)
        self.assertIsNotNone(match, "audioOnly is not set")
        self.assertEqual(
            match.group(1).strip(), "false",
            'audioOnly must be a bare boolean; a quoted "false" is truthy',
        )

    def test_controls_drive_the_video_for_a_normal_upload(self):
        output = self._run()
        self.assertIn("video.play() called : true", output)

    def test_every_id_the_script_looks_up_exists_in_the_markup(self):
        # These are only rendered when MP3 generation is running or the viewer
        # can generate subtitles, and the script already guards each lookup.
        conditional = {"audioStatus", "autoSubBtn", "autoSubMessage", "autoSubPanel"}

        output = self._run()
        line = [l for l in output.splitlines() if l.startswith("requested ids:")]
        self.assertTrue(line, "harness did not report the ids it looked up")
        requested = [i for i in line[0].split(":", 1)[1].split() if i not in conditional]

        body = self.client.get(
            reverse("videos:detail", args=[self.video.id])
        ).content.decode()
        missing = [i for i in requested if f'id="{i}"' not in body]
        self.assertEqual(
            missing, [],
            f"the script asks for elements the page does not render: {missing}",
        )
