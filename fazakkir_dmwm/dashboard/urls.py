from django.urls import path

from knowledge import views as knowledge_views

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("ai/", knowledge_views.manage, name="ai"),
    path("ai/prompt/", knowledge_views.save_prompt, name="ai_prompt"),
    path("ai/index/", knowledge_views.reindex, name="ai_index"),
    path("ai/source/", knowledge_views.review_source, name="ai_source"),
    path("ai/web-source/", knowledge_views.review_web_source, name="ai_web_source"),
    path(
        "ai/feedback/<int:feedback_id>/",
        knowledge_views.review_feedback,
        name="ai_feedback",
    ),
    path(
        "ai/interaction/<int:interaction_id>/",
        knowledge_views.review_interaction,
        name="ai_interaction",
    ),
    path("videos/", views.video_manage, name="videos"),
    path("categories/", views.category_manage, name="categories"),
    path("categories/create/", views.category_create, name="category_create"),
    path("categories/<int:category_id>/update/", views.category_update, name="category_update"),
    path("categories/<int:category_id>/delete/", views.category_delete, name="category_delete"),
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
