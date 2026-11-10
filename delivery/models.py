"""Project Delivery: data synced from external project management tools (Jira, ClickUp, ...).

Layer 1 is Connection / SyncRun / SyncChange (where data comes from and the history of every sync).
Layer 2 is the normalized delivery data every tool is mapped to: Release, Sprint, WorkItem, Dependency, TeamMember.
"""
from django.contrib.auth.models import User
from django.db import models

from projects.models import Project


class Connection(models.Model):
    """A link between a BaigNova project and one project in an external tool."""
    TOOLS = [('jira', 'Jira'), ('clickup', 'ClickUp'), ('azure', 'Azure DevOps'), ('linear', 'Linear'),
             ('monday', 'Monday.com'), ('demo', 'Demo Source')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='delivery_connections')
    tool = models.CharField(max_length=20, choices=TOOLS)
    base_url = models.CharField('Site URL', max_length=300, blank=True)
    account = models.CharField('Account / email', max_length=200, blank=True)
    token = models.CharField('API token', max_length=500, blank=True)
    # the project / space / team picked in the tool; only its data is synced
    external_id = models.CharField(max_length=200, blank=True)
    external_name = models.CharField(max_length=300, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=20, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.get_tool_display()} · {self.external_name or "not linked"}'

    @property
    def is_linked(self):
        return bool(self.external_id)

    @property
    def masked_token(self):
        return f'••••{self.token[-4:]}' if len(self.token) > 8 else ('••••' if self.token else '')


class SyncedModel(models.Model):
    """Fields every synced record has; `removed` marks records that disappeared from the tool."""
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='+')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='+')
    external_id = models.CharField(max_length=200)
    external_key = models.CharField(max_length=100, blank=True)
    url = models.URLField(max_length=500, blank=True)
    removed = models.BooleanField(default=False)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TeamMember(SyncedModel):
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['name']
        unique_together = [('connection', 'external_id')]

    def __str__(self):
        return self.name


class Release(SyncedModel):
    STATUSES = ['Planned', 'In Progress', 'Released', 'Archived']

    name = models.CharField(max_length=300)
    status = models.CharField(max_length=20, default='Planned')
    start_date = models.DateField(null=True, blank=True)
    release_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = [models.F('release_date').asc(nulls_last=True), 'name']
        unique_together = [('connection', 'external_id')]

    def __str__(self):
        return self.name


class Sprint(SyncedModel):
    STATES = ['future', 'active', 'closed']

    name = models.CharField(max_length=300)
    state = models.CharField(max_length=20, default='future')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    goal = models.TextField(blank=True)
    release = models.ForeignKey(Release, on_delete=models.SET_NULL, null=True, blank=True, related_name='sprints')

    class Meta:
        ordering = [models.F('start_date').asc(nulls_last=True), 'name']
        unique_together = [('connection', 'external_id')]

    def __str__(self):
        return self.name


class WorkItem(SyncedModel):
    """Epic, user story, task, subtask or bug. `kind` is decided by the connection's type mapping."""
    KINDS = [('epic', 'Epic'), ('story', 'User Story'), ('task', 'Task'), ('subtask', 'Subtask'), ('bug', 'Bug')]
    # every tool's own status is kept as is; `category` groups it for metrics
    CATEGORIES = [('todo', 'To Do'), ('in_progress', 'In Progress'), ('done', 'Done')]
    # QA verification of user stories, done in BaigNova (the tools do not have it)
    QA_STATUSES = ['Not Tested', 'Passed', 'Failed', 'Blocked']

    kind = models.CharField(max_length=20, choices=KINDS)
    source_type = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=500)
    status = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=20, choices=CATEGORIES, default='todo')
    priority = models.CharField(max_length=50, blank=True)
    severity = models.CharField(max_length=50, blank=True)
    story_points = models.FloatField(null=True, blank=True)
    assignee = models.ForeignKey(TeamMember, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    release = models.ForeignKey(Release, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    sprint = models.ForeignKey(Sprint, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    labels = models.CharField(max_length=500, blank=True)
    created_on = models.DateField(null=True, blank=True)
    updated_on = models.DateField(null=True, blank=True)
    resolved_on = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    # set by QA in BaigNova, never by a sync
    qa_status = models.CharField('QA status', max_length=20, choices=[(s, s) for s in QA_STATUSES], default='Not Tested')
    qa_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    qa_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['external_key', 'id']
        unique_together = [('connection', 'external_id')]

    def __str__(self):
        return f'{self.external_key} {self.title}'.strip()

    @property
    def is_done(self):
        return self.category == 'done'


class Dependency(models.Model):
    """`source` blocks `target` (or just relates to it)."""
    TYPES = [('blocks', 'Blocks'), ('relates', 'Relates to')]

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='+')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='+')
    source = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name='blocking')
    target = models.ForeignKey(WorkItem, on_delete=models.CASCADE, related_name='blocked_by')
    type = models.CharField(max_length=20, choices=TYPES, default='blocks')

    class Meta:
        unique_together = [('source', 'target', 'type')]


class SyncRun(models.Model):
    STATUSES = ['Running', 'Success', 'Warning', 'Failed']

    connection = models.ForeignKey(Connection, on_delete=models.SET_NULL, null=True, related_name='runs')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='delivery_sync_runs')
    tool = models.CharField(max_length=20, choices=Connection.TOOLS)
    source_name = models.CharField(max_length=300, blank=True)
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='+')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='Running')
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    removed = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)  # records fetched per entity, e.g. {"Sprint": 6}
    warnings = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at', '-id']

    @property
    def duration(self):
        return (self.finished_at - self.started_at).total_seconds() if self.finished_at else None


class SyncChange(models.Model):
    """One created / updated / removed record in a sync, with the fields that changed (old → new)."""
    run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name='changes')
    entity = models.CharField(max_length=30)
    external_key = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=500)
    change = models.CharField(max_length=20)
    fields = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['id']
