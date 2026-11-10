from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from delivery.connectors import CONNECTORS as PM_TOOLS
from delivery.models import Sprint, WorkItem
from portfolio.views import TABS, workspace_counts
from projects.models import Project, ProjectActivity

from . import checks
from .forms import ProjectLinkForm, RepositoryForm, SystemIntegrationForm, VersionEndpointForm
from .models import ConnectionCheck, ProjectLink, Repository, SystemIntegration, VersionEndpoint
from .repos import CONNECTORS as REPO_PROVIDERS
from .repos import sync_repository

SECTIONS = [('systems', 'System Integrations', 'diagram-3'), ('links', 'Project Linking', 'link-45deg'),
            ('tools', 'Project Management Tools', 'kanban'), ('repos', 'Repositories & Deployments', 'git')]


def _log(project, user, message):
    ProjectActivity.objects.create(project=project, user=user, category='integration', message=message[:500])


def project_links(project):
    return ProjectLink.objects.filter(Q(source=project) | Q(target=project)).select_related('source', 'target')


def _render(request, project, section, context):
    systems = project.system_integrations.all()
    section_counts = {
        'systems': systems.count(), 'links': project_links(project).count(),
        'tools': project.delivery_connections.exclude(external_id='').count(),
        'repos': project.repositories.count(),
    }
    context.update({
        'project': project, 'tabs': TABS, 'tab': 'integrations', 'counts': workspace_counts(project),
        'section_tabs': [(slug, label, icon, section_counts[slug]) for slug, label, icon in SECTIONS], 'section': section,
        'n_failed': systems.filter(last_status='Failed').count(),
    })
    return render(request, f'integrations/{section}.html', context)


@login_required
def integrations_page(request, pk, section='systems'):
    if section not in dict((s[0], s) for s in SECTIONS):
        raise Http404('Unknown section')
    project = get_object_or_404(Project.objects.select_related('manager', 'qa_lead'), pk=pk)
    result = SECTION_VIEWS[section](request, project)
    if not isinstance(result, dict):
        return result  # a redirect after a POST
    return _render(request, project, section, result)


def _record_check(integration, user, result):
    ConnectionCheck.objects.create(integration=integration, checked_by=user, passed=result['passed'],
                                   code=result['code'], ms=result['ms'], message=result['message'][:500])
    integration.last_status = 'Passed' if result['passed'] else 'Failed'
    integration.last_message = result['message'][:500]
    integration.last_code, integration.last_ms = result['code'], result['ms']
    integration.last_checked_at = timezone.now()
    integration.save(update_fields=['last_status', 'last_message', 'last_code', 'last_ms', 'last_checked_at'])


