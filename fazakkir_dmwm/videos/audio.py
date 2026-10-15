"""Extract an MP3 audio track from an uploaded video.

``imageio-ffmpeg`` ships a self-contained ffmpeg binary, so no system-wide
ffmpeg installation is required.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


class AudioExtractionError(Exception):
    """Raised when ffmpeg itself fails while extracting audio."""


def _ffmpeg_exe() -> str:
    # Imported lazily so the rest of the site works even if the optional
    # dependency is missing.
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def materialize(uploaded_file) -> tuple[str, str | None]:
    """Return a filesystem path for an uploaded file.

    Returns ``(path, cleanup_path)``. ``cleanup_path`` is a temporary file the
    caller should remove, or ``None`` when the upload already lives on disk
    (Django's ``TemporaryUploadedFile`` for larger files).
    """
    if hasattr(uploaded_file, "temporary_file_path"):
        return uploaded_file.temporary_file_path(), None

    suffix = Path(getattr(uploaded_file, "name", "") or "").suffix
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            uploaded_file.seek(0)
            for chunk in uploaded_file.chunks():
                handle.write(chunk)
    except Exception:
        os.unlink(temp_path)
        raise

    return temp_path, temp_path


def _build_command(source: str, dest_path: str, *, remote: bool = False) -> list[str]:
    command = [_ffmpeg_exe(), "-y", "-nostdin", "-loglevel", "error"]
    if remote:
        # When reading straight from an HTTP(S) URL, give up rather than hang
        # forever if the connection stalls.
        command += ["-rw_timeout", "60000000", "-user_agent", "Fazakkir/1.0"]
    command += [
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(dest_path),
    ]
    return command


def _run_ffmpeg(command: list[str], dest_path: str) -> bool:
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise AudioExtractionError(f"Could not run ffmpeg: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # A video without an audio stream is a normal, non-fatal case.
        if "does not contain any stream" in stderr or "matches no streams" in stderr:
            return False
        raise AudioExtractionError(f"ffmpeg failed: {stderr[:400]}")

    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0


def extract_mp3(source_path: str, dest_path: str) -> bool:
    """Extract the first audio stream of a local file to ``dest_path``.

    Returns ``True`` when an MP3 was produced and ``False`` when the video has
    no audio track. Raises :class:`AudioExtractionError` when ffmpeg fails.
    """
    return _run_ffmpeg(_build_command(source_path, dest_path), dest_path)


def extract_mp3_from_url(source_url: str, dest_path: str) -> bool:
    """Like :func:`extract_mp3`, but reads the video from an HTTP(S) URL.

    Handy for videos that are already in object storage: ffmpeg streams the
    file instead of the server downloading and storing a second copy.
    """
    return _run_ffmpeg(_build_command(source_url, dest_path, remote=True), dest_path)
