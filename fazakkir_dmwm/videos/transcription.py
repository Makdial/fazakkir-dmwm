"""Automatic subtitles and lyrics.

Turns a video's audio into timed subtitle tracks without anyone uploading a
file: the audio is transcribed in the spoken language and each cue is then
translated into the configured target languages.

The back end is pluggable.  A provider only has to implement two small methods
(:meth:`SubtitleProvider.transcribe` and :meth:`SubtitleProvider.translate`), so
a local/offline engine can be added later without touching anything else.

Two providers ship today, chosen with ``SUBTITLE_PROVIDER``: :class:`OpenAIProvider`
(``openai``) and :class:`GoogleProvider` (``google``, Google Cloud Speech-to-Text
plus Cloud Translation).

Pipeline per video::

    audio  ->  16 kHz mono chunks  ->  transcribe  ->  translate  ->  WebVTT

Long videos are split into chunks because speech-to-text APIs cap the size of a
single upload, and the per-chunk timestamps are shifted back so the cues line up
with the start of the full track.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
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


_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _audio_duration(path: str) -> float | None:
    """Length of an audio file in seconds, read from ffmpeg's metadata.

    ``ffmpeg -i`` prints the duration and exits without decoding, so this is
    cheap even for a long video (unlike decoding the whole file to measure it).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    match = _DURATION_PATTERN.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def chunk_audio(
    source_path: str, workdir: str, chunk_seconds: int = CHUNK_SECONDS
) -> list[tuple[str, float]]:
    """Split ``source_path`` into API-sized pieces.

    ``chunk_seconds`` is how long each piece lasts; it defaults to the global
    maximum but providers that cap a single request lower (Google's synchronous
    recogniser allows about a minute) pass their own, shorter value.

    Returns ``[(path, offset_seconds), ...]`` so the caller can shift the
    per-chunk timestamps back onto the full timeline.
    """
    import subprocess

    chunk_seconds = max(1, int(chunk_seconds))
    duration = _audio_duration(source_path)
    # MP3 frames are padded, so a file reported as exactly N seconds can be a
    # hair longer. Without a little slack, the boundary chunk at N seconds
    # would be a sliver of encoder padding.
    last_start = None if duration is None else duration - 0.5
    chunks: list[tuple[str, float]] = []
    for index in range(0, 1024):  # generous ceiling: ~85 hours of audio
        start = index * chunk_seconds
        # ffmpeg writes a header even when asked for audio past the end, so the
        # output file exists but is silent. Stop by the known duration instead
        # of relying on a missing file.
        if last_start is not None and start >= last_start:
            break
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
            str(chunk_seconds),
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

    # False for providers that can transcribe but have no text-translation
    # API (ElevenLabs only translates audio through its dubbing workflow), so
    # the pipeline saves the source track instead of failing on every target.
    supports_translation = True

    # Longest slice of audio handed to :meth:`transcribe` in one call. Back ends
    # whose API caps a request lower than the default shorten it (see
    # :class:`GoogleProvider`, whose synchronous recogniser allows ~1 minute).
    chunk_seconds = CHUNK_SECONDS

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


# --- Google Cloud provider --------------------------------------------------
#
# Speech-to-Text turns each audio chunk into timed words; Cloud Translation
# turns those words into captions for every target language. Both services take
# the same API key, so a single ``GOOGLE_API_KEY`` enables the pair.

GOOGLE_SPEECH_URL = "https://speech.googleapis.com/v1/speech:recognize"
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
GOOGLE_HTTP_TIMEOUT = 180.0

# The synchronous ``recognize`` endpoint accepts at most about one minute (and
# 10 MB) of audio, so Google chunks are much shorter than the OpenAI ones.
GOOGLE_CHUNK_SECONDS = 55

# Spoken-language codes: our short codes -> the BCP-47 tags Speech-to-Text
# expects. A tag that already contains a dash is passed through untouched, and
# ``GOOGLE_SPEECH_LANGUAGE`` overrides the lot (useful for an Arabic dialect
# other than ``ar-SA``).
GOOGLE_SPEECH_LANGUAGES = {
    "ar": "ar-SA",
    "en": "en-US",
    "fr": "fr-FR",
    "es": "es-ES",
    "tr": "tr-TR",
}

# How a long run of speech is broken into readable caption lines.
CAPTION_MAX_SECONDS = 7.0
CAPTION_MAX_CHARS = 90
CAPTION_GAP_SECONDS = 1.5
CAPTION_SENTENCE_ENDINGS = ".!?…。？！"


