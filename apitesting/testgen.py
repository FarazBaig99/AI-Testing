"""Autonomous (rule-based) API test generation.

From a synced API (its method, URL, headers, auth and example body) this builds a suite of test
scenarios - a happy path plus negative, boundary and authentication cases - that can be saved as
requests in a collection and run. It needs no AI and no network; it reasons from the example body's
own shape (a field that holds a number is treated as a number, and so on).

Pure module: it returns scenario dicts, it never writes to the database.
"""
import json

LONG_STRING = 'x' * 600
HUGE_NUMBER = 9999999999
NEG_NUMBER = -1

# scenario groups the UI can switch on
GROUPS = [
    ('happy', 'Happy path', 'The request as imported, expecting success.'),
    ('missing', 'Missing required field', 'Each body field removed in turn.'),
    ('null', 'Null field', 'Each body field set to null.'),
    ('type', 'Wrong data type', 'Each field sent as the wrong type (number as text and the reverse).'),
    ('empty', 'Empty value', 'Each text field sent as an empty string.'),
    ('boundary', 'Boundary value', 'Over-long text and out-of-range numbers.'),
    ('auth', 'Authentication', 'The request with no token and with an invalid token.'),
]
GROUP_KEYS = [g[0] for g in GROUPS]

# what a negative scenario is expected to return (the UI lets the user change this)
DEFAULT_NEG_STATUS = 400
DEFAULT_AUTH_STATUS = 401


def _parse_json_body(body, content_type):
    """The example body as a dict, or None when it is not a JSON object."""
    ct = (content_type or '').lower()
    if 'json' not in ct and not (body or '').lstrip().startswith(('{', '[')):
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _wrong_type(value):
    """A value of a deliberately wrong type for the given one."""
    if isinstance(value, bool):
        return 'not-a-boolean'
    if isinstance(value, (int, float)):
        return 'not-a-number'
    if isinstance(value, str):
        return 12345
    if isinstance(value, list):
        return 'not-a-list'
    if isinstance(value, dict):
        return 'not-an-object'
    return 0


def _test_script(low, high, label):
    return (f'pm.test("{label}", function () {{\n'
            f'    pm.expect(pm.response.code, "status code").to.be.within({low}, {high});\n'
            f'}});')


def _base(spec, name, kind, note, body=None, body_mode=None, auth=None,
          expected=None, low=200, high=299, label='succeeds'):
    """One scenario built from the source spec, overriding only what the case changes."""
    out = {
        'name': name, 'kind': kind, 'note': note,
        'method': spec['method'], 'url': spec['url'],
        'params': spec.get('params') or [], 'headers': spec.get('headers') or [],
        'auth': spec['auth'] if auth is None else auth,
        'body_mode': spec.get('body_mode', 'none') if body_mode is None else body_mode,
        'raw_language': spec.get('raw_language', 'json'),
        'body': spec.get('body', '') if body is None else body,
        'expected_status': expected,
        'test_script': _test_script(low, high, label),
    }
    return out


def generate(spec, groups=None, neg_status=DEFAULT_NEG_STATUS, auth_status=DEFAULT_AUTH_STATUS):
    """Build the scenarios. `spec` is the builder spec of a request/synced API (method, url, params,
    headers, auth, body_mode, body, raw_language, expected_status). Returns a list of scenario dicts."""
    groups = set(groups or GROUP_KEYS)
    base_name = spec.get('name') or f"{spec['method']} {spec['url']}"
    happy_status = spec.get('expected_status') or None
    out = []

    if 'happy' in groups:
        out.append(_base(spec, f'{base_name} · happy path', 'happy',
                         'The request as imported, expecting a success response.',
                         expected=happy_status,
                         low=happy_status or 200, high=happy_status or 299,
                         label='returns success'))

    fields = _parse_json_body(spec.get('body'), spec.get('raw_language') == 'xml' and 'xml' or 'json')
    has_body = spec.get('body_mode') == 'raw' and fields is not None

    if has_body:
        keys = list(fields.keys())
        if 'missing' in groups:
            for k in keys:
                body = json.dumps({x: v for x, v in fields.items() if x != k}, indent=2)
                out.append(_base(spec, f'{base_name} · missing "{k}"', 'missing',
                                 f'The field "{k}" is removed from the body.', body=body,
                                 expected=neg_status, low=400, high=499, label=f'rejects missing "{k}"'))
        if 'null' in groups:
            for k in keys:
                body = json.dumps({**fields, k: None}, indent=2)
                out.append(_base(spec, f'{base_name} · null "{k}"', 'null',
                                 f'The field "{k}" is set to null.', body=body,
                                 expected=neg_status, low=400, high=499, label=f'rejects null "{k}"'))
        if 'type' in groups:
            for k in keys:
                body = json.dumps({**fields, k: _wrong_type(fields[k])}, indent=2)
                out.append(_base(spec, f'{base_name} · wrong type "{k}"', 'type',
                                 f'The field "{k}" is sent as the wrong data type.', body=body,
                                 expected=neg_status, low=400, high=499, label=f'rejects wrong type for "{k}"'))
        if 'empty' in groups:
            for k, v in fields.items():
                if isinstance(v, str):
                    body = json.dumps({**fields, k: ''}, indent=2)
                    out.append(_base(spec, f'{base_name} · empty "{k}"', 'empty',
                                     f'The text field "{k}" is sent empty.', body=body,
                                     expected=neg_status, low=400, high=499, label=f'rejects empty "{k}"'))
        if 'boundary' in groups:
            for k, v in fields.items():
                if isinstance(v, str):
                    body = json.dumps({**fields, k: LONG_STRING}, indent=2)
                    out.append(_base(spec, f'{base_name} · over-long "{k}"', 'boundary',
                                     f'The text field "{k}" is sent far over a typical length (600 chars).',
                                     body=body, expected=neg_status, low=400, high=499,
                                     label=f'handles over-long "{k}"'))
                elif isinstance(v, bool):
                    continue
                elif isinstance(v, (int, float)):
                    for tag, bad in (('negative', NEG_NUMBER), ('huge', HUGE_NUMBER)):
                        body = json.dumps({**fields, k: bad}, indent=2)
                        out.append(_base(spec, f'{base_name} · {tag} "{k}"', 'boundary',
                                         f'The number field "{k}" is sent as {bad}.', body=body,
                                         expected=neg_status, low=400, high=499,
                                         label=f'handles {tag} "{k}"'))

    if 'auth' in groups:
        auth_type = (spec.get('auth') or {}).get('type', 'none')
        no_auth = {'type': 'none'}
        out.append(_base(spec, f'{base_name} · no authentication', 'auth',
                         'The request is sent without any token or login.', auth=no_auth,
                         expected=auth_status, low=400, high=403, label='requires authentication'))
        if auth_type in ('bearer', 'apikey', 'basic'):
            bad = dict(spec.get('auth') or {})
            if auth_type == 'bearer':
                bad['token'] = 'invalid.token.value'
            elif auth_type == 'apikey':
                bad['value'] = 'invalid-api-key'
            else:
                bad['username'], bad['password'] = 'invalid', 'invalid'
            out.append(_base(spec, f'{base_name} · invalid token', 'auth',
                             'The request is sent with an invalid token / key.', auth=bad,
                             expected=auth_status, low=400, high=403, label='rejects an invalid token'))
    return out


def summary(scenarios):
    """Count per group, for the preview."""
    counts = {}
    for s in scenarios:
        counts[s['kind']] = counts.get(s['kind'], 0) + 1
    return counts
