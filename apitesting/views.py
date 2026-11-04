import shlex

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from projects.models import Project

from .forms import ApiTestCaseForm
from .models import ApiFolder, ApiTestCase, CollectionSource, ImportSession
from .parsers import CollectionError, load_document, load_from_url, parse_collection, parse_environment

COMPARED_FIELDS = [('folder', 'Module'), ('method', 'Method'), ('url', 'URL'), ('content_type', 'Content-Type'),
                   ('headers', 'Headers'), ('query_params', 'Query params'), ('body', 'Body')]


def _read_upload(f):
    raw = f.read()
    for encoding in ('utf-8-sig', 'utf-16', 'latin-1'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CollectionError('Could not read the uploaded file.')


def _start_review(request, project, endpoints, label, source=None):
    session = ImportSession.objects.create(project=project, source=source, source_label=label,
                                           endpoints=endpoints, created_by=request.user)
    return redirect('api_review', pk=project.pk, session_id=session.pk)


def _sync_source(request, project, source, env_text=''):
    try:
        doc, spec_url = load_from_url(source.url, source.auth_header)
        endpoints = parse_collection(doc, parse_environment(env_text), spec_url)
    except CollectionError as exc:
        messages.error(request, str(exc))
        return redirect(f'/projects/{project.pk}/api/import/?tab=link')
    return _start_review(request, project, endpoints, source.url, source)


@login_required
def api_home(request, pk):
    project = get_object_or_404(Project, pk=pk)
    folders = project.api_folders.annotate(n=Count('test_cases'))
    return render(request, 'apitesting/home.html', {
        'project': project, 'folders': folders,
        'case_count': project.api_test_cases.count(),
        'active_source': project.api_sources.filter(is_active=True).first(),
    })


@login_required
def api_import(request, pk):
    project = get_object_or_404(Project, pk=pk)
    sources = project.api_sources.all()
    tab = request.GET.get('tab', 'upload')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'upload':
            collection = request.FILES.get('collection')
            if not collection:
                messages.error(request, 'Please select an API collection file.')
                return redirect('api_import', pk=pk)
            try:
                env_file = request.FILES.get('environment')
                env = parse_environment(_read_upload(env_file)) if env_file else {}
                endpoints = parse_collection(load_document(_read_upload(collection)), env)
            except CollectionError as exc:
                messages.error(request, str(exc))
                return redirect('api_import', pk=pk)
            return _start_review(request, project, endpoints, collection.name)

        if action == 'add_url':
            url = request.POST.get('url', '').strip()
            if not url.startswith(('http://', 'https://')):
                messages.error(request, 'Enter a valid http(s) URL.')
            elif sources.filter(url=url).exists():
                messages.warning(request, 'That URL is already configured.')
            else:
                CollectionSource.objects.create(project=project, url=url, is_active=not sources.exists())
            return redirect(f'/projects/{pk}/api/import/?tab=link')

        if action == 'set_active':
            source = get_object_or_404(CollectionSource, pk=request.POST.get('source'), project=project)
            sources.update(is_active=False)
            source.is_active = True
            source.save()
            return redirect(f'/projects/{pk}/api/import/?tab=link')

        if action == 'remove_url':
            source = get_object_or_404(CollectionSource, pk=request.POST.get('source'), project=project)
            was_active = source.is_active
            source.delete()
            if was_active and sources.exists():
                sources.update(is_active=False)
                first = sources.first()
                first.is_active = True
                first.save()
            return redirect(f'/projects/{pk}/api/import/?tab=link')

        if action == 'sync':
            source = sources.filter(is_active=True).first()
            if not source:
                messages.error(request, 'Please add a URL and select it as active.')
                return redirect(f'/projects/{pk}/api/import/?tab=link')
            source.auth_header = request.POST.get('auth_header', '').strip()
            source.save()
            env_file = request.FILES.get('environment')
            try:
                env_text = _read_upload(env_file) if env_file else ''
            except CollectionError as exc:
                messages.error(request, str(exc))
                return redirect(f'/projects/{pk}/api/import/?tab=link')
            return _sync_source(request, project, source, env_text)

    return render(request, 'apitesting/import.html', {
        'project': project, 'sources': sources, 'tab': tab,
        'active_source': sources.filter(is_active=True).first(),
    })


@login_required
def api_sync(request, pk):
    """Re-sync from the active collection URL (button on the API home page)."""
    project = get_object_or_404(Project, pk=pk)
    source = project.api_sources.filter(is_active=True).first()
    if request.method != 'POST' or not source:
        return redirect(f'/projects/{pk}/api/import/?tab=link')
    return _sync_source(request, project, source)


@login_required
def api_review(request, pk, session_id):
    project = get_object_or_404(Project, pk=pk)
    session = get_object_or_404(ImportSession, pk=session_id, project=project)
    existing = {tc.endpoint_key: tc for tc in project.api_test_cases.select_related('folder')}
    existing_folders = set(project.api_folders.values_list('name', flat=True))

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
        rows.append({**ep, 'change': change, 'diff': diff})

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
            folders = {f.name: f for f in project.api_folders.all()}
            for r in to_save:
                folder = folders.get(r['folder'])
                if folder is None:
                    folder = folders[r['folder']] = ApiFolder.objects.create(project=project, name=r['folder'])
                values = {
                    'folder': folder, 'name': f'Verify {r["method"]} {r["path"]}', 'summary': r['summary'],
                    'method': r['method'], 'path': r['path'], 'url': r['url'], 'content_type': r['content_type'],
                    'headers': r['headers'], 'path_params': r['path_params'], 'query_params': r['query_params'],
                    'body': r['body'], 'expected_status': r['expected_status'], 'fingerprint': r['fingerprint'],
                }
                ApiTestCase.objects.update_or_create(
                    project=project, endpoint_key=r['key'],
                    defaults=values, create_defaults={**values, 'created_by': request.user})
            if session.source:
                session.source.last_synced_at = timezone.now()
                session.source.save()
            session.delete()
        messages.success(request, f'Successfully saved {len(to_save)} test cases into their respective modules.')
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
        'project': project, 'session': session, 'filter': filter_by,
        'groups': [{'name': name, 'is_new': name not in existing_folders, 'rows': items}
                   for name, items in groups.items()],
        'counts': {
            'all': len(rows),
            'new': sum(r['change'] == 'new' for r in rows),
            'updated': sum(r['change'] == 'updated' for r in rows),
            'unchanged': sum(r['change'] == 'unchanged' for r in rows),
            'folders': len(folder_names - existing_folders),
        },
    })


