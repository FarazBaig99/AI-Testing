from django.urls import path

from . import views

urlpatterns = [
    path('', views.org_home, name='org_home'),
    path('employees/<int:user_id>/', views.org_employee, name='org_employee'),
    path('access/', views.org_access, name='org_access'),
]
