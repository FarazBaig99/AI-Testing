from django.contrib.auth.models import User
from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_projects')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name


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
        ('Security', 'Security'), ('UI', 'UI'), ('Performance', 'Performance'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='test_cases')
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='test_cases')
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    preconditions = models.TextField(blank=True)
    steps = models.TextField(help_text='One step per line')
    expected_result = models.TextField()
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='Medium')
    case_type = models.CharField('Label / Type', max_length=30, choices=TYPE_CHOICES, default='Functional')
    automatable = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_cases')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['module__name', 'id']

    def __str__(self):
        return f'#{self.id} {self.title}'


class TestRunner(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='runners')
    release = models.ForeignKey(Release, on_delete=models.SET_NULL, null=True, blank=True, related_name='runners')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    execution_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_runners')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_runners')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def counts(self):
        data = {s: 0 for s, _ in TestResult.STATUS_CHOICES}
        for r in self.results.all():
            data[r.status] += 1
        data['total'] = sum(data[s] for s, _ in TestResult.STATUS_CHOICES)
        # template-friendly aliases (keys with spaces can't be used in templates)
        data['on_hold'] = data['On Hold']
        data['partial'] = data['Partial Pass']
        done = data['total'] - data['Pending']
        data['progress'] = round(done * 100 / data['total']) if data['total'] else 0
        data['pass_rate'] = round(data['Pass'] * 100 / data['total']) if data['total'] else 0
        return data

    @property
    def status(self):
        c = self.counts()
        if c['total'] == 0 or c['Pending'] == c['total']:
            return 'Pending'
        if c['Pending'] == 0:
            return 'Completed'
        return 'In Progress'


class TestResult(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'), ('Pass', 'Pass'), ('Fail', 'Fail'),
        ('Blocked', 'Blocked'), ('On Hold', 'On Hold'), ('Partial Pass', 'Partial Pass'),
    ]

    runner = models.ForeignKey(TestRunner, on_delete=models.CASCADE, related_name='results')
    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='results')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    comment = models.TextField(blank=True)
    executed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['test_case__module__name', 'test_case__id']
        unique_together = ('runner', 'test_case')

    def __str__(self):
        return f'{self.runner} - {self.test_case} - {self.status}'
