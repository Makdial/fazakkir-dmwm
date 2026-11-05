from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _

from . import r2_storage


class Short(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="shorts")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    file_id = models.CharField(max_length=200, blank=True, default="")
    video_url = models.URLField(max_length=500, blank=True, default="")
    thumbnail_file_id = models.CharField(max_length=200, blank=True, default="")
    thumbnail_url = models.URLField(max_length=500, blank=True)

    # An MP3 copy of the sound, so the sound disc can play it on its own and it
    # can be downloaded. See ``shorts.audio`` for how each status is reached.
    AUDIO_STATUS_CHOICES = [
        ("", _("Not generated")),
        ("ready", _("Ready")),
        ("none", _("No audio track")),
        ("failed", _("Failed")),
    ]
    audio_file_id = models.CharField(max_length=200, blank=True, default="")
    audio_url = models.URLField(max_length=500, blank=True, default="")
    audio_status = models.CharField(
        max_length=12, choices=AUDIO_STATUS_CHOICES, blank=True, default=""
    )
    audio_checked_at = models.DateTimeField(null=True, blank=True)

    views = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def playback_url(self):
        return r2_storage.resolve_url(self.file_id, self.video_url)

    @property
    def display_thumbnail_url(self):
        return r2_storage.resolve_url(self.thumbnail_file_id, self.thumbnail_url)

    @property
    def audio_playback_url(self):
        """A URL the browser can play the MP3 from, or an empty string."""
        return r2_storage.resolve_url(self.audio_file_id, self.audio_url)

    @property
    def audio_download_url(self):
        """A URL that downloads the MP3, or an empty string when there is none."""
        if not self.audio_file_id:
            return ""
        return r2_storage.download_url(
            self.audio_file_id, f"{self.title}.mp3", self.audio_url
        )

    def delete_stored_files(self):
        for path in (self.file_id, self.thumbnail_file_id, self.audio_file_id):
            if path:
                try:
                    r2_storage.delete_file(path)
                except r2_storage.StorageError:
                    pass


class ShortLike(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="short_likes")
    short = models.ForeignKey(Short, on_delete=models.CASCADE, related_name="user_likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "short")

    def __str__(self):
        return f"{self.user} likes {self.short}"


class ShortComment(models.Model):
    short = models.ForeignKey(Short, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="short_comments")
    body = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user}: {self.body[:40]}"