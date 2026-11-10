from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from portfolio.views import TABS, workspace_counts
from projects.models import Project, ProjectActivity

from . import metrics
from .connectors import CONNECTORS, ConnectorError, connector_for, guess_kind
from .models import Connection, Dependency, Release, Sprint, SyncRun, TeamMember, WorkItem
from .sync import run_sync

SECTIONS = [
    ('overview', 'Delivery Overview', 'speedometer2'),
    ('integration', 'Sync & Integration', 'plug'),
    ('releases', 'Releases', 'rocket-takeoff'),
    ('sprints', 'Sprints & Iterations', 'arrow-repeat'),
    ('stories', 'User Stories & Epics', 'journal-text'),
    ('tasks', 'Tasks & Subtasks', 'list-check'),
    ('bugs', 'Bugs & Defects', 'bug'),
    ('milestones', 'Milestones & Dependencies', 'signpost-split'),
    ('team', 'Team & Workload', 'people'),
    ('analytics', 'Delivery Analytics & AI Insights', 'stars'),
    ('history', 'Sync History & Audit', 'clock-history'),
]
SECTION_SLUGS = [s[0] for s in SECTIONS]
PAGE_SIZE = 50


def _log(project, user, message):
    ProjectActivity.objects.create(project=project, user=user, category='delivery', message=message[:500])


@login_required
def delivery_page(request, pk, section='overview'):
    if section not in SECTION_SLUGS:
        raise Http404('Unknown section')
    project = get_object_or_404(Project.objects.select_related('manager', 'qa_lead', 'ba_lead'), pk=pk)
    if request.method == 'POST' and section == 'integration':
        return _integration_post(request, project)
    connections = list(project.delivery_connections.all())
    context = {
        'project': project, 'tabs': TABS, 'tab': 'delivery', 'counts': workspace_counts(project),
        'sections': SECTIONS, 'section': section, 'connections': connections,
        'linked': [c for c in connections if c.is_linked],
        'has_data': WorkItem.objects.filter(project=project).exists() or project.delivery_sync_runs.exists(),
    }
    context.update(globals()[f'_{section}'](request, project, connections))
    return render(request, f'delivery/{section}.html', context)


@login_required
@require_POST
def delivery_sync(request, pk):
    """The Sync button: syncs one connection (`connection`) or every linked one."""
    project = get_object_or_404(Project, pk=pk)
    connections = project.delivery_connections.exclude(external_id='')
    if request.POST.get('connection'):
        connections = connections.filter(pk=request.POST['connection'])
    if not connections:
        messages.error(request, 'No linked tool to sync. Connect one in Sync & Integration first.')
        return redirect('delivery_section', pk=pk, section='integration')
    for connection in connections:
        run = run_sync(connection, request.user)
        label = f'{connection.get_tool_display()} · {connection.external_name}'
        if run.status == 'Failed':
            messages.error(request, f'{label}: sync failed. {run.error}')
        else:
            messages.success(request, f'{label}: synced. {run.created} new, {run.updated} updated, '
                                      f'{run.unchanged} unchanged, {run.removed} removed'
                                      + (f', {len(run.warnings)} warning(s)' if run.warnings else '') + '.')
    back = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        return redirect(back)
    return redirect('delivery_home', pk=pk)


