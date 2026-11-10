import re

from django.contrib.auth.models import User
from django.db import models


def _lines(text):
    return [line.strip(' -*\t') for line in text.splitlines() if line.strip(' -*\t')]


class Project(models.Model):
    TYPES = ['Web', 'Mobile', 'API / Backend', 'Desktop', 'Hybrid', 'Data / ETL']
    STATUSES = ['Draft', 'Pending Approval', 'Active', 'On Hold', 'Completed', 'Closed', 'Archived']
    # lifecycle state machine: status -> statuses it may move to (every move needs a reason)
    TRANSITIONS = {
        'Draft': ['Pending Approval', 'Active', 'Archived'],
        'Pending Approval': ['Active', 'Draft'],
        'Active': ['On Hold', 'Completed'],
        'On Hold': ['Active', 'Closed'],
        'Completed': ['Closed', 'Active'],
        'Closed': ['Archived', 'Active'],
        'Archived': ['Active'],
    }
    CRITICALITIES = ['Critical', 'High', 'Medium', 'Low']
    VISIBILITIES = [('restricted', 'Restricted (assigned team only)'), ('organization', 'Organization-wide')]
    DELIVERY_MODELS = ['Agile Scrum', 'Kanban', 'Waterfall', 'Hybrid']

    name = models.CharField(max_length=200)
    key = models.CharField('Unique Project Key', max_length=12, unique=True,
                           help_text='2-12 capital letters or digits, e.g. ACS, PSW')
    description = models.TextField(blank=True)
    project_type = models.CharField('Project Type', max_length=20, choices=[(t, t) for t in TYPES], default='Web')
    status = models.CharField('Lifecycle Status', max_length=20, choices=[(s, s) for s in STATUSES], default='Draft')
    criticality = models.CharField('Business Criticality', max_length=10,
                                   choices=[(c, c) for c in CRITICALITIES], default='Medium')
    visibility = models.CharField(max_length=20, choices=VISIBILITIES, default='restricted')
    tags = models.CharField('Tags / Tech Stack', max_length=300, blank=True, help_text='Comma separated')
    delivery_model = models.CharField(max_length=20, choices=[(d, d) for d in DELIVERY_MODELS],
                                      default='Agile Scrum')
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField('Target completion', null=True, blank=True)
    sponsor = models.CharField('Business Owner / Sponsor', max_length=200, blank=True)
    manager = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='managed_projects', verbose_name='Project Manager')
    qa_lead = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='qa_led_projects', verbose_name='QA Lead')
    ba_lead = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='ba_led_projects', verbose_name='Business Analyst Lead')
    # scope lists: one item per line
    objective = models.TextField('Business Objective', blank=True)
    in_scope = models.TextField('In-Scope Items', blank=True)
    out_of_scope = models.TextField('Out-of-Scope Items', blank=True)
    deliverables = models.TextField(blank=True)
    success_criteria = models.TextField('Success Criteria', blank=True)
    # symmetrical: linking A -> B also shows A as linked on B
    linked_projects = models.ManyToManyField('self', blank=True, symmetrical=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_projects')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def tag_list(self):
        return [t.strip() for t in self.tags.split(',') if t.strip()]

    def in_scope_list(self):
        return _lines(self.in_scope)

    def out_of_scope_list(self):
        return _lines(self.out_of_scope)

    def deliverable_list(self):
        return _lines(self.deliverables)

    def success_criteria_list(self):
        return _lines(self.success_criteria)

    def next_statuses(self):
        return self.TRANSITIONS.get(self.status, [])


class ProjectMember(models.Model):
    ROLES = ['Viewer', 'Tester', 'Developer', 'Lead', 'Project Admin']

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_memberships')
    role = models.CharField(max_length=20, choices=[(r, r) for r in ROLES], default='Tester')
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['user__username']
        unique_together = ('project', 'user')

    def __str__(self):
        return f'{self.user} ({self.role})'


class Milestone(models.Model):
    STATUSES = ['Pending', 'In Progress', 'Completed']

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    title = models.CharField(max_length=200)
    target_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[(s, s) for s in STATUSES], default='Pending')

    class Meta:
        ordering = [models.F('target_date').asc(nulls_last=True), 'id']

    def __str__(self):
        return self.title


