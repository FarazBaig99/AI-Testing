from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from projects.models import Milestone, Module, Project, ProjectActivity, ProjectMember, TestResult

from .forms import (LeadershipForm, MemberForm, MilestoneForm, ProjectCreateForm, ScopeForm, SettingsForm,
                    StatusChangeForm, _user_label)

# workspace tabs: slug -> (label, icon); 'soon' tabs are placeholders until their module is built
TABS = [
    ('overview', 'Project Overview', 'speedometer2'),
    ('scope', 'Profile & Scope', 'bullseye'),
    ('team', 'Team & Access', 'people'),
    ('delivery', 'Project Delivery', 'graph-up-arrow'),
    ('integrations', 'External Integrations', 'plug'),
    ('environments', 'Environments', 'hdd-network'),
    ('modules', 'Modules (WBS)', 'diagram-3'),
    ('requirements', 'Requirements & Docs', 'file-earmark-text'),
    ('apis', 'APIs & Databases', 'database'),
    ('lifecycle', 'Lifecycle', 'arrow-repeat'),
    ('activity', 'Activity & Audit', 'clock-history'),
    ('settings', 'Settings', 'gear'),
]
TAB_SLUGS = [t[0] for t in TABS]
SOON = {
    'delivery': ('Sprints, epics, user stories and tasks synced from Jira / ClickUp / Azure DevOps, with story points, '
                 'velocity and QA verification status on each story.',
                 ['Active, future and closed sprints', 'Epic → Story → Task → Bug tree',
                  'QA verification status on user stories']),
    'integrations': ('Connect project management tools (Jira, ClickUp, Azure DevOps, Monday, Asana, Linear, Trello) '
                     'and engineering tools (GitHub, GitLab, Bitbucket, Jenkins).',
                     ['OAuth / API token connection', 'One-way import or two-way sync, status and field mapping',
                      'Repository binding and PR change detection', '"Sync Now" with sync audit log']),
    'requirements': ('Official project documents (PRD, SRS, architecture diagrams, user guides) with versions and '
                     'approval status. Built together with the Requirements & Docs module.',
                     ['Document upload with version tracking', 'Approval status',
                      'AI extraction of requirements and business scenarios']),
}
HEALTH_LEVELS = [(85, 'Healthy', 'success'), (60, 'At Risk', 'warning'), (0, 'Critical', 'danger')]


def health(passed, executed, blocked):
    """Health from executed results (not Pending): pass rate decides the level, any Blocked result caps it at At Risk."""
    if not executed:
        return {'rate': None, 'label': 'No executions', 'css': 'secondary'}
    rate = round(passed * 100 / executed)
    index = next(i for i, (minimum, _, _) in enumerate(HEALTH_LEVELS) if rate >= minimum)
    if blocked and index == 0:
        index = 1
    _, label, css = HEALTH_LEVELS[index]
    return {'rate': rate, 'label': label, 'css': css}


def _log(project, user, category, message, reason=''):
    ProjectActivity.objects.create(project=project, user=user, category=category, message=message[:500],
                                   reason=reason)


def _changed(form):
    return ', '.join(str(form.fields[name].label) for name in form.changed_data)


@login_required
def portfolio_list(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '')
    ptype = request.GET.get('type', '')
    criticality = request.GET.get('criticality', '')

    projects = Project.objects.select_related('manager').annotate(
        n_members=Count('members', distinct=True),
        n_cases=Count('test_cases', distinct=True),
        n_results=Count('runners__results', filter=~Q(runners__results__status='Pending'), distinct=True),
        n_passed=Count('runners__results', filter=Q(runners__results__status='Pass'), distinct=True),
        n_blocked=Count('runners__results', filter=Q(runners__results__status='Blocked'), distinct=True),
        n_milestones=Count('milestones', distinct=True),
        n_done=Count('milestones', filter=Q(milestones__status='Completed'), distinct=True),
    ).order_by('name')
    all_projects = list(projects)

    if q:
        projects = projects.filter(Q(name__icontains=q) | Q(key__icontains=q) | Q(tags__icontains=q)
                                   | Q(description__icontains=q))
    if status:
        projects = projects.filter(status=status)
    else:
        projects = projects.exclude(status='Archived')
    if ptype:
        projects = projects.filter(project_type=ptype)
    if criticality:
        projects = projects.filter(criticality=criticality)
    projects = list(projects)
    for p in projects:
        p.health = health(p.n_passed, p.n_results, p.n_blocked)
        p.progress = round(p.n_done * 100 / p.n_milestones) if p.n_milestones else None

    by_status = {s: 0 for s in Project.STATUSES}
    for p in all_projects:
        by_status[p.status] += 1
    rates = [health(p.n_passed, p.n_results, p.n_blocked)['rate'] for p in all_projects]
    rates = [r for r in rates if r is not None]
    kpis = {
        'total': len(all_projects),
        'active': by_status['Active'],
        'planning': by_status['Draft'] + by_status['Pending Approval'],
        'on_hold': by_status['On Hold'],
        'closed': by_status['Completed'] + by_status['Closed'],
        'archived': by_status['Archived'],
        'avg_pass': round(sum(rates) / len(rates)) if rates else None,
        'failed': TestResult.objects.filter(status='Fail').count(),
        'blocked': TestResult.objects.filter(status='Blocked').count(),
    }
    return render(request, 'portfolio/list.html', {
        'projects': projects, 'kpis': kpis, 'q': q, 'status': status, 'ptype': ptype, 'criticality': criticality,
        'statuses': Project.STATUSES, 'types': Project.TYPES, 'criticalities': Project.CRITICALITIES,
        'filtered': bool(q or status or ptype or criticality),
    })