def speech_language_code(language: str) -> str:
    """Return the Speech-to-Text code for one of our language codes."""
    override = (getattr(settings, "GOOGLE_SPEECH_LANGUAGE", "") or "").strip()
    if override:
        return override
    language = (language or "").strip()
    if not language:
        return "en-US"
    if "-" in language:
        return language
    return GOOGLE_SPEECH_LANGUAGES.get(language.lower(), language)


def translate_language_code(language: str) -> str:
    """Return the Cloud Translation code for one of our language codes."""
    return (language or "").strip().lower()


def _google_seconds(value) -> float:
    """Convert a Speech-to-Text duration such as ``"1.500s"`` to seconds."""
    if value is None:
        return 0.0
    try:
        return float(str(value).strip().rstrip("s"))
    except ValueError:
        return 0.0


def words_from_speech_results(results) -> list[tuple[str, float, float]]:
    """Flatten ``results`` into ``(text, start, end)`` timed words."""
    words: list[tuple[str, float, float]] = []
    for result in results or []:
        alternatives = result.get("alternatives") or []
        if not alternatives:
            continue
        for word in alternatives[0].get("words") or []:
            text = (word.get("word") or "").strip()
            if text:
                words.append(
                    (
                        text,
                        _google_seconds(word.get("startTime")),
                        _google_seconds(word.get("endTime")),
                    )
                )
    return words


def build_captions(words) -> list[Segment]:
    """Group timed words into YouTube-style caption lines.

    A new line starts on a sentence ending, after a pause, or once the current
    line has grown long enough to be hard to read.
    """
    segments: list[Segment] = []
    buffer: list[tuple[str, float, float]] = []
    length = 0

    def flush() -> None:
        nonlocal buffer, length
        if buffer:
            text = " ".join(item[0] for item in buffer)
            start = buffer[0][1]
            end = buffer[-1][2]
            if end <= start:
                end = start + 0.5
            segments.append(Segment(start, end, text))
        buffer = []
        length = 0

    for text, start, end in words:
        # Scribe emits standalone punctuation as its own word; on its own it is
        # not a caption line ("."), so keep it only when attached to text.
        if not any(char.isalnum() for char in text):
            continue
        if buffer:
            gap = start - buffer[-1][2]
            duration = end - buffer[0][1]
            if (
                gap > CAPTION_GAP_SECONDS
                or duration > CAPTION_MAX_SECONDS
                or length + len(text) + 1 > CAPTION_MAX_CHARS
            ):
                flush()
        buffer.append((text, start, end))
        length += len(text) + 1
        if text and text[-1] in CAPTION_SENTENCE_ENDINGS:
            flush()

    flush()
    return segments


def captions_from_speech_results(results) -> list[Segment]:
    """Turn a Speech-to-Text response into timed caption segments."""
    words = words_from_speech_results(results)
    if words:
        return build_captions(words)

    # Word timings are only absent when the API refuses to return them; fall
    # back to one caption per result so the transcription is not lost.
    segments: list[Segment] = []
    cursor = 0.0
    for result in results or []:
        alternatives = result.get("alternatives") or []
        text = ((alternatives[0].get("transcript") if alternatives else "") or "").strip()
        if not text:
            continue
        duration = max(1.0, min(CAPTION_MAX_SECONDS, 0.4 * len(text.split()) + 0.5))
        segments.append(Segment(cursor, cursor + duration, text))
        cursor += duration
    return segments


def _google_error(response) -> str:
    """A readable message taken from a failed Google API response."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        error = {}
    message = error.get("message") or (getattr(response, "text", "") or "").strip()
    return f"Google rejected the request: {message or f'HTTP {response.status_code}'}"


def _google_post_json(url: str, payload: dict, api_key: str) -> dict:
    """POST ``payload`` to a Google API and return the decoded JSON.

    ``httpx`` is imported lazily so the module keeps working for installs that
    only ever use the OpenAI provider.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    try:
        response = httpx.post(
            url, params={"key": api_key}, json=payload, timeout=GOOGLE_HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Could not reach Google: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_google_error(response))
    try:
        return response.json()
    except ValueError as exc:
        raise TranscriptionError("Google returned an unexpected response.") from exc


