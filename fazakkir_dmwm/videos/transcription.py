"""Automatic subtitles and lyrics.

Turns a video's audio into timed subtitle tracks without anyone uploading a
file: the audio is transcribed in the spoken language and each cue is then
translated into the configured target languages.

The back end is pluggable.  A provider only has to implement two small methods
(:meth:`SubtitleProvider.transcribe` and :meth:`SubtitleProvider.translate`), so
a local/offline engine can be added later without touching anything else.

Pipeline per video::

    audio  ->  16 kHz mono chunks  ->  transcribe  ->  translate  ->  WebVTT

Long videos are split into chunks because speech-to-text APIs cap the size of a
single upload, and the per-chunk timestamps are shifted back so the cues line up
with the start of the full track.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import r2_storage
from .models import SubtitleTrack, Video
from .subtitles import label_for

logger = logging.getLogger(__name__)

# Roughly 24 kB/s at 16 kHz mono, so ~15 MB per 10 minute chunk: comfortably
# below the 25 MB limit of the speech-to-text API while keeping cues long
# enough to stay accurate.
CHUNK_SECONDS = 480
CHUNK_SAMPLE_RATE = 16000
CHUNK_BITRATE = "32k"
TRANSLATE_BATCH = 40


class TranscriptionError(Exception):
    """Raised when the pipeline cannot produce subtitles."""


@dataclass
class Segment:
    """One timed line of dialogue."""

    start: float
    end: float
    text: str


# --- WebVTT rendering -------------------------------------------------------


def format_timestamp(seconds: float) -> str:
    """Format seconds as a WebVTT timestamp (``HH:MM:SS.mmm``)."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{int(round((seconds - int(seconds)) * 1000)):03d}"


def build_vtt(segments: list[Segment]) -> str:
    """Render transcribed/translated segments as a WebVTT document."""
    lines = ["WEBVTT", ""]
    for segment in segments:
        if not segment.text.strip():
            continue
        # WebVTT requires the whole timing on a single line.
        lines.append(
            f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}"
        )
        lines.append(segment.text.strip())
        lines.append("")
    return "\n".join(lines)


# --- Audio chunking ---------------------------------------------------------


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def chunk_audio(source_path: str, workdir: str) -> list[tuple[str, float]]:
    """Split ``source_path`` into API-sized pieces.

    Returns ``[(path, offset_seconds), ...]`` so the caller can shift the
    per-chunk timestamps back onto the full timeline.
    """
    import subprocess

    chunks: list[tuple[str, float]] = []
    for index in range(0, 1024):  # generous ceiling: ~85 hours of audio
        start = index * CHUNK_SECONDS
        path = os.path.join(workdir, f"chunk-{index:04d}.mp3")
        command = [
            _ffmpeg_exe(),
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(CHUNK_SECONDS),
            "-i",
            source_path,
            "-ac",
            "1",
            "-ar",
            str(CHUNK_SAMPLE_RATE),
            "-b:a",
            CHUNK_BITRATE,
            path,
        ]
        try:
            proc = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            raise TranscriptionError(f"Could not run ffmpeg: {exc}") from exc

        # ffmpeg exits non-zero once it runs past the end of the input, which
        # simply means "no more audio".
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            if os.path.exists(path):
                os.remove(path)
            break

        chunks.append((path, float(start)))

    if not chunks:
        raise TranscriptionError("No audio could be read from the video.")
    return chunks


def download_audio(video: Video, destination: str) -> str:
    """Download a video's MP3 from R2 to a local file."""
    if not video.has_audio:
        raise TranscriptionError(
            "This video has no audio track yet, so subtitles cannot be generated."
        )
    try:
        obj = r2_storage.get_object(video.audio_file_id)
    except r2_storage.StorageError as exc:
        raise TranscriptionError(f"Could not read the audio from storage: {exc}") from exc

    written = 0
    with open(destination, "wb") as handle:
        body = obj.get("Body")
        if body is None:
            raise TranscriptionError("Storage returned no audio data.")
        try:
            for piece in body.iter_chunks(chunk_size=1024 * 1024):
                handle.write(piece)
                written += len(piece)
        finally:
            body.close()

    if written == 0:
        raise TranscriptionError("The stored audio file was empty.")
    return destination


# --- Providers --------------------------------------------------------------


class SubtitleProvider:
    """Interface every speech-to-text / translation back end implements."""

    name = "base"

    def transcribe(self, path: str, language: str) -> list[Segment]:
        raise NotImplementedError

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        raise NotImplementedError