@login_required
def delivery_sprint(request, pk, sprint_id):
    project = get_object_or_404(Project.objects.select_related('manager', 'qa_lead', 'ba_lead'), pk=pk)
    sprint = next((s for s in metrics.sprints(project) if s.pk == sprint_id), None)
    if sprint is None:
        raise Http404('Unknown sprint')
    detail = metrics.sprint_detail(project, sprint)
    scope = detail['scope']

    f = {k: request.GET.get(k, '') for k in ('status', 'assignee', 'kind', 'qa')}

    def match(i):
        return ((not f['status'] or i.category == f['status'])
                and (not f['assignee'] or str(i.assignee_id or 'none') == f['assignee'])
                and (not f['kind'] or i.kind == f['kind'] or (f['kind'] == 'task' and i.kind == 'subtask'))
                and (not f['qa'] or (i.kind == 'story' and i.qa_status == f['qa'])))

    tree = detail['tree']
    if any(f.values()):
        for story in tree:
            story.rows = [r for r in story.rows if match(r)]
            story.dim = not match(story)
        tree = [s for s in tree if not s.dim or s.rows]
    kinds = {k: [i for i in scope if i.kind == k] for k in ('story', 'task', 'subtask', 'bug')}
    stories = kinds['story']

    statuses = {}
    for i in scope:
        key = (['todo', 'in_progress', 'done'].index(i.category), i.status or i.get_category_display(), i.category)
        statuses[key] = statuses.get(key, 0) + 1
    status_rows = [{'name': name, 'category': cat, 'count': n, 'pct': n * 100 / len(scope)}
                   for (_, name, cat), n in sorted(statuses.items())]

    people = {}
    for i in scope:
        if i.assignee_id:
            row = people.setdefault(i.assignee_id, {'name': i.assignee.name, 'open': 0, 'done': 0, 'points': 0, 'bugs': 0})
            row['done' if i.is_done else 'open'] += 1
            row['points'] += (i.story_points or 0) if not i.is_done and i.kind == 'story' else 0
            row['bugs'] += i.kind == 'bug' and not i.is_done
    return render(request, 'delivery/sprint.html', {
        'project': project, 'tabs': TABS, 'tab': 'delivery', 'counts': workspace_counts(project),
        'sections': SECTIONS, 'section': 'sprints', 'connections': list(project.delivery_connections.all()),
        'linked': list(project.delivery_connections.exclude(external_id='')),
        'sprint': sprint, 'tree': tree, 'other_tasks': [i for i in detail['other_tasks'] if match(i)],
        'other_bugs': [i for i in detail['other_bugs'] if match(i)], 'f': f, 'filtered': any(f.values()),
        'stats': {
            'stories': len(stories), 'stories_done': sum(i.is_done for i in stories),
            'verified': sum(i.qa_status == 'Passed' for i in stories),
            'qa_failed': sum(i.qa_status in ('Failed', 'Blocked') for i in stories),
            'tasks': len(kinds['task']), 'tasks_done': sum(i.is_done for i in kinds['task']),
            'subtasks': len(kinds['subtask']), 'subtasks_done': sum(i.is_done for i in kinds['subtask']),
            'bugs': len(kinds['bug']), 'bugs_open': sum(not i.is_done for i in kinds['bug']),
            'points': sum(i.story_points or 0 for i in stories),
            'points_done': sum(i.story_points or 0 for i in stories if i.is_done),
        },
        'status_rows': status_rows, 'people': sorted(people.values(), key=lambda p: (-p['open'], p['name'])),
        'people_filter': sorted({(i.assignee_id, i.assignee.name) for i in scope if i.assignee_id}, key=lambda p: p[1]),
        'qa_statuses': WorkItem.QA_STATUSES, 'categories': WorkItem.CATEGORIES,
    })


@login_required
@require_POST
def delivery_qa(request, pk, item_id):
    """QA verification of a user story; kept in BaigNova, a sync never changes it."""
    item = get_object_or_404(WorkItem, pk=item_id, project_id=pk, kind='story')
    status = request.POST.get('qa_status', '')
    if status in WorkItem.QA_STATUSES and status != item.qa_status:
        old, item.qa_status = item.qa_status, status
        item.qa_by, item.qa_at = request.user, timezone.now()
        item.save(update_fields=['qa_status', 'qa_by', 'qa_at'])
        _log(item.project, request.user, f'QA verification of {item.external_key} "{item.title[:150]}": {old} → {status}')
        messages.success(request, f'{item.external_key} marked {status}.')
    back = request.POST.get('next', '')
    if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        return redirect(back)
    return redirect('delivery_section', pk=pk, section='stories')


# ---------- sections ----------

