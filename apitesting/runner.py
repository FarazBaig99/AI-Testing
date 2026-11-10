"""Sends a workspace request (Postman-style spec) and returns the response for the response viewer.

A spec is a dict:
    method, url, params, headers, auth, body_mode, raw_language, body, form_data, urlencoded,
    graphql_query, graphql_variables, verify_ssl
Key/value lists are [{key, value, enabled}] (form_data rows also have type: text|file).
"""
import base64
import json
import ssl
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from .models import BASE_URL_VAR

TIMEOUT = 30
MAX_BODY = 5 * 1024 * 1024
RAW_TYPES = {'text': 'text/plain', 'javascript': 'application/javascript', 'json': 'application/json',
             'html': 'text/html', 'xml': 'application/xml'}
FIXED_TYPE_MODES = ('form-data', 'urlencoded', 'graphql')
TEXT_HINTS = ('text/', 'json', 'xml', 'javascript', 'html', 'x-www-form-urlencoded', 'graphql')


class RequestError(Exception):
    pass


def rows(items):
    """Enabled rows with a key, as (key, value) pairs."""
    return [(r.get('key', '').strip(), r.get('value', '')) for r in items or []
            if r.get('enabled', True) and r.get('key', '').strip()]


def resolve_url(url, env):
    url = (url or '').strip()
    if BASE_URL_VAR in url:
        if env is None:
            raise RequestError('The URL uses {{baseUrl}} but no environment is attached to this request.')
        url = env.resolve(url)
    if not url:
        raise RequestError('Enter a request URL.')
    if '://' not in url:
        url = 'http://' + url
    if urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc:
        raise RequestError(f'Invalid URL: {url}')
    return url


def _multipart(form_rows, files):
    boundary = '----BaigNova' + uuid.uuid4().hex
    out = []
    for i, r in enumerate(form_rows or []):
        key = r.get('key', '').strip()
        if not r.get('enabled', True) or not key:
            continue
        out.append(f'--{boundary}\r\n'.encode())
        upload = files.get(f'file_{i}') if r.get('type') == 'file' else None
        if upload is not None:
            out.append(f'Content-Disposition: form-data; name="{key}"; filename="{upload.name}"\r\n'.encode())
            out.append(f'Content-Type: {getattr(upload, "content_type", None) or "application/octet-stream"}\r\n\r\n'
                       .encode())
            out.append(upload.read())
        else:
            out.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            out.append(str(r.get('value', '')).encode())
        out.append(b'\r\n')
    out.append(f'--{boundary}--\r\n'.encode())
    return b''.join(out), f'multipart/form-data; boundary={boundary}'


def build(spec, env=None, files=None):
    """(method, url, headers, data) ready for urllib."""
    files = files or {}
    method = (spec.get('method') or 'GET').upper()
    url = resolve_url(spec.get('url'), env)
    headers = {k: env.resolve(v) if env else v for k, v in rows(spec.get('headers'))}
    names = {k.lower() for k in headers}

    auth = spec.get('auth') or {}
    kind = auth.get('type', 'none')
    if kind == 'bearer' and auth.get('token') and 'authorization' not in names:
        headers['Authorization'] = f"Bearer {auth['token']}"
    elif kind == 'basic' and 'authorization' not in names:
        pair = f"{auth.get('username', '')}:{auth.get('password', '')}".encode()
        headers['Authorization'] = 'Basic ' + base64.b64encode(pair).decode()
    elif kind == 'apikey' and auth.get('key'):
        if auth.get('add_to') == 'query':
            url += ('&' if '?' in url else '?') + urlencode({auth['key']: auth.get('value', '')})
        else:
            headers[auth['key']] = auth.get('value', '')

    mode = spec.get('body_mode', 'none')
    data, content_type = None, None
    if mode == 'raw':
        data = (spec.get('body') or '').encode()
        content_type = RAW_TYPES.get(spec.get('raw_language'), 'text/plain')
    elif mode == 'urlencoded':
        data = urlencode(rows(spec.get('urlencoded'))).encode()
        content_type = 'application/x-www-form-urlencoded'
    elif mode == 'form-data':
        data, content_type = _multipart(spec.get('form_data'), files)
    elif mode == 'binary':
        upload = files.get('binary')
        if upload is None:
            raise RequestError('Choose a file for the binary body.')
        data = upload.read()
        content_type = getattr(upload, 'content_type', None) or 'application/octet-stream'
    elif mode == 'graphql':
        variables = (spec.get('graphql_variables') or '').strip()
        try:
            variables = json.loads(variables) if variables else {}
        except ValueError:
            raise RequestError('GraphQL variables must be valid JSON.')
        data = json.dumps({'query': spec.get('graphql_query') or '', 'variables': variables}).encode()
        content_type = 'application/json'
    if content_type and (mode in FIXED_TYPE_MODES or 'content-type' not in names):
        # form-data, urlencoded and GraphQL bodies only make sense with their own content type
        headers = {k: v for k, v in headers.items() if k.lower() != 'content-type'}
        headers['Content-Type'] = content_type
    return method, url, headers, data


