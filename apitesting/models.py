import uuid

from django.contrib.auth.models import User
from django.db import models

from projects.models import Project


BASE_URL_VAR = '{{baseUrl}}'


class Environment(models.Model):
    """A deployment of the project's APIs (QA, UAT, Prod...). APIs, modules and collection URLs live inside one."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_environments')
    name = models.CharField(max_length=100)
    base_url = models.URLField('Base URL', max_length=500)
    variables = models.JSONField(default=list, blank=True)    # [{key, value, enabled}] for {{name}} and pm.environment
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        unique_together = ('project', 'name')

    def __str__(self):
        return self.name

    def resolve(self, text):
        """Replace {{baseUrl}} with this environment's base URL."""
        return text.replace(BASE_URL_VAR, self.base_url.rstrip('/')) if isinstance(text, str) else text

    def relativize(self, url):
        """https://env-base/api/x -> {{baseUrl}}/api/x when the URL belongs to this environment."""
        base = self.base_url.rstrip('/')
        if url == base or url.startswith(base + '/'):
            return BASE_URL_VAR + url[len(base):]
        return url


class CollectionSource(models.Model):
    """A live API documentation URL (Scalar, OpenAPI/Swagger, Postman) configured for an environment."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_sources')
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, null=True, related_name='sources')
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
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, null=True, related_name='folders')
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('environment', 'name')

    def __str__(self):
        return self.name


class ApiTestCase(models.Model):
    METHOD_CHOICES = [(m, m) for m in ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_test_cases')
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, null=True, related_name='test_cases')
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
    # what the API Testing builder saved for this API (params, headers, auth, body, scripts...); empty = as synced
    spec = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, default='not_run',
                              choices=[('not_run', 'Not Run'), ('pass', 'Pass'), ('fail', 'Fail')])
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['folder__name', 'id']
        unique_together = ('environment', 'endpoint_key')

    def __str__(self):
        return self.name

    @property
    def code(self):
        return f'TC-{self.pk}'


class ImportSession(models.Model):
    """Parsed collection waiting on the Review screen before it is applied to the project."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='api_import_sessions')
    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, null=True)
    source = models.ForeignKey(CollectionSource, on_delete=models.SET_NULL, null=True, blank=True)
    source_label = models.CharField(max_length=500)
    endpoints = models.JSONField(default=list)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# ---------- API Workspace (Postman-style) ----------

BODY_MODES = [('none', 'none'), ('form-data', 'form-data'), ('urlencoded', 'x-www-form-urlencoded'),
              ('raw', 'raw'), ('binary', 'binary'), ('graphql', 'GraphQL')]
RAW_LANGUAGES = [('text', 'Text'), ('javascript', 'JavaScript'), ('json', 'JSON'), ('html', 'HTML'), ('xml', 'XML')]


class ApiCollection(models.Model):
    """A named set of requests; may hold APIs of several projects and environments."""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    variables = models.JSONField(default=list, blank=True)    # pm.collectionVariables
    pre_request_script = models.TextField(blank=True)         # runs before every request of the collection
    test_script = models.TextField(blank=True)                # runs after every response of the collection
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CollectionRequest(models.Model):
    """A saved request inside a collection. Starts as a copy of a synced API; edits never touch the API."""
    collection = models.ForeignKey(ApiCollection, on_delete=models.CASCADE, related_name='requests')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    environment = models.ForeignKey(Environment, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    source = models.ForeignKey(ApiTestCase, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    name = models.CharField(max_length=600)
    method = models.CharField(max_length=10, default='GET')
    url = models.CharField(max_length=2000)
    params = models.JSONField(default=list, blank=True)      # [{key, value, enabled}]
    headers = models.JSONField(default=list, blank=True)     # [{key, value, enabled}]
    auth = models.JSONField(default=dict, blank=True)        # {type: none|bearer|basic|apikey, ...}
    body_mode = models.CharField(max_length=20, choices=BODY_MODES, default='none')
    raw_language = models.CharField(max_length=20, choices=RAW_LANGUAGES, default='json')
    body = models.TextField(blank=True)                      # raw body
    form_data = models.JSONField(default=list, blank=True)   # [{key, value, type: text|file, enabled}]
    urlencoded = models.JSONField(default=list, blank=True)  # [{key, value, enabled}]
    graphql_query = models.TextField(blank=True)
    graphql_variables = models.TextField(blank=True)
    pre_request_script = models.TextField(blank=True)
    test_script = models.TextField(blank=True)               # post-response script (pm.test ...)
    verify_ssl = models.BooleanField(default=True)
    # None: the synced API's expected status, else any 2xx
    expected_status = models.PositiveIntegerField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)
    last_status = models.PositiveIntegerField(null=True, blank=True)
    last_ms = models.PositiveIntegerField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return self.name


# ---------- Collection runner, schedules and mail ----------

RUN_KINDS = [('functional', 'Functional'), ('performance', 'Performance')]


class MailSettings(models.Model):
    """SMTP server used for run reports (one row)."""
    host = models.CharField('SMTP host', max_length=255, blank=True)
    port = models.PositiveIntegerField('Port', default=587)
    security = models.CharField(max_length=10, default='tls',
                                choices=[('tls', 'STARTTLS'), ('ssl', 'SSL/TLS'), ('none', 'None')])
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)
    from_email = models.CharField('From address', max_length=255, blank=True)
    site_url = models.CharField('BaigNova URL (for report links)', max_length=255, default='http://127.0.0.1:8000')
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        return cls.objects.order_by('pk').first() or cls()

    @property
    def is_configured(self):
        return bool(self.host and (self.from_email or self.username))

    @property
    def masked_password(self):
        return '•' * 8 if self.password else ''