def _overview(request, project, connections):
    release_list, sprint_list = metrics.releases(project), metrics.sprints(project)
    live = metrics.items(project)
    sprint = metrics.current_sprint(sprint_list)
    upcoming = [r for r in release_list if r.status not in ('Released', 'Archived')]
    kpis = {
        'releases': len(release_list), 'released': sum(r.status == 'Released' for r in release_list),
        'sprints': len(sprint_list), 'closed_sprints': sum(s.state == 'closed' for s in sprint_list),
        'stories': live.filter(kind='story').count(),
        'stories_done': live.filter(kind='story', category='done').count(),
        'epics': live.filter(kind='epic').count(),
        'tasks_open': live.filter(kind__in=['task', 'subtask']).exclude(category='done').count(),
        'bugs': live.filter(kind='bug').count(), 'bugs_open': live.filter(metrics.OPEN_BUG).count(),
        'bugs_serious': live.filter(metrics.OPEN_BUG & metrics.SERIOUS).count(),
    }
    kpis['stories_pct'] = metrics.pct(kpis['stories_done'], kpis['stories'])
    return {'kpis': kpis, 'sprint': sprint, 'next_release': upcoming[0] if upcoming else None,
            'timeline': metrics.timeline(release_list, sprint_list),
            'insights': metrics.insights(project, connections, release_list, sprint_list)[:5],
            'last_run': project.delivery_sync_runs.first()}


def _integration(request, project, connections):
    tool = request.GET.get('tool', '')
    pick = request.GET.get('pick', '')
    context = {'tools': list(CONNECTORS.values()), 'tool': CONNECTORS.get(tool)}
    if pick:
        connection = get_object_or_404(Connection, pk=pick, project=project)
        connection.connector = CONNECTORS[connection.tool]
        context['pick'] = connection
        context['options'] = request.session.get(f'delivery_options_{connection.pk}')
        if context['options'] is None:
            try:
                context['options'] = connector_for(connection).list_projects()
            except ConnectorError as e:
                context['pick_error'] = str(e)
    for c in connections:
        types = (WorkItem.objects.filter(connection=c).values('source_type').annotate(n=Count('id'))
                 .order_by('source_type'))
        configured = c.mapping.get('types', {})
        c.type_rows = [{'name': t['source_type'], 'count': t['n'],
                        'kind': configured.get(t['source_type'].lower()) or guess_kind(t['source_type'])}
                       for t in types if t['source_type']]
        c.connector = CONNECTORS[c.tool]
    context['kinds'] = WorkItem.KINDS
    return context


