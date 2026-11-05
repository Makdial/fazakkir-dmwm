"""An MP3 copy of each Short's sound.

Every Short gets its MP3 when it is uploaded. Shorts uploaded before this
existed get theirs the first time someone plays or downloads the sound. Shorts
are small clips, so the conversion runs inline and the result is stored beside
the video and recorded on the Short.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from videos.audio import AudioExtractionError, extract_mp3, materialize

from . import r2_storage
from .models import Short

logger = logging.getLogger(__name__)

# A Short whose sound failed is not converted again for this long. The endpoint
# is public, so without a pause every request could make the server download and
# encode the same clip again.
RETRY_AFTER = timedelta(minutes=5)


def _remove(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _record(short: Short, status: str, audio_file_id: str = "", audio_url: str = "") -> bool:
    """Save an audio result unless another request has already finished one.

    Two requests can convert the same Short at the same time. The first to
    finish is kept, and the second is told so it can drop the copy it made.
    """
    updated = (
        Short.objects.filter(pk=short.pk)
        .exclude(audio_status="ready", audio_file_id__gt="")
        .update(
            audio_status=status,
            audio_file_id=audio_file_id,
            audio_url=audio_url,
            audio_checked_at=timezone.now(),
        )
    )
    return bool(updated)


def store_sound(short: Short, video_path: str) -> str:
    """Make the MP3 of the clip at ``video_path`` and record it on ``short``.

    Returns ``"ready"``, ``"none"`` when the clip has no sound, or ``"failed"``
    when the conversion or the upload did not work.
    """
    handle_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(handle_fd)
    try:
        if not extract_mp3(video_path, mp3_path):
            _record(short, "none")
            return "none"

        with open(mp3_path, "rb") as mp3_file:
            result = r2_storage.upload_audio(mp3_file, "sound.mp3")

        if _record(short, "ready", result["path"], result["url"]):
            return "ready"

        # Another request finished first, so its MP3 is kept and this copy goes.
        try:
            r2_storage.delete_file(result["path"])
        except r2_storage.StorageError:
            pass
        return "ready"
    except (AudioExtractionError, r2_storage.StorageError, OSError):
        logger.warning("Making the sound for short %s failed", short.pk, exc_info=True)
        _record(short, "failed")
        return "failed"
    finally:
        _remove(mp3_path)


def _download_clip(short: Short) -> str:
    """Copy the clip out of storage into a temporary file and return its path."""
    handle_fd, path = tempfile.mkstemp(suffix=Path(short.file_id).suffix or ".mp4")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            body = r2_storage.get_object(short.file_id)["Body"]
            shutil.copyfileobj(body, handle)
    except Exception:
        _remove(path)
        raise
    return path


def ensure_sound(short: Short) -> str:
    """Return the sound status of ``short``, making its MP3 first when needed."""
    if short.audio_status == "none":
        return "none"
    if short.audio_status == "ready" and short.audio_file_id:
        return "ready"
    if (
        short.audio_status == "failed"
        and short.audio_checked_at
        and timezone.now() - short.audio_checked_at < RETRY_AFTER
    ):
        return "failed"

    video_path = ""
    try:
        video_path = _download_clip(short)
        return store_sound(short, video_path)
    except Exception:  # noqa: BLE001 - the visitor gets a retryable failure
        logger.warning("Could not read short %s to make its sound", short.pk, exc_info=True)
        _record(short, "failed")
        return "failed"
    finally:
        _remove(video_path)


def store_sound_from_upload(short: Short, uploaded_file) -> str:
    """Make the sound of a clip that has just been uploaded. Never raises.

    The sound is an extra: a failure here is recorded on the Short, and the
    upload itself still succeeds.
    """
    cleanup = None
    try:
        source_path, cleanup = materialize(uploaded_file)
        return store_sound(short, source_path)
    except Exception:  # noqa: BLE001 - must never fail the upload
        logger.warning("Could not make the sound for short %s", short.pk, exc_info=True)
        _record(short, "failed")
        return "failed"
    finally:
        _remove(cleanup)
