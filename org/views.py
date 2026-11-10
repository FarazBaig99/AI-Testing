from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from portfolio.forms import LeadershipForm, _user_label
from projects.models import Project, ProjectActivity, ProjectMember

from .forms import AssignMemberForm, AssignProjectForm, EmployeeCreateForm, EmployeeEditForm

# what each project role may do (shown in Org & Team; enforcement comes with RBAC later)
ROLE_PERMISSIONS = [
    ('Viewer', 'Read only: sees the project, test cases and reports.'),
    ('Tester', 'Executes test runners, records results, evidence and videos.'),
    ('Developer', 'Sees failures and fixes defects linked to the project.'),
    ('Lead', 'Manages modules, test cases, runners and releases.'),
    ('Project Admin', 'Full control of the project, including settings and deletion.'),
]


def _log(project, user, message):
    ProjectActivity.objects.create(project=project, user=user, category='team', message=message[:500])


def _member_action(request, member):
    """Role change / removal of one project membership; shared by the employee and project access pages."""
    if request.POST.get('action') == 'remove':
        member.delete()
        _log(member.project, request.user, f'{_user_label(member.user)} removed from the team')
        messages.success(request, f'{_user_label(member.user)} removed from {member.project.name}.')
    elif request.POST.get('role') in ProjectMember.ROLES and request.POST['role'] != member.role:
        old, member.role = member.role, request.POST['role']
        member.save(update_fields=['role'])
        _log(member.project, request.user, f'{_user_label(member.user)} role changed {old} → {member.role}')
        messages.success(request, f'{_user_label(member.user)} is now {member.role} in {member.project.name}.')


@login_required
def org_home(request):
    form = EmployeeCreateForm()
    if request.method == 'POST':
        form = EmployeeCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f'Employee {_user_label(user)} added. Now give them access to projects.')
            return redirect('org_employee', user_id=user.pk)

    q = request.GET.get('q', '').strip()
    department = request.GET.get('department', '')
    employees = User.objects.select_related('profile').annotate(n_projects=Count('project_memberships')).order_by(
        '-is_active', 'first_name', 'username')
    all_employees = list(employees)
    if q:
        employees = employees.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                                     | Q(email__icontains=q) | Q(profile__job_title__icontains=q))
    if department:
        employees = employees.filter(profile__department=department)
    employees = list(employees.prefetch_related('project_memberships__project'))
    kpis = {
        'total': len(all_employees),
        'active': sum(u.is_active for u in all_employees),
        'assigned': sum(u.n_projects > 0 for u in all_employees),
        'unassigned': sum(u.n_projects == 0 and u.is_active for u in all_employees),
        'projects': Project.objects.count(),
    }
    departments = sorted({u.profile.department for u in all_employees
                          if hasattr(u, 'profile') and u.profile.department})
    return render(request, 'org/home.html', {
        'employees': employees, 'kpis': kpis, 'form': form, 'q': q, 'department': department,
        'departments': departments, 'filtered': bool(q or department), 'role_permissions': ROLE_PERMISSIONS,
        'show_form': request.method == 'POST'})


@login_required
def org_employee(request, user_id):
    employee = get_object_or_404(User.objects.select_related('profile'), pk=user_id)
    edit_form = EmployeeEditForm(instance=employee)
    assign_form = AssignProjectForm(user=employee)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'edit':
            edit_form = EmployeeEditForm(request.POST, instance=employee)
            if edit_form.is_valid():
                edit_form.save()
                messages.success(request, 'Employee details saved.')
                return redirect('org_employee', user_id=employee.pk)
        elif action == 'assign':
            assign_form = AssignProjectForm(request.POST, user=employee)
            if assign_form.is_valid():
                member = assign_form.save(commit=False)
                member.user = employee
                member.save()
                _log(member.project, request.user, f'{_user_label(employee)} added as {member.role}')
                messages.success(request, f'{_user_label(employee)} added to {member.project.name} as {member.role}.')
                return redirect('org_employee', user_id=employee.pk)
        elif action in ('role', 'remove'):
            member = get_object_or_404(ProjectMember, pk=request.POST.get('member'), user=employee)
            _member_action(request, member)
            return redirect('org_employee', user_id=employee.pk)
    memberships = employee.project_memberships.select_related('project').order_by('project__name')
    return render(request, 'org/employee.html', {
        'employee': employee, 'memberships': memberships, 'edit_form': edit_form, 'assign_form': assign_form,
        'roles': ProjectMember.ROLES, 'role_permissions': ROLE_PERMISSIONS,
        'show_edit': bool(edit_form.errors)})


@login_required
def org_access(request):
    """Project access: pick a project, then add employees, change their role or remove them."""
    projects = Project.objects.annotate(n_members=Count('members')).order_by('name')
    project = None
    if request.GET.get('project') or request.POST.get('project'):
        project = get_object_or_404(Project, pk=request.GET.get('project') or request.POST.get('project'))
    form = AssignMemberForm(project=project) if project else None
    leader_form = LeadershipForm(instance=project) if project else None
    if request.method == 'POST' and project:
        action = request.POST.get('action')
        if action == 'leaders':
            leader_form = LeadershipForm(request.POST, instance=project)
            if leader_form.is_valid():
                if leader_form.changed_data:
                    leader_form.save()
                    names = ', '.join(f'{leader_form.fields[n].label}: '
                                      f'{_user_label(getattr(project, n)) if getattr(project, n) else "-"}'
                                      for n in leader_form.changed_data)
                    _log(project, request.user, f'Key leadership changed ({names})')
                messages.success(request, f'Key leadership of {project.name} saved.')
                return redirect(f'{request.path}?project={project.pk}')
        elif action == 'add':
            form = AssignMemberForm(request.POST, project=project)
            if form.is_valid():
                member = form.save(commit=False)
                member.project = project
                member.save()
                _log(project, request.user, f'{_user_label(member.user)} added as {member.role}')
                messages.success(request, f'{_user_label(member.user)} added to {project.name} as {member.role}.')
                return redirect(f'{request.path}?project={project.pk}')
        elif action in ('role', 'remove'):
            member = get_object_or_404(ProjectMember, pk=request.POST.get('member'), project=project)
            _member_action(request, member)
            return redirect(f'{request.path}?project={project.pk}')
    members = project.members.select_related('user', 'user__profile') if project else []
    return render(request, 'org/access.html', {
        'projects': projects, 'project': project, 'members': members, 'form': form, 'leader_form': leader_form,
        'roles': ProjectMember.ROLES, 'role_permissions': ROLE_PERMISSIONS})
