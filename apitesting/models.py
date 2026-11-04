import uuid

from django.contrib.auth.models import User
from django.db import models

from projects.models import Project


class CollectionSource(models.Model):
    """A live API documentation URL (Scalar, OpenAPI/Swagger, Postman) configured for a project."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_sources')
    url = models.URLField(max_length=500)
    is_active = models.BooleanField(default=False)
    auth_header = models.CharField(max_length=500, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.url


class ApiFolder(models.Model):
    """A module of API test cases, one per collection tag / folder."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_folders')
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('project', 'name')

    def __str__(self):
        return self.name


class ApiTestCase(models.Model):
    METHOD_CHOICES = [(m, m) for m in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_test_cases')
    folder = models.ForeignKey(ApiFolder, on_delete=models.CASCADE, related_name='test_cases')
    # METHOD + path template, e.g. "GET /api/v1/holidays/{id}" - used to match APIs on re-sync
    endpoint_key = models.CharField(max_length=600)
    name = models.CharField(max_length=600)
    summary = models.CharField(max_length=600, blank=True)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    path = models.CharField(max_length=500)
    url = models.CharField(max_length=1000)
    content_type = models.CharField(max_length=100, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    path_params = models.JSONField(default=dict, blank=True)
    query_params = models.JSONField(default=dict, blank=True)
    body = models.TextField(blank=True)
    expected_status = models.PositiveIntegerField(default=200)
    fingerprint = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, default='not_run')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['folder__name', 'id']
        unique_together = ('project', 'endpoint_key')

    def __str__(self):
        return self.name

    @property
    def code(self):
        return f'TC-{self.pk}'


class ImportSession(models.Model):
    """Parsed collection waiting on the Review screen before it is applied to the project."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_import_sessions')
    source = models.ForeignKey(CollectionSource, on_delete=models.SET_NULL, null=True, blank=True)
    source_label = models.CharField(max_length=500)
    endpoints = models.JSONField(default=list)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
