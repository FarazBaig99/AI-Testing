import shlex

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from projects.models import Project

from .forms import ApiTestCaseForm, EnvironmentForm
from .models import (BASE_URL_VAR, ApiFolder, ApiTestCase, CollectionSource, Environment, EnvironmentUrl, ImportSession,
                     url_key)
from .parsers import (CollectionError, fingerprint, load_document, load_from_url, parse_collection, parse_curl,
                      parse_environment)

COMPARED_FIELDS = [('folder', 'Module'), ('method', 'Method'), ('url', 'URL'), ('content_type', 'Content-Type'),
                   ('headers', 'Headers'), ('query_params', 'Query params'), ('body', 'Body')]


# ---------- helpers ----------

def _safe_next(request):
    """A same-site ?next= / next= to return to (the project's APIs & Databases tab, for example)."""
    nxt = request.GET.get('next') or request.POST.get('next') or ''
    return nxt if nxt.startswith('/') and not nxt.startswith('//') else ''


def _current_env(request, project):
    """The environment selected in API Testing (?env=<id>, remembered per project in the session)."""
    key = f'api_env_{project.pk}'
    env_id = request.GET.get('env') or request.session.get(key)
    envs = project.api_environments.all()
    env = envs.filter(pk=env_id).first() if str(env_id or '').isdigit() else None
    env = env or envs.first()
    if env:
        request.session[key] = env.pk
    return env


def _require_env(request, project):
    env = _current_env(request, project)
    if env is None:
        messages.warning(request, 'Create an environment first (name and base URL), then import APIs into it.')
    return env