@login_required
def api_folder(request, pk, folder_id):
    folder = get_object_or_404(ApiFolder.objects.select_related('project'), pk=folder_id, project_id=pk)
    if request.method == 'POST':
        ids = request.POST.getlist('case')
        deleted, _ = folder.test_cases.filter(pk__in=ids).delete()
        messages.success(request, f'{deleted} test case(s) deleted.')
        return redirect('api_folder', pk=pk, folder_id=folder_id)
    return render(request, 'apitesting/folder.html', {
        'project': folder.project, 'folder': folder, 'cases': folder.test_cases.all(),
    })


@login_required
def api_folder_delete(request, pk, folder_id):
    folder = get_object_or_404(ApiFolder, pk=folder_id, project_id=pk)
    if request.method == 'POST':
        folder.delete()
        messages.success(request, 'Module deleted.')
        return redirect('api_home', pk=pk)
    return render(request, 'projects/confirm_delete.html', {'object': folder, 'back_url': f'/projects/{pk}/api/'})


def _curl(case):
    parts = [f'curl -X {case.method} {shlex.quote(_full_url(case))}']
    parts += [f'-H {shlex.quote(f"{k}: {v}")}' for k, v in case.headers.items()]
    if case.body:
        parts.append(f'--data-raw {shlex.quote(case.body)}')
    return ' \\\n  '.join(parts)


def _full_url(case):
    query = '&'.join(f'{k}={v}' for k, v in case.query_params.items() if v not in ('', None))
    return f'{case.url}?{query}' if query else case.url


@login_required
def api_case(request, pk, case_id):
    case = get_object_or_404(ApiTestCase.objects.select_related('project', 'folder'), pk=case_id, project_id=pk)
    return render(request, 'apitesting/case.html', {
        'project': case.project, 'case': case, 'curl': _curl(case), 'full_url': _full_url(case),
    })


@login_required
def api_case_edit(request, pk, case_id):
    case = get_object_or_404(ApiTestCase.objects.select_related('project'), pk=case_id, project_id=pk)
    form = ApiTestCaseForm(request.POST or None, instance=case)
    if form.is_valid():
        form.save()
        messages.success(request, f'{case.code} saved.')
        return redirect('api_case', pk=pk, case_id=case_id)
    return render(request, 'projects/form.html', {
        'form': form, 'project': case.project, 'title': f'Edit {case.code}',
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
