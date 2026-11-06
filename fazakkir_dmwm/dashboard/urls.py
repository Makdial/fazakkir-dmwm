from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("videos/", views.video_manage, name="videos"),
    path("videos/order/", views.video_order, name="video_order"),
    path("videos/order/save/", views.video_order_save, name="video_order_save"),
    path("videos/audio/generate/", views.audio_generate, name="audio_generate"),
    path("videos/audio/status/", views.audio_status, name="audio_status"),
    path("videos/subtitles/generate/", views.subtitle_generate, name="subtitle_generate"),
    path("videos/subtitles/status/", views.subtitle_status, name="subtitle_status"),
    path("comments/", views.comment_manage, name="comments"),
    path("usage/", views.usage, name="usage"),
    path("users/", views.users, name="users"),
    path("users/", views.users, name="manage_users"),
    path("users/add/", views.add_admin, name="add_admin"),
    path("users/add/", views.add_admin, name="create_admin"),
    path("users/add/", views.add_admin, name="add_user"),
    path("users/add/", views.add_admin, name="create_user"),
    path("users/add/", views.add_admin, name="user_add"),
    path("users/add/", views.add_admin, name="user_create"),
    path("users/add/", views.add_admin, name="new_admin"),
    path("users/add/", views.add_admin, name="new_user"),
    path("admins/", views.add_admin, name="admins"),
    path("admins/add/", views.add_admin, name="add_admin"),
    path("storage/cleanup/", views.cleanup, name="cleanup"),
    path("storage/settings/", views.settings_update, name="settings"),
    path("storage/test-email/", views.test_email, name="test_email"),
]