class ProjectActivity(models.Model):
    """Append-only audit trail of a project: nothing in the app edits or deletes these rows."""
    CATEGORIES = [('created', 'Created'), ('scope', 'Profile & Scope'), ('team', 'Team & Access'),
                  ('milestone', 'Milestones'), ('status', 'Lifecycle'), ('settings', 'Settings'),
                  ('delivery', 'Project Delivery'), ('integration', 'External Integrations')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='activities')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    category = models.CharField(max_length=20, choices=CATEGORIES)
    message = models.CharField(max_length=500)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return self.message


class Release(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='releases')
    name = models.CharField(max_length=100, help_text='e.g. v1.2.0 or Release 1')
    description = models.TextField(blank=True)
    release_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('project', 'name')

    def __str__(self):
        return self.name


class Module(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='modules')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.parent.name} / {self.name}' if self.parent else self.name


class TestCase(models.Model):
    PRIORITY_CHOICES = [('Low', 'Low'), ('Medium', 'Medium'), ('High', 'High'), ('Critical', 'Critical')]
    TYPE_CHOICES = [
        ('Functional', 'Functional'), ('Negative', 'Negative'), ('Positive', 'Positive'),
        ('Regression', 'Regression'), ('Smoke', 'Smoke'), ('Integration', 'Integration'),
        ('Security', 'Security'), ('UI', 'UI'), ('Performance', 'Performance'), ('API', 'API'),
        ('Database', 'Database'), ('E2E', 'E2E'), ('Usability', 'Usability'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='test_cases')
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='test_cases')
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    preconditions = models.TextField(blank=True)
    steps = models.TextField(help_text='One step per line. Optional per-step expected result: Step -> Expected')
    expected_result = models.TextField()
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='Medium')
    # a case can have several types at once, e.g. ['Functional', 'Smoke'] or ['Functional', 'API']
    case_types = models.JSONField('Label / Type', default=list, blank=True)
    automatable = models.BooleanField(default=False)
    rtm_requirement = models.CharField('RTM Requirement', max_length=300, blank=True,
                                       help_text='e.g. View Airline Organization - 1.1.1 - 1')
    rtm_details = models.TextField('RTM Requirement Details', blank=True,
                                   help_text='User story / scenario of the linked requirement')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_cases')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['module__name', 'id']

    def __str__(self):
        return f'#{self.id} {self.title}'

    def step_list(self):
        """[(step, expected)] - a step line may carry its own expected result as 'step -> expected'."""
        rows = []
        for line in self.steps.splitlines():
            line = re.sub(r'^\s*\d+[.)]\s*', '', line).strip()
            if line:
                step, _, expected = line.partition('->')
                rows.append((step.strip(), expected.strip()))
        return rows


class TestRunner(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='runners')
    release = models.ForeignKey(Release, on_delete=models.SET_NULL, null=True, blank=True, related_name='runners')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    execution_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_runners')
    test_cases = models.ManyToManyField(TestCase, blank=True, related_name='runners')
    # environment the execution starts on; results are kept separately per environment
    environment = models.ForeignKey('apitesting.Environment', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='runners')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_runners')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def counts(self, results=None):
        """Status counts for the given results (default: all environments together)."""
        data = {s: 0 for s, _ in TestResult.STATUS_CHOICES}
        for r in (self.results.all() if results is None else results):
            data[r.status] += 1
        data['total'] = sum(data[s] for s, _ in TestResult.STATUS_CHOICES)
        # template-friendly aliases (keys with spaces can't be used in templates)
        data['on_hold'] = data['On Hold']
        data['partial'] = data['Partial Pass']
        done = data['total'] - data['Pending']
        data['progress'] = round(done * 100 / data['total']) if data['total'] else 0
        data['pass_rate'] = round(data['Pass'] * 100 / data['total']) if data['total'] else 0
        return data

    @staticmethod
    def status_of(c):
        if c['total'] == 0 or c['Pending'] == c['total']:
            return 'Pending'
        if c['Pending'] == 0:
            return 'Completed'
        return 'In Progress'

    @property
    def status(self):
        return self.status_of(self.counts())

    def environment_progress(self):
        """[{'environment', 'counts', 'status'}] for every environment this runner was executed on."""
        by_env = {}
        for r in self.results.all():
            by_env.setdefault(r.environment_id, []).append(r)
        rows = []
        for results in by_env.values():
            c = self.counts(results)
            rows.append({'environment': results[0].environment, 'counts': c, 'status': self.status_of(c)})
        return sorted(rows, key=lambda row: row['environment'].name if row['environment'] else '')


