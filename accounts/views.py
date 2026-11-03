from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from projects.models import TestRunner

from .forms import ProfileForm, RegisterForm, StyledPasswordChangeForm
from .models import Profile


def register(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    form = RegisterForm(request.POST or None)
    if form.is_valid():
        user = form.save()
        Profile.objects.create(user=user)
        login(request, user)
        messages.success(request, f'Welcome {user.username}! Your account has been created.')
        return redirect('dashboard')
    return render(request, 'accounts/register.html', {'form': form})


@login_required
def profile(request):
    prof, _ = Profile.objects.get_or_create(user=request.user)
    profile_form = ProfileForm(instance=prof)
    password_form = StyledPasswordChangeForm(request.user)
    if request.method == 'POST':
        if 'save_profile' in request.POST:
            profile_form = ProfileForm(request.POST, instance=prof)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, 'Profile details saved.')
                return redirect('profile')
        elif 'change_password' in request.POST:
            password_form = StyledPasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Password changed successfully.')
                return redirect('profile')
    my_runners = TestRunner.objects.filter(assigned_to=request.user).select_related('project')
    return render(request, 'accounts/profile.html', {
        'profile_form': profile_form, 'password_form': password_form, 'my_runners': my_runners,
    })
