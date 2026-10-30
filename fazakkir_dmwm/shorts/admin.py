from django.contrib import admin

from .models import Short, ShortLike, ShortComment


@admin.register(Short)
class ShortAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "views", "likes", "created_at")
    search_fields = ("title", "user__username")


@admin.register(ShortLike)
class ShortLikeAdmin(admin.ModelAdmin):
    list_display = ("user", "short", "created_at")


@admin.register(ShortComment)
class ShortCommentAdmin(admin.ModelAdmin):
    list_display = ("short", "user", "body", "created_at")
    search_fields = ("body", "user__username")