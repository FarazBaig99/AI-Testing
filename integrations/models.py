"""External Integrations of a project: partner systems with connection tests, links to other BaigNova projects,
and source repositories with their deployments, pull requests and pipelines."""
from django.contrib.auth.models import User
from django.db import models

from apitesting.models import Environment
from projects.models import Project


def mask(secret):
    return '••••' + secret[-4:] if len(secret) > 8 else ('••••' if secret else '')


class SystemIntegration(models.Model):
    """A partner system the project exchanges data with (a bank, an airline, PSW...)."""
    PARTNER_TYPES = ['Bank', 'Airline', 'Ground Handling (GHA)', 'Government', 'Payment Gateway', 'Logistics',
                     'Internal System', 'Other']
    PROTOCOLS = ['REST', 'SOAP', 'GraphQL', 'Kafka', 'MQ', 'SFTP', 'Database', 'Other']
    DIRECTIONS = ['Outbound', 'Inbound', 'Two-way']
    METHODS = ['GET', 'HEAD', 'POST']
    AUTH_TYPES = [('none', 'No auth'), ('basic', 'Basic (user / password)'), ('bearer', 'Bearer token'),
                  ('header', 'API key header')]
    STATUSES = ['Never tested', 'Passed', 'Failed']

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='system_integrations')
    name = models.CharField(max_length=150, help_text='e.g. HBL Payment Gateway, Emirates Cargo, PSW Single Window')
    partner_type = models.CharField(max_length=40, choices=[(t, t) for t in PARTNER_TYPES], default='Other')
    protocol = models.CharField(max_length=20, choices=[(p, p) for p in PROTOCOLS], default='REST')
    direction = models.CharField(max_length=10, choices=[(d, d) for d in DIRECTIONS], default='Two-way')
    environment = models.ForeignKey(Environment, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='system_integrations')
    endpoint = models.CharField(max_length=500, help_text='A URL (https://...) is called; host:port is checked for '
                                                          'reachability (Kafka, MQ, SFTP, databases).')
    check_method = models.CharField('Test method', max_length=6, choices=[(m, m) for m in METHODS], default='GET')
    expected_status = models.CharField(max_length=60, blank=True,
                                       help_text='HTTP codes that mean "connected", e.g. 200 or 200,401. '
                                                 'Empty: any response below 500.')
    auth_type = models.CharField(max_length=10, choices=AUTH_TYPES, default='none')
    auth_user = models.CharField('User / header name', max_length=200, blank=True)
    auth_secret = models.CharField('Password / token', max_length=500, blank=True)
    timeout = models.PositiveSmallIntegerField('Timeout (seconds)', default=10)
    contact = models.CharField('Partner contact', max_length=200, blank=True)
    description = models.TextField(blank=True)
    last_status = models.CharField(max_length=20, default='Never tested')
    last_message = models.CharField(max_length=500, blank=True)
    last_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_ms = models.PositiveIntegerField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def masked_secret(self):
        return mask(self.auth_secret)

    @property
    def is_url(self):
        return self.endpoint.lower().startswith(('http://', 'https://'))


class ConnectionCheck(models.Model):
    integration = models.ForeignKey(SystemIntegration, on_delete=models.CASCADE, related_name='checks')
    checked_at = models.DateTimeField(auto_now_add=True)
    checked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    passed = models.BooleanField()
    code = models.PositiveSmallIntegerField(null=True, blank=True)
    ms = models.PositiveIntegerField(null=True, blank=True)
    message = models.CharField(max_length=500)

    class Meta:
        ordering = ['-checked_at', '-id']


class ProjectLink(models.Model):
    """ACS <-> PACE / WeBOC / PSW: projects in BaigNova that exchange data. Stored once, shown on both projects."""
    RELATIONS = [('sends', 'Sends data to'), ('receives', 'Receives data from'), ('both', 'Two-way exchange')]
    REVERSE = {'sends': 'receives', 'receives': 'sends', 'both': 'both'}

    source = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='links_out')
    target = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='links_in')
    relation = models.CharField(max_length=10, choices=RELATIONS, default='both')
    interface = models.CharField(max_length=200, blank=True, help_text='e.g. REST API, Kafka topic, SFTP files')
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('source', 'target')
        ordering = ['created_at']

    def view_from(self, project):
        """(the other project, relation label as seen from `project`)."""
        if project.pk == self.source_id:
            return self.target, self.get_relation_display()
        return self.source, dict(self.RELATIONS)[self.REVERSE[self.relation]]


class Repository(models.Model):
    PROVIDERS = [('github', 'GitHub'), ('gitlab', 'GitLab'), ('azure', 'Azure DevOps'), ('demo', 'Demo Repository')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='repositories')
    provider = models.CharField(max_length=10, choices=PROVIDERS)
    url = models.CharField('Repository URL', max_length=500)
    api_url = models.CharField('API base URL', max_length=300, blank=True,
                               help_text='Only for self-hosted servers, e.g. https://gitlab.company.com')
    token = models.CharField('Access token', max_length=500, blank=True)
    name = models.CharField(max_length=300, blank=True)
    default_branch = models.CharField(max_length=200, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=20, default='Never synced')
    last_error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.name or self.url

    @property
    def masked_token(self):
        return mask(self.token)


class Deployment(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='deployments')
    environment_name = models.CharField(max_length=200)
    ref = models.CharField(max_length=200, blank=True)
    sha = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=40, blank=True)
    deployed_at = models.DateTimeField(null=True, blank=True)
    deployed_by = models.CharField(max_length=200, blank=True)
    url = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-deployed_at', '-id']


class PullRequest(models.Model):
    STATES = ['open', 'merged', 'closed']

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='pull_requests')
    number = models.CharField(max_length=20)
    title = models.CharField(max_length=500)
    state = models.CharField(max_length=10)
    author = models.CharField(max_length=200, blank=True)
    source_branch = models.CharField(max_length=200, blank=True)
    target_branch = models.CharField(max_length=200, blank=True)
    url = models.CharField(max_length=500, blank=True)
    created_on = models.DateTimeField(null=True, blank=True)
    updated_on = models.DateTimeField(null=True, blank=True)
    merged_on = models.DateTimeField(null=True, blank=True)
    additions = models.PositiveIntegerField(default=0)
    deletions = models.PositiveIntegerField(default=0)
    files = models.JSONField(default=list, blank=True)  # [{path, status, additions, deletions}]

    class Meta:
        ordering = ['-updated_on', '-id']


class Pipeline(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='pipelines')
    name = models.CharField(max_length=300)
    status = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=200, blank=True)
    run_at = models.DateTimeField(null=True, blank=True)
    url = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['name']


class VersionEndpoint(models.Model):
    """The /version or /health URL of a project environment: tells which build is really running there."""
    environment = models.OneToOneField(Environment, on_delete=models.CASCADE, related_name='version_endpoint')
    url = models.CharField(max_length=500)
    version_key = models.CharField('JSON key of the version', max_length=100, blank=True,
                                   help_text='e.g. version, build.commit. Empty: the whole response text.')
    last_value = models.CharField(max_length=200, blank=True)
    last_status = models.CharField(max_length=20, default='Never checked')
    last_message = models.CharField(max_length=500, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
