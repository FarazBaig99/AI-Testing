from django import forms

from projects.forms import StyledForm

from .models import ApiTestCase


class ApiTestCaseForm(StyledForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['folder'].queryset = self.instance.project.api_folders.all()
        self.fields['folder'].label = 'Module'
        self.make_searchable('folder', placeholder='Search module...')
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
