"""Views for the Composition editor. Rendering itself lives in ``videos.composition``."""

from __future__ import annotations

import json
import math
import os
import re

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from .composition import (
    CANVASES,
    MAX_AUDIO_CLIPS,
    MAX_TIMELINE_SECONDS,
    MAX_VIDEO_CLIPS,
    MIN_CLIP_SECONDS,
    CompositionError,
    discard_job,
    job_status,
    output_path,
    read_job,
    start_job,
)
from .models import Video

MAX_LIBRARY = 200
PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"


def _number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _video_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CompositionError("invalid")


def _lookup(videos: dict, item: dict):
    video = videos.get(_video_id(item.get("video_id")))
    if video is None:
        raise CompositionError("missing")
    return video


def _window(item: dict, duration) -> tuple[float, float]:
    start = max(_number(item.get("in"), 0.0), 0.0)
    end = _number(item.get("out"), None)
    if duration and duration > 0:
        end = float(duration) if end is None else min(end, float(duration))
    if end is None:
        raise CompositionError("invalid")
    if end - start < MIN_CLIP_SECONDS:
        raise CompositionError("short_clip")
    return round(start, 3), round(end, 3)


def _volume(item: dict) -> float:
    return min(max(_number(item.get("volume"), 1.0), 0.0), 1.0)


def build_spec(payload) -> dict:
    """Check a timeline sent by the editor and return the clean spec to render.

    Raises CompositionError with a short code the page translates.
    """
    if not isinstance(payload, dict):
        raise CompositionError("invalid")
    canvas = payload.get("canvas")
    canvas = canvas if canvas in CANVASES else "16:9"
    raw_video = payload.get("video") or []
    raw_audio = payload.get("audio") or []
    if not isinstance(raw_video, list) or not isinstance(raw_audio, list):
        raise CompositionError("invalid")
    if len(raw_video) > MAX_VIDEO_CLIPS or len(raw_audio) > MAX_AUDIO_CLIPS:
        raise CompositionError("too_many")
    if not raw_video and not raw_audio:
        raise CompositionError("empty")
    items = raw_video + raw_audio
    if not all(isinstance(item, dict) for item in items):
        raise CompositionError("invalid")
    ids = list({_video_id(item.get("video_id")) for item in items})
    videos = Video.objects.in_bulk(ids)

    video_clips = []
    for item in raw_video:
        video = _lookup(videos, item)
        if not video.has_video:
            raise CompositionError("no_picture")
        start, end = _window(item, video.duration_seconds)
        video_clips.append(
            {
                "key": f"video:{video.id}",
                "url": video.playback_url,
                "start": start,
                "end": end,
                "volume": _volume(item),
            }
        )

    audio_clips = []
    for item in raw_audio:
        video = _lookup(videos, item)
        if not video.has_audio:
            raise CompositionError("no_audio")
        start, end = _window(item, video.duration_seconds)
        offset = min(max(_number(item.get("offset"), 0.0), 0.0), MAX_TIMELINE_SECONDS)
        audio_clips.append(
            {
                "key": f"audio:{video.id}",
                "url": video.audio_playback_url,
                "start": start,
                "end": end,
                "offset": round(offset, 3),
                "volume": _volume(item),
            }
        )

    timeline = max(
        [sum(clip["end"] - clip["start"] for clip in video_clips)]
        + [clip["offset"] + clip["end"] - clip["start"] for clip in audio_clips]
    )
    if timeline > MAX_TIMELINE_SECONDS:
        raise CompositionError("too_long")
    return {
        "kind": "video" if video_clips else "audio",
        "canvas": canvas,
        "title": str(payload.get("title") or "")[:120],
        "video": video_clips,
        "audio": audio_clips,
    }


def _library_entry(video: Video) -> dict:
    return {
        "id": video.id,
        "title": video.title,
        "uploader": video.user.username,
        "views": video.views,
        "duration": float(video.duration_seconds or 0),
        "thumb": video.display_thumbnail_url or "",
        "hasVideo": video.has_video,
        "hasAudio": video.has_audio,
        "videoUrl": video.playback_url if video.has_video else "",
        "audioUrl": reverse("videos:audio_stream", args=[video.id]) if video.has_audio else "",
    }


