from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Book, Category, Review


class BookForm(forms.ModelForm):
    pdf_file = forms.FileField(required=False, label=_("PDF file"))
    cover_file = forms.FileField(required=False, label=_("Cover image"))

    class Meta:
        model = Book
        fields = [
            "title", "author", "description", "category", "language", "tags",
            "isbn", "publisher", "published_year", "page_count",
            "status", "is_featured", "allow_download", "is_public",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "tags": forms.TextInput(attrs={"placeholder": _("e.g. science, fiction, history")}),
        }

    def clean_pdf_file(self):
        f = self.cleaned_data.get("pdf_file")
        if f and not f.name.lower().endswith(".pdf"):
            raise forms.ValidationError(_("Only PDF files are allowed."))
        return f

    def clean_cover_file(self):
        f = self.cleaned_data.get("cover_file")
        if f:
            if not f.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                raise forms.ValidationError(_("Cover must be an image (jpg, png, webp)."))
        return f


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "description", "icon"]


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["rating", "comment"]
        widgets = {
            "rating": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }
