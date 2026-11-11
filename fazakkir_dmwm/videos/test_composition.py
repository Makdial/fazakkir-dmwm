import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .composition import (
    JOB_TTL_SECONDS,
    CompositionError,
    ffmpeg_exe,
    job_dir,
    render,
    sweep_jobs,
    write_job,
)
from .composition_views import build_spec
from .models import Video


def _ffmpeg(*args):
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def _make_av(path, seconds):
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", path,
    )


def _make_silent(path, seconds):
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", path,
    )


def _make_tone(path, seconds):
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100",
        "-t", str(seconds), "-c:a", "aac", path,
    )


def _duration(path):
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class BuildSpecTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("composer", password="pw12345678")
        self.video = Video.objects.create(
            user=self.user,
            title="Talk",
            file_id="videos/talk.mp4",
            video_url="https://example.com/talk.mp4",
            duration_seconds=30.0,
        )
        self.song = Video.objects.create(
            user=self.user,
            title="Song",
            media_type="audio",
            audio_file_id="audio/song.mp3",
            audio_url="https://example.com/song.mp3",
            audio_status="ready",
            duration_seconds=12.0,
        )

    def test_builds_a_clean_timeline(self):
        spec = build_spec({
            "canvas": "9:16",
            "title": "Mix",
            "video": [{"video_id": self.video.id, "in": 2, "out": 10, "volume": 0.5}],
            "audio": [{"video_id": self.song.id, "in": 0, "out": 5, "offset": 3, "volume": 2}],
        })
        self.assertEqual(spec["kind"], "video")
        self.assertEqual(spec["canvas"], "9:16")
        self.assertEqual(spec["video"][0]["start"], 2.0)
        self.assertEqual(spec["video"][0]["end"], 10.0)
        self.assertEqual(spec["video"][0]["volume"], 0.5)
        self.assertEqual(spec["audio"][0]["offset"], 3.0)
        self.assertEqual(spec["audio"][0]["volume"], 1.0)

    def test_out_point_is_clamped_to_the_duration(self):
        spec = build_spec({"video": [{"video_id": self.video.id, "in": 0, "out": 99}]})
        self.assertEqual(spec["video"][0]["end"], 30.0)

    def test_audio_only_timeline_is_its_own_kind(self):
        spec = build_spec({"audio": [{"video_id": self.song.id, "in": 0, "out": 4}]})
        self.assertEqual(spec["kind"], "audio")
        self.assertEqual(spec["video"], [])

    def test_bad_timelines_are_rejected_with_a_code(self):
        cases = {
            "invalid": [],
            "empty": {"video": [], "audio": []},
            "missing": {"video": [{"video_id": 999999, "in": 0, "out": 5}]},
            "no_picture": {"video": [{"video_id": self.song.id, "in": 0, "out": 5}]},
            "no_audio": {"audio": [{"video_id": self.video.id, "in": 0, "out": 5}]},
            "short_clip": {"video": [{"video_id": self.video.id, "in": 4, "out": 4.1}]},
            "too_long": {"video": [{"video_id": self.video.id, "in": 0, "out": 30}] * 21},
            "too_many": {"video": [{"video_id": self.video.id, "in": 0, "out": 1}] * 31},
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(CompositionError) as raised:
                    build_spec(payload)
                self.assertEqual(raised.exception.code, code)


class CompositionViewTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch("videos.composition.jobs_root", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.job = uuid.uuid4()
        os.makedirs(job_dir(self.job))
        with open(os.path.join(job_dir(self.job), "output.mp4"), "wb") as handle:
            handle.write(b"0123456789" * 10)
        write_job(
            self.job,
            status="ready",
            stage="done",
            percent=100,
            file="output.mp4",
            kind="video",
            title="My film",
        )

    def test_page_renders_the_editor(self):
        response = self.client.get(reverse("videos:composition"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="comp-config"')
        self.assertContains(response, "js/composition.js")

    def test_start_rejects_a_broken_request(self):
        response = self.client.post(
            reverse("videos:composition_start"),
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid")

    def test_start_returns_a_job_id(self):
        user = User.objects.create_user("viewer", password="pw12345678")
        clip = Video.objects.create(
            user=user,
            title="Clip",
            file_id="videos/clip.mp4",
            video_url="https://example.com/clip.mp4",
            duration_seconds=20.0,
        )
        payload = {"video": [{"video_id": clip.id, "in": 0, "out": 5}]}
        with mock.patch("videos.composition_views.start_job", return_value=self.job) as start:
            response = self.client.post(
                reverse("videos:composition_start"),
                data=payload,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "job_id": str(self.job)})
        self.assertEqual(start.call_args[0][0]["video"][0]["end"], 5.0)

    def test_status_of_a_ready_job(self):
        response = self.client.get(reverse("videos:composition_status", args=[self.job]))
        data = response.json()
        self.assertEqual(data["status"], "ready")
        self.assertIn("preview_url", data)
        self.assertNotIn("file", data)

    def test_library_cards_show_uploader_and_views(self):
        user = User.objects.create_user("composer", password="pw12345678")
        Video.objects.create(
            user=user,
            title="Talk",
            file_id="videos/talk.mp4",
            video_url="https://example.com/talk.mp4",
            duration_seconds=30.0,
            views=7,
        )
        response = self.client.get(reverse("videos:composition"))
        self.assertContains(response, '"uploader": "composer"')
        self.assertContains(response, '"views": 7')

    def test_unknown_job_is_not_found(self):
        url = reverse("videos:composition_status", args=[uuid.uuid4()])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get("/composition/status/not-a-uuid/").status_code, 404)

    def test_inline_download_serves_the_file(self):
        url = reverse("videos:composition_download", args=[self.job]) + "?inline=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(response.streaming_content), b"0123456789" * 10)

    def test_range_requests_are_honoured(self):
        url = reverse("videos:composition_download", args=[self.job])
        response = self.client.get(url, HTTP_RANGE="bytes=0-3")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 0-3/100")
        self.assertEqual(b"".join(response.streaming_content), b"0123")

    def test_download_is_named_after_the_title(self):
        url = reverse("videos:composition_download", args=[self.job])
        response = self.client.get(url)
        self.assertIn('filename="My-film.mp4"', response["Content-Disposition"])

    def test_the_file_survives_a_preview_and_then_a_discard(self):
        url = reverse("videos:composition_download", args=[self.job]) + "?inline=1"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        discard = reverse("videos:composition_discard", args=[self.job])
        self.assertEqual(self.client.post(discard).status_code, 200)
        self.assertFalse(os.path.exists(job_dir(self.job)))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_old_jobs_are_swept(self):
        fresh = uuid.uuid4()
        os.makedirs(job_dir(fresh))
        write_job(fresh, status="ready", file="output.mp4")
        stale = uuid.uuid4()
        os.makedirs(job_dir(stale))
        write_job(stale, status="ready", file="output.mp4")
        state_path = os.path.join(job_dir(stale), "job.json")
        old = time.time() - JOB_TTL_SECONDS - 60
        os.utime(state_path, (old, old))
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["updated"] = old
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)

        sweep_jobs()
        self.assertFalse(os.path.exists(job_dir(stale)))
        self.assertTrue(os.path.exists(job_dir(fresh)))


class CompositionRenderTests(TestCase):
    def test_renders_a_video_timeline_with_audio_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = {
                "video:1": os.path.join(tmp, "a.mp4"),
                "video:2": os.path.join(tmp, "silent.mp4"),
                "audio:3": os.path.join(tmp, "tone.m4a"),
            }
            _make_av(sources["video:1"], 1.5)
            _make_silent(sources["video:2"], 1.0)
            _make_tone(sources["audio:3"], 2.0)
            spec = {
                "kind": "video",
                "canvas": "9:16",
                "title": "",
                "video": [
                    {"key": "video:1", "url": "video:1", "start": 0.0, "end": 1.0, "volume": 1.0},
                    {"key": "video:2", "url": "video:2", "start": 0.0, "end": 0.6, "volume": 0.5},
                ],
                "audio": [
                    {"key": "audio:3", "url": "audio:3", "start": 0.0, "end": 1.2,
                     "offset": 0.5, "volume": 1.0},
                ],
            }
            workdir = os.path.join(tmp, "work")
            os.makedirs(workdir)
            output = render(spec, workdir, fetch=lambda url, dest: shutil.copyfile(sources[url], dest))
            self.assertTrue(output.endswith("final.mp4"))
            self.assertAlmostEqual(_duration(output), 1.6, delta=0.3)

    def test_renders_an_audio_only_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "tone.m4a")
            _make_tone(source, 2.0)
            spec = {
                "kind": "audio",
                "canvas": "16:9",
                "title": "",
                "video": [],
                "audio": [
                    {"key": "audio:1", "url": "audio:1", "start": 0.0, "end": 2.0,
                     "offset": 1.0, "volume": 1.0},
                ],
            }
            workdir = os.path.join(tmp, "work")
            os.makedirs(workdir)
            output = render(spec, workdir, fetch=lambda url, dest: shutil.copyfile(source, dest))
            self.assertTrue(output.endswith("final.m4a"))
            self.assertAlmostEqual(_duration(output), 3.0, delta=0.3)

    def test_a_failed_source_is_reported_as_a_code(self):
        def broken(url, dest):
            raise OSError("no network")

        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "kind": "video",
                "canvas": "16:9",
                "title": "",
                "video": [{"key": "video:1", "url": "x", "start": 0.0, "end": 1.0, "volume": 1.0}],
                "audio": [],
            }
            with self.assertRaises(CompositionError) as raised:
                render(spec, tmp, fetch=broken)
            self.assertEqual(raised.exception.code, "source_failed")
