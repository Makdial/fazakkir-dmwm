from django.urls import path

from . import views

app_name = "library"

urlpatterns = [
    path("", views.book_list, name="book_list"),
    path("book/<slug:slug>/", views.book_detail, name="book_detail"),
    path("read/<slug:slug>/", views.read_book, name="read_book"),
    path("pdf/<slug:slug>/", views.serve_pdf, name="serve_pdf"),
    path("download/<slug:slug>/", views.download_book, name="download_book"),
    path("upload/", views.upload_book, name="upload_book"),
    path("edit/<slug:slug>/", views.edit_book, name="edit_book"),
    path("delete/<slug:slug>/", views.delete_book, name="delete_book"),
    path("manage/", views.manage_books, name="manage_books"),
    path("manage/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("manage/categories/", views.manage_categories, name="manage_categories"),
    path("manage/categories/<int:pk>/delete/", views.delete_category, name="delete_category"),
    path("dashboard/", views.user_dashboard, name="user_dashboard"),
    path("my/", views.my_library, name="my_library"),
    path("translate/", views.translate_text, name="translate_text"),
    path("translations/", views.translation_history, name="translation_history"),
    path("translations/<int:pk>/save/", views.toggle_saved_translation, name="toggle_saved_translation"),
    path("translations/<int:pk>/delete/", views.delete_translation, name="delete_translation"),
    path("translations/export/", views.export_translations, name="export_translations"),
    path("highlights/", views.highlights_list, name="highlights_list"),
    path("api/highlight/", views.add_highlight, name="add_highlight"),
    path("api/highlight/<int:pk>/delete/", views.delete_highlight, name="delete_highlight"),
    path("collections/", views.collections_list, name="collections_list"),
    path("api/collection/", views.create_collection, name="create_collection"),
    path("api/collection/<int:pk>/add/", views.collection_add_book, name="collection_add_book"),
    path("api/collection/<int:pk>/remove/", views.collection_remove_book, name="collection_remove_book"),
    path("api/collection/<int:pk>/delete/", views.delete_collection, name="delete_collection"),
    path("api/progress/<slug:slug>/", views.save_progress, name="save_progress"),
    path("api/favorite/<slug:slug>/", views.toggle_favorite, name="toggle_favorite"),
    path("api/bookmark/<slug:slug>/", views.add_bookmark, name="add_bookmark"),
    path("api/bookmark/<int:pk>/delete/", views.delete_bookmark, name="delete_bookmark"),
    path("review/<slug:slug>/", views.submit_review, name="submit_review"),
]
