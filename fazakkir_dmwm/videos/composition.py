"""Render a Composition timeline into one file with ffmpeg.

A render runs in a background thread. Its state is kept in a folder under the
system temp directory rather than in memory, so every gunicorn worker can answer
the status and download requests. The result is never written to the database
or to Cloudflare R2. It stays in that folder until the visitor discards it or
JOB_TTL_SECONDS have passed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid

logger = logging.getLogger(__name__)

CANVASES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960)}
MAX_VIDEO_CLIPS = 30
MAX_AUDIO_CLIPS = 20
MAX_TIMELINE_SECONDS = 600.0
MIN_CLIP_SECONDS = 0.2
JOB_TTL_SECONDS = 30 * 60
STALE_SECONDS = 15 * 60
FETCH_TIMEOUT_SECONDS = 120
FFMPEG_TIMEOUT_SECONDS = 900
FPS = 30
SAMPLE_RATE = 44100
AUDIO_BITRATE = "192k"

_RENDER_SLOT = threading.BoundedSemaphore(1)
_HAS_AUDIO = re.compile(r"Stream #\d+:\d+[^\n]*: Audio:")


class CompositionError(Exception):
    """A render that cannot finish. ``code`` is translated by the page."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


# --- Job storage --------------------------------------------------------------


def jobs_root() -> str:
    return os.path.join(tempfile.gettempdir(), "fazakkir-compositions")


def job_dir(job_id: uuid.UUID) -> str:
    return os.path.join(jobs_root(), job_id.hex)


def _read_state(folder: str) -> dict | None:
    path = os.path.join(folder, "job.json")
    for _attempt in range(3):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)  # the render thread is replacing the file right now
    return None


def read_job(job_id: uuid.UUID) -> dict | None:
    return _read_state(job_dir(job_id))


def write_job(job_id: uuid.UUID, **fields) -> None:
    folder = job_dir(job_id)
    if not os.path.isdir(folder):
        return  # discarded while it was rendering
    state = _read_state(folder) or {}
    state.update(fields, updated=time.time())
    path = os.path.join(folder, "job.json")
    for _attempt in range(3):
        try:
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(temp, path)
            return
        except OSError:
            time.sleep(0.05)
    logger.warning("Could not save the state of composition %s", job_id)


def job_status(job_id: uuid.UUID) -> dict | None:
    state = read_job(job_id)
    if state is None:
        return None
    running = state.get("status") in ("queued", "processing")
    if running and time.time() - state.get("updated", 0) > STALE_SECONDS:
        return dict(state, status="failed", error="interrupted")
    return state


def output_path(job_id: uuid.UUID) -> str | None:
    state = job_status(job_id)
    if not state or state.get("status") != "ready":
        return None
    name = state.get("file") or ""
    if not name or os.path.basename(name) != name:
        return None
    path = os.path.join(job_dir(job_id), name)
    return path if os.path.isfile(path) else None


def discard_job(job_id: uuid.UUID) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def sweep_jobs() -> None:
    root = jobs_root()
    if not os.path.isdir(root):
        return
    now = time.time()
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        state = _read_state(folder) or {}
        try:
            last = state.get("updated") or os.path.getmtime(folder)
        except OSError:
            continue
        if now - last > JOB_TTL_SECONDS:
            shutil.rmtree(folder, ignore_errors=True)


def start_job(spec: dict) -> uuid.UUID:
    sweep_jobs()
    job_id = uuid.uuid4()
    os.makedirs(job_dir(job_id), exist_ok=True)
    write_job(
        job_id,
        status="queued",
        stage="queued",
        percent=0,
        error="",
        kind=spec["kind"],
        title=spec.get("title", ""),
    )
    threading.Thread(target=_run_job, args=(job_id, spec), daemon=True).start()
    return job_id


def _run_job(job_id: uuid.UUID, spec: dict) -> None:
    folder = job_dir(job_id)
    workdir = os.path.join(folder, "work")
    with _RENDER_SLOT:
        try:
            os.makedirs(workdir, exist_ok=True)
            write_job(job_id, status="processing", stage="download", percent=5)

            def progress(stage: str, percent: int) -> None:
                write_job(job_id, stage=stage, percent=percent)

            output = render(spec, workdir, progress)
            name = "output." + ("m4a" if spec["kind"] == "audio" else "mp4")
            os.replace(output, os.path.join(folder, name))
            write_job(job_id, status="ready", stage="done", percent=100, file=name)
        except CompositionError as exc:
            write_job(job_id, status="failed", error=exc.code)
        except Exception:
            logger.exception("Composition %s failed", job_id)
            write_job(job_id, status="failed", error="failed")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# --- Rendering ----------------------------------------------------------------


def render(spec: dict, workdir: str, progress=None, fetch=None) -> str:
    """Render ``spec`` inside ``workdir`` and return the path of the finished file.

    ``spec`` is the cleaned timeline built by ``composition_views.build_spec``:
    each clip has a ``key`` (its source), a ``url`` to download it from, and
    times in seconds. ``fetch(url, path)`` can be replaced in tests.
    """
    report = progress or (lambda stage, percent: None)
    fetch_file = fetch or _download
    ffmpeg = ffmpeg_exe()
    sources: dict[str, str] = {}

    def source(key: str, url: str) -> str:
        if key not in sources:
            path = os.path.join(workdir, f"src{len(sources):03d}")
            try:
                fetch_file(url, path)
            except Exception as exc:
                raise CompositionError("source_failed") from exc
            sources[key] = path
        return sources[key]

    if spec["kind"] == "audio":
        return _render_audio(spec, ffmpeg, source, report, workdir)
    return _render_video(spec, ffmpeg, source, report, workdir)


def _render_video(spec, ffmpeg, source, report, workdir) -> str:
    width, height = CANVASES[spec["canvas"]]
    clips = spec["video"]
    segments = []
    for index, clip in enumerate(clips):
        src = source(clip["key"], clip["url"])
        seg = os.path.join(workdir, f"seg{index:03d}.mp4")
        _encode_segment(
            ffmpeg,
            src,
            seg,
            clip["start"],
            clip["end"] - clip["start"],
            clip["volume"],
            width,
            height,
        )
        segments.append(seg)
        report("encode", 10 + int(60 * (index + 1) / len(clips)))

    joined = os.path.join(workdir, "joined.mp4")
    _concat(ffmpeg, segments, os.path.join(workdir, "segments.txt"), joined)
    report("join", 72)

    final = os.path.join(workdir, "final.mp4")
    if not spec["audio"]:
        _run(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", joined, "-c", "copy", "-movflags", "+faststart", final,
            ]
        )
    else:
        report("mix", 80)
        _mix(
            ffmpeg,
            ["-i", joined],
            spec["audio"],
            source,
            final,
            video_map=["-map", "0:v:0"],
            out_args=[
                "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                "-ar", str(SAMPLE_RATE), "-ac", "2", "-movflags", "+faststart",
            ],
        )
    report("finish", 95)
    return final


def _render_audio(spec, ffmpeg, source, report, workdir) -> str:
    total = max(clip["offset"] + clip["end"] - clip["start"] for clip in spec["audio"])
    final = os.path.join(workdir, "final.m4a")
    report("mix", 20)
    _mix(
        ffmpeg,
        ["-f", "lavfi", "-t", f"{total:.3f}",
         "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"],
        spec["audio"],
        source,
        final,
        video_map=[],
        out_args=[
            "-vn", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-ar", str(SAMPLE_RATE), "-ac", "2",
        ],
    )
    report("finish", 95)
    return final


def _encode_segment(ffmpeg, src, dest, start, length, volume, width, height) -> None:
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={FPS},format=yuv420p"
    )
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
    ]
    if _has_audio(ffmpeg, src):
        audio_filter = f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo"
        if abs(volume - 1.0) > 0.001:
            audio_filter += f",volume={volume:.3f}"
        command += ["-map", "0:v:0", "-map", "0:a:0", "-af", audio_filter]
    else:
        # A silent source still needs an audio track so the clips can be joined.
        command += [
            "-f", "lavfi", "-t", f"{length:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        ]
    command += [
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", str(SAMPLE_RATE), "-ac", "2",
        dest,
    ]
    _run(command)


def _mix(ffmpeg, base, audio, source, dest, video_map, out_args) -> None:
    """Lay the audio clips over the base input's sound and write ``dest``."""
    inputs = list(base)
    filters = [f"[0:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo[a0]"]
    labels = ["[a0]"]
    for index, clip in enumerate(audio, start=1):
        path = source(clip["key"], clip["url"])
        length = clip["end"] - clip["start"]
        inputs += ["-ss", f"{clip['start']:.3f}", "-t", f"{length:.3f}", "-i", path]
        delay = int(round(clip["offset"] * 1000))
        filters.append(
            f"[{index}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
            f"adelay={delay}:all=1,volume={clip['volume']:.3f}[a{index}]"
        )
        labels.append(f"[a{index}]")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(filters),
            *video_map, "-map", "[aout]", *out_args, dest,
        ]
    )


def _concat(ffmpeg, segments, list_path, dest) -> None:
    with open(list_path, "w", encoding="utf-8") as handle:
        for seg in segments:
            safe = os.path.abspath(seg).replace("\\", "/").replace("'", "'\\''")
            handle.write(f"file '{safe}'\n")
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", dest,
        ]
    )


def _has_audio(ffmpeg: str, path: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompositionError("timeout") from exc
    return bool(_HAS_AUDIO.search(result.stderr or ""))


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CompositionError("timeout") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"")[-400:].decode("utf-8", "replace")
        logger.warning("ffmpeg failed: %s", tail)
        raise CompositionError("clip_failed") from exc


def _download(url: str, dest: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Fazakkir-Composition/1"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        with open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle, 1024 * 1024)
