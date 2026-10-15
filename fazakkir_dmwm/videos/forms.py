from django import forms
from django.utils.translation import gettext_lazy as _

MAX_VIDEO_SIZE = 3 * 1024 * 1024 * 1024  # 3 GB
ALLOWED_VIDEO_TYPES = [
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
]


class VideoUploadForm(forms.Form):
    title = forms.CharField(
        max_length=200,
        label=_("Title"),
        widget=forms.TextInput(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video title"),
            }
        ),
    )
    description = forms.CharField(
        required=False,
        label=_("Description"),
        widget=forms.Textarea(
            attrs={
                "class": "form-input",
                "placeholder": _("Enter video description"),
                "rows": 4,
            }
        ),
    )
    video_file = forms.FileField(
        label=_("Video File"),
        widget=forms.FileInput(attrs={
            "class": "form-input",
            "accept": "video/*",
        }),
    )

    def clean_video_file(self):
        video = self.cleaned_data.get("video_file")
        if video:
            if video.size > MAX_VIDEO_SIZE:
                raise forms.ValidationError(_("Video must be under 3 GB."))

            if video.content_type not in ALLOWED_VIDEO_TYPES:
                raise forms.ValidationError(_("This video type is not allowed."))

        return video