class OpenAIProvider(SubtitleProvider):
    """OpenAI: ``whisper-1`` for speech-to-text, a chat model for translation.

    ``whisper-1`` is used rather than ``gpt-4o-transcribe`` because only the
    former returns the per-segment timestamps subtitles need.
    """

    name = "openai"

    def __init__(self):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise TranscriptionError(
                "The 'openai' package is not installed. "
                "Run: pip install openai"
            ) from exc

        api_key = getattr(settings, "OPENAI_API_KEY", "")
        if not api_key:
            raise TranscriptionError(
                "OPENAI_API_KEY is not set, so subtitles cannot be generated."
            )
        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key)

    def transcribe(self, path: str, language: str) -> list[Segment]:
        model = getattr(settings, "SUBTITLE_TRANSCRIBE_MODEL", "whisper-1")
        with open(path, "rb") as handle:
            try:
                result = self._client.audio.transcriptions.create(
                    model=model,
                    file=handle,
                    language=language,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                raise TranscriptionError(f"Transcription failed: {exc}") from exc

        segments = [
            Segment(
                start=float(item.start),
                end=float(item.end),
                text=(item.text or "").strip(),
            )
            for item in getattr(result, "segments", None) or []
        ]
        if not segments and (getattr(result, "text", "") or "").strip():
            # No timestamps available: fall back to one full-length cue so the
            # translation is still useful instead of losing it entirely.
            segments = [Segment(0.0, CHUNK_SECONDS, result.text.strip())]
        return [segment for segment in segments if segment.text]

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        model = getattr(settings, "SUBTITLE_TRANSLATE_MODEL", "gpt-4o-mini")
        translated: list[Segment] = []

        for start in range(0, len(segments), TRANSLATE_BATCH):
            batch = segments[start : start + TRANSLATE_BATCH]
            texts = self._translate_batch(batch, target_language, model)
            for segment, text in zip(batch, texts):
                if text:
                    translated.append(Segment(segment.start, segment.end, text))

        if len(translated) != len(segments):
            raise TranscriptionError(
                "The translation returned a different number of lines than expected."
            )
        return translated

    def _translate_batch(
        self, batch: list[Segment], target_language: str, model: str
    ) -> list[str]:
        numbered = "\n".join(
            f"{index}: {segment.text}" for index, segment in enumerate(batch)
        )
        prompt = (
            "Translate the following subtitle lines into "
            f"{target_language}. Keep the numbering exactly as it is and return "
            "only the translated lines, one per line, with no numbering, no extra "
            "commentary and no empty lines. Preserve the meaning, punctuation and "
            "line breaks within each numbered line.\n\n" + numbered
        )
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - SDK raises many types
            raise TranscriptionError(f"Translation failed: {exc}") from exc

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        # Tolerate a model that echoes the numbering back.
        cleaned: list[str] = []
        for index, line in enumerate(lines):
            prefix = f"{index}:"
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
            cleaned.append(line)

        if len(cleaned) != len(batch):
            raise TranscriptionError(
                f"Expected {len(batch)} translated lines but got {len(cleaned)}."
            )
        return cleaned


def get_provider() -> SubtitleProvider:
    """Build the provider named by ``SUBTITLE_PROVIDER``."""
    name = (getattr(settings, "SUBTITLE_PROVIDER", "openai") or "openai").strip().lower()
    if name == "openai":
        return OpenAIProvider()
    raise TranscriptionError(f"Unknown subtitle provider: {name!r}")


def is_configured() -> bool:
    """True when the configured provider has everything it needs to run."""
    name = (getattr(settings, "SUBTITLE_PROVIDER", "openai") or "openai").strip().lower()
    if name == "openai":
        return bool(getattr(settings, "OPENAI_API_KEY", ""))
    return False


# --- Orchestration ----------------------------------------------------------


def generate_subtitles(video_id: int, include_lyrics: bool = True) -> dict:
    """Transcribe and translate a video, saving the result as subtitle tracks.

    Returns a summary dict with the languages that were created. Raises
    :class:`TranscriptionError` on any failure.
    """
    video = Video.objects.filter(id=video_id).first()
    if video is None:
        raise TranscriptionError("This video no longer exists.")

    source_language = getattr(settings, "SUBTITLE_SOURCE_LANGUAGE", "ar")
    targets = [
        code.strip()
        for code in getattr(settings, "SUBTITLE_TARGET_LANGUAGES", "fr,en").split(",")
        if code.strip()
    ]

    provider = get_provider()
    workdir = tempfile.mkdtemp(prefix="fazakkir-subs-")
    try:
        audio_path = download_audio(video, os.path.join(workdir, "audio.mp3"))
        chunks = chunk_audio(audio_path, workdir)

        segments: list[Segment] = []
        for chunk_path, offset in chunks:
            for segment in provider.transcribe(chunk_path, source_language):
                segments.append(
                    Segment(
                        start=segment.start + offset,
                        end=segment.end + offset,
                        text=segment.text,
                    )
                )

        if not segments:
            raise TranscriptionError("No speech was detected in this video.")

        created: list[str] = []
        if source_language:
            _save_track(video, source_language, segments)
            created.append(source_language)

        for target in targets:
            if target == source_language:
                continue
            translated = provider.translate(segments, target)
            _save_track(video, target, translated)
            created.append(target)

        if include_lyrics and not _has_lyrics(video):
            # Lyrics are a single full-length cue holding the transcription,
            # which is how they behave when uploaded by hand.
            _save_lyrics(video, segments)

        return {"languages": created, "cues": len(segments)}
    finally:
        _remove_tree(workdir)


def _save_track(video: Video, language_code: str, segments: list[Segment]) -> None:
    SubtitleTrack.objects.update_or_create(
        video=video,
        language_code=language_code,
        defaults={
            "label": label_for(language_code),
            "content": build_vtt(segments),
            "is_lyrics": False,
        },
    )


def _save_lyrics(video: Video, segments: list[Segment]) -> None:
    SubtitleTrack.objects.update_or_create(
        video=video,
        language_code="lyrics",
        defaults={
            "label": label_for("lyrics"),
            "content": build_vtt(segments),
            "is_lyrics": True,
        },
    )


def _has_lyrics(video: Video) -> bool:
    return video.subtitle_tracks.filter(language_code="lyrics").exists()


def _remove_tree(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --- Background scheduling --------------------------------------------------

# Progress of a bulk subtitle run started from the dashboard. The database
# column is still the source of truth; this only drives the progress bar.
_SWEEP_STATE = {
    "running": False,
    "total": 0,
    "done": 0,
    "succeeded": 0,
    "failed": 0,
}
_SWEEP_LOCK = threading.Lock()


def eligible_subtitle_queryset():
    """Videos that can get subtitles: they need an MP3 to transcribe."""
    return Video.objects.exclude(audio_file_id="").exclude(
        auto_subtitle_status="processing"
    )


def audio_sweep_status() -> dict:
    """Return a snapshot of the bulk subtitle progress."""
    with _SWEEP_LOCK:
        return dict(_SWEEP_STATE)


def _run_for_video(video_id: int) -> None:
    close_old_connections()
    try:
        generate_and_record(video_id)
    finally:
        close_old_connections()


def schedule_subtitle_generation(video_id: int, retry: bool = False) -> bool:
    """Start generating a video's subtitles in a background thread.

    ``auto_subtitle_status`` doubles as a lock, so a video is never processed by
    two workers at once. With ``retry=False`` only untouched videos are queued.
    """
    queryset = Video.objects.filter(id=video_id).exclude(audio_file_id="").exclude(
        auto_subtitle_status="processing"
    )
    if not retry:
        queryset = queryset.filter(auto_subtitle_status="")

    if not queryset.update(
        auto_subtitle_status="processing", job_started_at=timezone.now()
    ):
        return False

    threading.Thread(target=_run_for_video, args=(video_id,), daemon=True).start()
    return True


def _sweep_videos() -> None:
    """Generate subtitles for every eligible video, one at a time."""
    video_ids = list(eligible_subtitle_queryset().order_by("id").values_list("id", flat=True))
    with _SWEEP_LOCK:
        _SWEEP_STATE.update(
            running=True, total=len(video_ids), done=0, succeeded=0, failed=0
        )

    for video_id in video_ids:
        if not Video.objects.filter(id=video_id).exclude(audio_file_id="").exclude(
            auto_subtitle_status="processing"
        ).update(auto_subtitle_status="processing"):
            continue
        succeeded = generate_and_record(video_id)
        with _SWEEP_LOCK:
            _SWEEP_STATE["done"] += 1
            _SWEEP_STATE["succeeded" if succeeded else "failed"] += 1


def generate_and_record(video_id: int) -> bool:
    """Run the pipeline for one video and record the outcome. Returns success.

    Never raises: any failure is written to ``auto_subtitle_status`` /
    ``auto_subtitle_error`` so background workers and bulk runs stay alive.
    """
    try:
        generate_subtitles(video_id)
    except TranscriptionError as exc:
        logger.warning("Subtitle generation failed for video %s: %s", video_id, exc)
        Video.objects.filter(id=video_id).update(
            auto_subtitle_status="failed",
            auto_subtitle_error=str(exc)[:500],
            job_started_at=None,
        )
        return False
    except Exception:  # noqa: BLE001 - keep the sweep going
        logger.exception("Unexpected subtitle failure for video %s", video_id)
        Video.objects.filter(id=video_id).update(
            auto_subtitle_status="failed",
            auto_subtitle_error="Unexpected error while generating subtitles.",
            job_started_at=None,
        )
        return False
    Video.objects.filter(id=video_id).update(
        auto_subtitle_status="ready", auto_subtitle_error="", job_started_at=None
    )
    return True


def _run_sweep() -> None:
    close_old_connections()
    try:
        _sweep_videos()
    finally:
        with _SWEEP_LOCK:
            _SWEEP_STATE["running"] = False
        close_old_connections()


def start_subtitle_sweep() -> bool:
    """Kick off a background subtitle run over every eligible video.

    Returns ``False`` when a run is already in progress.
    """
    with _SWEEP_LOCK:
        if _SWEEP_STATE["running"]:
            return False
        _SWEEP_STATE["running"] = True

    threading.Thread(target=_run_sweep, daemon=True).start()
    return True