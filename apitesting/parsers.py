"""Turn Postman / OpenAPI / Swagger collections (file or live URL) into a flat list of endpoints.

Every endpoint is a plain dict so it can be stored in ImportSession.endpoints (JSON):
    folder, key, name, summary, method, path, url, content_type,
    headers, path_params, query_params, body, expected_status
"""
import base64
import hashlib
import json
import os
import re
import shlex
import urllib.error
import urllib.request
from urllib.parse import unquote_plus, urljoin, urlparse

import yaml
from xml.sax.saxutils import escape as xml_escape

HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
VAR_RE = re.compile(r'\{\{\s*([\w.\-]+)\s*\}\}')


class CollectionError(Exception):
    pass


# ---------- loading ----------

def load_document(text):
    text = text.strip()
    if not text:
        raise CollectionError('The collection is empty.')
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CollectionError(f'Could not read the file as JSON or YAML: {exc}')
    if not isinstance(data, dict):
        raise CollectionError('The file is not a valid Postman, OpenAPI or Swagger collection.')
    return data


def parse_environment(text):
    """Postman environment JSON -> {name: value}."""
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise CollectionError('The environment file is not valid JSON.')
    values = data.get('values', []) if isinstance(data, dict) else []
    return {v['key']: str(v.get('value', '')) for v in values
            if isinstance(v, dict) and v.get('key') and v.get('enabled', True)}


def auth_headers(auth_header):
    """'Bearer xyz' / 'ApiKey xyz' -> Authorization header; 'X-Api-Key: xyz' -> that header."""
    auth_header = (auth_header or '').strip()
    if not auth_header:
        return {}
    name, sep, value = auth_header.partition(':')
    if sep and ' ' not in name.strip():
        return {name.strip(): value.strip()}
    return {'Authorization': auth_header}


def fetch(url, auth_header=''):
    req = urllib.request.Request(url, headers={'User-Agent': 'BaigNova-TestManager', **auth_headers(auth_header)})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.geturl(), resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        raise CollectionError(f'{url} returned HTTP {exc.code}. Check the URL or the authentication header.')
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise CollectionError(f'Could not reach {url}: {getattr(exc, "reason", exc)}')


def spec_candidates(html, page_url):
    """Scalar / Swagger UI / Redoc pages load the spec from a URL in their config.

    Relative links are tried against the page and against its parent, because e.g. ASP.NET
    Scalar served at /app/scalar/ references "openapi/v1.json" relative to /app/.
    """
    patterns = [
        r'"sources"\s*:\s*\[\s*\{[^\]]*?"url"\s*:\s*"([^"]+)"',   # Scalar (ASP.NET)
        r'data-url\s*=\s*["\']([^"\']+)["\']',                     # Scalar classic
        r'spec-url\s*=\s*["\']([^"\']+)["\']',                     # Redoc
        r'["\']?url["\']?\s*:\s*["\']([^"\']+\.(?:json|ya?ml))["\']',  # Swagger UI
        r'["\']([^"\'\s]+(?:openapi|swagger)[^"\'\s]*\.(?:json|ya?ml))["\']',
    ]
    found = [m.group(1) for p in patterns for m in [re.search(p, html, re.IGNORECASE)] if m]
    found += ['openapi/v1.json', 'swagger/v1/swagger.json', 'openapi.json', 'swagger.json']
    page = page_url if page_url.endswith('/') else page_url + '/'
    parent = urljoin(page, '..')
    candidates = []
    for link in found:
        for base in (page, parent):
            url = urljoin(base, link)
            if url not in candidates:
                candidates.append(url)
    return candidates


def load_from_url(url, auth_header=''):
    """Returns (document, final_url). Follows Scalar/Swagger UI pages to the spec they load."""
    final_url, text = fetch(url, auth_header)
    if not text.lstrip().startswith('<'):
        return load_document(text), final_url
    for candidate in spec_candidates(text, final_url):
        try:
            spec_url, spec_text = fetch(candidate, auth_header)
            doc = load_document(spec_text)
        except CollectionError:
            continue
        if 'openapi' in doc or 'swagger' in doc or ('item' in doc and 'info' in doc):
            return doc, spec_url
    raise CollectionError('No OpenAPI/Swagger spec could be loaded from that page. Check the authentication '
                          'header, or paste the direct openapi.json / swagger.json URL instead.')


# ---------- helpers ----------

def substitute(value, env):
    if not env or value is None:
        return value
    if isinstance(value, str):
        return VAR_RE.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {substitute(k, env): substitute(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, env) for v in value]
    return value


