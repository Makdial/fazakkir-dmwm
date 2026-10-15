from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("videos/", views.video_manage, name="videos"),
    path("videos/audio/generate/", views.audio_generate, name="audio_generate"),
    path("videos/audio/status/", views.audio_status, name="audio_status"),
    path("comments/", views.comment_manage, name="comments"),
    path("storage/cleanup/", views.cleanup, name="cleanup"),
    path("storage/settings/", views.settings_update, name="settings"),
    path("storage/test-email/", views.test_email, name="test_email"),
]
