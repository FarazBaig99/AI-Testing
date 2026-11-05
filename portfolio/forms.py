import re

from django import forms
from django.contrib.auth.models import User

from projects.forms import StyledForm
from projects.models import Milestone, Project, ProjectMember

SCOPE_FIELDS = ['objective', 'in_scope', 'out_of_scope', 'deliverables', 'success_criteria']
LIST_HELP = 'One item per line.'


def clean_project_key(form):
    key = form.cleaned_data['key'].strip().upper()
    if not re.fullmatch(r'[A-Z0-9]{2,12}', key):
        raise forms.ValidationError('Use 2-12 capital letters or digits, no spaces.')
    if Project.objects.filter(key=key).exclude(pk=form.instance.pk).exists():
        raise forms.ValidationError(f'Key "{key}" is already used by another project.')
    return key


def check_dates(form, data):
    if data.get('start_date') and data.get('target_date') and data['target_date'] < data['start_date']:
        form.add_error('target_date', 'Target completion cannot be before the start date.')
    return data


def _user_label(user):
    return user.get_full_name() or user.username


class UserChoiceMixin:
    def setup_users(self, *names):
        for name in names:
            field = self.fields[name]
            field.queryset = User.objects.filter(is_active=True).order_by('username')
            field.label_from_instance = _user_label
            field.widget.attrs.update({'class': 'form-select searchable', 'data-placeholder': 'Select a user...'})


class ProjectCreateForm(UserChoiceMixin, StyledForm):
    """All six wizard steps in one form; the steps are only split up in the browser."""
    INITIAL_STATUSES = ['Draft', 'Pending Approval', 'Active']

    members = forms.ModelMultipleChoiceField(queryset=User.objects.none(), required=False, label='Team Members',
                                             help_text='Added with the Tester role; change roles later in Team & Access.')
    milestones = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 4}),
                                 label='Milestones & Key Deliverables',
                                 help_text='One per line: Title | YYYY-MM-DD (date optional)')
    modules = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 5}),
                              label='Functional Modules (WBS)',
                              help_text='One module per line, e.g. Airline Contracts. Created in Test Management.')

    class Meta:
        model = Project
        fields = ['name', 'key', 'project_type', 'description', 'visibility', 'tags',
                  'objective', 'in_scope', 'out_of_scope', 'deliverables', 'success_criteria', 'criticality',
                  'start_date', 'target_date', 'delivery_model', 'status',
                  'sponsor', 'manager', 'qa_lead', 'ba_lead']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'objective': forms.Textarea(attrs={'rows': 3}),
            'in_scope': forms.Textarea(attrs={'rows': 4}),
            'out_of_scope': forms.Textarea(attrs={'rows': 4}),
            'deliverables': forms.Textarea(attrs={'rows': 3}),
            'success_criteria': forms.Textarea(attrs={'rows': 3}),
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'target_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_users('manager', 'qa_lead', 'ba_lead', 'members')
        self.fields['status'].choices = [(s, s) for s in self.INITIAL_STATUSES]
        self.fields['status'].label = 'Initial Status'
        self.fields['key'].widget.attrs.update({'style': 'text-transform:uppercase', 'maxlength': 12})
        for name in ['in_scope', 'out_of_scope', 'deliverables', 'success_criteria']:
            self.fields[name].help_text = LIST_HELP
        self.fields['name'].widget.attrs['placeholder'] = 'e.g. Air Cargo & Automated Customs System'
        self.fields['key'].widget.attrs['placeholder'] = 'e.g. ACS'
        self.fields['tags'].widget.attrs['placeholder'] = 'e.g. GovTech, Customs, High Priority'

    clean_key = clean_project_key

    def clean_milestones(self):
        rows = []
        for n, line in enumerate(self.cleaned_data['milestones'].splitlines(), 1):
            if not line.strip():
                continue
            title, _, date = (part.strip() for part in line.partition('|'))
            if date:
                try:
                    date = forms.DateField().clean(date)
                except forms.ValidationError:
                    raise forms.ValidationError(f'Line {n}: "{date}" is not a date (use YYYY-MM-DD).')
            rows.append((title, date or None))
        return rows

    def clean_modules(self):
        names = []
        for line in self.cleaned_data['modules'].splitlines():
            name = line.strip(' -*\t')
            if name and name.lower() not in [n.lower() for n in names]:
                names.append(name[:200])
        return names

    def clean(self):
        return check_dates(self, super().clean())


class ScopeForm(StyledForm):
    class Meta:
        model = Project
        fields = SCOPE_FIELDS + ['sponsor', 'criticality', 'visibility']
        widgets = {name: forms.Textarea(attrs={'rows': 4}) for name in SCOPE_FIELDS}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in SCOPE_FIELDS[1:]:
            self.fields[name].help_text = LIST_HELP


class LeadershipForm(UserChoiceMixin, StyledForm):
    class Meta:
        model = Project
        fields = ['manager', 'qa_lead', 'ba_lead']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_users('manager', 'qa_lead', 'ba_lead')


class MemberForm(UserChoiceMixin, StyledForm):
    class Meta:
        model = ProjectMember
        fields = ['user', 'role']

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_users('user')
        self.fields['user'].queryset = self.fields['user'].queryset.exclude(project_memberships__project=project)


class MilestoneForm(StyledForm):
    class Meta:
        model = Milestone
        fields = ['title', 'target_date']
        widgets = {'target_date': forms.DateInput(attrs={'type': 'date'})}


class StatusChangeForm(forms.Form):
    status = forms.ChoiceField(label='Move to')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
                             help_text='Required. Saved in the lifecycle history and audit trail.')

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['status'].choices = [(s, s) for s in project.next_statuses()]
        self.fields['status'].widget.attrs['class'] = 'form-select'


class SettingsForm(StyledForm):
    class Meta:
        model = Project
        fields = ['name', 'key', 'description', 'project_type', 'delivery_model', 'tags', 'start_date', 'target_date']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'start_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'target_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        }

    clean_key = clean_project_key

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['key'].widget.attrs.update({'style': 'text-transform:uppercase', 'maxlength': 12})

    def clean(self):
        return check_dates(self, super().clean())
