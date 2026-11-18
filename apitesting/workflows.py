"""API Workflows: a visual canvas where API calls are nodes and arrows chain them. An arrow runs its target
after its source and can carry a value from the source's response into a variable the target uses. A run goes
node by node in dependency order, sharing one variable bag so captured values flow forward, and stops on the
first failing node (unless that node is set to carry on).

The actual sending, scripting, variable substitution and auth all reuse the same engine the request builder and
collection runner use (scripting.execute + auth.sender), so a workflow behaves exactly like running each request
by hand, in order, with the data handed on between them.
"""
import json
import re

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import auth, scripting
from .collection_runner import expected_status, is_pass
from .models import (ApiCollection, ApiTestCase, ApiWorkflow, CollectionRequest, Environment,
                     WorkflowEdge, WorkflowNode, WorkflowRun)
from .workspace import case_spec, item_spec

BODY_LIMIT = 20000
SCOPES = ('environment', 'collection', 'project', 'globals')

# /gha/{id} -> /gha/{{id}} when a variable named id has been captured, so a value carried
# from an earlier step lands in the next step's URL. Already-{{doubled}} names are left alone.
_PATH_PARAM_RE = re.compile(r'(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})')

def _fill_path_params(url, scopes):
    if not url or '{' not in url:
        return url
    keys = (set(scopes.local) | set(scopes.environment) | set(scopes.collection)
            | set(scopes.project) | set(scopes.globals))
    return _PATH_PARAM_RE.sub(lambda m: ('{{' + m.group(1) + '}}') if m.group(1) in keys else m.group(0), url)


# ---------- building a node's request ----------

def _spec_from_source(kind, ref_id):
    """The full request spec for a node added from a saved request (an item) or a synced API (a case)."""
    if kind == 'item':
        item = CollectionRequest.objects.select_related('collection', 'environment', 'project').filter(pk=ref_id).first()
        return (item_spec(item), item) if item else (None, None)
    if kind == 'case':
        case = ApiTestCase.objects.select_related('environment', 'project').filter(pk=ref_id).first()
        return (case_spec(case), case) if case else (None, None)
    return None, None


def _blank_spec():
    return {'method': 'GET', 'url': '', 'params': [], 'headers': [], 'auth': {'type': 'none'}, 'body_mode': 'none',
            'raw_language': 'json', 'body': '', 'form_data': [], 'urlencoded': [], 'graphql_query': '',
            'graphql_variables': '', 'verify_ssl': True, 'pre_request_script': '', 'test_script': ''}


def _node_env(node, default_env):
    return node.environment or default_env


# ---------- data mappings (a value from a response into a variable) ----------

def dig(value, path):
    """Walk a finder path like data.items.0.id into a parsed JSON value. Returns None if any step is missing."""
    cur = value
    for part in str(path or '').split('.'):
        if part == '':
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def _response_json(result):
    body = result.get('body')
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _apply_mappings(mappings, result, scopes, captured):
    """After a source node runs, read each mapping's path from its response and set the variable so later
    nodes resolve {{var}}. Returns notes for the run report."""
    notes = []
    data = _response_json(result)
    for m in mappings or []:
        var = (m.get('to_var') or '').strip()
        if not var:
            continue
        scope = m.get('scope') if m.get('scope') in SCOPES else 'collection'
        value = dig(data, m.get('from_path')) if data is not None else None
        text = value if isinstance(value, str) else ('' if value is None else json.dumps(value))
        getattr(scopes, scope)[var] = text        # later nodes resolve {{var}} from this scope
        captured[var] = text
        notes.append({'var': var, 'path': m.get('from_path'), 'value': text, 'found': value is not None})
    return notes


# ---------- run order ----------

def _order(nodes, edges):
    """Nodes in dependency order (a source before its targets). Returns (order, cycle) - cycle True if the graph
    loops, in which case order falls back to node id order."""
    ids = [n.pk for n in nodes]
    incoming = {i: 0 for i in ids}
    outgoing = {i: [] for i in ids}
    for e in edges:
        if e.source_id in outgoing and e.target_id in incoming:
            outgoing[e.source_id].append(e.target_id)
            incoming[e.target_id] += 1
    # Kahn's algorithm; keep a stable order by node id
    ready = sorted([i for i in ids if incoming[i] == 0])
    order, seen = [], set()
    while ready:
        i = ready.pop(0)
        order.append(i)
        seen.add(i)
        for j in sorted(outgoing[i]):
            incoming[j] -= 1
            if incoming[j] == 0:
                ready.append(j)
        ready.sort()
    if len(order) != len(ids):                 # a cycle: run what is left in id order so nothing is lost
        order += [i for i in sorted(ids) if i not in seen]
        return order, True
    return order, False