class GoogleProvider(SubtitleProvider):
    """Google Cloud: Speech-to-Text transcription and Translation captions.

    Authenticates with a single API key (``GOOGLE_API_KEY``). Transcription uses
    the synchronous recogniser, whose ~1 minute ceiling is why this provider
    asks for shorter audio chunks, and returns word-level timings that are
    grouped into caption lines. Translation uses Cloud Translation v2 with its
    Neural Machine Translation model (``GOOGLE_TRANSLATE_MODEL``).
    """

    name = "google"
    chunk_seconds = GOOGLE_CHUNK_SECONDS

    def __init__(self):
        api_key = (getattr(settings, "GOOGLE_API_KEY", "") or "").strip()
        if not api_key:
            raise TranscriptionError(
                "GOOGLE_API_KEY is not set, so subtitles cannot be generated."
            )
        self._api_key = api_key
        self._source_language = ""

    def transcribe(self, path: str, language: str) -> list[Segment]:
        # Remembered so translation can be told the source language instead of
        # auto-detecting it line by line.
        self._source_language = language

        with open(path, "rb") as handle:
            audio = base64.b64encode(handle.read()).decode("ascii")

        config = {
            "encoding": "MP3",
            "sampleRateHertz": CHUNK_SAMPLE_RATE,
            "audioChannelCount": 1,
            "languageCode": speech_language_code(language),
            "enableAutomaticPunctuation": True,
            "enableWordTimeOffsets": True,
        }
        model = (getattr(settings, "GOOGLE_SPEECH_MODEL", "") or "").strip()
        if model:
            config["model"] = model
        alternatives = [
            speech_language_code(code)
            for code in (
                getattr(settings, "GOOGLE_SPEECH_ALTERNATIVE_LANGUAGES", "") or ""
            ).split(",")
            if code.strip()
        ]
        if alternatives:
            config["alternativeLanguageCodes"] = alternatives

        data = self._post_json(
            GOOGLE_SPEECH_URL, {"config": config, "audio": {"content": audio}}
        )
        return captions_from_speech_results(data.get("results"))

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        target = translate_language_code(target_language)
        source = translate_language_code(self._source_language)
        translated: list[Segment] = []

        for start in range(0, len(segments), TRANSLATE_BATCH):
            batch = segments[start : start + TRANSLATE_BATCH]
            texts = self._translate_batch(
                [segment.text for segment in batch], target, source
            )
            for segment, text in zip(batch, texts):
                translated.append(Segment(segment.start, segment.end, text))

        if len(translated) != len(segments):
            raise TranscriptionError(
                "The translation returned a different number of lines than expected."
            )
        return translated

    def _translate_batch(
        self, texts: list[str], target: str, source: str
    ) -> list[str]:
        payload = {"q": texts, "target": target, "format": "text"}
        model = (getattr(settings, "GOOGLE_TRANSLATE_MODEL", "") or "").strip()
        if model:
            payload["model"] = model
        if source:
            payload["source"] = source

        data = self._post_json(GOOGLE_TRANSLATE_URL, payload)
        translations = (data.get("data") or {}).get("translations") or []
        # Cloud Translation escapes punctuation even in ``text`` mode.
        return [
            html.unescape((item.get("translatedText") or "").strip())
            for item in translations
        ]

    def _post_json(self, url: str, payload: dict) -> dict:
        return _google_post_json(url, payload, self._api_key)


# --- ElevenLabs provider ----------------------------------------------------
#
# Scribe returns word-level timings, which are grouped into the same caption
# lines as the Google provider. ElevenLabs has no text-translation endpoint, so
# this provider only produces the source-language track; the pipeline skips the
# target languages when ``supports_translation`` is false.

ELEVENLABS_SPEECH_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_HTTP_TIMEOUT = 300.0

# Scribe accepts files up to 5 GB / 10 hours; this is only a sane request size
# that still keeps each upload comfortably small.
ELEVENLABS_CHUNK_SECONDS = 480


def elevenlabs_language_code(language: str) -> str:
    """The ISO-639-1 code to pass to Scribe, or ``""`` to auto-detect.

    ``ELEVENLABS_LANGUAGE_CODE`` overrides the source language; set it to
    ``auto`` to let Scribe detect the language of every chunk.
    """
    override = (getattr(settings, "ELEVENLABS_LANGUAGE_CODE", "") or "").strip()
    if override:
        return "" if override.lower() == "auto" else override
    return (language or "").strip().lower()


def words_from_elevenlabs(result) -> list[tuple[str, float, float]]:
    """Flatten a Scribe response into ``(text, start, end)`` timed words."""
    words: list[tuple[str, float, float]] = []
    for item in (result or {}).get("words") or []:
        # "spacing" and "audio_event" entries carry no caption text.
        if item.get("type") != "word":
            continue
        text = (item.get("text") or "").strip()
        start = item.get("start")
        end = item.get("end")
        if not text or start is None or end is None:
            continue
        try:
            words.append((text, float(start), float(end)))
        except (TypeError, ValueError):
            continue
    return words


