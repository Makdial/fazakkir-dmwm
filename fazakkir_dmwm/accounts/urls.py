from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", auth_views.LoginView.as_view(
        template_name="accounts/login.html",
        redirect_authenticated_user=True,
    ), name="login"),
    path("logout/", auth_views.LogoutView.as_view(
        template_name="accounts/logout.html",
    ), name="logout"),
    path("manage/users/", views.manage_users, name="manage_users"),
    path("manage/users/<int:user_id>/block/", views.user_set_blocked, name="user_block"),
    path("manage/users/<int:user_id>/access/", views.user_set_access, name="user_access"),
    path("manage/users/<int:user_id>/delete/", views.user_delete, name="user_delete"),
]