def _integration_post(request, project):
    action = request.POST.get('action', '')
    back = redirect('delivery_section', pk=project.pk, section='integration')

    if action == 'connect':
        cls = CONNECTORS.get(request.POST.get('tool', ''))
        if not cls or cls.phase > 1:
            messages.error(request, 'This tool cannot be connected yet.')
            return back
        connection = Connection(project=project, tool=cls.tool, created_by=request.user)
        for name, _, _, _ in cls.fields:
            setattr(connection, name, request.POST.get(name, '').strip())
        try:
            options = cls(connection).list_projects()
        except ConnectorError as e:
            messages.error(request, f'Could not connect to {cls.label}: {e}')
            return redirect(f'{request.path}?tool={cls.tool}')
        if not options:
            messages.error(request, f'Connected to {cls.label}, but this account cannot see any {cls.pick_label.lower()}.')
            return redirect(f'{request.path}?tool={cls.tool}')
        connection.save()
        request.session[f'delivery_options_{connection.pk}'] = options
        _log(project, request.user, f'{cls.label} connected')
        return redirect(f'{request.path}?pick={connection.pk}')

    connection = get_object_or_404(Connection, pk=request.POST.get('connection'), project=project)
    label = connection.get_tool_display()

    if action == 'pick':
        options = dict(request.session.get(f'delivery_options_{connection.pk}') or [])
        external_id = request.POST.get('external_id', '')
        if connection.is_linked or external_id not in options:
            messages.error(request, 'Pick one of the listed projects.')
            return redirect(f'{request.path}?pick={connection.pk}')
        connection.external_id, connection.external_name = external_id, options[external_id]
        connection.save(update_fields=['external_id', 'external_name'])
        request.session.pop(f'delivery_options_{connection.pk}', None)
        _log(project, request.user, f'{label} linked to "{connection.external_name}"')
        messages.success(request, f'{label} linked to "{connection.external_name}". Click Sync Now to bring its data in.')
        return back

    if action == 'credentials':
        cls = CONNECTORS[connection.tool]
        for name, _, _, _ in cls.fields:
            value = request.POST.get(name, '').strip()
            if value or name != 'token':  # an empty token box keeps the saved token
                setattr(connection, name, value)
        try:
            cls(connection).check_config()
        except ConnectorError as e:
            messages.error(request, str(e))
            return back
        connection.save()
        _log(project, request.user, f'{label} connection details updated')
        messages.success(request, 'Connection details saved.')
        return back

    if action == 'mapping':
        types = {}
        for key, value in request.POST.items():
            if key.startswith('type:') and value in dict(WorkItem.KINDS):
                types[key[5:].lower()] = value
        connection.mapping['types'] = types
        for name in ('sprint_keyword', 'release_keyword'):
            if name in request.POST:
                connection.mapping[name] = request.POST[name].strip().lower()
        connection.save(update_fields=['mapping'])
        for source_type, kind in types.items():  # apply at once; the next sync keeps it
            WorkItem.objects.filter(connection=connection, source_type__iexact=source_type).update(kind=kind)
        _log(project, request.user, f'{label} mapping updated')
        messages.success(request, 'Mapping saved and applied.')
        return back

    if action == 'delete':
        if request.POST.get('confirm') != 'yes':
            messages.error(request, 'Tick the confirmation box to disconnect.')
            return back
        name = str(connection)
        connection.delete()
        _log(project, request.user, f'{name} disconnected and its synced data deleted')
        messages.success(request, f'{name} disconnected.')
        return back

    messages.error(request, 'Unknown action.')
    return back


def _releases(request, project, connections):
    release_list = metrics.releases(project)
    for r in release_list:
        r.n_sprints = sum(1 for _ in r.sprints.filter(removed=False))
    return {'releases': release_list}


def _sprints(request, project, connections):
    state = request.GET.get('state', '')
    sprint_list = metrics.sprints(project)
    counts = {s: sum(x.state == s for x in sprint_list) for s in ('active', 'future', 'closed')}
    active = metrics.current_sprint(sprint_list)
    total_points = sum(s.points or 0 for s in sprint_list)
    kpis = {'active': active, 'points': total_points, 'n_sprints': len(sprint_list),
            'velocity': metrics.pct(active.points_done or 0, active.points) if active and active.points else None,
            'qa': metrics.qa_verification(project)}
    if state:
        sprint_list = [s for s in sprint_list if s.state == state]
    return {'sprints': sprint_list, 'state': state, 'state_counts': counts, 'kpis': kpis}


def _item_filters(request, project, qs):
    f = {k: request.GET.get(k, '') for k in ('q', 'status', 'release', 'sprint', 'assignee', 'kind', 'severity')}
    if f['q']:
        qs = qs.filter(Q(title__icontains=f['q']) | Q(external_key__icontains=f['q']) | Q(labels__icontains=f['q']))
    if f['status'] in dict(WorkItem.CATEGORIES):
        qs = qs.filter(category=f['status'])
    for field in ('release', 'sprint', 'assignee'):
        if f[field] == 'none':
            qs = qs.filter(**{f'{field}__isnull': True})
        elif f[field].isdigit():
            qs = qs.filter(**{f'{field}_id': f[field]})
    if f['kind']:
        qs = qs.filter(kind=f['kind'])
    if f['severity']:
        qs = qs.filter(Q(severity=f['severity']) | Q(severity='', priority=f['severity']))
    page = Paginator(qs.select_related('assignee', 'sprint', 'release', 'parent'), PAGE_SIZE).get_page(request.GET.get('page'))
    query = request.GET.copy()
    query.pop('page', None)
    return {'page': page, 'f': f, 'query': query.urlencode(), 'filtered': any(f.values()),
            'filter_releases': Release.objects.filter(project=project, removed=False),
            'filter_sprints': Sprint.objects.filter(project=project, removed=False),
            'filter_people': TeamMember.objects.filter(project=project, removed=False),
            'categories': WorkItem.CATEGORIES, 'qa_statuses': WorkItem.QA_STATUSES}


