"""API Testing workspace (Postman-style): pick projects + environments, browse their APIs, build collections,
send requests and read the response."""
import json
import re
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from projects.models import Project

from . import auth, lov, runner, scripting
from .parsers import is_empty_body
from .models import (BODY_MODES, RAW_LANGUAGES, ApiCollection, ApiGlobals, ApiLov, ApiTestCase, AuthProfile, ProjectVariables,
                     CollectionRequest, Environment)

SESSION_KEY = 'api_ws'
METHODS = [m for m, _ in ApiTestCase.METHOD_CHOICES]
SPEC_FIELDS = ['method', 'url', 'params', 'headers', 'auth', 'body_mode', 'raw_language', 'body', 'form_data',
               'urlencoded', 'graphql_query', 'graphql_variables', 'verify_ssl', 'expected_status',
               'pre_request_script', 'test_script']
SCRIPT_LIMIT = 100000


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
    language = language if language in RAW_TYPES_KEYS else 'text'
    if case.method in runner.NO_BODY_METHODS and is_empty_body(body):
        # GET / HEAD: no body by default; the template stays ready under "raw"
        return 'none', language, body, [], []
    return 'raw', language, body, [], []


RAW_TYPES_KEYS = {k for k, _ in RAW_LANGUAGES}


def case_spec(case):
    """The builder's spec of a synced API: what Save stored on it, else the values from the sync."""
    params = _kv(case.query_params, enabled=lambda v: v not in ('', None))
    query = '&'.join(f"{p['key']}={p['value']}" for p in params if p['enabled'])
    mode, language, body, form_data, urlencoded = _body_from_case(case)
    headers = [h for h in _kv(case.headers)
               if not (mode in ('form-data', 'urlencoded') and h['key'].lower() == 'content-type')]
    spec = {
        'kind': 'case', 'case': case.pk, 'item': None, 'name': case.name, 'saved': bool(case.spec),
        'project': case.project_id, 'project_name': case.project.name,
        'environment': case.environment_id, 'environment_name': case.environment.name if case.environment else '',
        'base_url': case.environment.base_url if case.environment else '',
        'method': case.method, 'url': case.url + (f'?{query}' if query else ''),
        'params': params, 'headers': headers, 'auth': {'type': 'none'},
        'body_mode': mode, 'raw_language': language, 'body': body, 'form_data': form_data,
        'urlencoded': urlencoded, 'graphql_query': '', 'graphql_variables': '', 'verify_ssl': True,
        'expected_status': case.expected_status, 'pre_request_script': '', 'test_script': '',
    }
    if case.spec:
        spec.update({f: case.spec[f] for f in SPEC_FIELDS if f in case.spec})
    return spec


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
                                         ('token', 'username', 'password', 'key', 'value', 'add_to') if k in auth},
                 # which of the environment's logins the request is sent with ('' = default, 'none' = no login)
                 **({'profile': str(auth['profile'])[:100]} if auth.get('profile') else {})},
        'body_mode': data.get('body_mode') if data.get('body_mode') in dict(BODY_MODES) else 'none',
        'raw_language': data.get('raw_language') if data.get('raw_language') in RAW_TYPES_KEYS else 'json',
        'body': str(data.get('body') or ''),
        'form_data': _clean_rows(data.get('form_data'), file_rows=True),
        'urlencoded': _clean_rows(data.get('urlencoded')),
        'graphql_query': str(data.get('graphql_query') or ''),
        'graphql_variables': str(data.get('graphql_variables') or ''),
        'verify_ssl': bool(data.get('verify_ssl', True)),
        'expected_status': _status_code(data.get('expected_status')),
        'pre_request_script': str(data.get('pre_request_script') or '')[:SCRIPT_LIMIT],
        'test_script': str(data.get('test_script') or '')[:SCRIPT_LIMIT],
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
                                        variables=collection.variables, pre_request_script=collection.pre_request_script,
                                        test_script=collection.test_script, created_by=user)
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
        'auth_profiles_json': json.dumps(_profiles_by_env(Environment.objects.filter(project_id__in=selection))),
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
def api_ws_send(request, opener=None, login_opener=None):
    try:
        data = json.loads(request.POST.get('spec') or '{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    spec = clean_spec(data)
    env = _environment(data.get('environment'))
    item = CollectionRequest.objects.select_related('collection').filter(pk=data.get('item') or 0).first()
    collection = item.collection if item else None
    scopes = scripting.load_scopes(env, collection)
    with scripting.ScriptEngine() as engine:       # Node only starts when the request has scripts
        out = scripting.execute(
            spec, env, scopes, engine, collection, files=request.FILES,
            send=auth.sender(opener=opener, login_opener=login_opener),
            info={'requestName': item.name if item else data.get('name', ''), 'requestId': str(item.pk if item else '')})
    scripting.save_scopes(scopes, env, collection)
    result = out['result']
    if item and result.get('ok'):
        item.last_status, item.last_ms, item.last_run_at = result['status'], result['ms'], timezone.now()
        item.save(update_fields=['last_status', 'last_ms', 'last_run_at'])
    return JsonResponse(dict(result, tests=out['tests'], console=out['console'], script_errors=out['errors'],
                             lov=out.get('lov', {})))


# ---------- variables and collection settings ----------

def clean_variable_rows(rows):
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and str(r.get('key', '')).strip():
            out.append({'key': str(r['key']).strip()[:200], 'value': str(r.get('value', ''))[:10000],
                        'enabled': bool(r.get('enabled', True))})
    return out


# ---------- environment logins (Keycloak) and the cookie jar ----------

def _profile_brief(profile):
    return {'id': profile.pk, 'name': profile.name, 'is_default': profile.is_default, 'kind': profile.kind,
            'active': profile.is_active, 'last_status': profile.last_status}


def _profiles_by_env(envs):
    """{environment id: [login, ...]} for the request builder's Login menu."""
    out = {str(e.pk): [] for e in envs}
    for p in AuthProfile.objects.filter(environment__in=envs):
        out[str(p.environment_id)].append(_profile_brief(p))
    return out


def _profile_json(env, profile):
    out = {'environment': env.pk, 'name': env.name, 'project': env.project.name, 'base_url': env.base_url,
           'profiles': [_profile_brief(p) for p in AuthProfile.objects.filter(environment=env)], 'profile': None}
    if profile is not None:
        out['profile'] = {
            'id': profile.pk, 'login_name': profile.name, 'is_default': profile.is_default,
            'kind': profile.kind, 'login_url': profile.login_url, 'username': profile.username,
            'has_password': bool(profile.password), 'check_url': profile.check_url,
            'auto_relogin': profile.auto_relogin, 'verify_ssl': profile.verify_ssl,
            'last_status': profile.last_status, 'last_message': profile.last_message,
            'last_login_at': timezone.localtime(profile.last_login_at).strftime('%d %b %Y, %H:%M:%S')
            if profile.last_login_at else ''}
    return out


def _when(dt):
    return timezone.localtime(dt).strftime('%d %b %Y, %H:%M:%S') if dt else ''


def _overview(request):
    """The projects and environments the workspace shows (the top-bar selection), and the logins of those
    environments, for the Auth & Cookies dialog."""
    selection = get_selection(request)
    env_ids = {e for envs in selection.values() for e in envs}
    projects = []
    for project in Project.objects.filter(pk__in=selection).prefetch_related('api_environments').order_by('name'):
        envs = [e for e in project.api_environments.all() if e.pk in env_ids]
        projects.append({'id': project.pk, 'name': project.name,   # a project without a selected environment is listed too
                         'environments': [{'id': e.pk, 'name': e.name, 'base_url': e.base_url} for e in envs]})
    logins = [dict(_profile_brief(pr), environment=pr.environment_id, environment_name=pr.environment.name,
                   project=pr.environment.project_id, project_name=pr.environment.project.name,
                   username=pr.username, last_message=pr.last_message, last_login_at=_when(pr.last_login_at))
              for pr in AuthProfile.objects.filter(environment_id__in=env_ids).select_related('environment__project')
              .order_by('environment__project__name', 'environment__name', '-is_default', 'name')]
    return {'projects': projects, 'logins': logins}


def _make_default(profile):
    AuthProfile.objects.filter(environment=profile.environment).exclude(pk=profile.pk).update(is_default=False)
    if not profile.is_default:
        profile.is_default = True
        profile.save(update_fields=['is_default'])


@login_required
def api_ws_auth(request, login_opener=None):
    """GET without parameters: all logins + projects with environments (the dialog's list). GET ?environment=
    [&profile=]: one login to edit.

    POST actions: test (try the login, save nothing), save (without `profile` it creates a new login, so an
    environment can have "Application client", "Backoffice client"...), login (save and log in), login_now (log
    in with the saved settings), delete, set_default.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
        env = get_object_or_404(Environment.objects.select_related('project'), pk=data.get('environment') or 0)
    else:
        data = request.GET
        if not data.get('environment'):
            return JsonResponse(_overview(request))
        env = get_object_or_404(Environment.objects.select_related('project'), pk=data.get('environment'))
    profiles = AuthProfile.objects.filter(environment=env)
    profile = profiles.filter(pk=data.get('profile') or 0).first()
    if request.method == 'GET':
        if profile is None and not data.get('profile'):
            profile = profiles.first()           # ordering: the default first
        return JsonResponse(_profile_json(env, profile))

    action = data.get('action') or 'save'
    if action in ('delete', 'set_default', 'login_now'):
        if profile is None:
            return JsonResponse({'ok': False, 'error': 'This login no longer exists.'})
        if action == 'delete':
            name, was_default = profile.name, profile.is_default
            profile.delete()
            if was_default and profiles.exists():
                _make_default(profiles.first())
            return JsonResponse(dict(_profile_json(env, profiles.first()), ok=True, message=f'Login "{name}" deleted.',
                                     **_overview(request)))
        if action == 'login_now':
            if not profile.is_active:
                return JsonResponse({'ok': False, 'error': f'"{profile.name}" has no Keycloak login set up yet. Edit it first.'})
            ok, message = auth.login(profile, opener=login_opener)
            profile.refresh_from_db()
            return JsonResponse(dict(_profile_json(env, profile), ok=ok, **({'message': message} if ok else {'error': message}),
                                     **_overview(request)))
        _make_default(profile)
        return JsonResponse(dict(_profile_json(env, profile), ok=True, message=f'"{profile.name}" is now the default login.',
                                 **_overview(request)))

    if profile is None:
        if data.get('profile'):
            return JsonResponse({'ok': False, 'error': 'This login no longer exists. Reopen Auth & Cookies.'})
        profile = AuthProfile(environment=env)
    name = str(data.get('name') or '').strip()[:100] or profile.name or 'Default'
    if profiles.exclude(pk=profile.pk).filter(name__iexact=name).exists():
        return JsonResponse({'ok': False, 'error': f'{env.project.name} · {env.name} already has a login named "{name}".',
                             'fields': ['name']})
    kind = data.get('kind') if data.get('kind') in dict(AuthProfile.KINDS) else 'none'
    if kind == 'keycloak':
        has_password = data.get('password') or (profile.password and not data.get('clear_password'))
        missing = [field for field, present in (('Username', str(data.get('username') or '').strip()),
                                                ('Password', has_password)) if not present]
        if missing:
            verb = 'is' if len(missing) == 1 else 'are'
            return JsonResponse({'ok': False, 'error': f'{" and ".join(missing)} {verb} required.',
                                 'fields': [m.lower() for m in missing]})
    if action == 'test':                         # try it with a throw-away profile: nothing is saved
        if kind != 'keycloak':
            return JsonResponse({'ok': False, 'error': 'Choose "Keycloak / OpenID Connect login" to test a login.'})
        probe = AuthProfile(environment=env, kind=kind, login_url=str(data.get('login_url') or '{{baseUrl}}/login').strip(),
                            username=str(data.get('username') or '').strip(),
                            password=str(data.get('password') or profile.password or ''),
                            check_url=str(data.get('check_url') or '').strip(), verify_ssl=bool(data.get('verify_ssl', True)))
        ok, message = auth.test_login(probe, login_opener)
        return JsonResponse({'ok': ok, 'tested': True, **({'message': message} if ok else {'error': message})})
    profile.name = name
    profile.kind = kind
    profile.login_url = str(data.get('login_url') or '{{baseUrl}}/login').strip()[:1000]
    profile.username = str(data.get('username') or '').strip()[:255]
    if data.get('password'):                     # empty keeps the saved password
        profile.password = str(data['password'])[:255]
    if data.get('clear_password'):
        profile.password = ''
    profile.check_url = str(data.get('check_url') or '').strip()[:1000]
    profile.auto_relogin = bool(data.get('auto_relogin', True))
    profile.verify_ssl = bool(data.get('verify_ssl', True))
    # the first login of an environment is its default; "Default" ticked moves the default here
    profile.is_default = profile.is_default or bool(data.get('is_default')) or not profiles.exclude(pk=profile.pk).exists()
    profile.save()
    if data.get('is_default') or profile.is_default:
        _make_default(profile)
    result = {'ok': True, 'saved': True}
    if action == 'login':
        if profile.kind != 'keycloak':
            result = {'ok': False, 'error': 'Choose "Keycloak / OpenID Connect login" first.'}
        else:
            ok, message = auth.login(profile, opener=login_opener)
            result = {'ok': ok, 'message': message} if ok else {'ok': False, 'error': message}
    profile.refresh_from_db()
    return JsonResponse(dict(_profile_json(env, profile), **result, **_overview(request)))


@login_required
def api_ws_cookies(request):
    """The cookie jar: list (values shortened), add a cookie by hand, delete one, or clear a domain."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
        domain = str(data.get('domain') or '').strip().lower()
        domain = re.sub(r'^https?://', '', domain).split('/')[0].split(':')[0]
        action = data.get('action')
        profile = AuthProfile.objects.filter(pk=data.get('profile') or 0).first()   # a login's own cookies
        if not domain:
            return JsonResponse({'ok': False, 'error': 'Enter the domain, e.g. qa-app.acs.gov.pk'})
        if action == 'add':
            name = str(data.get('name') or '').strip()
            if not name:
                return JsonResponse({'ok': False, 'error': 'Enter the cookie name.'})
            auth.add_cookie(domain, name, str(data.get('value') or ''), str(data.get('path') or '/'),
                            bool(data.get('secure')))
        elif action == 'delete':
            auth.remove_cookies(domain, str(data.get('name') or ''), profile)
        elif action == 'clear':
            auth.remove_cookies(domain, profile=profile)
        else:
            return JsonResponse({'ok': False, 'error': 'Unknown action.'}, status=400)
    groups = auth.cookie_list()
    for g in groups:
        for ck in g['cookies']:
            v = ck['value']
            ck['value'] = v if len(v) <= 24 else f'{v[:12]}…{v[-6:]} ({len(v)} chars)'
            ck['expires'] = (datetime.fromtimestamp(ck['expires'], tz=timezone.get_current_timezone())
                             .strftime('%d %b %Y, %H:%M') if ck.get('expires') else 'Session')
    return JsonResponse({'ok': True, 'domains': groups})


@login_required
def api_ws_resolve(request):
    """Variables a request can use, by scope, for the {{name}} hover in the request builder."""
    env = _environment(request.GET.get('environment'))
    collection = ApiCollection.objects.filter(pk=request.GET.get('collection') or 0).first()
    scopes = []
    if env:
        values = scripting.rows_to_dict(env.variables)
        values.setdefault('baseUrl', env.base_url.rstrip('/'))
        scopes.append({'scope': 'Environment', 'source': f'{env.project.name} · {env.name}', 'values': values})
    if collection:
        scopes.append({'scope': 'Collection', 'source': collection.name,
                       'values': scripting.rows_to_dict(collection.variables)})
    if env:
        scopes.append({'scope': 'Project', 'source': env.project.name,
                       'values': scripting.rows_to_dict(scripting.project_rows(env))})
    scopes.append({'scope': 'Global', 'source': 'Globals', 'values': scripting.rows_to_dict(ApiGlobals.load().variables)})
    lovs = lov.Registry.for_project(env.project if env else None).lovs.values()
    scopes.append({'scope': 'LOV', 'source': 'LOV',
                   'values': {l.name: lov.example(l.kind, l.options, 1)[0] for l in lovs},
                   'kinds': {l.name: lov.KINDS.get(l.kind, (l.kind,))[0] for l in lovs}})
    return JsonResponse({'scopes': scopes})


VARIABLE_NAME_RE = re.compile(r'^[\w.\-]{1,200}$')


def _scope_target(scope, pk, env=None):
    """(holder object, label) of a variable scope: the environment, its project, or the globals."""
    if scope == 'globals':
        return ApiGlobals.load(), 'Globals'
    if scope == 'environment':
        target = env or get_object_or_404(Environment, pk=pk or 0)
        return target, f'Environment {target.project.name} · {target.name}'
    if scope == 'project':
        project = env.project if env is not None else get_object_or_404(Project, pk=pk or 0)
        return ProjectVariables.for_project(project), f'Project {project.name}'
    return None, ''


@login_required
@require_POST
def api_ws_variable_set(request):
    """Save one value from a response as a variable: {scope: environment|project|globals, environment, key, value,
    overwrite}. Refuses a name that already exists in the chosen scope (or in another scope the request can
    see) unless `overwrite` is set, so duplicates are caught before they shadow each other."""
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    key = str(data.get('key') or '').strip()
    value = data.get('value')
    value = value if isinstance(value, str) else json.dumps(value)
    scope = str(data.get('scope') or '')
    env = _environment(data.get('environment'))
    if not VARIABLE_NAME_RE.match(key) or key.startswith('$'):
        return JsonResponse({'ok': False, 'error': 'Variable name: letters, digits, dot, dash or underscore only.'})
    if scope in ('environment', 'project') and env is None:
        return JsonResponse({'ok': False, 'error': 'This request has no environment; use a global variable.'})
    holder, label = _scope_target(scope, None, env)
    if holder is None:
        return JsonResponse({'ok': False, 'error': 'Unknown scope.'}, status=400)
    # where does this name already exist?
    found = []
    for other_scope in ('environment', 'project', 'globals'):
        if other_scope != 'globals' and env is None:
            continue
        other, other_label = _scope_target(other_scope, None, env)
        row = next((r for r in other.variables if r.get('key') == key), None)
        if row is not None:
            found.append({'scope': other_scope, 'label': other_label, 'value': str(row.get('value', ''))[:200],
                          'same': other_scope == scope})
    if ApiLov.objects.filter(name=key).exists():
        found.append({'scope': 'lov', 'label': 'LOV', 'value': 'generated on every send', 'same': False})
    if found and not data.get('overwrite'):
        same = [f for f in found if f['same']]
        msg = (f'"{key}" already exists in {same[0]["label"]} (value: {same[0]["value"] or "empty"}).' if same
               else f'"{key}" is already defined in ' + ', '.join(f['label'] for f in found) + '.')
        return JsonResponse({'ok': False, 'duplicate': True, 'found': found, 'error': msg})
    rows = list(holder.variables)
    row = next((r for r in rows if r.get('key') == key), None)
    if row is not None:
        row['value'], row['enabled'] = value[:10000], True
    else:
        rows.append({'key': key, 'value': value[:10000], 'enabled': True})
    holder.variables = rows
    holder.save(update_fields=['variables'])
    return JsonResponse({'ok': True, 'scope': scope, 'label': label, 'key': key, 'replaced': row is not None})


@login_required
def api_ws_variables(request):
    """Globals and the variables of the environments selected in the workspace."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
        rows = clean_variable_rows(data.get('rows'))
        if data.get('scope') == 'globals':
            g = ApiGlobals.load()
            g.variables = rows
            g.save(update_fields=['variables'])
        elif data.get('scope') == 'environment':
            env = get_object_or_404(Environment, pk=data.get('id') or 0)
            env.variables = rows
            env.save(update_fields=['variables'])
        elif data.get('scope') == 'project':
            pv = ProjectVariables.for_project(get_object_or_404(Project, pk=data.get('id') or 0))
            pv.variables = rows
            pv.save(update_fields=['variables'])
        else:
            return JsonResponse({'ok': False, 'error': 'Unknown scope.'}, status=400)
        return JsonResponse({'ok': True, 'rows': rows})
    selection = get_selection(request)
    envs = (Environment.objects.filter(project_id__in=selection).select_related('project')
            .order_by('project__name', 'created_at'))
    projects = Project.objects.filter(pk__in=selection).order_by('name')
    stored = {pv.project_id: pv.variables for pv in ProjectVariables.objects.filter(project__in=projects)}
    return JsonResponse({
        'globals': ApiGlobals.load().variables,
        'projects': [{'id': p.pk, 'name': p.name, 'variables': stored.get(p.pk, [])} for p in projects],
        'environments': [{'id': e.pk, 'name': e.name, 'project': e.project.name, 'base_url': e.base_url,
                          'variables': e.variables} for e in envs if e.pk in selection.get(e.project_id, [])],
    })


@login_required
def api_ws_collection_settings(request, collection_id):
    """Collection variables and the scripts that run around every request of the collection."""
    collection = get_object_or_404(ApiCollection, pk=collection_id)
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
        collection.variables = clean_variable_rows(data.get('variables'))
        collection.pre_request_script = str(data.get('pre_request_script') or '')[:SCRIPT_LIMIT]
        collection.test_script = str(data.get('test_script') or '')[:SCRIPT_LIMIT]
        collection.description = str(data.get('description') or '')[:5000]
        collection.save(update_fields=['variables', 'pre_request_script', 'test_script', 'description'])
        return JsonResponse({'ok': True})
    return JsonResponse({'id': collection.pk, 'name': collection.name, 'description': collection.description,
                         'variables': collection.variables, 'pre_request_script': collection.pre_request_script,
                         'test_script': collection.test_script})


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
    if item is None and data.get('case') and not (data.get('collection') or data.get('new_collection') or data.get('save_as')):
        # a synced API (APIs tab): Save keeps the values on the API itself
        case = get_object_or_404(ApiTestCase, pk=data.get('case'))
        case.spec = spec
        case.expected_status = spec['expected_status'] or case.expected_status
        case.save(update_fields=['spec', 'expected_status', 'updated_at'])
        return JsonResponse({'ok': True, 'case': case.pk, 'saved': True})
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


# ---------- LOV: generated values ----------

def _lov_brief(l):
    return {'id': l.pk, 'name': l.name, 'kind': l.kind, 'kind_label': lov.KINDS.get(l.kind, (l.kind,))[0],
            'options': lov.clean_options(l.kind, l.options), 'project': l.project_id,
            'project_name': l.project.name if l.project else '', 'enabled': l.enabled,
            'examples': lov.example(l.kind, l.options, 3)}


def _lov_name_taken(name, exclude_pk=None):
    """Where a variable with this name already exists (an LOV must not shadow or be shadowed by one)."""
    taken = []
    if ApiLov.objects.filter(name=name).exclude(pk=exclude_pk or 0).exists():
        taken.append('another LOV')
    if any(r.get('key') == name for r in ApiGlobals.load().variables):
        taken.append('Globals')
    for pv in ProjectVariables.objects.select_related('project'):
        if any(r.get('key') == name for r in pv.variables):
            taken.append(f'project {pv.project.name}')
    for env in Environment.objects.select_related('project'):
        if any(r.get('key') == name for r in env.variables):
            taken.append(f'environment {env.project.name} · {env.name}')
    return taken


@login_required
def api_ws_lov(request):
    """GET: the LOVs of the selected projects (and the project-less ones) with the kind catalogue.
    POST {action: save|delete|preview, ...}: keep an LOV, remove it, or preview values of a kind + options."""
    if request.method == 'POST':
        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
        action = data.get('action')
        kind = str(data.get('kind') or '')
        if action == 'preview':
            error = lov.validate(kind, data.get('options'))
            return JsonResponse({'ok': not error, 'error': error,
                                 'examples': [] if error else lov.example(kind, data.get('options'), 3)})
        if action == 'delete':
            ApiLov.objects.filter(pk=data.get('id') or 0).delete()
            return JsonResponse({'ok': True})
        if action == 'toggle':
            l = get_object_or_404(ApiLov, pk=data.get('id') or 0)
            l.enabled = not l.enabled
            l.save(update_fields=['enabled'])
            return JsonResponse({'ok': True, 'lov': _lov_brief(l)})
        if action != 'save':
            return JsonResponse({'ok': False, 'error': 'Unknown action.'}, status=400)
        name = str(data.get('name') or '').strip()
        if not lov.NAME_RE.match(name) or name.startswith('$'):
            return JsonResponse({'ok': False, 'error': 'LOV name: letters, digits, dot, dash or underscore only.'})
        error = lov.validate(kind, data.get('options'))
        if error:
            return JsonResponse({'ok': False, 'error': error})
        l = ApiLov.objects.filter(pk=data.get('id') or 0).first() or ApiLov(created_by=request.user)
        taken = _lov_name_taken(name, l.pk)
        if taken:
            return JsonResponse({'ok': False, 'duplicate': True,
                                 'error': f'"{name}" is already used by ' + ', '.join(taken) + '. Choose another name.'})
        project = Project.objects.filter(pk=data.get('project') or 0).first()
        if l.pk and (l.name != name or l.kind != kind):
            l.state = {}                                   # a renamed / retyped LOV starts counting again
        l.name, l.kind, l.options, l.project = name, kind, lov.clean_options(kind, data.get('options')), project
        l.save()
        return JsonResponse({'ok': True, 'lov': _lov_brief(l)})
    selection = get_selection(request)
    projects = Project.objects.filter(pk__in=selection).order_by('name')
    lovs = ApiLov.objects.select_related('project').filter(Q(project=None) | Q(project__in=projects))
    return JsonResponse({
        'lovs': [_lov_brief(l) for l in lovs],
        'projects': [{'id': p.pk, 'name': p.name} for p in projects],
        'kinds': [{'kind': k, 'label': label, 'options': [{'key': key, 'type': typ, 'default': default, 'help': help_}
                                                          for key, typ, default, help_ in opts]}
                  for k, (label, opts) in lov.KINDS.items()],
        'common': [{'key': key, 'type': typ, 'default': default, 'help': help_} for key, typ, default, help_ in lov.COMMON_OPTIONS],
    })