def _systems(request, project):
    back = redirect('integrations_section', pk=project.pk, section='systems')
    form = SystemIntegrationForm(project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            form = SystemIntegrationForm(request.POST, project=project)
            if form.is_valid():
                integration = form.save(commit=False)
                integration.project, integration.created_by = project, request.user
                integration.save()
                _log(project, request.user, f'System integration "{integration.name}" added')
                messages.success(request, f'"{integration.name}" added. Press Test to check the connection.')
                return back
        elif action == 'test_all':
            items = list(project.system_integrations.all())
            for integration in items:
                _record_check(integration, request.user, checks.check(integration))
            failed = sum(i.last_status == 'Failed' for i in items)
            _log(project, request.user, f'Tested all {len(items)} system integrations: {len(items) - failed} passed, {failed} failed')
            (messages.error if failed else messages.success)(
                request, f'{len(items)} connection(s) tested: {len(items) - failed} passed, {failed} failed.')
            return back
    systems = project.system_integrations.select_related('environment')
    status = request.GET.get('status', '')
    env = request.GET.get('env', '')
    if status:
        systems = systems.filter(last_status=status)
    if env:
        systems = systems.filter(environment_id=env) if env != 'none' else systems.filter(environment__isnull=True)
    all_systems = project.system_integrations.all()
    return {'systems': systems, 'form': form, 'show_form': request.method == 'POST',
            'status': status, 'env': env, 'environments': project.api_environments.all(),
            'statuses': SystemIntegration.STATUSES,
            'kpis': {'total': all_systems.count(), 'passed': all_systems.filter(last_status='Passed').count(),
                     'failed': all_systems.filter(last_status='Failed').count(),
                     'never': all_systems.filter(last_status='Never tested').count()}}


@login_required
def system_detail(request, pk, system_id):
    project = get_object_or_404(Project, pk=pk)
    integration = get_object_or_404(SystemIntegration, pk=system_id, project=project)
    form = SystemIntegrationForm(instance=integration, project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'test':
            result = checks.check(integration)
            _record_check(integration, request.user, result)
            (messages.success if result['passed'] else messages.error)(
                request, f'{integration.name}: connection {"passed" if result["passed"] else "failed"}. {result["message"]}')
            nxt = request.POST.get('next')
            if nxt == 'list':
                return redirect('integrations_section', pk=pk, section='systems')
            return redirect('integrations_system', pk=pk, system_id=integration.pk)
        if action == 'edit':
            form = SystemIntegrationForm(request.POST, instance=integration, project=project)
            if form.is_valid():
                form.save()
                _log(project, request.user, f'System integration "{integration.name}" updated')
                messages.success(request, 'Integration saved.')
                return redirect('integrations_system', pk=pk, system_id=integration.pk)
        if action == 'delete':
            integration.delete()
            _log(project, request.user, f'System integration "{integration.name}" deleted')
            messages.success(request, f'"{integration.name}" deleted.')
            return redirect('integrations_section', pk=pk, section='systems')
    history = integration.checks.select_related('checked_by')[:30]
    return _render(request, project, 'system', {'integration': integration, 'form': form, 'history': history,
                                                'show_edit': request.method == 'POST'})


def _links_section(project):
    rows = []
    for link in project_links(project):
        other, relation = link.view_from(project)
        rows.append({'link': link, 'other': other, 'relation': relation, 'owned': link.source_id == project.pk})
    return rows


def _links_view(request, project):
    back = redirect('integrations_section', pk=project.pk, section='links')
    form = ProjectLinkForm(project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'link':
            form = ProjectLinkForm(request.POST, project=project)
            if form.is_valid():
                link = form.save(commit=False)
                link.source, link.created_by = project, request.user
                link.save()
                message = f'Linked with {link.target.name} ({link.get_relation_display().lower()})'
                _log(project, request.user, message)
                _log(link.target, request.user, f'Linked with {project.name} (from {project.name})')
                messages.success(request, f'{project.name} is now linked with {link.target.name}.')
                return back
        elif action == 'unlink':
            link = get_object_or_404(project_links(project), pk=request.POST.get('link'))
            other, _ = link.view_from(project)
            link.delete()
            _log(project, request.user, f'Link with {other.name} removed')
            _log(other, request.user, f'Link with {project.name} removed (from {project.name})')
            messages.success(request, f'Link with {other.name} removed.')
            return back
    return {'rows': _links_section(project), 'form': form, 'show_form': request.method == 'POST',
            'any_left': form.fields['target'].queryset.exists()}


def _tools(request, project):
    connections = list(project.delivery_connections.all())
    for c in connections:
        kinds = dict(WorkItem.objects.filter(connection=c, removed=False).order_by().values('kind')
                     .annotate(n=Count('id')).values_list('kind', 'n'))
        c.stats = {'sprints': Sprint.objects.filter(connection=c, removed=False).count(),
                   'stories': kinds.get('story', 0), 'tasks': kinds.get('task', 0) + kinds.get('subtask', 0),
                   'bugs': kinds.get('bug', 0)}
        c.recent_runs = list(c.runs.order_by('-started_at')[:3])
        c.connector = PM_TOOLS[c.tool]
    connected = {c.tool for c in connections if c.is_linked}
    return {'connections': connections, 'pm_tools': list(PM_TOOLS.values()), 'connected_tools': connected}


def _repos(request, project):
    back = redirect('integrations_section', pk=project.pk, section='repos')
    form = RepositoryForm(project=project)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'bind':
            form = RepositoryForm(request.POST, project=project)
            if form.is_valid():
                repo = form.save(commit=False)
                repo.project, repo.created_by = project, request.user
                repo.save()
                result = sync_repository(repo)
                _log(project, request.user, f'Repository {repo} bound ({repo.get_provider_display()})')
                (messages.success if result['ok'] else messages.error)(
                    request, f'Repository bound. First sync: {result["message"]}')
                return back
        elif action in ('sync', 'unbind', 'token'):
            repo = get_object_or_404(Repository, pk=request.POST.get('repo'), project=project)
            if action == 'sync':
                result = sync_repository(repo)
                _log(project, request.user, f'Repository {repo} synced: {"ok" if result["ok"] else "failed"}')
                (messages.success if result['ok'] else messages.error)(request, f'{repo}: {result["message"]}')
            elif action == 'token':
                token = request.POST.get('token', '').strip()
                if token:
                    repo.token = token
                    repo.save(update_fields=['token'])
                    messages.success(request, f'{repo}: token updated. Press Sync to read it again.')
            else:
                repo.delete()
                _log(project, request.user, f'Repository {repo} unbound')
                messages.success(request, f'Repository {repo} unbound.')
            return back
        elif action in ('version_save', 'version_check', 'version_remove'):
            environment = get_object_or_404(project.api_environments, pk=request.POST.get('environment'))
            endpoint = VersionEndpoint.objects.filter(environment=environment).first()
            if action == 'version_save':
                vform = VersionEndpointForm(request.POST, instance=endpoint or VersionEndpoint(environment=environment))
                if vform.is_valid():
                    endpoint = vform.save()
                    action = 'version_check'
                else:
                    messages.error(request, f'{environment.name}: ' + ' '.join(e for errs in vform.errors.values() for e in errs))
                    return back
            if action == 'version_remove' and endpoint:
                endpoint.delete()
                messages.success(request, f'{environment.name}: version URL removed.')
                return back
            if action == 'version_check' and endpoint:
                result = checks.read_version(endpoint)
                endpoint.last_status = 'OK' if result['ok'] else 'Failed'
                endpoint.last_value = result['value'] if result['ok'] else endpoint.last_value
                endpoint.last_message = result['message'][:500]
                endpoint.last_checked_at = timezone.now()
                endpoint.save()
                (messages.success if result['ok'] else messages.error)(
                    request, f'{environment.name}: {"running " + result["value"] if result["ok"] else result["message"]}')
            return back

    repos = list(project.repositories.prefetch_related('deployments', 'pipelines', 'pull_requests'))
    environments = list(project.api_environments.all())
    endpoints = {e.environment_id: e for e in VersionEndpoint.objects.filter(environment__in=environments)}
    # environment board: per project environment, the latest deployment of any repository + what the env reports
    board = []
    for env in environments:
        deployments = sorted((d for r in repos for d in r.deployments.all() if d.environment_name.lower() == env.name.lower()),
                             key=lambda d: d.deployed_at or timezone.now(), reverse=True)
        latest = deployments[0] if deployments else None
        endpoint = endpoints.get(env.pk)
        match = None
        if latest and endpoint and endpoint.last_value:
            reported = endpoint.last_value.lower()
            match = any(v and (v.lower() in reported or reported in v.lower())
                        for v in (latest.sha[:7], latest.sha, latest.ref) if len(v or '') >= 3)
        status = (latest.status if latest else '').lower()
        css = ('st-success' if status in ('success', 'succeeded') else
               'st-failed' if status in ('failure', 'failed', 'error', 'canceled', 'cancelled') else 'st-muted')
        board.append({'env': env, 'latest': latest, 'endpoint': endpoint, 'match': match, 'css': css})
    known = {e.name.lower() for e in environments}
    for r in repos:
        r.unmatched = [d for d in r.deployments.all() if d.environment_name.lower() not in known]
        r.open_prs = [p for p in r.pull_requests.all() if p.state == 'open']
    return {'repos': repos, 'form': form, 'show_form': request.method == 'POST', 'board': board,
            'providers': list(REPO_PROVIDERS.values()), 'version_form': VersionEndpointForm()}


SECTION_VIEWS = {'systems': _systems, 'links': _links_view, 'tools': _tools, 'repos': _repos}
