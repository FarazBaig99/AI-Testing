"""API Testing workspace (Postman-style): pick projects + environments, browse their APIs, build collections,
send requests and read the response."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from projects.models import Project

from . import runner
from .models import BODY_MODES, RAW_LANGUAGES, ApiCollection, ApiTestCase, CollectionRequest, Environment

SESSION_KEY = 'api_ws'
METHODS = [m for m, _ in ApiTestCase.METHOD_CHOICES]
SPEC_FIELDS = ['method', 'url', 'params', 'headers', 'auth', 'body_mode', 'raw_language', 'body', 'form_data',
               'urlencoded', 'graphql_query', 'graphql_variables', 'verify_ssl', 'expected_status']


# ---------- selection (projects + environments, kept in the session) ----------

def get_selection(request):
    """{project_id: [env_id, ...]} of what the workspace shows."""
    raw = request.session.get(SESSION_KEY) or {}
    return {int(p): [int(e) for e in envs] for p, envs in raw.items()}


def set_selection(request, selection):
    request.session[SESSION_KEY] = {str(p): envs for p, envs in selection.items()}


def _change_selection(request):
    selection = get_selection(request)
    action = request.POST.get('action')
    project = Project.objects.filter(pk=request.POST.get('project') or 0).first()
    if project is None:
        return
    if action == 'add_project':
        first = project.api_environments.first()
        selection.setdefault(project.pk, [first.pk] if first else [])
    elif action == 'remove_project':
        selection.pop(project.pk, None)
    elif action == 'toggle_env':
        env = project.api_environments.filter(pk=request.POST.get('env') or 0).first()
        if env:
            envs = selection.setdefault(project.pk, [])
            envs.remove(env.pk) if env.pk in envs else envs.append(env.pk)
    set_selection(request, selection)


# ---------- spec <-> models ----------

def _kv(mapping, enabled=None):
    return [{'key': k, 'value': '' if v is None else str(v), 'enabled': enabled(v) if enabled else True}
            for k, v in (mapping or {}).items()]


def _body_from_case(case):
    """body_mode, raw_language, body, form_data, urlencoded for a synced API."""
    ct = (case.content_type or case.headers.get('Content-Type', '')).lower()
    body = case.body or ''
    try:
        parsed = json.loads(body) if body.strip() else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and 'x-www-form-urlencoded' in ct:
        return 'urlencoded', 'json', '', [], _kv(parsed)
    if isinstance(parsed, dict) and 'multipart' in ct:
        return 'form-data', 'json', '', [dict(r, type='text') for r in _kv(parsed)], []
    if not body.strip():
        return 'none', 'json', '', [], []
    language = runner.guess_language(ct, body)
    return 'raw', language if language in RAW_TYPES_KEYS else 'text', body, [], []


RAW_TYPES_KEYS = {k for k, _ in RAW_LANGUAGES}


def case_spec(case):
    params = _kv(case.query_params, enabled=lambda v: v not in ('', None))
    query = '&'.join(f"{p['key']}={p['value']}" for p in params if p['enabled'])
    mode, language, body, form_data, urlencoded = _body_from_case(case)
    headers = [h for h in _kv(case.headers)
               if not (mode in ('form-data', 'urlencoded') and h['key'].lower() == 'content-type')]
    return {
        'kind': 'case', 'case': case.pk, 'item': None, 'name': case.name,
        'project': case.project_id, 'project_name': case.project.name,
        'environment': case.environment_id, 'environment_name': case.environment.name if case.environment else '',
        'base_url': case.environment.base_url if case.environment else '',
        'method': case.method, 'url': case.url + (f'?{query}' if query else ''),
        'params': params, 'headers': headers, 'auth': {'type': 'none'},
        'body_mode': mode, 'raw_language': language, 'body': body, 'form_data': form_data,
        'urlencoded': urlencoded, 'graphql_query': '', 'graphql_variables': '', 'verify_ssl': True,
        'expected_status': case.expected_status,
    }


def item_spec(item):
    spec = {f: getattr(item, f) for f in SPEC_FIELDS}
    spec.update({
        'kind': 'item', 'item': item.pk, 'case': item.source_id, 'name': item.name,
        'collection': item.collection_id, 'collection_name': item.collection.name,
        'project': item.project_id, 'project_name': item.project.name if item.project else '',
        'environment': item.environment_id, 'environment_name': item.environment.name if item.environment else '',
        'base_url': item.environment.base_url if item.environment else '',
    })
    return spec


def _clean_rows(value, file_rows=False):
    out = []
    for r in value if isinstance(value, list) else []:
        if not isinstance(r, dict) or not (str(r.get('key', '')).strip() or str(r.get('value', '')).strip()):
            continue
        row = {'key': str(r.get('key', '')), 'value': str(r.get('value', '')), 'enabled': bool(r.get('enabled', True))}
        if file_rows:
            row['type'] = 'file' if r.get('type') == 'file' else 'text'
            if row['type'] == 'file':
                row['value'] = ''      # files are chosen again at send time, never stored
        out.append(row)
    return out


def clean_spec(data):
    """Validate a spec posted by the page."""
    method = str(data.get('method') or 'GET').upper()
    auth = data.get('auth') if isinstance(data.get('auth'), dict) else {}
    auth_type = auth.get('type') if auth.get('type') in ('none', 'bearer', 'basic', 'apikey') else 'none'
    return {
        'method': method if method in METHODS else 'GET',
        'url': str(data.get('url') or '')[:2000],
        'params': _clean_rows(data.get('params')),
        'headers': _clean_rows(data.get('headers')),
        'auth': {'type': auth_type, **{k: str(auth.get(k, '')) for k in
                                         ('token', 'username', 'password', 'key', 'value', 'add_to') if k in auth}},
        'body_mode': data.get('body_mode') if data.get('body_mode') in dict(BODY_MODES) else 'none',
        'raw_language': data.get('raw_language') if data.get('raw_language') in RAW_TYPES_KEYS else 'json',
        'body': str(data.get('body') or ''),
        'form_data': _clean_rows(data.get('form_data'), file_rows=True),
        'urlencoded': _clean_rows(data.get('urlencoded')),
        'graphql_query': str(data.get('graphql_query') or ''),
        'graphql_variables': str(data.get('graphql_variables') or ''),
        'verify_ssl': bool(data.get('verify_ssl', True)),
        'expected_status': _status_code(data.get('expected_status')),
    }


def _status_code(value):
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return code if 100 <= code <= 599 else None


def _next_position(collection):
    return (collection.requests.aggregate(m=Max('position'))['m'] or 0) + 1


def item_from_case(collection, case, position):
    spec = case_spec(case)
    item = CollectionRequest(collection=collection, project=case.project, environment=case.environment, source=case,
                             name=case.name, position=position)
    for field in SPEC_FIELDS:
        setattr(item, field, spec[field])
    item.save()
    return item


def _copy_name(name, limit):
    return (name[:limit - 5] + ' Copy') if len(name) + 5 > limit else name + ' Copy'


def duplicate_item(item, collection=None, position=None):
    """Copy a saved request; by default right below the original in the same collection."""
    copy = CollectionRequest.objects.get(pk=item.pk)
    copy.pk = None
    copy.last_status = copy.last_ms = copy.last_run_at = None
    if collection is None:
        item.collection.requests.filter(position__gt=item.position).update(position=F('position') + 1)
        copy.name, copy.position = _copy_name(item.name, 600), item.position + 1
    else:
        copy.collection, copy.position = collection, position
    copy.save()
    return copy


def duplicate_collection(collection, user):
    copy = ApiCollection.objects.create(name=_copy_name(collection.name, 200), description=collection.description,
                                        created_by=user)
    for i, item in enumerate(collection.requests.all(), 1):
        duplicate_item(item, copy, i)
    return copy


def move_item(item, target, before=None):
    """Put a request into `target` just above `before` (or at the end) and renumber that collection."""
    rows = list(target.requests.exclude(pk=item.pk))
    index = rows.index(before) if before in rows else len(rows)
    rows.insert(index, item)
    item.collection = target
    for i, row in enumerate(rows, 1):
        row.position = i
    CollectionRequest.objects.bulk_update(rows, ['collection', 'position'])


def _environment(env_id):
    return Environment.objects.select_related('project').filter(pk=env_id or 0).first()


# ---------- pages ----------

@login_required
def api_workspace(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('add_project', 'remove_project', 'toggle_env'):
            _change_selection(request)
        elif action == 'new_collection':
            name = request.POST.get('name', '').strip()
            if name:
                c = ApiCollection.objects.create(name=name[:200], created_by=request.user)
                messages.success(request, f'Collection "{c.name}" created. Open an API and press Save to add it.')
            else:
                messages.error(request, 'Give the collection a name.')
        elif action == 'rename_collection':
            c = get_object_or_404(ApiCollection, pk=request.POST.get('collection'))
            name = request.POST.get('name', '').strip()
            if name:
                c.name = name[:200]
                c.save(update_fields=['name'])
        elif action == 'delete_collection':
            c = get_object_or_404(ApiCollection, pk=request.POST.get('collection'))
            c.delete()
            messages.success(request, f'Collection "{c.name}" deleted.')
        elif action == 'add_request':
            c = get_object_or_404(ApiCollection, pk=request.POST.get('collection'))
            item = CollectionRequest.objects.create(collection=c, name='New Request', method='GET', url='',
                                                    position=_next_position(c))
            return redirect(f"{reverse('api_workspace')}?item={item.pk}")
        elif action == 'add_apis':
            c = get_object_or_404(ApiCollection, pk=request.POST.get('collection'))
            ids = [int(x) for x in request.POST.get('cases', '').split(',') if x.strip().isdigit()]
            cases = ApiTestCase.objects.select_related('project', 'environment').in_bulk(ids)
            position = _next_position(c)
            added = 0
            for case_id in ids:          # keep the order the APIs were picked in
                if case_id in cases:
                    item_from_case(c, cases[case_id], position + added)
                    added += 1
            if added:
                messages.success(request, f'{added} API{"s" if added > 1 else ""} added to "{c.name}" in the order you picked.')
            else:
                messages.error(request, 'Pick at least one API.')
            return redirect(f"{reverse('api_workspace')}?tab=collections&open={c.pk}")
        elif action == 'duplicate_item':
            item = get_object_or_404(CollectionRequest, pk=request.POST.get('item'))
            copy = duplicate_item(item)
            messages.success(request, f'"{item.name}" duplicated.')
            return redirect(f"{reverse('api_workspace')}?item={copy.pk}")
        elif action == 'duplicate_collection':
            c = get_object_or_404(ApiCollection, pk=request.POST.get('collection'))
            copy = duplicate_collection(c, request.user)
            messages.success(request, f'Collection "{c.name}" duplicated as "{copy.name}".')
            return redirect(f"{reverse('api_workspace')}?open={copy.pk}")
        elif action == 'delete_item':
            item = get_object_or_404(CollectionRequest, pk=request.POST.get('item'))
            item.delete()
            messages.success(request, f'"{item.name}" removed from {item.collection.name}.')
        return redirect('api_workspace')

    selection = get_selection(request)
    projects = Project.objects.filter(pk__in=selection).prefetch_related('api_environments')
    groups, selected = [], []
    for project in projects:
        envs = list(project.api_environments.all())
        chosen = [e for e in envs if e.pk in selection[project.pk]]
        selected.append({'project': project, 'envs': [{'env': e, 'on': e in chosen} for e in envs]})
        for env in chosen:
            cases = (ApiTestCase.objects.filter(environment=env).select_related('folder')
                     .only('id', 'name', 'method', 'path', 'folder__name').order_by('folder__name', 'id'))
            folders = {}
            for case in cases:
                folders.setdefault(case.folder.name, []).append(case)
            groups.append({'project': project, 'env': env, 'count': len(cases),
                           'folders': [{'name': n, 'cases': c} for n, c in folders.items()]})
    collections = ApiCollection.objects.prefetch_related('requests')
    return render(request, 'apitesting/workspace.html', {
        'selected': selected, 'groups': groups, 'collections': collections,
        'other_projects': Project.objects.exclude(pk__in=selection).order_by('name'),
        'methods': METHODS, 'body_modes': BODY_MODES, 'raw_languages': RAW_LANGUAGES,
        'open_case': request.GET.get('case', ''), 'open_item': request.GET.get('item', ''),
        'open_collection': request.GET.get('open', ''),
        'tab': 'collections' if request.GET.get('item') or request.GET.get('open') else request.GET.get('tab', 'apis'),
    })


@login_required
def api_ws_request(request):
    """The spec of a synced API (?case=) or a saved collection request (?item=) for the builder."""
    if request.GET.get('item'):
        item = get_object_or_404(CollectionRequest.objects.select_related('collection', 'project', 'environment'),
                                 pk=request.GET['item'])
        return JsonResponse(item_spec(item))
    case = get_object_or_404(ApiTestCase.objects.select_related('project', 'environment'), pk=request.GET.get('case'))
    return JsonResponse(case_spec(case))


@login_required
@require_POST
def api_ws_send(request, opener=None):
    try:
        data = json.loads(request.POST.get('spec') or '{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    spec = clean_spec(data)
    env = _environment(data.get('environment'))
    result = runner.send(spec, env, request.FILES, opener=opener)
    item = CollectionRequest.objects.filter(pk=data.get('item') or 0).first()
    if item and result.get('ok'):
        item.last_status, item.last_ms, item.last_run_at = result['status'], result['ms'], timezone.now()
        item.save(update_fields=['last_status', 'last_ms', 'last_run_at'])
    return JsonResponse(result)


@login_required
@require_POST
def api_ws_move(request):
    """Drag and drop: reorder a request, or move it into another collection."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    item = get_object_or_404(CollectionRequest, pk=data.get('item') or 0)
    target = get_object_or_404(ApiCollection, pk=data.get('collection') or 0)
    before = target.requests.filter(pk=data.get('before') or 0).first()
    with transaction.atomic():
        move_item(item, target, before)
    return JsonResponse({'ok': True, 'order': list(target.requests.values_list('pk', flat=True))})


