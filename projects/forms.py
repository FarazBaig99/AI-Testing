from django import forms
from django.contrib.auth.models import User

from .models import Module, Project, Release, TestCase, TestRunner


class StyledForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = 'form-check-input' if isinstance(field.widget, forms.CheckboxInput) else 'form-control'
            if isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                css = 'form-select'
            field.widget.attrs.setdefault('class', css)

    def make_searchable(self, *names, placeholder='Search...'):
        """Turn these dropdowns into type-to-search selects (see base.html)."""
        for name in names:
            self.fields[name].widget.attrs.update({'class': 'form-select searchable', 'data-placeholder': placeholder})


class ProjectForm(StyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        others = Project.objects.order_by('name')
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        self.fields['linked_projects'].queryset = others
        self.fields['linked_projects'].label = 'Link Parent Project'
        self.fields['linked_projects'].help_text = 'Select one or more existing projects to link with this project.'

    class Meta:
        model = Project
        fields = ['name', 'description', 'linked_projects']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'linked_projects': forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        }


class ReleaseForm(StyledForm):
    class Meta:
        model = Release
        fields = ['name', 'release_date', 'description']
        widgets = {
            'release_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 2}),
        }


class ModuleForm(StyledForm):
    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['parent'].queryset = Module.objects.filter(project=project, parent__isnull=True)
        self.fields['parent'].empty_label = '-- Root module --'
        self.make_searchable('parent', placeholder='Search parent module...')

    class Meta:
        model = Module
        fields = ['name', 'parent', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}


class TestCaseForm(StyledForm):
    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['module'].queryset = Module.objects.filter(project=project).select_related('parent')
        self.make_searchable('module', placeholder='Search module...')
        self.make_searchable('case_types', placeholder='Select one or more types...')

    # several types at once, e.g. Functional + Smoke, Functional + API
    case_types = forms.MultipleChoiceField(label='Label / Type', choices=TestCase.TYPE_CHOICES,
                                           help_text='Select one or more types.')

    class Meta:
        model = TestCase
        fields = ['module', 'title', 'rtm_requirement', 'rtm_details', 'description', 'preconditions', 'steps',
                  'expected_result', 'priority', 'case_types', 'automatable']
        widgets = {
            'rtm_details': forms.Textarea(attrs={'rows': 2}),
            'description': forms.Textarea(attrs={'rows': 2}),
            'preconditions': forms.Textarea(attrs={'rows': 2}),
            'steps': forms.Textarea(attrs={'rows': 4, 'placeholder': '1. Open login page -> Login page is displayed\n2. Enter credentials\n3. Click Login -> Dashboard opens'}),
            'expected_result': forms.Textarea(attrs={'rows': 2}),
        }


class TestRunnerForm(StyledForm):
    test_cases = forms.ModelMultipleChoiceField(queryset=TestCase.objects.none(), required=True,
                                                widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['release'].queryset = Release.objects.filter(project=project)
        self.fields['release'].empty_label = '-- Select Release --'
        self.fields['assigned_to'].queryset = User.objects.order_by('username')
        self.fields['assigned_to'].empty_label = '-- Select User (Optional) --'
        self.make_searchable('assigned_to', placeholder='Search user...')
        self.fields['test_cases'].queryset = TestCase.objects.filter(project=project).select_related('module')
        self.fields['test_cases'].widget.attrs = {}
        envs = project.api_environments.all()
        self.fields['environment'].queryset = envs
        self.fields['environment'].required = envs.exists()
        self.fields['environment'].empty_label = None if envs.exists() else '-- No environments --'
        self.fields['environment'].help_text = 'Execution starts on this environment; switch it later on the execute page.'
        self.make_searchable('environment', placeholder='Search environment...')

    class Meta:
        model = TestRunner
        fields = ['name', 'release', 'environment', 'execution_date', 'due_date', 'assigned_to', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'e.g., Sprint 1 Regression'}),
            'execution_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Execution scope, objectives...'}),
        }