def captions_from_elevenlabs(result) -> list[Segment]:
    """Turn a Scribe response into timed caption segments."""
    words = words_from_elevenlabs(result)
    if words:
        return build_captions(words)

    # No word timings: keep the transcript as one cue spanning the audio.
    text = ((result or {}).get("text") or "").strip()
    if not text:
        return []
    duration = (result or {}).get("audio_duration_secs") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        duration = max(1.0, 0.4 * len(text.split()) + 0.5)
    return [Segment(0.0, duration, text)]


def _elevenlabs_error(response) -> str:
    """A readable message taken from a failed ElevenLabs response."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("code") or ""
    elif isinstance(detail, list):
        message = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item)
            for item in detail
        )
    else:
        message = str(detail or "")
    message = message or (getattr(response, "text", "") or "").strip()
    return f"ElevenLabs rejected the request: {message or f'HTTP {response.status_code}'}"


def _elevenlabs_transcribe_request(api_key: str, path: str, data: dict) -> dict:
    """POST one audio file to Scribe and return the decoded JSON."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise TranscriptionError(
            "The 'httpx' package is not installed. Run: pip install httpx"
        ) from exc

    try:
        with open(path, "rb") as handle:
            response = httpx.post(
                ELEVENLABS_SPEECH_URL,
                headers={"xi-api-key": api_key},
                data=data,
                files={"file": (os.path.basename(path), handle, "audio/mpeg")},
                timeout=ELEVENLABS_HTTP_TIMEOUT,
            )
    except (OSError, httpx.HTTPError) as exc:
        raise TranscriptionError(f"Could not reach ElevenLabs: {exc}") from exc

    if response.status_code >= 400:
        raise TranscriptionError(_elevenlabs_error(response))
    try:
        return response.json()
    except ValueError as exc:
        raise TranscriptionError("ElevenLabs returned an unexpected response.") from exc


class ElevenLabsProvider(SubtitleProvider):
    """ElevenLabs Scribe for speech-to-text.

    Authenticates with ``ELEVENLABS_API_KEY``. Scribe returns word-level
    timings, which become YouTube-style caption lines. ElevenLabs only
    translates audio (through dubbing), not text, so this provider produces the
    source-language track and the pipeline does not attempt translations.
    """

    name = "elevenlabs"
    chunk_seconds = ELEVENLABS_CHUNK_SECONDS
    supports_translation = False

    def __init__(self):
        api_key = (getattr(settings, "ELEVENLABS_API_KEY", "") or "").strip()
        if not api_key:
            raise TranscriptionError(
                "ELEVENLABS_API_KEY is not set, so subtitles cannot be generated."
            )
        self._api_key = api_key

    def transcribe(self, path: str, language: str) -> list[Segment]:
        data = {
            "model_id": (
                getattr(settings, "ELEVENLABS_STT_MODEL", "") or "scribe_v2"
            ).strip(),
            "timestamps_granularity": "word",
        }
        code = elevenlabs_language_code(language)
        if code:
            data["language_code"] = code

        result = _elevenlabs_transcribe_request(self._api_key, path, data)
        return captions_from_elevenlabs(result)

    def translate(self, segments: list[Segment], target_language: str) -> list[Segment]:
        raise TranscriptionError(
            "ElevenLabs does not provide text translation, so only the "
            "source-language track can be generated."
        )


def provider_name() -> str:
    """The configured provider's short name."""
    return (getattr(settings, "SUBTITLE_PROVIDER", "openai") or "openai").strip().lower()


def get_provider() -> SubtitleProvider:
    """Build the provider named by ``SUBTITLE_PROVIDER``."""
    name = provider_name()
    if name == "openai":
        return OpenAIProvider()
    if name == "google":
        return GoogleProvider()
    if name == "elevenlabs":
        return ElevenLabsProvider()
    raise TranscriptionError(f"Unknown subtitle provider: {name!r}")


def is_configured() -> bool:
    """True when the configured provider has everything it needs to run."""
    name = provider_name()
    if name == "openai":
        return bool(getattr(settings, "OPENAI_API_KEY", ""))
    if name == "google":
        return bool(getattr(settings, "GOOGLE_API_KEY", ""))
    if name == "elevenlabs":
        return bool(getattr(settings, "ELEVENLABS_API_KEY", ""))
    return False


def configuration_hint() -> str:
    """The ``.env`` variable the configured provider needs to be switched on."""
    return {
        "google": "GOOGLE_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
    }.get(provider_name(), "OPENAI_API_KEY")


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
        chunks = chunk_audio(audio_path, workdir, provider.chunk_seconds)

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
            if not provider.supports_translation:
                # Providers with no text-translation API (ElevenLabs) still
                # give a usable source-language track; don't fail the run.
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