def _stories(request, project, connections):
    live = metrics.items(project)
    epics = live.filter(kind='epic').annotate(
        n_children=Count('children', filter=Q(children__removed=False), distinct=True),
        n_done=Count('children', filter=Q(children__removed=False, children__category='done'), distinct=True))
    for e in epics:
        e.progress = metrics.pct(e.n_done, e.n_children)
    context = _item_filters(request, project, live.filter(kind='story'))
    context['epics'] = epics
    return context


def _tasks(request, project, connections):
    return _item_filters(request, project, metrics.items(project).filter(kind__in=['task', 'subtask']))


def _bugs(request, project, connections):
    live = metrics.items(project).filter(kind='bug')
    by_severity = (live.exclude(category='done').values('severity', 'priority').annotate(n=Count('id')))
    levels = {}
    for row in by_severity:
        name = row['severity'] or row['priority'] or 'Not set'
        levels[name] = levels.get(name, 0) + row['n']
    context = _item_filters(request, project, live)
    context.update({'bug_total': live.count(), 'bug_open': live.exclude(category='done').count(),
                    'bug_serious': live.filter(metrics.OPEN_BUG & metrics.SERIOUS).count(),
                    'bug_levels': sorted(levels.items(), key=lambda x: -x[1]),
                    'severities': sorted({v for row in by_severity for v in (row['severity'] or row['priority'],) if v})})
    return context


def _milestones(request, project, connections):
    deps = list(Dependency.objects.filter(project=project, source__removed=False, target__removed=False)
                .select_related('source', 'target', 'target__sprint', 'source__assignee', 'target__assignee'))
    for d in deps:
        d.risk = d.type == 'blocks' and not d.source.is_done and not d.target.is_done
    release_list = metrics.releases(project)
    return {'milestones': list(project.milestones.all()), 'release_milestones': [r for r in release_list if r.release_date],
            'dependencies': sorted(deps, key=lambda d: (not d.risk, d.source.external_key)),
            'at_risk': sum(d.risk for d in deps)}


def _team(request, project, connections):
    return metrics.workload(project)


def _analytics(request, project, connections):
    release_list, sprint_list = metrics.releases(project), metrics.sprints(project)
    open_releases = [r for r in release_list if r.status not in ('Released', 'Archived')]
    return {'velocity': metrics.velocity(sprint_list), 'bug_trend': metrics.bug_trend(project, sprint_list),
            'readiness': [metrics.readiness(project, r) for r in open_releases],
            'insights': metrics.insights(project, connections, release_list, sprint_list)}


def _history(request, project, connections):
    runs = project.delivery_sync_runs.select_related('started_by', 'connection')
    status = request.GET.get('status', '')
    if status in SyncRun.STATUSES:
        runs = runs.filter(status=status)
    page = Paginator(runs, 20).get_page(request.GET.get('page'))
    selected = None
    if request.GET.get('run', '').isdigit():
        selected = get_object_or_404(SyncRun, pk=request.GET['run'], project=project)
        change = request.GET.get('change', '')
        changes = selected.changes.all()
        if change in ('created', 'updated', 'removed'):
            changes = changes.filter(change=change)
        selected.change_page = Paginator(changes, 100).get_page(request.GET.get('cpage'))
        selected.change_filter = change
        selected.summary_rows = list(selected.summary.items())  # a key named 'items' hides dict.items in templates
        counts = dict(selected.changes.values_list('change').annotate(n=Count('id')).order_by())
        selected.change_tabs = [('', 'All', sum(counts.values()))] + [
            (value, value.title(), counts.get(value, 0)) for value in ('created', 'updated', 'removed')]
    return {'runs': page, 'status': status, 'statuses': SyncRun.STATUSES, 'selected': selected,
            'activity': project.activities.filter(category='delivery').select_related('user')[:15]}
