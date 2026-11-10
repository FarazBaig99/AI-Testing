from django.urls import path

from . import views

# mounted under /portfolio/<pk>/delivery/
urlpatterns = [
    path('', views.delivery_page, name='delivery_home'),
    path('sync/', views.delivery_sync, name='delivery_sync'),
    path('sprints/<int:sprint_id>/', views.delivery_sprint, name='delivery_sprint'),
    path('qa/<int:item_id>/', views.delivery_qa, name='delivery_qa'),
    path('<slug:section>/', views.delivery_page, name='delivery_section'),
]
