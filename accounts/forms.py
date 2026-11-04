from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm
from django.contrib.auth.models import User

from .models import Profile


def _style(form):
    for field in form.fields.values():
        field.widget.attrs.setdefault('class', 'form-control')


class LoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)
        self.fields['username'].widget.attrs['placeholder'] = 'Enter your username'
        self.fields['password'].widget.attrs['placeholder'] = 'Enter your password'


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ['username', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)
        self.fields['username'].widget.attrs['placeholder'] = 'Choose a unique username'
        self.fields['email'].widget.attrs['placeholder'] = 'your.name@example.com'
        self.fields['password1'].widget.attrs['placeholder'] = 'Create a strong password'
        self.fields['password2'].widget.attrs['placeholder'] = 'Re-enter your password'

    def clean_email(self):
        email = self.cleaned_data['email']
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('This email is already registered.')
        return email


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['job_title', 'department', 'capacity_hours']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class StyledPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)