@login_required
def portfolio_create(request):
    form = ProjectCreateForm(request.POST or None)
    if form.is_valid():
        with transaction.atomic():
            project = form.save(commit=False)
            project.created_by = request.user
            project.save()
            for user in form.cleaned_data['members']:
                ProjectMember.objects.create(project=project, user=user)
            for title, date in form.cleaned_data['milestones']:
                Milestone.objects.create(project=project, title=title[:200], target_date=date)
            for name in form.cleaned_data['modules']:
                Module.objects.create(project=project, name=name)
            _log(project, request.user, 'created', f'Project {project.key} created with status {project.status}')
        messages.success(request, f'Project "{project.name}" ({project.key}) created.')
        return redirect('portfolio_workspace', pk=project.pk)
    return render(request, 'portfolio/create.html', {'form': form})


@login_required
def portfolio_workspace(request, pk, tab='overview'):
    if tab not in TAB_SLUGS:
        raise Http404('Unknown tab')
    project = get_object_or_404(Project.objects.select_related('manager', 'qa_lead', 'ba_lead'), pk=pk)
    if request.method == 'POST':
        return _handle_post(request, project, tab)
    return _render_workspace(request, project, tab)


def _render_workspace(request, project, tab, **overrides):
    """overrides: bound forms with errors, shown instead of the blank ones."""
    results = TestResult.objects.filter(runner__project=project)
    executed = results.exclude(status='Pending')
    counts = {
        'passed': executed.filter(status='Pass').count(),
        'failed': executed.filter(status='Fail').count(),
        'blocked': executed.filter(status='Blocked').count(),
        'executed': executed.count(),
        'pending': results.filter(status='Pending').count(),
        'cases': project.test_cases.count(),
        'api_cases': project.api_test_cases.count(),
        'environments': project.api_environments.count(),
        'members': project.members.count(),
        'modules': project.modules.filter(parent__isnull=True).count(),
        'runners': project.runners.count(),
    }
    milestones = list(project.milestones.all())
    done = sum(m.status == 'Completed' for m in milestones)
    context = {
        'project': project, 'tab': tab, 'tabs': TABS, 'counts': counts,
        'health': health(counts['passed'], counts['executed'], counts['blocked']),
        'milestones': milestones,
        'progress': round(done * 100 / len(milestones)) if milestones else None,
        'soon': SOON.get(tab),
    }
    context.update(_tab_context(request, project, tab, results))
    context.update(overrides)
    return render(request, 'portfolio/workspace.html', context)


def _tab_context(request, project, tab, results):
    if tab == 'overview':
        return {'milestone_form': MilestoneForm(), 'recent': project.activities.select_related('user')[:5]}
    if tab == 'scope':
        return {'form': ScopeForm(instance=project)}
    if tab == 'team':
        return {'members': project.members.select_related('user', 'user__profile'),
                'member_form': MemberForm(project=project), 'leader_form': LeadershipForm(instance=project),
                'roles': ProjectMember.ROLES}
    if tab == 'environments':
        return {'environments': project.api_environments.annotate(
            n_cases=Count('test_cases', distinct=True), n_results=Count('test_results', distinct=True))}
    if tab == 'modules':
        modules = project.modules.filter(parent__isnull=True).prefetch_related('children').annotate(
            n_cases=Count('test_cases', distinct=True))
        rows = []
        for m in modules:
            ids = [m.pk] + [c.pk for c in m.children.all()]
            mod_results = results.filter(test_case__module_id__in=ids)
            executed = mod_results.exclude(status='Pending')
            n_cases = project.test_cases.filter(module_id__in=ids).count()
            covered = mod_results.exclude(status='Pending').values('test_case').distinct().count()
            rows.append({'module': m, 'cases': n_cases, 'subs': len(ids) - 1,
                         'coverage': round(covered * 100 / n_cases) if n_cases else None,
                         'failed': executed.filter(status='Fail').count(),
                         'health': health(executed.filter(status='Pass').count(), executed.count(),
                                          executed.filter(status='Blocked').count())})
        return {'module_rows': rows}
    if tab == 'apis':
        return {'api_envs': project.api_environments.annotate(
            n_folders=Count('folders', distinct=True), n_cases=Count('test_cases', distinct=True),
            n_pass=Count('test_cases', filter=Q(test_cases__status='pass'), distinct=True),
            n_fail=Count('test_cases', filter=Q(test_cases__status='fail'), distinct=True)),
            'api_sources': project.api_sources.select_related('environment')}
    if tab == 'lifecycle':
        return {'status_form': StatusChangeForm(project=project), 'statuses': Project.STATUSES,
                'transitions': Project.TRANSITIONS,
                'history': project.activities.filter(category__in=['status', 'created']).select_related('user')}
    if tab == 'activity':
        category = request.GET.get('category', '')
        activities = project.activities.select_related('user')
        if category:
            activities = activities.filter(category=category)
        return {'activities': activities, 'category': category, 'categories': ProjectActivity.CATEGORIES}
    if tab == 'settings':
        return {'form': SettingsForm(instance=project)}
    return {}


