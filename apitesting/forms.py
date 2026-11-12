from django import forms

from projects.forms import StyledForm

from .models import ApiTestCase, Environment


class EnvironmentForm(StyledForm):
    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields['name'].widget.attrs['placeholder'] = 'e.g., QA, SIT, UAT, Production, QA01'

    class Meta:
        model = Environment
        fields = ['name']

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        clash = Environment.objects.filter(project=self.project, name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('An environment with this name already exists in this project.')
        return name

class ApiTestCaseForm(StyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['folder'].queryset = self.instance.environment.folders.all()
        self.fields['folder'].label = 'Module'
        self.make_searchable('folder', placeholder='Search module...')
        self.fields['url'].help_text = '{{baseUrl}} is replaced with the environment base URL.'
        for name in ('headers', 'path_params', 'query_params'):
            self.fields[name].help_text = 'JSON object, e.g. {"Authorization": "Bearer ..."}'

    class Meta:
        model = ApiTestCase
        fields = ['folder', 'name', 'method', 'url', 'content_type', 'headers', 'path_params', 'query_params',
                  'body', 'expected_status']
        widgets = {
            'headers': forms.Textarea(attrs={'rows': 3, 'class': 'form-control font-monospace'}),
            'path_params': forms.Textarea(attrs={'rows': 2, 'class': 'form-control font-monospace'}),
            'query_params': forms.Textarea(attrs={'rows': 3, 'class': 'form-control font-monospace'}),
            'body': forms.Textarea(attrs={'rows': 10, 'class': 'form-control font-monospace'}),
        }