def _strings() -> dict:
    return {
        "add": _("Add to timeline"),
        "noMatches": _("No matches for this search."),
        "onTimeline": _("On timeline"),
        "views": _("Views"),
        "noItems": _("Nothing here yet."),
        "play": _("Play"),
        "pause": _("Pause"),
        "empty": _("Add a video or audio to the timeline to start."),
        "audioOnly": _("Audio only: the sound plays while you preview."),
        "addFirst": _("Add a clip to the timeline first."),
        "loadFailed": _("Could not read this file."),
        "splitNothing": _("Move the playhead onto a clip, then split."),
        "splitTooClose": _("The playhead is too close to the edge of the clip."),
        "discarded": _("Export discarded."),
        "waiting": _("Waiting for the renderer…"),
        "stageDownload": _("Preparing clips…"),
        "stageEncode": _("Encoding clips…"),
        "stageJoin": _("Joining clips…"),
        "stageMix": _("Mixing audio…"),
        "stageDone": _("Finishing…"),
        "errors": {
            "invalid": _("The export request was invalid."),
            "empty": _("Add at least one clip."),
            "too_many": _("There are too many clips for one export."),
            "too_long": _("The timeline is too long for one export."),
            "missing": _("One of the clips is no longer available."),
            "no_picture": _("A video clip has no picture."),
            "no_audio": _("An audio clip has no sound track."),
            "short_clip": _("A clip is too short (0.2 seconds minimum)."),
            "source_failed": _("A clip could not be downloaded."),
            "clip_failed": _("A clip could not be rendered."),
            "timeout": _("The export took too long. Try a shorter timeline."),
            "interrupted": _("The export was interrupted. Please try again."),
            "failed": _("The export failed. Please try again."),
            "network": _("Could not reach the server. Check your connection."),
        },
    }


def composition_page(request):
    library = [
        _library_entry(video)
        for video in Video.objects.select_related("user").order_by("-created_at")[:MAX_LIBRARY]
    ]
    config = {
        "urls": {
            "start": reverse("videos:composition_start"),
            "status": reverse("videos:composition_status", args=[PLACEHOLDER_ID]),
            "discard": reverse("videos:composition_discard", args=[PLACEHOLDER_ID]),
        },
        "csrf": get_token(request),
        "maxSeconds": MAX_TIMELINE_SECONDS,
        "library": library,
        "strings": _strings(),
    }
    return render(request, "videos/composition.html", {"config": config})


@require_POST
def composition_start(request):
    try:
        spec = build_spec(json.loads(request.body or b"{}"))
    except CompositionError as exc:
        return JsonResponse({"success": False, "error": exc.code}, status=400)
    except ValueError:
        return JsonResponse({"success": False, "error": "invalid"}, status=400)
    job_id = start_job(spec)
    return JsonResponse({"success": True, "job_id": str(job_id)})


@require_GET
def composition_status(request, job_id):
    state = job_status(job_id)
    if state is None:
        return JsonResponse({"status": "unknown", "error": "missing"}, status=404)
    data = {key: state.get(key) for key in ("status", "stage", "percent", "error", "kind")}
    if state.get("status") == "ready":
        download = reverse("videos:composition_download", args=[job_id])
        data["download_url"] = download
        data["preview_url"] = download + "?inline=1"
    return JsonResponse(data)


@require_GET
def composition_download(request, job_id):
    path = output_path(job_id)
    if path is None:
        return JsonResponse({"success": False, "error": "missing"}, status=404)
    state = read_job(job_id) or {}
    audio = state.get("kind") == "audio"
    filename = _file_name(state.get("title"), "m4a" if audio else "mp4")
    return _serve(
        request,
        path,
        "audio/mp4" if audio else "video/mp4",
        filename,
        inline=request.GET.get("inline") == "1",
    )


@require_POST
def composition_discard(request, job_id):
    discard_job(job_id)
    return JsonResponse({"success": True})


def _file_name(title, extension: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")[:60]
    return f"{stem or 'composition'}.{extension}"


def _serve(request, path: str, content_type: str, filename: str, inline: bool):
    """Send the file, honouring Range requests so the preview can seek."""
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, 200
    match = re.match(r"bytes=(\d*)-(\d*)$", request.META.get("HTTP_RANGE", "").strip())
    if match and size and (match.group(1) or match.group(2)):
        first, last = match.groups()
        if first == "":
            start = max(0, size - int(last))
        else:
            start = int(first)
            if last:
                end = min(int(last), size - 1)
        if start > end or start >= size:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        status = 206
    length = end - start + 1
    response = StreamingHttpResponse(
        _chunks(path, start, length), status=status, content_type=content_type
    )
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(length)
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    if status == 206:
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response


def _chunks(path: str, start: int, length: int):
    # Opened here rather than in the view, so a response that is never read
    # does not keep the file open (which would block a later discard).
    with open(path, "rb") as handle:
        handle.seek(start)
        while length > 0:
            chunk = handle.read(min(64 * 1024, length))
            if not chunk:
                break
            length -= len(chunk)
            yield chunk
