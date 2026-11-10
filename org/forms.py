from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from accounts.models import Profile
from portfolio.forms import StyledForm, UserChoiceMixin
from projects.models import Project, ProjectMember


class EmployeeCreateForm(UserCreationForm):
    """A new employee account; the employee changes the initial password from their own profile."""
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField()
    job_title = forms.CharField(max_length=100, required=False)
    department = forms.CharField(max_length=100, required=False)

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')
        self.fields['password1'].label = 'Initial password'
        self.fields['password1'].help_text = 'Share it with the employee; they can change it from their profile.'

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An employee with this email already exists.')
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data['email']
        user.save()
        Profile.objects.create(user=user, job_title=self.cleaned_data['job_title'],
                               department=self.cleaned_data['department'])
        return user


class EmployeeEditForm(forms.ModelForm):
    job_title = forms.CharField(max_length=100, required=False)
    department = forms.CharField(max_length=100, required=False)

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'is_active']
        labels = {'is_active': 'Active (can sign in)'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile, _ = Profile.objects.get_or_create(user=self.instance)
        self.fields['job_title'].initial = self.profile.job_title
        self.fields['department'].initial = self.profile.department
        for name, field in self.fields.items():
            field.widget.attrs.setdefault('class', 'form-check-input' if name == 'is_active' else 'form-control')

    def clean_email(self):
        email = self.cleaned_data['email']
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Another employee already uses this email.')
        return email

    def save(self, commit=True):
        user = super().save()
        self.profile.job_title = self.cleaned_data['job_title']
        self.profile.department = self.cleaned_data['department']
        self.profile.save()
        return user


class AssignProjectForm(StyledForm):
    """Give an employee access to a project (employee page)."""
    class Meta:
        model = ProjectMember
        fields = ['project', 'role']

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['project'].queryset = Project.objects.exclude(members__user=user).order_by('name')
        self.fields['project'].widget.attrs['class'] = 'form-select'


class AssignMemberForm(UserChoiceMixin, StyledForm):
    """Add an employee to a project (project access page)."""
    class Meta:
        model = ProjectMember
        fields = ['user', 'role']
        labels = {'user': 'Employee'}

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_users('user')
        self.fields['user'].queryset = self.fields['user'].queryset.exclude(project_memberships__project=project)
