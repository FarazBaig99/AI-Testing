from django.urls import path

from . import runs, workspace

urlpatterns = [
    path('', workspace.api_workspace, name='api_workspace'),
    path('request/', workspace.api_ws_request, name='api_ws_request'),
    path('send/', workspace.api_ws_send, name='api_ws_send'),
    path('save/', workspace.api_ws_save, name='api_ws_save'),
    path('move/', workspace.api_ws_move, name='api_ws_move'),
    path('rename/', workspace.api_ws_rename, name='api_ws_rename'),
    path('variables/', workspace.api_ws_variables, name='api_ws_variables'),
    path('variables/resolve/', workspace.api_ws_resolve, name='api_ws_resolve'),
    path('variables/set/', workspace.api_ws_variable_set, name='api_ws_variable_set'),
    path('lov/', workspace.api_lov_page, name='api_lov_page'),
    path('lov/data/', workspace.api_ws_lov, name='api_ws_lov'),
    path('lov/template.csv', workspace.api_lov_template, name='api_lov_template'),
    path('auth/', workspace.api_ws_auth, name='api_ws_auth'),
    path('cookies/', workspace.api_ws_cookies, name='api_ws_cookies'),
    path('collections/<int:collection_id>/settings/', workspace.api_ws_collection_settings,
         name='api_ws_collection_settings'),
    path('collections/<int:collection_id>/run/', runs.collection_run, name='api_collection_run'),
    path('runs/<int:run_id>/', runs.run_detail, name='api_run_detail'),
    path('runs/<int:run_id>/status/', runs.run_status, name='api_run_status'),
    path('runs/<int:run_id>/stop/', runs.run_stop, name='api_run_stop'),
    path('mail-settings/', runs.mail_settings, name='api_mail_settings'),
]
