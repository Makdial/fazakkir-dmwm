from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST, require_GET

from core.permissions import write_required

from . import r2_storage
from .models import Short, ShortLike, ShortComment


def short_list(request):
    shorts = Short.objects.select_related("user").all()
    liked_ids = set()
    if request.user.is_authenticated:
        liked_ids = set(
            ShortLike.objects.filter(user=request.user, short__in=shorts).values_list("short_id", flat=True)
        )
    return render(
        request,
        "shorts/list.html",
        {"shorts": shorts, "liked_ids": liked_ids},
    )


@require_GET
def short_detail(request, short_id):
    short = get_object_or_404(Short.objects.select_related("user"), id=short_id)
    short.views += 1
    short.save(update_fields=["views"])
    liked = False
    if request.user.is_authenticated:
        liked = ShortLike.objects.filter(user=request.user, short=short).exists()
    comments = short.comments.select_related("user").all()
    return render(
        request,
        "shorts/detail.html",
        {"short": short, "liked": liked, "comments": comments},
    )


@write_required
def short_upload_page(request):
    return render(request, "shorts/upload.html", {"storage_ready": r2_storage.is_configured()})


@write_required
@require_POST
def short_upload(request):
    upload = request.FILES.get("video_file")
    if not upload:
        return JsonResponse({"success": False, "error": _("Please choose a video file.")}, status=400)
    title = (request.POST.get("title") or "").strip() or Path(upload.name).stem
    try:
        result = r2_storage.upload_video(file_data=upload, file_name=upload.name)
    except r2_storage.StorageError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    short = Short.objects.create(
        user=request.user,
        title=title[:120],
        description=(request.POST.get("description") or "").strip(),
        file_id=result["path"],
        video_url=result["url"],
    )
    return JsonResponse({"success": True, "short_id": short.id, "message": _("Short uploaded")})


@write_required
@require_POST
def short_delete(request, short_id):
    short = get_object_or_404(Short, id=short_id)
    short.delete_stored_files()
    short.delete()
    return JsonResponse({"success": True, "message": _("Short deleted")})


@require_POST
def short_like(request, short_id):
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": _("You must be signed in to like.")}, status=403)
    short = get_object_or_404(Short, id=short_id)
    existing = ShortLike.objects.filter(user=request.user, short=short).first()
    if existing:
        existing.delete()
        short.likes = max(0, short.likes - 1)
        short.save(update_fields=["likes"])
        liked = False
    else:
        ShortLike.objects.create(user=request.user, short=short)
        short.likes += 1
        short.save(update_fields=["likes"])
        liked = True
    return JsonResponse({"success": True, "likes": short.likes, "liked": liked})


@require_POST
def short_view(request, short_id):
    short = get_object_or_404(Short, id=short_id)
    short.views += 1
    short.save(update_fields=["views"])
    return JsonResponse({"success": True, "views": short.views})


@require_POST
def short_comment_add(request, short_id):
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": _("You must be signed in to comment.")}, status=403)
    short = get_object_or_404(Short, id=short_id)
    body = (request.POST.get("body") or "").strip()
    if not body:
        return JsonResponse({"success": False, "error": _("Comment cannot be empty.")}, status=400)
    comment = ShortComment.objects.create(short=short, user=request.user, body=body[:500])
    return JsonResponse(
        {
            "success": True,
            "comment": {
                "id": comment.id,
                "user": comment.user.username,
                "initial": comment.user.username[:1].upper(),
                "body": comment.body,
                "created_at": comment.created_at.strftime("%Y-%m-%d %H:%M"),
            },
        }
    )


@require_GET
def short_comments_list(request, short_id):
    short = get_object_or_404(Short, id=short_id)
    comments = short.comments.select_related("user").all()
    data = [
        {
            "id": c.id,
            "user": c.user.username,
            "initial": c.user.username[:1].upper(),
            "body": c.body,
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
            "can_delete": request.user == c.user or request.user.is_staff,
            "delete_url": f"/shorts/comments/{c.id}/delete/",
        }
        for c in comments
    ]
    return JsonResponse({"success": True, "comments": data})


@require_POST
def short_comment_delete(request, comment_id):
    comment = get_object_or_404(ShortComment, id=comment_id)
    if not (request.user == comment.user or request.user.is_staff):
        return JsonResponse({"success": False, "error": _("Not allowed.")}, status=403)
    comment.delete()
    return JsonResponse({"success": True})