"""API Dependency Intelligence.

Static analysis of a collection's saved requests: which request produces a variable, which request
consumes it, how tokens flow from one API to the next, where the run order is wrong, and what breaks
when one API changes. Reads only the saved requests and their scripts - it changes nothing.

Everything here is pure (no database writes), so it is safe to run on any collection at any time.
"""
import re

# {{variable}} anywhere in a URL, header, param or body
VAR_RE = re.compile(r'\{\{\s*([\w.\-$]+)\s*\}\}')

# a script writing a variable: pm.environment.set("x"), pm.collectionVariables.set('x'), pm.globals.set,
# pm.variables.set, and the legacy postman.setEnvironmentVariable / setGlobalVariable
SET_RE = re.compile(r'pm\.(?:environment|collectionVariables|globals|variables)\.set\(\s*[\'"]([\w.\-$]+)[\'"]')
LEGACY_SET_RE = re.compile(r'postman\.set(?:Environment|Global)Variable\(\s*[\'"]([\w.\-$]+)[\'"]')

# a script reading a variable: pm.environment.get("x") etc.
GET_RE = re.compile(r'pm\.(?:environment|collectionVariables|globals|variables)\.get\(\s*[\'"]([\w.\-$]+)[\'"]')

# dynamic values Postman/BaigNova fills itself - never a dependency
DYNAMIC = {'$guid', '$randomUUID', '$timestamp', '$isoTimestamp', '$randomInt'}

# names that look like an auth token / signature flowing between APIs
TOKENISH = ('token', 'auth', 'bearer', 'jwt', 'access', 'refresh', 'session', 'signature', 'sign',
            'otp', 'apikey', 'api_key', 'secret', 'cookie', 'csrf', 'xsrf')


def _rows_text(rows):
    """Join every key and value of a [{key, value}] list into one searchable string."""
    out = []
    for r in rows or []:
        out.append(str(r.get('key', '')))
        out.append(str(r.get('value', '')))
    return '\n'.join(out)


def _auth_text(auth):
    if not isinstance(auth, dict):
        return ''
    return '\n'.join(str(v) for v in auth.values())


def request_reads(item):
    """Every variable the request needs: {{name}} in url/params/headers/body/auth/graphql, plus pm.*.get(...)
    in its own scripts. Dynamic values are excluded."""
    blob = '\n'.join([
        item.url or '', _rows_text(item.params), _rows_text(item.headers), _auth_text(item.auth),
        item.body or '', _rows_text(item.urlencoded), _rows_text(item.form_data),
        item.graphql_query or '', item.graphql_variables or '',
    ])
    names = set(VAR_RE.findall(blob))
    for script in (item.pre_request_script or '', item.test_script or ''):
        names.update(GET_RE.findall(script))
    return {n for n in names if n not in DYNAMIC}


def request_writes(item):
    """Every variable the request sets in its pre-request or test script."""
    names = set()
    for script in (item.pre_request_script or '', item.test_script or ''):
        names.update(SET_RE.findall(script))
        names.update(LEGACY_SET_RE.findall(script))
    return {n for n in names if n not in DYNAMIC}


def is_tokenish(name):
    low = name.lower()
    return any(t in low for t in TOKENISH)


def _static_names(collection, items, lov_names=None):
    """Variable names a request can get without another request producing them: collection variables,
    each environment's variables and URLs, project and global variables, and LOV category names."""
    names = set()
    for r in collection.variables or []:
        if r.get('key'):
            names.add(r['key'])
    seen_env, seen_proj = set(), set()
    import_globals = True
    from .models import ApiGlobals, ProjectVariables
    if import_globals:
        for r in ApiGlobals.load().variables or []:
            if r.get('key'):
                names.add(r['key'])
    for item in items:
        env = item.environment
        if env and env.pk not in seen_env:
            seen_env.add(env.pk)
            names.update(env.url_vars().keys())
            for r in env.variables or []:
                if r.get('key'):
                    names.add(r['key'])
            proj = env.project
            if proj and proj.pk not in seen_proj:
                seen_proj.add(proj.pk)
                pv = ProjectVariables.objects.filter(project=proj).first()
                for r in (pv.variables if pv else []):
                    if r.get('key'):
                        names.add(r['key'])
    names.update(lov_names or [])
    return names