# ---------- the run ----------

def _node_result(node, result, out, passed):
    body = result.get('body') or ''
    req = out.get('request') or {}
    auth_spec = (req.get('auth') or {})
    return {
        'node': node.pk, 'name': node.name or 'Request',
        'method': req.get('method') or (node.spec or {}).get('method', ''),
        'url': result.get('url') or req.get('url') or (node.spec or {}).get('url', ''),
        'status': 'passed' if passed else 'failed',
        'status_code': result.get('status'), 'reason': result.get('reason', ''),
        'ms': result.get('ms'), 'size': result.get('size'), 'ok': bool(result.get('ok')),
        'error': result.get('error', ''),
        'tests': out.get('tests', []), 'console': out.get('console', []),
        'request': {'method': req.get('method', ''), 'url': req.get('url', ''),
                    'headers': req.get('headers') or [], 'body': (req.get('body') or '')[:BODY_LIMIT],
                    'auth': {k: auth_spec.get(k) for k in ('type', 'profile', 'username', 'key') if auth_spec.get(k)}},
        'response': {'headers': result.get('headers', []), 'body': body[:BODY_LIMIT],
                     'truncated': len(body) > BODY_LIMIT, 'language': result.get('language', 'text')},
    }


def run_workflow(workflow, user=None, sender=None):
    nodes = list(workflow.nodes.all())
    edges = list(workflow.edges.all())
    by_id = {n.pk: n for n in nodes}
    out_edges = {}
    for e in edges:
        out_edges.setdefault(e.source_id, []).append(e)
    order, cycle = _order(nodes, edges)

    env = workflow.environment
    collection = workflow.collection
    scopes = scripting.load_scopes(env, collection)
    send = sender or auth.sender()
    results, captured = [], {}
    passed = failed = total_ms = 0
    stopped = False

    with scripting.ScriptEngine() as engine:
        for nid in order:
            node = by_id[nid]
            if stopped:
                results.append({'node': node.pk, 'name': node.name or 'Request', 'status': 'skipped',
                                'request': {}, 'response': {}, 'tests': []})
                continue
            spec = dict(node.spec or _blank_spec())
            spec['url'] = _fill_path_params(spec.get('url', ''), scopes)
            node_env = _node_env(node, env)
            out = scripting.execute(spec, node_env, scopes, engine, collection,
                                    send=lambda s, e, files=None: send(s, e),
                                    info={'requestName': node.name or 'Request', 'requestId': str(node.pk)})
            result, tests, errors = out['result'], out['tests'], out['errors']
            expected = spec.get('expected_status') or (expected_status(node.source) if node.source else None)
            failing = [t for t in tests if not t.get('passed')]
            ok = is_pass(result, expected) and not failing and not errors
            row = _node_result(node, result, out, ok)
            # hand values forward along this node's arrows
            maps = []
            for e in out_edges.get(nid, []):
                maps += _apply_mappings(e.mappings, result, scopes, captured)
            if maps:
                row['captured'] = maps
            results.append(row)
            total_ms += result.get('ms') or 0
            if ok:
                passed += 1
            else:
                failed += 1
                if node.on_fail == 'stop':
                    stopped = True

    # Keep what the nodes set (pm.globals/environment/... and the arrow mappings), like a workspace send does.
    scripting.save_scopes(scopes, env, collection)
    run = WorkflowRun.objects.create(
        workflow=workflow, ok=(failed == 0 and passed > 0), passed=passed, failed=failed,
        total_ms=total_ms, results=results, variables=captured, created_by=user)
    return {'ok': run.ok, 'run': run.pk, 'passed': passed, 'failed': failed, 'total_ms': total_ms,
            'skipped': sum(1 for r in results if r.get('status') == 'skipped'), 'cycle': cycle,
            'results': results, 'variables': captured}


# ---------- pages and endpoints ----------

@login_required
def workflow_list(request):
    workflows = ApiWorkflow.objects.all().order_by('name')
    return render(request, 'apitesting/workflow_list.html', {'workflows': workflows})