def _read_upload(f):
    raw = f.read()
    for encoding in ('utf-8-sig', 'utf-16', 'latin-1'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CollectionError('Could not read the uploaded file.')


def _start_review(request, project, env, endpoints, label, source=None):
    for ep in endpoints:
        ep['url'] = env.relativize(ep['url'])
        ep['fingerprint'] = fingerprint(ep)
    session = ImportSession.objects.create(project=project, environment=env, source=source, source_label=label,
                                           endpoints=endpoints, created_by=request.user)
    return redirect('api_review', pk=project.pk, session_id=session.pk)


def _sync_source(request, project, source, env_text=''):
    try:
        doc, spec_url = load_from_url(source.url, source.auth_header)
        endpoints = parse_collection(doc, parse_environment(env_text), spec_url)
    except CollectionError as exc:
        messages.error(request, str(exc))
        return redirect(f'/projects/{project.pk}/api/import/?tab=link')
    return _start_review(request, project, source.environment, endpoints, source.url, source)


# ---------- API Testing home & environments ----------

@login_required
def api_home(request, pk):
    project = get_object_or_404(Project, pk=pk)
    env = _current_env(request, project)
    if request.method == 'POST':
        # "Save APIs": APIs are already stored on import; this confirms and returns to the project
        count = env.test_cases.count() if env else 0
        messages.success(request, f'{count} API test cases saved to {project.name} ({env}).')
        return redirect('project_detail', pk=pk)
    if env is None:
        return render(request, 'apitesting/home.html', {
            'project': project, 'env_form': EnvironmentForm(project=project)})
    return render(request, 'apitesting/home.html', {
        'project': project, 'env': env, 'environments': project.api_environments.all(),
        'folders': env.folders.annotate(n=Count('test_cases')),
        'case_count': env.test_cases.count(),
        'active_source': env.sources.filter(is_active=True).first(),
    })


@login_required
def api_environments(request, pk):
    project = get_object_or_404(Project, pk=pk)
    form = EnvironmentForm(request.POST or None, project=project)
    if request.method == 'POST' and form.is_valid():
        env = form.save(commit=False)
        env.project = project
        env.save()
        request.session[f'api_env_{project.pk}'] = env.pk
        messages.success(request, f'Environment "{env.name}" created and selected. Now import APIs into it.')
        next_url = request.POST.get('next', '')
        return redirect(next_url) if next_url.startswith('/') else redirect('api_home', pk=pk)
    envs = project.api_environments.annotate(n_cases=Count('test_cases', distinct=True),
                                             n_folders=Count('folders', distinct=True))
    return render(request, 'apitesting/environments.html', {
        'project': project, 'environments': envs, 'form': form, 'current': _current_env(request, project),
    })


@login_required
def api_environment_edit(request, pk, env_id):
    project = get_object_or_404(Project, pk=pk)
    env = get_object_or_404(Environment, pk=env_id, project=project)
    form = EnvironmentForm(request.POST or None, instance=env, project=project)
    back = _safe_next(request) or f'/projects/{pk}/api/environments/'
    if form.is_valid():
        form.save()
        messages.success(request, f'Environment "{env.name}" saved.')
        return redirect(back)
    return render(request, 'projects/form.html', {
        'form': form, 'project': project, 'title': f'Edit Environment: {env.name}', 'back_url': back,
    })


@login_required
def api_environment_delete(request, pk, env_id):
    env = get_object_or_404(Environment, pk=env_id, project_id=pk)
    back = _safe_next(request) or f'/projects/{pk}/api/environments/'
    if request.method == 'POST':
        env.delete()
        messages.success(request, f'Environment "{env.name}" and its APIs were deleted.')
        return redirect(back)
    return render(request, 'projects/confirm_delete.html', {
        'object': (f'environment "{env.name}" with its {env.folders.count()} modules, {env.test_cases.count()} APIs '
                   f'and {env.test_results.count()} test runner results'),
        'back_url': back,
    })


@login_required
@require_POST
def api_environment_url_add(request, pk, env_id):
    """Add a URL (host) to an environment: name + URL. The first one becomes the default ({{baseUrl}})."""
    env = get_object_or_404(Environment, pk=env_id, project_id=pk)
    back = _safe_next(request) or f'/projects/{pk}/api/environments/'
    name = request.POST.get('name', '').strip()[:100]
    url = request.POST.get('url', '').strip().rstrip('/')[:500]
    if not name or not url:
        messages.error(request, 'Give the URL a name (e.g. Application client) and the URL itself.')
        return redirect(back)
    if '://' not in url:
        url = 'https://' + url
    key = url_key(name)
    if env.urls.filter(Q(name__iexact=name) | Q(key=key)).exists():
        messages.error(request, f'"{name}" already exists in {env.name}.')
        return redirect(back)
    row = EnvironmentUrl.objects.create(environment=env, name=name, key=key, url=url)
    if not env.urls.filter(is_default=True).exclude(pk=row.pk).exists():
        row.make_default()
    messages.success(request, f'URL "{name}" added to {env.name}: use it as {{{{{key}}}}} in requests.')
    return redirect(back)


@login_required
@require_POST
def api_environment_url_default(request, pk, env_id, url_id):
    row = get_object_or_404(EnvironmentUrl, pk=url_id, environment_id=env_id, environment__project_id=pk)
    row.make_default()
    messages.success(request, f'"{row.name}" is now the default URL ({{{{baseUrl}}}}) of {row.environment.name}.')
    return redirect(_safe_next(request) or f'/projects/{pk}/api/environments/')


@login_required
@require_POST
def api_environment_url_delete(request, pk, env_id, url_id):
    row = get_object_or_404(EnvironmentUrl, pk=url_id, environment_id=env_id, environment__project_id=pk)
    env = row.environment
    was_default = row.is_default
    row.delete()
    if was_default:
        nxt = env.urls.first()
        if nxt:
            nxt.make_default()
        else:
            env.base_url = ''
            env.save(update_fields=['base_url'])
    messages.success(request, f'URL "{row.name}" removed from {env.name}.')
    return redirect(_safe_next(request) or f'/projects/{pk}/api/environments/')


# ---------- import ----------

@login_required
def api_import(request, pk):
    project = get_object_or_404(Project, pk=pk)
    env = _require_env(request, project)
    if env is None:
        return redirect('api_environments', pk=pk)
    sources = env.sources.all()
    tab = request.GET.get('tab', 'upload')
    link_tab = f'/projects/{pk}/api/import/?tab=link'
    manual = {}

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'manual':
            manual = request.POST
            saved = _manual_insert(request, project, env)
            if saved:
                messages.success(request, f'API "{saved.name}" saved to module "{saved.folder.name}" ({env}).')
                return redirect('api_home', pk=pk)
            tab = 'manual'

        if action == 'upload':
            collection = request.FILES.get('collection')
            if not collection:
                messages.error(request, 'Please select an API collection file.')
                return redirect('api_import', pk=pk)
            try:
                env_file = request.FILES.get('environment')
                variables = parse_environment(_read_upload(env_file)) if env_file else {}
                endpoints = parse_collection(load_document(_read_upload(collection)), variables)
            except CollectionError as exc:
                messages.error(request, str(exc))
                return redirect('api_import', pk=pk)
            return _start_review(request, project, env, endpoints, collection.name)

        if action == 'add_url':
            url = request.POST.get('url', '').strip()
            if not url.startswith(('http://', 'https://')):
                messages.error(request, 'Enter a valid http(s) URL.')
            elif sources.filter(url=url).exists():
                messages.warning(request, 'That URL is already configured.')
            else:
                CollectionSource.objects.create(project=project, environment=env, url=url,
                                                is_active=not sources.exists())
            return redirect(link_tab)

        if action == 'set_active':
            source = get_object_or_404(CollectionSource, pk=request.POST.get('source'), environment=env)
            sources.update(is_active=False)
            source.is_active = True
            source.save()
            return redirect(link_tab)

        if action == 'remove_url':
            source = get_object_or_404(CollectionSource, pk=request.POST.get('source'), environment=env)
            was_active = source.is_active
            source.delete()
            if was_active and sources.exists():
                first = sources.first()
                first.is_active = True
                first.save()
            return redirect(link_tab)

        if action == 'sync':
            source = sources.filter(is_active=True).first()
            if not source:
                messages.error(request, 'Please add a URL and select it as active.')
                return redirect(link_tab)
            source.auth_header = request.POST.get('auth_header', '').strip()
            source.save()
            env_file = request.FILES.get('environment')
            try:
                env_text = _read_upload(env_file) if env_file else ''
            except CollectionError as exc:
                messages.error(request, str(exc))
                return redirect(link_tab)
            return _sync_source(request, project, source, env_text)

    return render(request, 'apitesting/import.html', {
        'project': project, 'env': env, 'environments': project.api_environments.all(),
        'sources': sources, 'tab': tab, 'active_source': sources.filter(is_active=True).first(),
        'folders': env.folders.all(), 'manual': manual,
    })


def _manual_insert(request, project, env):
    """Manual Insert API tab: save one API from a pasted cURL into a new or existing module.

    Returns the saved ApiTestCase, or None after adding error messages.
    """
    mode = request.POST.get('module_mode', 'new')
    new_module = request.POST.get('new_module', '').strip()
    module_id = request.POST.get('module_id', '')
    name = request.POST.get('api_name', '').strip()
    curl = request.POST.get('curl', '').strip()

    errors = []
    folder = None
    if mode == 'existing':
        folder = env.folders.filter(pk=module_id).first() if module_id.isdigit() else None
        if folder is None:
            errors.append('Select an existing module.')
    elif not new_module:
        errors.append('Enter a name for the new module.')
    elif env.folders.filter(name__iexact=new_module).exists():
        errors.append(f'Module "{new_module}" already exists in {env}. Choose "Select existing module" instead.')
    if not curl:
        errors.append('Paste the cURL command of the API.')
    if not name:
        errors.append('Enter the API name.')

    parsed = None
    if curl:
        try:
            parsed = parse_curl(curl)
        except CollectionError as exc:
            errors.append(str(exc))
    if parsed:
        # path relative to the environment base URL, so it matches APIs imported from a collection
        parsed['url'] = env.relativize(parsed['url'])
        if parsed['url'].startswith(BASE_URL_VAR):
            parsed['path'] = parsed['url'][len(BASE_URL_VAR):] or '/'
        key = f'{parsed["method"]} {parsed["path"]}'
        duplicate = env.test_cases.select_related('folder').filter(endpoint_key=key).first()
        if duplicate:
            errors.append(f'{key} already exists in {env} as {duplicate.code} in module "{duplicate.folder.name}".')
    if errors:
        for e in errors:
            messages.error(request, e)
        return None

    with transaction.atomic():
        if folder is None:
            folder = ApiFolder.objects.create(project=project, environment=env, name=new_module)
        case = ApiTestCase(project=project, environment=env, folder=folder, endpoint_key=key, name=name,
                           created_by=request.user, method=parsed['method'], path=parsed['path'],
                           url=parsed['url'], content_type=parsed['content_type'],
                           headers=parsed['headers'], query_params=parsed['query_params'], body=parsed['body'])
        case.save()
    return case


@login_required
def api_sync(request, pk):
    """Re-sync the selected environment from its active collection URL (button on the API home page)."""
    project = get_object_or_404(Project, pk=pk)
    env = _require_env(request, project)
    source = env.sources.filter(is_active=True).first() if env else None
    if request.method != 'POST' or not source:
        return redirect(f'/projects/{pk}/api/import/?tab=link')
    return _sync_source(request, project, source)


@login_required
def api_review(request, pk, session_id):
    project = get_object_or_404(Project, pk=pk)
    session = get_object_or_404(ImportSession.objects.select_related('environment'), pk=session_id, project=project)
    env = session.environment
    existing = {tc.endpoint_key: tc for tc in env.test_cases.select_related('folder')}
    existing_folders = set(env.folders.values_list('name', flat=True))

    rows = []
    for ep in session.endpoints:
        tc = existing.get(ep['key'])
        if tc is None:
            change, diff = 'new', 'New API to be imported into folder'
        elif tc.fingerprint == ep['fingerprint']:
            change, diff = 'unchanged', 'No changes detected'
        else:
            current = {'folder': tc.folder.name, 'method': tc.method, 'url': tc.url, 'content_type': tc.content_type,
                       'headers': tc.headers, 'query_params': tc.query_params, 'body': tc.body}
            changed = [label for field, label in COMPARED_FIELDS if current[field] != ep.get(field)]
            change, diff = 'updated', 'Changed: ' + (', '.join(changed) or 'request details')
        rows.append({**ep, 'change': change, 'diff': diff, 'display_url': env.resolve(ep['url'])})

    if request.method == 'POST':
        if 'discard' in request.POST:
            session.delete()
            messages.info(request, 'Import discarded. Nothing was saved.')
            return redirect('api_home', pk=pk)
        selected = set(request.POST.getlist('k'))
        to_save = [r for r in rows if r['key'] in selected and r['change'] != 'unchanged']
        if not to_save:
            messages.warning(request, 'Select at least one new or updated API to import.')
            return redirect('api_review', pk=pk, session_id=session_id)
        with transaction.atomic():
            folders = {f.name: f for f in env.folders.all()}
            for r in to_save:
                folder = folders.get(r['folder'])
                if folder is None:
                    folder = folders[r['folder']] = ApiFolder.objects.create(
                        project=project, environment=env, name=r['folder'])
                values = {
                    'project': project, 'folder': folder, 'name': f'Verify {r["method"]} {r["path"]}',
                    'summary': r['summary'], 'method': r['method'], 'path': r['path'], 'url': r['url'],
                    'content_type': r['content_type'], 'headers': r['headers'], 'path_params': r['path_params'],
                    'query_params': r['query_params'], 'body': r['body'], 'expected_status': r['expected_status'],
                    'fingerprint': r['fingerprint'],
                }
                ApiTestCase.objects.update_or_create(
                    environment=env, endpoint_key=r['key'],
                    defaults=values, create_defaults={**values, 'created_by': request.user})
            if session.source:
                session.source.last_synced_at = timezone.now()
                session.source.save()
            session.delete()
        request.session[f'api_env_{project.pk}'] = env.pk
        messages.success(request, f'Successfully saved {len(to_save)} test cases into their respective modules ({env}).')
        return redirect('api_home', pk=pk)

    filter_by = request.GET.get('filter', 'all')
    groups = {}
    for r in rows:
        if filter_by in ('new', 'updated', 'unchanged') and r['change'] != filter_by:
            continue
        if filter_by == 'folders' and r['folder'] in existing_folders:
            continue
        groups.setdefault(r['folder'], []).append(r)
    folder_names = {r['folder'] for r in rows}
    return render(request, 'apitesting/review.html', {
        'project': project, 'session': session, 'env': env, 'filter': filter_by,
        'groups': [{'name': name, 'is_new': name not in existing_folders, 'rows': items}
                   for name, items in groups.items()],
        'other_host': sum(not r['url'].startswith(BASE_URL_VAR) for r in rows),
        'counts': {
            'all': len(rows),
            'new': sum(r['change'] == 'new' for r in rows),
            'updated': sum(r['change'] == 'updated' for r in rows),
            'unchanged': sum(r['change'] == 'unchanged' for r in rows),
            'folders': len(folder_names - existing_folders),
        },
    })


# ---------- modules & test cases ----------

@login_required
def api_folder(request, pk, folder_id):
    folder = get_object_or_404(ApiFolder.objects.select_related('project', 'environment'), pk=folder_id, project_id=pk)
    if request.method == 'POST':
        ids = request.POST.getlist('case')
        deleted, _ = folder.test_cases.filter(pk__in=ids).delete()
        messages.success(request, f'{deleted} test case(s) deleted.')
        return redirect('api_folder', pk=pk, folder_id=folder_id)
    return render(request, 'apitesting/folder.html', {
        'project': folder.project, 'folder': folder, 'env': folder.environment, 'cases': folder.test_cases.all(),
    })


@login_required
def api_folder_delete(request, pk, folder_id):
    folder = get_object_or_404(ApiFolder, pk=folder_id, project_id=pk)
    if request.method == 'POST':
        folder.delete()
        messages.success(request, 'Module deleted.')
        return redirect('api_home', pk=pk)
    return render(request, 'projects/confirm_delete.html', {'object': folder, 'back_url': f'/projects/{pk}/api/'})


def _full_url(case):
    query = '&'.join(f'{k}={v}' for k, v in case.query_params.items() if v not in ('', None))
    url = case.environment.resolve(case.url)
    return f'{url}?{query}' if query else url


def _curl(case):
    parts = [f'curl -X {case.method} {shlex.quote(_full_url(case))}']
    parts += [f'-H {shlex.quote(f"{k}: {v}")}' for k, v in case.headers.items()]
    if case.body:
        parts.append(f'--data-raw {shlex.quote(case.body)}')
    return ' \\\n  '.join(parts)


@login_required
def api_case(request, pk, case_id):
    case = get_object_or_404(ApiTestCase.objects.select_related('project', 'folder', 'environment'),
                             pk=case_id, project_id=pk)
    return render(request, 'apitesting/case.html', {
        'project': case.project, 'case': case, 'env': case.environment,
        'curl': _curl(case), 'full_url': _full_url(case),
    })


@login_required
def api_case_edit(request, pk, case_id):
    case = get_object_or_404(ApiTestCase.objects.select_related('project', 'environment'), pk=case_id, project_id=pk)
    form = ApiTestCaseForm(request.POST or None, instance=case)
    if form.is_valid():
        form.save()
        messages.success(request, f'{case.code} saved.')
        return redirect('api_case', pk=pk, case_id=case_id)
    return render(request, 'projects/form.html', {
        'form': form, 'project': case.project, 'title': f'Edit {case.code} ({case.environment})',
        'back_url': f'/projects/{pk}/api/cases/{case_id}/',
    })


@login_required
def api_case_delete(request, pk, case_id):
    case = get_object_or_404(ApiTestCase, pk=case_id, project_id=pk)
    if request.method == 'POST':
        folder_id = case.folder_id
        case.delete()
        messages.success(request, 'Test case deleted.')
        return redirect('api_folder', pk=pk, folder_id=folder_id)
    return render(request, 'projects/confirm_delete.html', {'object': case, 'back_url': f'/projects/{pk}/api/cases/{case_id}/'})
