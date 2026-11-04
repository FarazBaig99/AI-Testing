from django.urls import path

from . import views

urlpatterns = [
    path('', views.api_home, name='api_home'),
    path('import/', views.api_import, name='api_import'),
    path('import/review/<uuid:session_id>/', views.api_review, name='api_review'),
    path('sync/', views.api_sync, name='api_sync'),
    path('modules/<int:folder_id>/', views.api_folder, name='api_folder'),
    path('modules/<int:folder_id>/delete/', views.api_folder_delete, name='api_folder_delete'),
    path('cases/<int:case_id>/', views.api_case, name='api_case'),
    path('cases/<int:case_id>/edit/', views.api_case_edit, name='api_case_edit'),
    path('cases/<int:case_id>/delete/', views.api_case_delete, name='api_case_delete'),
]
