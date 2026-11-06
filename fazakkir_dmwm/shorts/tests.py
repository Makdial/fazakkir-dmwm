import io
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from videos import audio as video_audio
from videos.audio import AudioExtractionError

from . import audio as short_audio
from .models import Short
from .r2_storage import StorageError


def fake_links(path, expires_in=None):
    return f"https://r2.example/{path}?sig=1"


def fake_download_link(path, file_name="", stored_url_value=""):
    return f"https://r2.example/{path}?download=1"


def make_mp3(source, dest):
    Path(dest).write_bytes(b"ID3 fake mp3")
    return True


def no_sound(source, dest):
    return False


class ShortsSoundTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("maker", password="pw12345678")
        self.short = Short.objects.create(
            user=user,
            title="Clip",
            file_id="demo/clip.mp4",
            video_url="/media/demo/clip.mp4",
        )

    def _video_tag(self, body):
        match = re.search(r'<video class="short-video"[^>]*>', body)
        self.assertIsNotNone(match, "no short video on the page")
        return match.group(0)

    def _speaker_icon(self, body):
        match = re.search(r'<button class="short-mute-btn"[^>]*>(.*?)</button>', body)
        self.assertIsNotNone(match, "no speaker button on the page")
        return match.group(1)

    def test_feed_video_starts_with_sound_on(self):
        body = self.client.get(reverse("shorts:list")).content.decode()
        self.assertNotIn("muted", self._video_tag(body))
        self.assertEqual(self._speaker_icon(body), "🔊")

    def test_detail_video_starts_with_sound_on(self):
        body = self.client.get(reverse("shorts:detail", args=[self.short.id])).content.decode()
        tag = self._video_tag(body)
        self.assertNotIn("muted", tag)
        self.assertNotIn("autoplay", tag)
        self.assertEqual(self._speaker_icon(body), "🔊")


