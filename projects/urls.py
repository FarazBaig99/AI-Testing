from django.urls import include, path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    path('', views.project_list, name='project_list'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('my-tasks/', views.my_tasks, name='my_tasks'),
    path('modules/<slug:slug>/', views.menu_module, name='menu_module'),
    path('modules/<slug:slug>/projects/', views.menu_pick_project, name='menu_pick_project'),
    # projects are created in the Projects module (Project & Business Analysis)
    path('create/', RedirectView.as_view(pattern_name='portfolio_create'), name='project_create'),
    path('<int:pk>/', views.project_detail, name='project_detail'),
    path('<int:pk>/edit/', views.project_form, name='project_edit'),
    path('<int:pk>/delete/', views.project_delete, name='project_delete'),

    path('<int:pk>/releases/create/', views.release_form, name='release_create'),
    path('<int:pk>/releases/<int:release_id>/edit/', views.release_form, name='release_edit'),

    path('<int:pk>/modules/create/', views.module_form, name='module_create'),
    path('<int:pk>/modules/<int:module_id>/edit/', views.module_form, name='module_edit'),
    path('<int:pk>/modules/<int:module_id>/delete/', views.module_delete, name='module_delete'),

    path('<int:pk>/testcases/create/', views.testcase_form, name='testcase_create'),
    path('<int:pk>/testcases/<int:case_id>/', views.testcase_detail, name='testcase_detail'),
    path('<int:pk>/testcases/<int:case_id>/edit/', views.testcase_form, name='testcase_edit'),
    path('<int:pk>/testcases/<int:case_id>/delete/', views.testcase_delete, name='testcase_delete'),

    path('<int:pk>/runners/', views.runner_list, name='runner_list'),
    path('<int:pk>/runners/create/', views.runner_create, name='runner_create'),
    path('<int:pk>/runners/<int:runner_id>/', views.runner_execute, name='runner_execute'),
    path('<int:pk>/runners/<int:runner_id>/delete/', views.runner_delete, name='runner_delete'),
    path('<int:pk>/runners/<int:runner_id>/results/<int:result_id>/videos/', views.result_video_upload,
         name='result_video_upload'),
    path('<int:pk>/videos/<int:video_id>/', views.video_watch, name='video_watch'),
    path('<int:pk>/videos/<int:video_id>/delete/', views.result_video_delete, name='result_video_delete'),

    path('<int:pk>/api/', include('apitesting.urls')),
]
