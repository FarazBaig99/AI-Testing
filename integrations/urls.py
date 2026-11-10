from django.urls import path

from . import views

urlpatterns = [
    path('', views.integrations_page, name='integrations_home'),
    path('systems/<int:system_id>/', views.system_detail, name='integrations_system'),
    path('<slug:section>/', views.integrations_page, name='integrations_section'),
]
