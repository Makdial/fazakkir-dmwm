from django.urls import path

from . import views

app_name = "shorts"

urlpatterns = [
    path("", views.short_list, name="list"),
    path("<int:short_id>/", views.short_detail, name="detail"),
    path("upload/", views.short_upload_page, name="upload"),
    path("upload/submit/", views.short_upload, name="upload_submit"),
    path("<int:short_id>/delete/", views.short_delete, name="delete"),
    path("<int:short_id>/like/", views.short_like, name="like"),
    path("<int:short_id>/view/", views.short_view, name="view"),
    path("<int:short_id>/comments/", views.short_comments_list, name="comments_list"),
    path("<int:short_id>/comments/add/", views.short_comment_add, name="comment_add"),
    path("comments/<int:comment_id>/delete/", views.short_comment_delete, name="comment_delete"),
]