@login_required
def workflow_page(request, workflow_id):
    workflow = get_object_or_404(ApiWorkflow, pk=workflow_id)
    collections = []
    for c in ApiCollection.objects.prefetch_related('requests').order_by('name'):
        reqs = [{'id': r.pk, 'name': r.name, 'method': r.method} for r in c.requests.all()]
        if reqs:
            collections.append({'id': c.pk, 'name': c.name, 'requests': reqs})
    envs = [{'id': e.pk, 'name': e.name, 'project': e.project.name, 'base_url': e.base_url}
            for e in Environment.objects.select_related('project').order_by('project__name', 'name')]
    # the synced APIs (fresh, no data) grouped like the workspace "APIs" tab: project / environment -> folder
    apis, groups = [], {}
    cases = (ApiTestCase.objects.select_related('environment__project', 'folder')
             .only('id', 'name', 'method', 'folder__name', 'environment__name',
                   'environment__project__name', 'environment_id')
             .order_by('environment__project__name', 'environment__name', 'folder__name', 'id'))
    for c in cases:
        env = c.environment
        label = (f'{env.project.name} · {env.name}' if env and env.project_id else
                 (env.name if env else 'No environment'))
        g = groups.setdefault(c.environment_id, {'label': label, 'folders': {}})
        fname = c.folder.name if c.folder_id else 'Ungrouped'
        g['folders'].setdefault(fname, []).append({'id': c.pk, 'name': c.name, 'method': c.method})
    for g in groups.values():
        apis.append({'label': g['label'],
                     'folders': [{'name': n, 'requests': r} for n, r in g['folders'].items()]})
    return render(request, 'apitesting/workflow.html', {
        'workflow': workflow,
        'graph_json': json.dumps(_graph(workflow)),
        'collections_json': json.dumps(collections),
        'apis_json': json.dumps(apis),
        'environments_json': json.dumps(envs),
        'scopes_json': json.dumps(list(SCOPES)),
    })


def _graph(workflow):
    nodes = [{
        'id': n.pk, 'name': n.name, 'x': n.x, 'y': n.y, 'on_fail': n.on_fail,
        'source': n.source_id, 'case': n.case_id, 'environment': n.environment_id,
        'spec': n.spec or _blank_spec(),
    } for n in workflow.nodes.all()]
    edges = [{'id': e.pk, 'source': e.source_id, 'target': e.target_id, 'mappings': e.mappings or []}
             for e in workflow.edges.all()]
    return {'id': workflow.pk, 'name': workflow.name, 'environment': workflow.environment_id,
            'collection': workflow.collection_id, 'nodes': nodes, 'edges': edges}


@login_required
@require_POST
def workflow_create(request):
    name = (request.POST.get('name') or '').strip()[:200]
    if not name:
        return redirect(reverse('api_workflow_list'))
    wf = ApiWorkflow.objects.create(name=name, created_by=request.user)
    return redirect(reverse('api_workflow_page', args=[wf.pk]))


@login_required
@require_POST
def workflow_delete(request, workflow_id):
    get_object_or_404(ApiWorkflow, pk=workflow_id).delete()
    return redirect(reverse('api_workflow_list'))


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except ValueError:
        return None


