from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from projects.models import Milestone, Module, Project, ProjectActivity, ProjectMember, TestResult

from apitesting.forms import EnvironmentForm

from .forms import (MilestoneForm, ProjectCreateForm, ScopeForm, SettingsForm,
                    StatusChangeForm, _user_label)

# workspace tabs: slug -> (label, icon); 'soon' tabs are placeholders until their module is built
TABS = [
    ('overview', 'Project Overview', 'speedometer2'),
    ('scope', 'Profile & Scope', 'bullseye'),
    ('team', 'Team & Access', 'people'),
    ('delivery', 'Project Delivery', 'graph-up-arrow'),
    ('integrations', 'External Integrations', 'plug'),
    ('environments', 'Environment Management', 'hdd-network'),
    ('modules', 'Project Modules', 'diagram-3'),
    ('requirements', 'Requirements & Docs', 'file-earmark-text'),
    ('apis', 'APIs & Databases', 'database'),
    ('lifecycle', 'Lifecycle', 'arrow-repeat'),
    ('activity', 'Activity & Audit', 'clock-history'),
    ('settings', 'Settings', 'gear'),
]
TAB_SLUGS = [t[0] for t in TABS]
SOON = {
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


def workspace_counts(project):
    """Counts shown in the workspace header and tabs (also used by the Project Delivery pages)."""
    from delivery.models import Sprint
    from integrations.models import ProjectLink
    return {'members': project.members.count(), 'modules': project.modules.filter(parent__isnull=True).count(),
            'environments': project.api_environments.count(),
            'sprints': Sprint.objects.filter(project=project, removed=False).count(),
            'integrations': (project.system_integrations.count() + project.repositories.count()
                             + project.delivery_connections.exclude(external_id='').count()
                             + ProjectLink.objects.filter(Q(source=project) | Q(target=project)).count()),
            'integrations_failed': project.system_integrations.filter(last_status='Failed').count(),
            'projects_total': Project.objects.count()}


def schedule(done_pct, project):
    """On Schedule / At Risk / Behind: work done compared with the share of the project timeline already passed."""
    if project.status in ('Completed', 'Closed'):
        return {'label': project.status, 'css': 'success'}
    if done_pct is None:
        return {'label': 'No progress data', 'css': 'secondary'}
    start, end = project.start_date, project.target_date
    if not (start and end and end > start):
        return {'label': 'No timeline', 'css': 'secondary'}
    elapsed = round((min(max(date.today(), start), end) - start).days * 100 / (end - start).days)
    gap = elapsed - done_pct
    if gap <= 10:
        return {'label': 'On Schedule', 'css': 'success', 'elapsed': elapsed}
    if gap <= 25:
        return {'label': 'At Risk', 'css': 'warning', 'elapsed': elapsed}
    return {'label': 'Behind Schedule', 'css': 'danger', 'elapsed': elapsed}


def overview_context(project, milestones):
    """Overview tab: completion, team, environments and the external delivery (synced sprints) summary."""
    from delivery import metrics
    work = metrics.items(project).exclude(kind='epic')
    n_work = work.count()
    if n_work:
        completion = {'pct': metrics.pct(work.filter(category='done').count(), n_work),
                      'source': f'From {n_work} synced work items'}
    elif milestones:
        completion = {'pct': round(sum(m.status == 'Completed' for m in milestones) * 100 / len(milestones)),
                      'source': f'From {len(milestones)} milestones'}
    else:
        completion = {'pct': None, 'source': 'No synced work or milestones yet'}
    completion['schedule'] = schedule(completion['pct'], project)

    members = list(project.members.select_related('user'))
    roles = {m.role for m in members}

    environments = list(project.api_environments.all())
    for e in environments:
        # production-like environments are protected from destructive test runs
        e.locked = any(word in e.name.lower() for word in ('prod', 'live'))

    linked = list(project.delivery_connections.exclude(external_id=''))
    kinds = dict(work.order_by().values('kind').annotate(n=Count('id')).values_list('kind', 'n'))
    sprint_list = metrics.sprints(project)
    last_runs = [c.runs.order_by('-started_at').first() for c in linked]
    delivery = {
        'linked': linked,
        'failed': [c for c in linked if c.last_status == 'Failed'],
        'sprints': sprint_list,
        'n_active': sum(s.state == 'active' for s in sprint_list),
        'n_future': sum(s.state == 'future' for s in sprint_list),
        'n_closed': sum(s.state == 'closed' for s in sprint_list),
        'stories': kinds.get('story', 0),
        'tasks': kinds.get('task', 0) + kinds.get('subtask', 0),
        'subtasks': kinds.get('subtask', 0),
        'bugs': kinds.get('bug', 0),
        'bugs_open': work.filter(kind='bug').exclude(category='done').count(),
        'warnings': sum(len(r.warnings) for r in last_runs if r),
    }
    return {'completion': completion, 'team': members,
            'team_label': 'Cross-functional' if len(roles) > 1 else (next(iter(roles)) if roles else 'No members yet'),
            'env_list': environments, 'delivery': delivery,
            'milestone_form': MilestoneForm(), 'recent': project.activities.select_related('user')[:6]}


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
        'last_project': Project.objects.filter(pk=request.session.get('last_project')).first(),
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
    request.session['last_project'] = project.pk  # target of the list page's "Project Workspace" toggle
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
        'runners': project.runners.count(),
        **workspace_counts(project),
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
        return overview_context(project, list(project.milestones.all()))
    if tab == 'scope':
        return {'form': ScopeForm(instance=project)}
    if tab == 'team':
        members = list(project.members.select_related('user', 'user__profile'))
        by_role = {r: 0 for r in ProjectMember.ROLES}
        for m in members:
            by_role[m.role] = by_role.get(m.role, 0) + 1
        role = request.GET.get('role', '')
        return {'members': [m for m in members if not role or m.role == role], 'n_members': len(members),
                'by_role': by_role, 'role': role}
    if tab == 'environments':
        env_form = EnvironmentForm(project=project)
        for f in env_form.fields.values():
            f.widget.attrs['class'] = 'form-control form-control-sm'
        envs = list(project.api_environments.annotate(
            n_cases=Count('test_cases', distinct=True), n_results=Count('test_results', distinct=True),
            n_folders=Count('folders', distinct=True)))
        for e in envs:
            # how many APIs each URL of the environment holds ({{baseUrl}}/... or {{otherUrl}}/...)
            rows = []
            for u in e.urls.all():
                var = 'baseUrl' if u.is_default else u.key
                u.n_cases = e.test_cases.filter(url__startswith='{{' + var + '}}').count()
                rows.append(u)
            e.url_rows = rows
            e.last_import = e.sources.filter(last_synced_at__isnull=False).order_by('-last_synced_at').first()
            e.active_source = e.sources.filter(is_active=True).first()      # the link Sync fetches again
        return {'environments': envs, 'env_form': env_form}
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
        envs = list(project.api_environments.annotate(
            n_folders=Count('folders', distinct=True), n_cases=Count('test_cases', distinct=True),
            n_pass=Count('test_cases', filter=Q(test_cases__status='pass'), distinct=True),
            n_fail=Count('test_cases', filter=Q(test_cases__status='fail'), distinct=True)))
        sources = {}
        for s in project.api_sources.select_related('environment').order_by('-is_active', '-last_synced_at'):
            sources.setdefault(s.environment_id, s)       # the active (or latest) source of each environment
        for e in envs:
            e.source = sources.get(e.pk)
        return {'api_envs': envs}
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
            # the header's Put on Hold / Resume dialog posts from any workspace or delivery page
            nxt = request.POST.get('next', '')
            if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
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