def endpoint_name(method, path, summary):
    return summary.strip() if summary and summary.strip() else f'{method} {path}'


def fingerprint(ep):
    keys = ['folder', 'method', 'url', 'content_type', 'headers', 'query_params', 'body']
    raw = json.dumps({k: ep.get(k) for k in keys}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------- request body templates ----------

XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'


def is_xml(content_type):
    return 'xml' in (content_type or '').lower()


def _xml_name(name):
    name = re.sub(r'[^\w.\-]', '_', str(name)) or 'item'
    return name if re.match(r'[A-Za-z_]', name) else '_' + name


def to_xml(value, name='root', indent=0):
    pad, tag = '  ' * indent, _xml_name(name)
    if isinstance(value, dict):
        inner = ''.join(to_xml(v, k, indent + 1) for k, v in value.items())
        return f'{pad}<{tag}>\n{inner}{pad}</{tag}>\n' if inner else f'{pad}<{tag}/>\n'
    if isinstance(value, list):
        return ''.join(to_xml(v, name, indent) for v in value) or f'{pad}<{tag}/>\n'
    if value is None:
        return f'{pad}<{tag}/>\n'
    text = str(value).lower() if isinstance(value, bool) else str(value)
    return f'{pad}<{tag}>{xml_escape(text)}</{tag}>\n'


def xml_document(value, root='root'):
    if isinstance(value, dict) and len(value) == 1 and isinstance(next(iter(value.values())), (dict, list)):
        root, value = next(iter(value.items()))
    return XML_DECLARATION + to_xml(value, root).rstrip('\n')


def empty_body(xml=False):
    """Template shown in the Body tab of an API whose spec has no request body."""
    return XML_DECLARATION + '<root/>' if xml else '{}'


def is_empty_body(body):
    text = (body or '').strip()
    return text in ('', '{}', '[]') or text == empty_body(xml=True)


def make_endpoint(folder, method, path, url, summary='', content_type='', headers=None,
                  path_params=None, query_params=None, body='', expected_status=200, body_xml=None):
    method = method.upper()
    if not (body or '').strip():
        body = empty_body(is_xml(content_type) if body_xml is None else body_xml)
    ep = {
        'folder': folder or 'Default',
        'key': f'{method} {path}',
        'name': endpoint_name(method, path, summary),
        'summary': summary or '',
        'method': method,
        'path': path,
        'url': url,
        'content_type': content_type or '',
        'headers': headers or {},
        'path_params': path_params or {},
        'query_params': query_params or {},
        'body': body or '',
        'expected_status': expected_status,
    }
    ep['fingerprint'] = fingerprint(ep)
    return ep


# ---------- OpenAPI 3.x / Swagger 2.0 ----------

class SchemaExample:
    def __init__(self, doc):
        self.doc = doc

    def root_name(self, schema):
        """XML root element: the schema's xml.name, else the name of the referenced schema."""
        if isinstance(schema, dict):
            if isinstance(schema.get('xml'), dict) and schema['xml'].get('name'):
                return schema['xml']['name']
            if '$ref' in schema:
                target = self.resolve(schema)
                if isinstance(target.get('xml'), dict) and target['xml'].get('name'):
                    return target['xml']['name']
                return schema['$ref'].rsplit('/', 1)[-1]
        return 'root'

    def resolve(self, obj, depth=0):
        while isinstance(obj, dict) and '$ref' in obj and depth < 20:
            ref = obj['$ref']
            if not ref.startswith('#/'):
                return {}
            target = self.doc
            for part in ref[2:].split('/'):
                target = target.get(part.replace('~1', '/').replace('~0', '~'), {}) if isinstance(target, dict) else {}
            obj = target
            depth += 1
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def primary_type(schema):
        t = schema.get('type')
        if isinstance(t, list):  # OpenAPI 3.1: ["integer", "string"], ["string", "null"]
            t = next((x for x in t if x != 'null'), None)
        if not t:
            if 'properties' in schema:
                return 'object'
            if 'items' in schema:
                return 'array'
        return t

    def example(self, schema, depth=0):
        schema = self.resolve(schema)
        if depth > 6 or not schema:
            return None
        for key in ('example', 'default'):
            if key in schema:
                return schema[key]
        if schema.get('examples') and isinstance(schema['examples'], list):
            return schema['examples'][0]
        if schema.get('enum'):
            return schema['enum'][0]
        for combo in ('allOf', 'oneOf', 'anyOf'):
            if schema.get(combo):
                if combo == 'allOf':
                    merged = {}
                    for part in schema['allOf']:
                        value = self.example(part, depth + 1)
                        if isinstance(value, dict):
                            merged.update(value)
                    return merged
                return self.example(schema[combo][0], depth + 1)
        t = self.primary_type(schema)
        if t == 'object':
            return {name: self.example(prop, depth + 1) for name, prop in schema.get('properties', {}).items()}
        if t == 'array':
            item = self.example(schema.get('items', {}), depth + 1)
            return [item] if item is not None else []
        if t in ('integer', 'number'):
            return 1
        if t == 'boolean':
            return True
        if t == 'string':
            fmt = schema.get('format', '')
            return {'date': '2026-01-01', 'date-time': '2026-01-01T00:00:00Z', 'email': 'user@example.com',
                    'uuid': '00000000-0000-0000-0000-000000000000', 'uri': 'https://example.com'}.get(fmt, 'string')
        return None


def openapi_base_url(doc, source_url):
    if doc.get('swagger'):
        host = doc.get('host')
        if host:
            scheme = (doc.get('schemes') or ['https'])[0]
            return f'{scheme}://{host}{doc.get("basePath", "")}'.rstrip('/')
        base = doc.get('basePath', '')
    else:
        servers = doc.get('servers') or []
        base = servers[0].get('url', '') if servers and isinstance(servers[0], dict) else ''
        for name, var in ((servers[0].get('variables') or {}).items() if servers else []):
            base = base.replace('{' + name + '}', str(var.get('default', '')))
    if base.startswith('http'):
        return base.rstrip('/')
    if source_url:
        parsed = urlparse(source_url)
        return f'{parsed.scheme}://{parsed.netloc}{base}'.rstrip('/')
    return ('{{baseUrl}}' + base).rstrip('/')


def path_param_value(param, schema):
    if 'example' in param:
        return str(param['example'])
    if 'example' in schema or 'default' in schema:
        return str(schema.get('example', schema.get('default')))
    t = SchemaExample.primary_type(schema) or param.get('type')
    if t in ('integer', 'number') or param['name'].lower().endswith('id'):
        return '1'
    return '{{' + param['name'] + '}}'


def parse_openapi(doc, source_url=''):
    examples = SchemaExample(doc)
    base_url = openapi_base_url(doc, source_url)
    is_swagger2 = bool(doc.get('swagger'))
    endpoints = []
    for path, item in (doc.get('paths') or {}).items():
        item = examples.resolve(item)
        shared = item.get('parameters', [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = {}
            for p in shared + op.get('parameters', []):
                p = examples.resolve(p)
                if p.get('name'):
                    params[(p['name'], p.get('in'))] = p

            path_params, query_params, headers = {}, {}, {}
            url_path = path
            for (name, location), p in params.items():
                schema = examples.resolve(p.get('schema', {})) if not is_swagger2 else p
                if location == 'path':
                    value = path_param_value(p, schema)
                    path_params[name] = value
                    url_path = url_path.replace('{' + name + '}', value)
                elif location == 'query':
                    value = p.get('example', schema.get('example', schema.get('default', '')))
                    query_params[name] = '' if value is None else str(value)
                elif location == 'header':
                    headers[name] = str(p.get('example', schema.get('example', '')))

            content_type, body = '', ''
            if is_swagger2:
                body_param = next((p for p in params.values() if p.get('in') == 'body'), None)
                if body_param:
                    consumes = op.get('consumes') or doc.get('consumes') or ['application/json']
                    content_type = 'application/json' if 'application/json' in consumes else consumes[0]
                    schema = body_param.get('schema', {})
                    body = format_body(examples.example(schema), content_type, examples.root_name(schema))
                produces = op.get('produces') or doc.get('produces') or []
            else:
                content = examples.resolve(op.get('requestBody', {})).get('content', {})
                if content:
                    content_type = 'application/json' if 'application/json' in content else next(iter(content))
                    media = content[content_type]
                    value = media.get('example')
                    if value is None:
                        value = examples.example(media.get('schema', {}))
                    body = format_body(value, content_type, examples.root_name(media.get('schema', {})))
                produces = [ct for r in (op.get('responses') or {}).values()
                            for ct in (examples.resolve(r).get('content') or {})]
            # no request body in the spec: the template follows the format the API answers in
            speaks_xml = is_xml(content_type) or (not content_type and any(is_xml(p) for p in produces)
                                                  and not any('json' in p for p in produces))
            if content_type:
                headers.setdefault('Content-Type', content_type)

            success = sorted(int(c) for c in (op.get('responses') or {}) if str(c).isdigit() and str(c).startswith('2'))
            tags = op.get('tags') or []
            folder = tags[0] if tags else (path.strip('/').split('/')[0] or 'Default')
            endpoints.append(make_endpoint(
                folder, method, path, base_url + url_path, op.get('summary', ''), content_type,
                headers, path_params, query_params, body, success[0] if success else 200, body_xml=speaks_xml))
    return endpoints


def format_body(value, content_type, root='root'):
    """Example value -> body text in the request's format (JSON, XML or plain text)."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if is_xml(content_type):
        return xml_document(value, root)
    return json.dumps(value, indent=2)


# ---------- Postman v2.0 / v2.1 ----------

def postman_url(url):
    if isinstance(url, str):
        return url, {}
    if not isinstance(url, dict):
        return '', {}
    raw = url.get('raw')
    query = {q['key']: q.get('value') or '' for q in url.get('query', []) if q.get('key') and not q.get('disabled')}
    if not raw:
        host = url.get('host', '')
        host = '.'.join(host) if isinstance(host, list) else host
        path = url.get('path', '')
        path = '/'.join(p if isinstance(p, str) else p.get('value', '') for p in path) if isinstance(path, list) else path
        raw = f'{url.get("protocol", "https")}://{host}/{path}' if not host.startswith('{{') else f'{host}/{path}'
    return raw, query


def parse_postman(doc, env):
    variables = {v['key']: str(v.get('value', '')) for v in doc.get('variable', []) if v.get('key')}
    variables.update(env)
    endpoints = []

    def walk(items, folder_path):
        for it in items:
            if 'item' in it:
                walk(it['item'], folder_path + [it.get('name', 'Folder')])
                continue
            req = it.get('request')
            if req is None:
                continue
            if isinstance(req, str):
                req = {'method': 'GET', 'url': req}
            method = (req.get('method') or 'GET').upper()
            raw_url, query = postman_url(req.get('url'))
            url = substitute(raw_url, variables)
            parsed = urlparse(url)
            path = parsed.path or '/'
            headers = {h['key']: h.get('value', '') for h in req.get('header', [])
                       if isinstance(h, dict) and h.get('key') and not h.get('disabled')}
            body_def = req.get('body') or {}
            body, content_type = '', headers.get('Content-Type', '')
            mode = body_def.get('mode')
            if mode == 'raw':
                body = body_def.get('raw', '')
                if not content_type:
                    lang = (body_def.get('options') or {}).get('raw', {}).get('language', '')
                    content_type = 'application/json' if lang == 'json' or body.lstrip().startswith(('{', '[')) else 'text/plain'
            elif mode in ('urlencoded', 'formdata'):
                fields = {f['key']: f.get('value', '') for f in body_def.get(mode, []) if f.get('key') and not f.get('disabled')}
                body = json.dumps(fields, indent=2)
                content_type = content_type or ('application/x-www-form-urlencoded' if mode == 'urlencoded' else 'multipart/form-data')
            if content_type:
                headers.setdefault('Content-Type', content_type)
            folder = ' · '.join(folder_path) if folder_path else doc.get('info', {}).get('name', 'Default')
            endpoints.append(make_endpoint(
                folder, method, path, url.split('?')[0], it.get('name', ''), content_type,
                substitute(headers, variables), {}, substitute(query, variables), substitute(body, variables)))

    walk(doc.get('item', []), [])
    return endpoints


# ---------- cURL (Manual Insert API) ----------

def _curl_tokens(command):
    text = command.strip()
    # Windows "Copy as cURL (cmd)": ^ escapes and ^ line continuations
    if '^"' in text or re.search(r'\^\s*\n', text):
        text = re.sub(r'\^(.)', r'\1', re.sub(r'\^\s*\n', ' ', text))
    text = re.sub(r'\\\s*\n', ' ', text)                 # bash line continuations
    text = re.sub(r"\$'((?:[^'\\]|\\.)*)'", lambda m: "'" + m.group(1).replace("\\'", "'\"'\"'") + "'", text)
    try:
        return shlex.split(text, posix=True)
    except ValueError as exc:
        raise CollectionError(f'Could not read the cURL command: {exc}')


def parse_curl(command):
    """'curl -X POST https://host/api/x?a=1 -H "k: v" --data-raw '{..}'' -> request parts."""
    tokens = _curl_tokens(command)
    if not tokens or os.path.basename(tokens[0]).lower() not in ('curl', 'curl.exe'):
        raise CollectionError('Paste a cURL command that starts with "curl".')
    method, url, headers, data, force_get = None, None, {}, [], False
    with_value = {'-X', '--request', '-H', '--header', '-d', '--data', '--data-raw', '--data-binary',
                  '--data-ascii', '--data-urlencode', '--json', '-u', '--user', '--url', '-A', '--user-agent',
                  '-b', '--cookie', '-e', '--referer', '-F', '--form', '-o', '--output', '-m', '--max-time',
                  '--connect-timeout', '-x', '--proxy'}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('--') and '=' in tok:
            tok, value = tok.split('=', 1)
            tokens[i + 1:i + 1] = [value]
        if tok in with_value:
            value = tokens[i + 1] if i + 1 < len(tokens) else ''
            i += 2
            if tok in ('-X', '--request'):
                method = value.upper()
            elif tok in ('-H', '--header'):
                name, _, val = value.partition(':')
                if name.strip():
                    headers[name.strip()] = val.strip()
            elif tok in ('-d', '--data', '--data-raw', '--data-binary', '--data-ascii', '--data-urlencode'):
                data.append(value)
            elif tok == '--json':
                data.append(value)
                headers.setdefault('Content-Type', 'application/json')
                headers.setdefault('Accept', 'application/json')
            elif tok in ('-F', '--form'):
                data.append(value)
                headers.setdefault('Content-Type', 'multipart/form-data')
            elif tok in ('-u', '--user'):
                headers['Authorization'] = 'Basic ' + base64.b64encode(value.encode()).decode()
            elif tok in ('-A', '--user-agent'):
                headers['User-Agent'] = value
            elif tok in ('-b', '--cookie'):
                headers['Cookie'] = value
            elif tok in ('-e', '--referer'):
                headers['Referer'] = value
            elif tok == '--url':
                url = value
            continue
        if tok in ('-G', '--get'):
            force_get = True
        elif not tok.startswith('-') and url is None:
            url = tok
        i += 1

    if not url:
        raise CollectionError('No URL was found in the cURL command.')
    if not re.match(r'^[a-z]+://', url, re.IGNORECASE) and not url.startswith('{{'):
        url = 'https://' + url
    body = '&'.join(data)
    parsed = urlparse(url)
    query = {}
    for pair in filter(None, parsed.query.split('&')):
        key, _, value = pair.partition('=')
        query[unquote_plus(key)] = unquote_plus(value)
    if force_get and body:
        for pair in body.split('&'):
            key, _, value = pair.partition('=')
            query[unquote_plus(key)] = unquote_plus(value)
        body = ''
    method = 'GET' if force_get else (method or ('POST' if body else 'GET'))
    content_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '')
    if body and not content_type:
        content_type = 'application/json' if body.lstrip().startswith(('{', '[')) else 'application/x-www-form-urlencoded'
        headers['Content-Type'] = content_type
    if content_type.startswith('application/json') and body:
        try:
            body = json.dumps(json.loads(body), indent=2)
        except ValueError:
            pass
    base = url.split('?', 1)[0].split('#', 1)[0]
    if not body.strip():
        body = empty_body(is_xml(content_type) or is_xml(headers.get('Accept', '')))
    return {
        'method': method,
        'url': base,
        'path': parsed.path or '/',
        'headers': headers,
        'query_params': query,
        'body': body,
        'content_type': content_type,
    }


# ---------- entry point ----------

def parse_collection(doc, env=None, source_url=''):
    env = env or {}
    if 'openapi' in doc or 'swagger' in doc:
        endpoints = parse_openapi(doc, source_url)
        endpoints = [dict(ep, **{k: substitute(ep[k], env) for k in ('url', 'headers', 'query_params', 'body')})
                     for ep in endpoints] if env else endpoints
    elif 'item' in doc and 'info' in doc:
        endpoints = parse_postman(doc, env)
    else:
        raise CollectionError('Unsupported format. Use Postman Collection v2.0/v2.1 or OpenAPI/Swagger 2.0/3.x.')
    if not endpoints:
        raise CollectionError('No API endpoints were found in the collection.')
    # keep the first occurrence if a collection repeats METHOD + path
    unique = {}
    for ep in endpoints:
        ep['fingerprint'] = fingerprint(ep)
        unique.setdefault(ep['key'], ep)
    return list(unique.values())