@login_required
@require_POST
def workflow_add_node(request, workflow_id):
    """Add a node from a saved request/synced API, or a blank inline one. Returns the new node."""
    workflow = get_object_or_404(ApiWorkflow, pk=workflow_id)
    data = _json_body(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    kind = data.get('kind')
    x, y = float(data.get('x') or 60), float(data.get('y') or 60)
    source = case = None
    if kind in ('item', 'case'):
        spec, obj = _spec_from_source(kind, data.get('ref'))
        if spec is None:
            return JsonResponse({'ok': False, 'error': 'That request no longer exists.'}, status=404)
        name = spec.get('name') or 'Request'
        if kind == 'item':
            source = obj
        else:
            case = obj
        # the first node's environment becomes the workflow's, so the whole run shares one environment
        if workflow.environment_id is None and spec.get('environment'):
            workflow.environment_id = spec['environment']
            if kind == 'item' and obj.collection_id and workflow.collection_id is None:
                workflow.collection_id = obj.collection_id
            workflow.save(update_fields=['environment', 'collection'])
    else:
        spec, name = _blank_spec(), (data.get('name') or 'New Request')
    node = WorkflowNode.objects.create(workflow=workflow, name=name[:300], spec=spec, x=x, y=y,
                                       source=source, case=case)
    return JsonResponse({'ok': True, 'node': {
        'id': node.pk, 'name': node.name, 'x': node.x, 'y': node.y, 'on_fail': node.on_fail,
        'source': node.source_id, 'case': node.case_id, 'environment': node.environment_id, 'spec': node.spec},
        'environment': workflow.environment_id})


@login_required
@require_POST
def workflow_save(request, workflow_id):
    """Save the whole canvas: node positions / names / specs / on-fail, and all arrows with their mappings.
    The client is the source of truth - nodes and edges not in the payload are removed."""
    workflow = get_object_or_404(ApiWorkflow, pk=workflow_id)
    data = _json_body(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    with transaction.atomic():
        if 'name' in data and (data.get('name') or '').strip():
            workflow.name = data['name'].strip()[:200]
        if 'environment' in data:
            workflow.environment_id = data['environment'] or None
        workflow.save()
        keep_nodes = set()
        for n in data.get('nodes', []):
            node = workflow.nodes.filter(pk=n.get('id')).first()
            if not node:
                continue
            node.name = (n.get('name') or node.name)[:300]
            node.x, node.y = float(n.get('x', node.x)), float(n.get('y', node.y))
            node.on_fail = 'continue' if n.get('on_fail') == 'continue' else 'stop'
            if isinstance(n.get('spec'), dict):
                node.spec = n['spec']
            node.save()
            keep_nodes.add(node.pk)
        workflow.nodes.exclude(pk__in=keep_nodes).delete()
        # rebuild edges from the payload
        workflow.edges.all().delete()
        valid = set(workflow.nodes.values_list('pk', flat=True))
        made = set()
        for e in data.get('edges', []):
            s, t = e.get('source'), e.get('target')
            if s in valid and t in valid and s != t and (s, t) not in made:
                mappings = [{'from_path': (m.get('from_path') or '').strip(),
                             'to_var': (m.get('to_var') or '').strip(),
                             'scope': m.get('scope') if m.get('scope') in SCOPES else 'collection'}
                            for m in (e.get('mappings') or []) if (m.get('to_var') or '').strip()]
                WorkflowEdge.objects.create(workflow=workflow, source_id=s, target_id=t, mappings=mappings)
                made.add((s, t))
    return JsonResponse({'ok': True, 'graph': _graph(workflow)})


@login_required
@require_POST
def workflow_run(request, workflow_id):
    workflow = get_object_or_404(ApiWorkflow, pk=workflow_id)
    if not workflow.nodes.exists():
        return JsonResponse({'ok': False, 'error': 'Add at least one step to the workflow first.'}, status=400)
    return JsonResponse(run_workflow(workflow, user=request.user))


@login_required
@require_POST
def workflow_run_node(request, workflow_id):
    """Run one step on its own so the user can see its response and wire the next step from it.
    It uses the workflow's environment / collection plus whatever earlier steps already captured,
    then applies this step's out-edge mappings so the captured values are ready for the next steps."""
    workflow = get_object_or_404(ApiWorkflow, pk=workflow_id)
    data = _json_body(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'Bad request data.'}, status=400)
    node = workflow.nodes.filter(pk=data.get('node') or 0).first()
    if node is None:
        return JsonResponse({'ok': False, 'error': 'That step no longer exists.'}, status=404)
    env, collection = workflow.environment, workflow.collection
    scopes = scripting.load_scopes(env, collection)
    send = auth.sender()
    with scripting.ScriptEngine() as engine:
        spec = dict(node.spec or _blank_spec())
        spec['url'] = _fill_path_params(spec.get('url', ''), scopes)
        node_env = _node_env(node, env)
        out = scripting.execute(spec, node_env, scopes, engine, collection,
                                send=lambda s, e, files=None: send(s, e),
                                info={'requestName': node.name or 'Request', 'requestId': str(node.pk)})
    result, tests, errors = out['result'], out['tests'], out['errors']
    expected = spec.get('expected_status') or (expected_status(node.source) if node.source else None)
    ok = is_pass(result, expected) and not [t for t in tests if not t.get('passed')] and not errors
    captured, notes = {}, []
    for e in WorkflowEdge.objects.filter(source=node):
        notes += _apply_mappings(e.mappings, result, scopes, captured)
    scripting.save_scopes(scopes, env, collection)
    row = _node_result(node, result, out, ok)
    if notes:
        row['captured'] = notes
    return JsonResponse({'ok': True, 'result': row, 'captured': captured})
