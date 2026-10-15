from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "access_level", "created_at")
    list_filter = ("access_level",)
    search_fields = ("user__username", "user__email")