def _open(req, timeout, verify_ssl):
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return build_opener(HTTPSHandler(context=context)).open(req, timeout=timeout)


def guess_language(content_type, body):
    ct = (content_type or '').lower()
    for lang, hint in (('json', 'json'), ('xml', 'xml'), ('html', 'html'), ('javascript', 'javascript')):
        if hint in ct:
            return lang
    head = body.lstrip()[:1]
    if head in ('{', '['):
        return 'json'
    if head == '<':
        return 'html' if '<html' in body[:500].lower() else 'xml'
    return 'text'


def _response(resp, started):
    raw = resp.read(MAX_BODY + 1)
    ms = int((time.monotonic() - started) * 1000)
    truncated = len(raw) > MAX_BODY
    raw = raw[:MAX_BODY]
    headers = list(resp.headers.items()) if resp.headers else []
    content_type = resp.headers.get('Content-Type', '') if resp.headers else ''
    charset = resp.headers.get_content_charset() if resp.headers and hasattr(resp.headers, 'get_content_charset') else None
    is_text = not raw or any(h in content_type.lower() for h in TEXT_HINTS)
    if not is_text:
        try:
            body = raw.decode(charset or 'utf-8')
            is_text = True
        except UnicodeDecodeError:
            body = f'(binary content, {len(raw)} bytes, {content_type or "unknown type"})'
    if is_text:
        body = raw.decode(charset or 'utf-8', errors='replace')
    return {'ok': True, 'status': resp.status, 'reason': getattr(resp, 'reason', '') or '', 'ms': ms,
            'size': len(raw), 'headers': headers, 'content_type': content_type, 'body': body,
            'language': guess_language(content_type, body) if is_text else 'text', 'truncated': truncated}


def send(spec, env=None, files=None, opener=None):
    """Send the request; never raises. Returns the response dict or {'ok': False, 'error': ...}."""
    started = time.monotonic()
    try:
        method, url, headers, data = build(spec, env, files)
    except RequestError as exc:
        return {'ok': False, 'error': str(exc), 'ms': 0}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        resp = (opener or _open)(req, TIMEOUT, spec.get('verify_ssl', True))
        with resp:
            result = _response(resp, started)
    except HTTPError as exc:
        result = _response(exc, started)
    except (URLError, OSError, ValueError) as exc:
        reason = getattr(exc, 'reason', exc)
        text = str(reason)
        if isinstance(reason, ssl.SSLError) or 'CERTIFICATE' in text.upper():
            text += ' (turn off SSL certificate verification in Settings to send anyway)'
        elif 'timed out' in text:
            text = f'Request timed out after {TIMEOUT} s'
        return {'ok': False, 'error': f'Could not send request: {text}', 'url': url,
                'ms': int((time.monotonic() - started) * 1000)}
    result['url'] = url
    result['method'] = method
    return result