class RunSchedule(models.Model):
    FREQUENCIES = [('interval', 'Every N minutes'), ('hourly', 'Hourly'), ('daily', 'Daily'),
                   ('weekly', 'Weekly'), ('cron', 'Cron expression')]
    EMAIL_WHEN = [('always', 'After every run'), ('failure', 'Only when it fails'), ('never', 'Do not email')]

    collection = models.ForeignKey(ApiCollection, on_delete=models.CASCADE, related_name='schedules')
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=RUN_KINDS, default='functional')
    config = models.JSONField(default=dict, blank=True)       # same options as a manual run
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default='daily')
    interval_minutes = models.PositiveIntegerField(default=60)
    minute = models.PositiveIntegerField(default=0)           # hourly: minute past the hour
    time = models.TimeField(null=True, blank=True)            # daily / weekly
    weekdays = models.JSONField(default=list, blank=True)     # weekly: 0=Mon .. 6=Sun
    cron = models.CharField(max_length=100, blank=True)
    emails = models.TextField(blank=True)
    email_when = models.CharField(max_length=10, choices=EMAIL_WHEN, default='always')
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CollectionRun(models.Model):
    STATUSES = [('queued', 'Queued'), ('running', 'Running'), ('passed', 'Passed'), ('failed', 'Failed'),
                ('stopped', 'Stopped'), ('error', 'Error')]

    collection = models.ForeignKey(ApiCollection, on_delete=models.CASCADE, related_name='runs')
    schedule = models.ForeignKey(RunSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name='runs')
    kind = models.CharField(max_length=20, choices=RUN_KINDS, default='functional')
    trigger = models.CharField(max_length=10, default='manual', choices=[('manual', 'Manual'), ('schedule', 'Schedule')])
    status = models.CharField(max_length=10, choices=STATUSES, default='queued')
    config = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    stop_requested = models.BooleanField(default=False)
    emails = models.TextField(blank=True)
    email_when = models.CharField(max_length=10, default='always')
    email_status = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.collection} {self.kind} run #{self.pk}'

    @property
    def is_active(self):
        return self.status in ('queued', 'running')

    @property
    def duration_s(self):
        if not self.started_at:
            return None
        from django.utils import timezone
        return round(((self.finished_at or timezone.now()) - self.started_at).total_seconds(), 1)


class RunResult(models.Model):
    """One request sent during a functional run."""
    run = models.ForeignKey(CollectionRun, on_delete=models.CASCADE, related_name='results')
    request = models.ForeignKey(CollectionRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    iteration = models.PositiveIntegerField(default=1)
    position = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=600)
    method = models.CharField(max_length=10)
    url = models.CharField(max_length=2000, blank=True)
    expected = models.CharField(max_length=20, blank=True)
    status_code = models.PositiveIntegerField(null=True, blank=True)
    ms = models.PositiveIntegerField(null=True, blank=True)
    size = models.PositiveIntegerField(null=True, blank=True)
    passed = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    response_headers = models.JSONField(default=list, blank=True)
    response_body = models.TextField(blank=True)
    tests = models.JSONField(default=list, blank=True)       # [{name, passed, error}] from the scripts
    console = models.JSONField(default=list, blank=True)     # console.log lines of the scripts
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class ApiGlobals(models.Model):
    """Global variables (pm.globals) and the cookie jar, one row for the whole workspace."""
    variables = models.JSONField(default=list, blank=True)
    cookies = models.JSONField(default=list, blank=True)     # cookie jar, like Postman's (per domain)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        return cls.objects.order_by('pk').first() or cls.objects.create()


class AuthProfile(models.Model):
    """One way of logging in to an environment before calling its APIs (e.g. Keycloak behind a session cookie).

    An environment can have several: "Application client", "Backoffice client", "Integration client"... Each keeps
    its own session cookies, and every request picks the one it needs (or the environment's default).
    """
    KINDS = [('none', 'No login'), ('keycloak', 'Keycloak / OpenID Connect login (session cookie)')]

    environment = models.ForeignKey(Environment, on_delete=models.CASCADE, related_name='auth_profiles')
    name = models.CharField(max_length=100, default='Default')
    is_default = models.BooleanField(default=False)   # used by requests that do not pick a login
    kind = models.CharField(max_length=20, choices=KINDS, default='none')
    login_url = models.CharField(max_length=1000, default='{{baseUrl}}/login')
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)
    check_url = models.CharField('Login check URL', max_length=1000, blank=True)
    auto_relogin = models.BooleanField(default=True)
    verify_ssl = models.BooleanField(default=True)
    last_status = models.CharField(max_length=20, default='Never')
    last_message = models.TextField(blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    cookies = models.JSONField(default=list, blank=True)     # this login's own session cookies (per domain)

    class Meta:
        ordering = ['-is_default', 'name', 'id']
        constraints = [models.UniqueConstraint(fields=['environment', 'name'], name='apitesting_authprofile_env_name')]

    def __str__(self):
        return f'{self.environment} · {self.name}'

    @property
    def is_active(self):
        return self.kind == 'keycloak' and bool(self.username and self.password)


class ProjectVariables(models.Model):
    """Project-level variables: {{name}} works in every environment of the project (between environment and globals)."""
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='api_variables')
    variables = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'project variables'

    def __str__(self):
        return f'{self.project} variables'

    @classmethod
    def for_project(cls, project):
        if project is None:
            return None
        return cls.objects.get_or_create(project=project)[0]


class ApiLov(models.Model):
    """A list-of-values generator: {{name}} is filled with a fresh value on every send (random name, email,
    number, date +/- offset, counter, pick from a list...). Works in every project and environment."""
    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=30)
    options = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    state = models.JSONField(default=dict, blank=True)          # counters of sequence / sequential list LOVs
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'LOV'

    def __str__(self):
        return self.name