def analyse(collection, lov_names=None):
    """Build the dependency picture of a collection.

    Returns a dict:
        nodes:   [{id, name, method, position, reads, writes, tokens}]
        edges:   [{source, target, var, token}]  (producer -> consumer)
        tokens:  [{var, producers: [id...], consumers: [id...]}]
        unresolved: [{id, name, var}]        variables nobody provides
        order:   [{id, name, var, producers:[id...]}]   consumed before it is produced
        static:  sorted list of statically provided variable names
    """
    items = collection.ordered_requests()
    index = {item.pk: pos for pos, item in enumerate(items)}
    static = _static_names(collection, items, lov_names)

    reads = {item.pk: request_reads(item) for item in items}
    writes = {item.pk: request_writes(item) for item in items}
    producers = {}                      # var -> [item.pk in run order]
    for item in items:
        for var in writes[item.pk]:
            producers.setdefault(var, []).append(item.pk)

    nodes = [{
        'id': item.pk, 'name': item.name, 'method': item.method, 'position': pos + 1,
        'reads': sorted(reads[item.pk]), 'writes': sorted(writes[item.pk]),
        'tokens': sorted(v for v in writes[item.pk] if is_tokenish(v)),
    } for pos, item in enumerate(items)]
    name_of = {item.pk: item.name for item in items}

    edges, unresolved, order = [], [], []
    for item in items:
        for var in sorted(reads[item.pk]):
            made_by = producers.get(var, [])
            external = var in static
            if not made_by and not external:
                unresolved.append({'id': item.pk, 'name': name_of[item.pk], 'var': var})
                continue
            # producers that run before this consumer satisfy it; note the ones that don't
            before = [p for p in made_by if index[p] < index[item.pk] and p != item.pk]
            after = [p for p in made_by if index[p] >= index[item.pk] and p != item.pk]
            for p in made_by:
                if p != item.pk:
                    edges.append({'source': p, 'target': item.pk, 'var': var, 'token': is_tokenish(var)})
            if made_by and not before and not external:
                order.append({'id': item.pk, 'name': name_of[item.pk], 'var': var,
                              'producers': [{'id': p, 'name': name_of[p]} for p in after]})

    tokens = [{'var': var, 'producers': [{'id': p, 'name': name_of[p]} for p in producers.get(var, [])],
               'consumers': [{'id': i.pk, 'name': i.name} for i in items if var in reads[i.pk]]}
              for var in sorted({v for v in producers if is_tokenish(v)}
                                | {v for pk in reads for v in reads[pk] if is_tokenish(v)})]

    return {'nodes': nodes, 'edges': edges, 'tokens': tokens, 'unresolved': unresolved,
            'order': order, 'static': sorted(static)}


def impact(result, request_id):
    """Everything downstream of one request: the requests that (directly or transitively) consume a
    variable it produces. Returns [{id, name, via: [var...]}] in run order."""
    out_edges = {}
    for e in result['edges']:
        out_edges.setdefault(e['source'], []).append(e)
    pos = {n['id']: n['position'] for n in result['nodes']}
    name = {n['id']: n['name'] for n in result['nodes']}
    seen, via = {}, {}
    stack = [request_id]
    while stack:
        cur = stack.pop()
        for e in out_edges.get(cur, []):
            tgt = e['target']
            via.setdefault(tgt, set()).add(e['var'])
            if tgt not in seen:
                seen[tgt] = True
                stack.append(tgt)
    return [{'id': i, 'name': name.get(i, '?'), 'via': sorted(via[i])}
            for i in sorted(seen, key=lambda x: pos.get(x, 0))]


def upstream(result, request_id):
    """Everything this request depends on: requests that produce a variable it (transitively) consumes."""
    in_edges = {}
    for e in result['edges']:
        in_edges.setdefault(e['target'], []).append(e)
    pos = {n['id']: n['position'] for n in result['nodes']}
    name = {n['id']: n['name'] for n in result['nodes']}
    seen, via = {}, {}
    stack = [request_id]
    while stack:
        cur = stack.pop()
        for e in in_edges.get(cur, []):
            src = e['source']
            via.setdefault(src, set()).add(e['var'])
            if src not in seen:
                seen[src] = True
                stack.append(src)
    return [{'id': i, 'name': name.get(i, '?'), 'via': sorted(via[i])}
            for i in sorted(seen, key=lambda x: pos.get(x, 0))]
