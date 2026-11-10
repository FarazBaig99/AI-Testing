from django import forms
from django.db.models import Q

from delivery.connectors.base import ConnectorError
from projects.forms import StyledForm
from projects.models import Project

from .checks import host_port
from .models import ProjectLink, Repository, SystemIntegration, VersionEndpoint
from .repos import CONNECTORS


class SecretKeepMixin:
    """A blank password / token field on edit keeps the stored value."""
    secret_field = ''
    masked_attr = ''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields[self.secret_field]
        field.widget = forms.PasswordInput(render_value=False, attrs={'class': 'form-control', 'autocomplete': 'new-password'})
        field.required = False
        if self.instance.pk and getattr(self.instance, self.secret_field):
            field.help_text = f'Saved ({getattr(self.instance, self.masked_attr)}). Leave empty to keep it.'

    def clean(self):
        data = super().clean()
        if self.instance.pk and not data.get(self.secret_field):
            data[self.secret_field] = type(self.instance).objects.filter(pk=self.instance.pk).values_list(
                self.secret_field, flat=True).first() or ''
        return data


class SystemIntegrationForm(SecretKeepMixin, StyledForm):
    secret_field = 'auth_secret'
    masked_attr = 'masked_secret'

    class Meta:
        model = SystemIntegration
        fields = ['name', 'partner_type', 'protocol', 'direction', 'environment', 'endpoint', 'check_method',
                  'expected_status', 'auth_type', 'auth_user', 'auth_secret', 'timeout', 'contact', 'description']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['environment'].queryset = project.api_environments.all()
        self.fields['environment'].empty_label = 'All / not specific'
        for name in ('partner_type', 'protocol', 'direction', 'environment', 'check_method', 'auth_type'):
            self.fields[name].widget.attrs['class'] = 'form-select'

    def clean_endpoint(self):
        endpoint = self.cleaned_data['endpoint'].strip()
        if not endpoint.lower().startswith(('http://', 'https://')) and not host_port(endpoint):
            raise forms.ValidationError('Enter a URL (https://...) or host:port, e.g. kafka.bank.com:9092.')
        return endpoint

    def clean_timeout(self):
        return min(max(self.cleaned_data['timeout'] or 10, 1), 60)


class ProjectLinkForm(StyledForm):
    class Meta:
        model = ProjectLink
        fields = ['target', 'relation', 'interface', 'description']
        labels = {'target': 'Project to link', 'relation': 'This project'}
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        linked = ProjectLink.objects.filter(Q(source=project) | Q(target=project))
        taken = {l.source_id for l in linked} | {l.target_id for l in linked} | {project.pk}
        self.fields['target'].queryset = Project.objects.exclude(pk__in=taken).order_by('name')
        self.fields['target'].label_from_instance = lambda p: f'{p.name} ({p.key})'
        self.fields['target'].widget.attrs['class'] = 'form-select'
        self.fields['relation'].widget.attrs['class'] = 'form-select'


class RepositoryForm(SecretKeepMixin, StyledForm):
    secret_field = 'token'
    masked_attr = 'masked_token'

    class Meta:
        model = Repository
        fields = ['provider', 'url', 'api_url', 'token']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields['provider'].widget.attrs['class'] = 'form-select'
        self.fields['url'].widget.attrs['placeholder'] = 'https://github.com/owner/repo'

    def clean(self):
        data = super().clean()
        provider = data.get('provider')
        if provider and data.get('url'):
            probe = Repository(project=self.project, provider=provider, url=data['url'].strip(),
                               api_url=data.get('api_url', ''))
            try:
                CONNECTORS[provider](probe)
            except ConnectorError as e:
                self.add_error('url', str(e))
            if provider != 'demo' and not data.get('token'):
                self.add_error('token', 'The access token is required (the tool will not share data without it).')
        return data


class VersionEndpointForm(StyledForm):
    class Meta:
        model = VersionEndpoint
        fields = ['url', 'version_key']
        labels = {'url': 'Version / health URL'}

    def clean_url(self):
        url = self.cleaned_data['url'].strip()
        if not url.lower().startswith(('http://', 'https://')):
            raise forms.ValidationError('Use a full URL, e.g. https://sit.acs.gov.pk/api/version')
        return url