def _handle_post(request, project, tab):
    """Every workspace form posts back to its own tab with an `action`; on success redirect (PRG)."""
    action = request.POST.get('action', '')
    user = request.user
    back = redirect('portfolio_workspace', pk=project.pk, tab=tab)

    if action == 'scope':
        form = ScopeForm(request.POST, instance=project)
        if form.is_valid():
            if form.changed_data:
                form.save()
                _log(project, user, 'scope', f'Profile & scope updated: {_changed(form)}')
            messages.success(request, 'Profile & scope saved.')
            return back
        return _render_workspace(request, project, tab, form=form)

    if action == 'leaders':
        form = LeadershipForm(request.POST, instance=project)
        if form.is_valid():
            if form.changed_data:
                form.save()
                names = ', '.join(f'{form.fields[n].label}: {_user_label(getattr(project, n)) if getattr(project, n) else "-"}'
                                  for n in form.changed_data)
                _log(project, user, 'team', f'Key leadership changed ({names})')
            messages.success(request, 'Key leadership saved.')
        return back

    if action == 'member_add':
        form = MemberForm(request.POST, project=project)
        if form.is_valid():
            member = form.save(commit=False)
            member.project = project
            member.save()
            _log(project, user, 'team', f'{_user_label(member.user)} added as {member.role}')
            messages.success(request, f'{_user_label(member.user)} added to the team.')
        else:
            messages.error(request, 'Select a user who is not already in the team.')
        return back

    if action in ('member_role', 'member_remove'):
        member = get_object_or_404(ProjectMember, pk=request.POST.get('member'), project=project)
        if action == 'member_remove':
            member.delete()
            _log(project, user, 'team', f'{_user_label(member.user)} removed from the team')
            messages.success(request, f'{_user_label(member.user)} removed.')
        elif request.POST.get('role') in ProjectMember.ROLES and request.POST['role'] != member.role:
            old, member.role = member.role, request.POST['role']
            member.save(update_fields=['role'])
            _log(project, user, 'team', f'{_user_label(member.user)} role changed {old} → {member.role}')
            messages.success(request, f'{_user_label(member.user)} is now {member.role}.')
        return back

    if action == 'milestone_add':
        form = MilestoneForm(request.POST)
        if form.is_valid():
            milestone = form.save(commit=False)
            milestone.project = project
            milestone.save()
            _log(project, user, 'milestone', f'Milestone "{milestone.title}" added')
        else:
            messages.error(request, 'Milestone title is required.')
        return back

    if action in ('milestone_status', 'milestone_delete'):
        milestone = get_object_or_404(Milestone, pk=request.POST.get('milestone'), project=project)
        if action == 'milestone_delete':
            milestone.delete()
            _log(project, user, 'milestone', f'Milestone "{milestone.title}" deleted')
        elif request.POST.get('status') in Milestone.STATUSES and request.POST['status'] != milestone.status:
            old, milestone.status = milestone.status, request.POST['status']
            milestone.save(update_fields=['status'])
            _log(project, user, 'milestone', f'Milestone "{milestone.title}": {old} → {milestone.status}')
        return back

    if action == 'status':
        form = StatusChangeForm(request.POST, project=project)
        if form.is_valid():
            old, project.status = project.status, form.cleaned_data['status']
            project.save(update_fields=['status', 'updated_at'])
            _log(project, user, 'status', f'Lifecycle status changed {old} → {project.status}',
                 reason=form.cleaned_data['reason'])
            messages.success(request, f'Project is now {project.status}.')
            return back
        return _render_workspace(request, project, tab, status_form=form)

    if action == 'settings':
        form = SettingsForm(request.POST, instance=project)
        if form.is_valid():
            if form.changed_data:
                form.save()
                _log(project, user, 'settings', f'Settings updated: {_changed(form)}')
            messages.success(request, 'Settings saved.')
            return back
        return _render_workspace(request, project, tab, form=form)

    if action == 'delete':
        if request.POST.get('confirm_key', '').strip().upper() != project.key:
            messages.error(request, f'Type the project key "{project.key}" exactly to delete this project.')
            return back
        name = project.name
        project.delete()
        messages.success(request, f'Project "{name}" and all its data were deleted.')
        return redirect('portfolio_list')

    messages.error(request, 'Unknown action.')
    return back