@login_required
@require_POST
def api_ws_rename(request):
    """Inline rename of a saved request or a collection."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    model, limit = (ApiCollection, 200) if data.get('kind') == 'collection' else (CollectionRequest, 600)
    obj = get_object_or_404(model, pk=data.get('id') or 0)
    name = str(data.get('name') or '').strip()[:limit]
    if not name:
        return JsonResponse({'ok': False, 'error': 'Name cannot be empty.'})
    obj.name = name
    obj.save(update_fields=['name'])
    return JsonResponse({'ok': True, 'name': obj.name})


@login_required
@require_POST
def api_ws_save(request):
    """Update a saved request, or save the open request into a (new) collection."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    spec = clean_spec(data)
    name = str(data.get('name') or '').strip()[:600]
    item = CollectionRequest.objects.filter(pk=data.get('item') or 0).first() if not data.get('save_as') else None
    if item is None:
        new_name = str(data.get('new_collection') or '').strip()
        if new_name:
            collection = ApiCollection.objects.create(name=new_name[:200], created_by=request.user)
        else:
            collection = ApiCollection.objects.filter(pk=data.get('collection') or 0).first()
        if collection is None:
            return JsonResponse({'ok': False, 'error': 'Choose a collection or type a new collection name.'})
        env = _environment(data.get('environment'))
        source = ApiTestCase.objects.filter(pk=data.get('case') or 0).first()
        position = _next_position(collection)
        item = CollectionRequest(collection=collection, environment=env, project=env.project if env else None,
                                 source=source, position=position)
    item.name = name or item.name or spec['url'] or 'New Request'
    for field, value in spec.items():
        setattr(item, field, value)
    item.save()
    return JsonResponse({'ok': True, 'item': item.pk, 'collection': item.collection_id,
                         'collection_name': item.collection.name})
