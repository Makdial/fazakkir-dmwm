"""Subtitle utilities: turn uploaded SRT/VTT text into clean WebVTT."""
from __future__ import annotations

import re

ALLOWED_SUBTITLE_EXTENSIONS = {".vtt", ".srt", ".txt"}

# SRT uses 00:00:01,500 while WebVTT uses 00:00:01.500.
_SRT_TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _decode(content: bytes | str) -> str:
    if isinstance(content, bytes):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return content.decode(encoding)
            except UnicodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return content


def normalize_to_vtt(content: bytes | str) -> str:
    """Return WebVTT text for ``content`` (accepts VTT, SRT or plain text).

    Plain text without timestamps is turned into a single cue covering the whole
    video so it still works as lyrics.
    """
    text = _decode(content).replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()

    if not stripped:
        raise ValueError("The subtitle file is empty.")

    if stripped.startswith("WEBVTT"):
        return stripped + "\n"

    if _SRT_TIMESTAMP.search(text):
        # Convert SRT timestamps and drop the numeric cue counters.
        body = _SRT_TIMESTAMP.sub(
            lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}", text
        )
        lines = [
            line for line in body.split("\n")
            if not re.fullmatch(r"\d+", line.strip())
        ]
        return "WEBVTT\n\n" + "\n".join(lines).strip() + "\n"

    # Plain text (e.g. lyrics without timings): one cue from 0 to the end.
    return f"WEBVTT\n\n00:00:00.000 --> 99:59:59.000\n{stripped}\n"


def label_for(language_code: str) -> str:
    """Human readable label for a language code."""
    return {
        "ar": "Arabic",
        "fr": "French",
        "en": "English",
        "lyrics": "Lyrics",
    }.get(language_code, language_code)