class TestResult(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'), ('Pass', 'Pass'), ('Fail', 'Fail'),
        ('Blocked', 'Blocked'), ('On Hold', 'On Hold'), ('Partial Pass', 'Partial Pass'),
    ]
    REASON_REQUIRED = ('Blocked', 'On Hold', 'Partial Pass')
    # IEEE 1044 anomaly classification
    ANOMALY_TYPES = ['Functionality / Logic', 'Interface', 'Data', 'Performance', 'Usability / UI', 'Security',
                     'Configuration / Environment', 'Documentation', 'Other']
    SEVERITIES = ['Blocking', 'Critical', 'Major', 'Minor', 'Inconsequential']
    PHASES = ['Requirements', 'Design', 'Implementation', 'Testing', 'Deployment', 'Operation / Maintenance']
    RESOLUTION_CODES = ['Fixed', 'Deferred', 'Duplicate', 'Not a Defect', 'Cannot Reproduce', 'Works as Designed',
                        "Won't Fix"]

    runner = models.ForeignKey(TestRunner, on_delete=models.CASCADE, related_name='results')
    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='results')
    environment = models.ForeignKey('apitesting.Environment', on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='test_results')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    comment = models.TextField('Additional Details', blank=True)
    bug_id = models.CharField('Bug/Defect ID', max_length=100, blank=True)
    failure_description = models.TextField(blank=True)
    anomaly_type = models.CharField(max_length=50, blank=True, choices=[(c, c) for c in ANOMALY_TYPES])
    severity = models.CharField(max_length=30, blank=True, choices=[(c, c) for c in SEVERITIES])
    origin_phase = models.CharField(max_length=50, blank=True, choices=[(c, c) for c in PHASES])
    detection_phase = models.CharField(max_length=50, blank=True, choices=[(c, c) for c in PHASES])
    resolution_code = models.CharField(max_length=50, blank=True, choices=[(c, c) for c in RESOLUTION_CODES])
    evidence_notes = models.TextField(blank=True)
    reason = models.TextField(blank=True)
    executed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['test_case__module__name', 'test_case__id']
        unique_together = ('runner', 'test_case', 'environment')

    def __str__(self):
        return f'{self.runner} - {self.test_case} - {self.environment} - {self.status}'


class ResultAttachment(models.Model):
    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='evidence/%Y/%m/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']

    @property
    def name(self):
        return self.file.name.rsplit('/', 1)[-1]


class ResultVideo(models.Model):
    """Screen recording of the steps performed while executing a result ('Video Steps')."""
    SOURCE_CHOICES = [('web', 'Website'), ('desktop', 'Desktop Software')]

    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name='videos')
    file = models.FileField(upload_to='videos/%Y/%m/')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='web')
    start_url = models.URLField(max_length=1000, blank=True)
    app_name = models.CharField('Software / application', max_length=200, blank=True)
    duration = models.PositiveIntegerField(default=0, help_text='seconds')
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']

    @property
    def duration_display(self):
        return f'{self.duration // 60}:{self.duration % 60:02d}'


class ResultHistory(models.Model):
    """Execution timeline: one row per status change / saved update of a result."""
    result = models.ForeignKey(TestResult, on_delete=models.CASCADE, related_name='history')
    old_status = models.CharField(max_length=20)
    new_status = models.CharField(max_length=20)
    note = models.CharField(max_length=100)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    changed_at = models.DateTimeField()

    class Meta:
        ordering = ['-changed_at', '-id']
