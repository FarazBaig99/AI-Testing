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


class ProjectForm(StyledForm):
    class Meta:
        model = Project
        fields = ['name', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}


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

    class Meta:
        model = Module
        fields = ['name', 'parent', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}


class TestCaseForm(StyledForm):
    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['module'].queryset = Module.objects.filter(project=project)

    class Meta:
        model = TestCase
        fields = ['module', 'title', 'description', 'preconditions', 'steps', 'expected_result',
                  'priority', 'case_type', 'automatable']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2}),
            'preconditions': forms.Textarea(attrs={'rows': 2}),
            'steps': forms.Textarea(attrs={'rows': 4, 'placeholder': '1. Open login page\n2. Enter credentials\n3. Click Login'}),
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
        self.fields['test_cases'].queryset = TestCase.objects.filter(project=project).select_related('module')
        self.fields['test_cases'].widget.attrs = {}

    class Meta:
        model = TestRunner
        fields = ['name', 'release', 'execution_date', 'due_date', 'assigned_to', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'e.g., Sprint 1 Regression'}),
            'execution_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Execution scope, objectives...'}),
        }
