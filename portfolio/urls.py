from django.urls import include, path

from . import views

urlpatterns = [
    path('', views.portfolio_list, name='portfolio_list'),
    path('create/', views.portfolio_create, name='portfolio_create'),
    path('<int:pk>/', views.portfolio_workspace, name='portfolio_workspace'),
    path('<int:pk>/delivery/', include('delivery.urls')),
    path('<int:pk>/integrations/', include('integrations.urls')),
    path('<int:pk>/<slug:tab>/', views.portfolio_workspace, name='portfolio_workspace'),
]