class ShortsScriptTests(TestCase):
    """Run shorts.js against a fake DOM that enforces the browser autoplay rule.

    A browser only lets a video start with sound after the visitor has tapped the
    page. The harness reproduces that, so the fallback to muted playback and the
    speaker button are checked by running the script, not by grepping it.
    """

    def setUp(self):
        self.node = shutil.which("node") or shutil.which("node.exe")
        if not self.node:
            self.skipTest("node is not installed on this machine")

    def _run(self, mode):
        project = Path(__file__).resolve().parent.parent
        harness = project / "static" / "js" / "tests" / "shorts_smoke.js"
        script = project / "static" / "js" / "shorts.js"
        proc = subprocess.run(
            [self.node, str(harness), str(script), mode],
            capture_output=True, text=True, timeout=120, cwd=str(project),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_feed_starts_with_sound_and_keeps_the_visitors_choice(self):
        code, output = self._run("feed")
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT: OK", output)

    def test_detail_page_plays_and_its_speaker_button_works(self):
        code, output = self._run("detail")
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT: OK", output)

    def test_sound_sign_plays_only_the_mp3_and_downloads_work(self):
        code, output = self._run("feed")
        self.assertEqual(code, 0, output)
        self.assertIn("the sound sign plays only the MP3", output)
        self.assertIn("download goes straight to a short's MP3", output)

    def test_an_older_short_makes_its_mp3_when_it_is_first_used(self):
        code, output = self._run("older")
        self.assertEqual(code, 0, output)
        self.assertIn("downloading makes the MP3 first", output)
        self.assertIn("the sound sign plays the MP3 once it exists", output)

    def test_a_dropped_connection_is_tried_once_more(self):
        code, output = self._run("older-flaky")
        self.assertEqual(code, 0, output)
        self.assertIn("a dropped connection is tried again", output)
        self.assertIn("the download starts after the retry", output)

    def test_a_failed_mp3_is_reported_and_sound_only_mode_turns_off(self):
        code, output = self._run("older-fails")
        self.assertEqual(code, 0, output)
        self.assertIn("a failed MP3 turns sound-only mode off", output)
        self.assertIn("a failed download is reported and goes nowhere", output)


class ShortSoundControlsTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("maker", password="pw12345678")
        self.short = Short.objects.create(
            user=user,
            title="Recitation",
            file_id="videos/rec.mp4",
            video_url="https://cdn/rec.mp4",
            audio_file_id="audio/rec.mp3",
            audio_status="ready",
        )
        patches = [
            mock.patch.dict(os.environ, {"R2_PUBLIC_BASE_URL": "", "R2_SHORTS_PUBLIC_BASE_URL": ""}),
            mock.patch("shorts.r2_storage.generate_presigned_url", side_effect=fake_links),
            mock.patch("shorts.r2_storage.download_url", side_effect=fake_download_link),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_feed_offers_the_sound_and_its_download(self):
        body = self.client.get(reverse("shorts:list")).content.decode()
        self.assertIn('class="short-audio"', body)
        self.assertIn('src="https://r2.example/audio/rec.mp3?sig=1"', body)
        self.assertIn('data-audio-download="https://r2.example/audio/rec.mp3?download=1"', body)
        self.assertIn('class="short-action download-btn"', body)
        self.assertIn('class="sound-disc sound-toggle"', body)
        self.assertIn(f'data-audio-prepare-url="/shorts/{self.short.id}/audio/"', body)

    def test_detail_offers_the_sound_and_its_download(self):
        body = self.client.get(reverse("shorts:detail", args=[self.short.id])).content.decode()
        self.assertIn('class="short-audio"', body)
        self.assertIn('class="short-action download-btn"', body)
        self.assertIn('class="sound-disc sound-toggle"', body)

    def test_a_short_without_sound_offers_no_sound_controls(self):
        Short.objects.filter(pk=self.short.pk).update(audio_status="none", audio_file_id="")
        body = self.client.get(reverse("shorts:list")).content.decode()
        self.assertNotIn('class="short-audio"', body)
        self.assertNotIn("download-btn", body)
        self.assertNotIn("sound-toggle", body)

    def test_an_older_short_still_offers_the_controls(self):
        Short.objects.filter(pk=self.short.pk).update(audio_status="", audio_file_id="")
        body = self.client.get(reverse("shorts:list")).content.decode()
        self.assertIn('class="short-audio"', body)
        self.assertNotIn('src="https://r2.example/audio/', body)
        self.assertIn('class="sound-disc sound-toggle"', body)


class ShortSoundConversionTests(TestCase):
    def setUp(self):
        self.maker = User.objects.create_user("maker", password="pw12345678")
        self.admin = User.objects.create_superuser("root", "root@example.com", "pw12345678")
        self.short = Short.objects.create(
            user=self.maker,
            title="Clip",
            file_id="videos/clip.mp4",
            video_url="https://cdn/clip.mp4",
        )
        patches = [
            mock.patch.dict(os.environ, {"R2_PUBLIC_BASE_URL": "", "R2_SHORTS_PUBLIC_BASE_URL": ""}),
            mock.patch("shorts.r2_storage.generate_presigned_url", side_effect=fake_links),
            mock.patch("shorts.r2_storage.download_url", side_effect=fake_download_link),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _upload(self, title="Hello"):
        return self.client.post(
            reverse("shorts:upload_submit"),
            {
                "video_file": SimpleUploadedFile("hello.mp4", b"clip bytes", content_type="video/mp4"),
                "title": title,
            },
        )

    def test_an_uploaded_short_gets_its_sound(self):
        self.client.force_login(self.admin)
        with mock.patch("shorts.r2_storage.upload_video", return_value={"path": "videos/new.mp4", "url": ""}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=make_mp3), \
                mock.patch(
                    "shorts.r2_storage.upload_audio",
                    return_value={"path": "audio/new.mp3", "url": "https://cdn/new.mp3"},
                ) as upload_audio:
            response = self._upload()

        self.assertTrue(response.json()["success"])
        short = Short.objects.get(title="Hello")
        self.assertEqual(short.audio_status, "ready")
        self.assertEqual(short.audio_file_id, "audio/new.mp3")
        upload_audio.assert_called_once()

    def test_an_upload_without_sound_is_marked_as_silent(self):
        self.client.force_login(self.admin)
        with mock.patch("shorts.r2_storage.upload_video", return_value={"path": "videos/new.mp4", "url": ""}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=no_sound), \
                mock.patch("shorts.r2_storage.upload_audio") as upload_audio:
            response = self._upload()

        self.assertTrue(response.json()["success"])
        self.assertEqual(Short.objects.get(title="Hello").audio_status, "none")
        upload_audio.assert_not_called()

    def test_a_failed_conversion_never_fails_the_upload(self):
        self.client.force_login(self.admin)
        with mock.patch("shorts.r2_storage.upload_video", return_value={"path": "videos/new.mp4", "url": ""}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=AudioExtractionError("ffmpeg boom")):
            response = self._upload()

        self.assertTrue(response.json()["success"])
        self.assertEqual(Short.objects.get(title="Hello").audio_status, "failed")

    def test_an_older_short_gets_its_sound_the_first_time_it_is_used(self):
        with mock.patch("shorts.r2_storage.get_object", return_value={"Body": io.BytesIO(b"clip")}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=make_mp3), \
                mock.patch(
                    "shorts.r2_storage.upload_audio",
                    return_value={"path": "audio/new.mp3", "url": "https://cdn/new.mp3"},
                ):
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))

        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["url"], "https://r2.example/audio/new.mp3?sig=1")
        self.assertEqual(payload["download_url"], "https://r2.example/audio/new.mp3?download=1")
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_status, "ready")

    def test_a_short_that_already_has_its_sound_is_not_converted_again(self):
        Short.objects.filter(pk=self.short.pk).update(
            audio_status="ready", audio_file_id="audio/old.mp3", audio_url="https://cdn/old.mp3"
        )
        with mock.patch("shorts.r2_storage.get_object") as get_object, \
                mock.patch("shorts.audio.extract_mp3") as extract:
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))

        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["url"], "https://r2.example/audio/old.mp3?sig=1")
        get_object.assert_not_called()
        extract.assert_not_called()

    def test_a_short_marked_ready_without_a_file_is_made_again(self):
        Short.objects.filter(pk=self.short.pk).update(audio_status="ready", audio_file_id="")
        with mock.patch("shorts.r2_storage.get_object", return_value={"Body": io.BytesIO(b"clip")}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=make_mp3), \
                mock.patch(
                    "shorts.r2_storage.upload_audio",
                    return_value={"path": "audio/fixed.mp3", "url": ""},
                ):
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))

        self.assertTrue(response.json()["success"])
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_file_id, "audio/fixed.mp3")

    def test_a_short_without_sound_says_so(self):
        Short.objects.filter(pk=self.short.pk).update(audio_status="none")
        response = self.client.post(reverse("shorts:audio", args=[self.short.id]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "This short has no sound.")

    def test_a_failed_download_is_reported_and_retried_after_a_pause(self):
        with mock.patch("shorts.r2_storage.get_object", side_effect=StorageError("R2 is down")):
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))
        self.assertEqual(response.status_code, 502)
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_status, "failed")

        with mock.patch("shorts.r2_storage.get_object") as get_object:
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))
        self.assertEqual(response.status_code, 502)
        get_object.assert_not_called()

        Short.objects.filter(pk=self.short.pk).update(
            audio_checked_at=timezone.now() - short_audio.RETRY_AFTER - timedelta(seconds=1)
        )
        with mock.patch("shorts.r2_storage.get_object", return_value={"Body": io.BytesIO(b"clip")}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=make_mp3), \
                mock.patch(
                    "shorts.r2_storage.upload_audio",
                    return_value={"path": "audio/later.mp3", "url": ""},
                ):
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))
        self.assertTrue(response.json()["success"])
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_status, "ready")

    def test_the_first_sound_to_finish_is_kept_when_two_conversions_race(self):
        def other_request_finishes_first(source, dest):
            Short.objects.filter(pk=self.short.pk).update(
                audio_status="ready", audio_file_id="audio/other.mp3", audio_url="https://cdn/other.mp3"
            )
            return make_mp3(source, dest)

        with mock.patch("shorts.r2_storage.get_object", return_value={"Body": io.BytesIO(b"clip")}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=other_request_finishes_first), \
                mock.patch(
                    "shorts.r2_storage.upload_audio",
                    return_value={"path": "audio/new.mp3", "url": ""},
                ), \
                mock.patch("shorts.r2_storage.delete_file") as delete_file:
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))

        self.assertTrue(response.json()["success"])
        delete_file.assert_called_once_with("audio/new.mp3")
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_file_id, "audio/other.mp3")

    def test_a_failure_after_another_request_made_the_sound_still_succeeds(self):
        def other_finishes_then_this_one_fails(source, dest):
            Short.objects.filter(pk=self.short.pk).update(
                audio_status="ready", audio_file_id="audio/other.mp3", audio_url="https://cdn/other.mp3"
            )
            raise AudioExtractionError("ffmpeg boom")

        with mock.patch("shorts.r2_storage.get_object", return_value={"Body": io.BytesIO(b"clip")}), \
                mock.patch("shorts.audio.extract_mp3", side_effect=other_finishes_then_this_one_fails):
            response = self.client.post(reverse("shorts:audio", args=[self.short.id]))

        self.assertTrue(response.json()["success"])
        self.short.refresh_from_db()
        self.assertEqual(self.short.audio_file_id, "audio/other.mp3")

    def test_removing_a_short_removes_its_sound_file(self):
        Short.objects.filter(pk=self.short.pk).update(audio_file_id="audio/x.mp3", audio_status="ready")
        self.short.refresh_from_db()
        with mock.patch("shorts.r2_storage.delete_file") as delete_file:
            self.short.delete_stored_files()
        self.assertIn(mock.call("audio/x.mp3"), delete_file.call_args_list)

    def _real_clip(self, with_sound):
        try:
            ffmpeg = video_audio._ffmpeg_exe()
        except Exception:  # noqa: BLE001 - no ffmpeg on this machine
            self.skipTest("ffmpeg is not available")
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        clip = str(Path(directory) / "clip.mp4")
        command = [ffmpeg, "-y"]
        if with_sound:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        command += ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1"]
        if with_sound:
            command += ["-shortest", "-c:a", "aac"]
        command += ["-c:v", "mpeg4", clip]
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode != 0:
            self.skipTest("ffmpeg could not build the test clip")
        return clip

    def test_a_real_clip_with_sound_gives_a_real_mp3(self):
        clip = self._real_clip(with_sound=True)
        stored = {}

        def keep(file_data, file_name):
            stored["bytes"] = file_data.read()
            return {"path": "audio/real.mp3", "url": ""}

        with mock.patch("shorts.r2_storage.upload_audio", side_effect=keep):
            status = short_audio.store_sound(self.short, clip)

        self.assertEqual(status, "ready")
        self.assertTrue(stored["bytes"].startswith((b"ID3", b"\xff")))

    def test_a_real_silent_clip_is_marked_as_silent(self):
        clip = self._real_clip(with_sound=False)
        with mock.patch("shorts.r2_storage.upload_audio") as upload_audio:
            status = short_audio.store_sound(self.short, clip)
        self.assertEqual(status, "none")
        upload_audio.assert_not_called()
