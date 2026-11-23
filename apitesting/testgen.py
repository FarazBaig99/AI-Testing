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
    # --- opt-in groups (never selected by default, so existing generated suites do not change) ---
    ('injection', 'Injection (SQL / NoSQL / XSS)', 'Each text field replaced by SQL, NoSQL and XSS payloads; the server must not 5xx or echo the script back.'),
    ('jwt', 'Token tampering (JWT)', 'Expired, tampered, alg=none, empty and malformed bearer tokens must be rejected.'),
    ('idor', 'IDOR / BOLA', 'The id in the URL changed to another record; access must be refused or not found.'),
    ('massassign', 'Mass assignment', 'Privileged fields (isAdmin, role, id...) added to the body; they must not be accepted.'),
    ('secheaders', 'Security headers', 'The happy request, checking X-Content-Type-Options, HSTS, no Server/X-Powered-By leak.'),
    ('cors', 'CORS', 'A request from an untrusted Origin must not be allowed by Access-Control-Allow-Origin.'),
    ('special', 'Special / Unicode characters', 'Each text field sent with special characters, emoji, RTL and multi-byte text.'),
    ('bva', 'Boundary values (BVA)', 'Numbers at 0, 1, -1, int32 max and max+1; text of 1 char and whitespace only.'),
    ('equivalence', 'Equivalence partitions', 'One representative from each invalid class per field: wrong type, blank, oversized, negative.'),
    ('pairwise', 'Pairwise (combinations)', 'Every pair of field values is covered in as few requests as possible.'),
    ('combinatorial', 'Combinatorial (all combinations)', 'Every combination of each field valid/blank/invalid values (kept small: at most 30 cases).'),
    ('pagination', 'Pagination / sorting / filtering', 'Odd page, limit, sort and filter query values; the server must not 5xx.'),
    ('bulk', 'Bulk / large payload', 'The body sent as an array of many items, and with very long text; the server must not 5xx.'),
    ('idempotency', 'Idempotency', 'The same request sent twice in a row; the second response must match the first.'),
    ('contract', 'Contract / schema', 'The happy request, checking the response is JSON of the expected shape (needs a sample response for field-level checks).'),
    ('sensitive', 'Sensitive data exposure', 'The happy request, checking the response does not leak passwords, secrets or stack traces.'),
]
# the groups a plain generate() call uses (unchanged from before the opt-in groups were added)
GROUP_KEYS = ['happy', 'missing', 'null', 'type', 'empty', 'boundary', 'auth']
EXTRA_GROUP_KEYS = [g[0] for g in GROUPS if g[0] not in GROUP_KEYS]

SQLI = "' OR '1'='1' --"
XSS = '<script>alert(1)</script>'
NOSQL = {'$ne': None}
SPECIAL_TEXTS = [('special chars', "!@#$%^&*()_+-=[]{};':\",./<>?\\|`~"), ('unicode', 'Ünïcödé ñ 日本語 العربية'),
                 ('emoji', '😀🔥'), ('newline/tab', 'line1\nline2\ttab')]
INT32_MAX = 2147483647
# tokens that are intentionally invalid (header.payload.signature)
_B64 = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
EXPIRED_JWT = _B64 + '.eyJzdWIiOiIxIiwiZXhwIjoxfQ.invalidsignature'
NONE_JWT = 'eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxIiwicm9sZSI6ImFkbWluIn0.'
MASS_FIELDS = {'isAdmin': True, 'is_admin': True, 'role': 'admin', 'admin': True, 'id': 999999, 'verified': True}
LEAK_WORDS = ['password', 'passwd', 'secret', 'api_key', 'apikey', 'private_key', 'stack trace', 'traceback',
              'exception in', 'sqlstate', 'at java.', 'at system.']

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


def infer_schema(value, depth=0):
    """A JSON Schema describing a sample JSON value: every key seen is required (so a missing field is
    caught) and no other key is allowed (so an unexpected field is caught). Arrays use their first item."""
    if value is None:
        return {}                              # null says nothing about the real type
    if isinstance(value, bool):
        return {'type': 'boolean'}
    if isinstance(value, int):
        return {'type': 'integer'}
    if isinstance(value, float):
        return {'type': 'number'}
    if isinstance(value, str):
        return {'type': 'string'}
    if isinstance(value, list):
        out = {'type': 'array'}
        if value and depth < 6:
            out['items'] = infer_schema(value[0], depth + 1)
        return out
    if isinstance(value, dict):
        if depth >= 6:
            return {'type': 'object'}
        return {'type': 'object', 'required': list(value), 'additionalProperties': False,
                'properties': {k: infer_schema(v, depth + 1) for k, v in value.items()}}
    return {}


def _js(value):
    """A value as a JS string literal (safe to embed in a generated script)."""
    return json.dumps(value if isinstance(value, str) else json.dumps(value))


def _script(label, *checks):
    """A test script made of several named checks: each is (title, js_condition)."""
    return '\n'.join(f'pm.test({_js(title)}, function () {{\n    pm.expect({cond}, {_js(title)}).to.be.true;\n}});'
                     for title, cond in checks) or _test_script(200, 299, label)


def _base(spec, name, kind, note, body=None, body_mode=None, auth=None,
          expected=None, low=200, high=299, label='succeeds', script=None, headers=None, url=None):
    """One scenario built from the source spec, overriding only what the case changes."""
    out = {
        'name': name, 'kind': kind, 'note': note,
        'method': spec['method'], 'url': spec['url'] if url is None else url,
        'params': spec.get('params') or [], 'headers': (spec.get('headers') or []) if headers is None else headers,
        'auth': spec['auth'] if auth is None else auth,
        'body_mode': spec.get('body_mode', 'none') if body_mode is None else body_mode,
        'raw_language': spec.get('raw_language', 'json'),
        'body': spec.get('body', '') if body is None else body,
        'expected_status': expected,
        'test_script': script if script is not None else _test_script(low, high, label),
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

    out.extend(_extra_scenarios(spec, groups, base_name, fields if has_body else None, neg_status, auth_status))
    return out


def _candidates(value):
    """A few values for one field: the original first, then a blank/zero and a wrong-typed one."""
    if isinstance(value, bool):
        return [value, not value, 'not-a-boolean']
    if isinstance(value, (int, float)):
        return [value, 0, -1, 'not-a-number']
    if isinstance(value, str):
        return [value, '', None, 12345]
    return [value, None]


def pairwise(columns):
    """Greedy all-pairs: a list of rows of indexes (one per column) such that every pair of values taken
    from two different columns appears together in at least one row."""
    from itertools import combinations
    n = len(columns)
    if n == 0:
        return []
    if n == 1:
        return [[i] for i in range(len(columns[0]))]
    uncovered = {(a, i, b, j) for a, b in combinations(range(n), 2)
                 for i in range(len(columns[a])) for j in range(len(columns[b]))}
    rows = []
    while uncovered:
        a, i, b, j = min(uncovered)
        row = [None] * n
        row[a], row[b] = i, j
        for c in range(n):
            if row[c] is not None:
                continue
            best = None
            for v in range(len(columns[c])):
                gain = 0
                for d in range(n):
                    if row[d] is None or d == c:
                        continue
                    key = (c, v, d, row[d]) if c < d else (d, row[d], c, v)
                    gain += key in uncovered
                if best is None or gain > best[0]:
                    best = (gain, v)
            row[c] = best[1]
        rows.append(row)
        for x, y in combinations(range(n), 2):
            uncovered.discard((x, row[x], y, row[y]))
    return rows


def _with_field(fields, key, value):
    return json.dumps({**fields, key: value}, indent=2)


def _extra_scenarios(spec, groups, base_name, fields, neg_status, auth_status):
    """Opt-in groups: security, extra validation and design-technique cases."""
    out = []
    auth = spec.get('auth') or {}
    auth_type = auth.get('type', 'none')
    method = (spec.get('method') or 'GET').upper()
    headers = list(spec.get('headers') or [])

    if fields is not None:
        text_keys = [k for k, v in fields.items() if isinstance(v, str)]
        num_keys = [k for k, v in fields.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        no5xx = ('server does not crash (no 5xx)', 'pm.response.code < 500')

        if 'injection' in groups:
            for k in text_keys:
                for tag, payload, echo in (('SQL injection', SQLI, False), ('XSS', XSS, True)):
                    checks = [no5xx]
                    if echo:
                        checks.append(('payload is not echoed back unescaped', f'!pm.response.text().includes({_js(payload)})'))
                    out.append(_base(spec, f'{base_name} · {tag} in "{k}"', 'injection',
                                     f'The text field "{k}" carries a {tag} payload.',
                                     body=_with_field(fields, k, payload), script=_script('', *checks)))
                out.append(_base(spec, f'{base_name} · NoSQL injection in "{k}"', 'injection',
                                 f'The text field "{k}" is replaced by a NoSQL operator object.',
                                 body=_with_field(fields, k, NOSQL), script=_script('', no5xx)))

        if 'massassign' in groups:
            extra = {k: v for k, v in MASS_FIELDS.items() if k not in fields}
            if extra:
                out.append(_base(spec, f'{base_name} · mass assignment', 'massassign',
                                 'Privileged fields (' + ', '.join(extra) + ') are added to the body.',
                                 body=json.dumps({**fields, **extra}, indent=2),
                                 script=_script('', no5xx,
                                                ('privileged fields are not reflected back as accepted',
                                                 '!/"(isAdmin|is_admin|admin|verified)"\\s*:\\s*true/.test(pm.response.text())'))))

        if 'special' in groups:
            for k in text_keys:
                for tag, text in SPECIAL_TEXTS:
                    out.append(_base(spec, f'{base_name} · {tag} in "{k}"', 'special',
                                     f'The text field "{k}" is sent with {tag}.',
                                     body=_with_field(fields, k, text), script=_script('', no5xx)))

        if 'bva' in groups:
            for k in num_keys:
                for tag, val in (('zero', 0), ('one', 1), ('minus one', -1),
                                 ('int32 max', INT32_MAX), ('int32 max + 1', INT32_MAX + 1)):
                    out.append(_base(spec, f'{base_name} · {tag} "{k}"', 'bva',
                                     f'The number field "{k}" is sent as {val}.',
                                     body=_with_field(fields, k, val), script=_script('', no5xx)))
            for k in text_keys:
                for tag, val in (('1 character', 'a'), ('whitespace only', '   ')):
                    out.append(_base(spec, f'{base_name} · {tag} "{k}"', 'bva',
                                     f'The text field "{k}" is sent as {tag}.',
                                     body=_with_field(fields, k, val), script=_script('', no5xx)))

        if 'equivalence' in groups:
            for k, v in fields.items():
                classes = [('wrong type', _wrong_type(v))]
                if isinstance(v, str):
                    classes += [('blank', ''), ('oversized', LONG_STRING)]
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    classes += [('negative', NEG_NUMBER), ('oversized', HUGE_NUMBER)]
                for tag, val in classes:
                    out.append(_base(spec, f'{base_name} · invalid class "{tag}" for "{k}"', 'equivalence',
                                     f'One representative of the invalid class "{tag}" for "{k}".',
                                     body=_with_field(fields, k, val), expected=neg_status, low=400, high=499,
                                     label=f'rejects {tag} "{k}"'))

    if fields is not None and ('pairwise' in groups or 'combinatorial' in groups):
        from itertools import product
        keys = list(fields)[:8]
        cols = [_candidates(fields[k]) for k in keys]
        kind = 'pairwise' if 'pairwise' in groups else 'combinatorial'
        if kind == 'pairwise':
            combos = pairwise(cols)
        else:
            total = 1
            for c in cols:
                total *= len(c)
            combos = [list(c) for c in product(*[range(len(c)) for c in cols])] if total <= 30 else []
        for n, idx in enumerate(combos, 1):
            if not any(idx):                 # the all-original row is the happy path
                continue
            vals = {k: cols[c][i] for c, (k, i) in enumerate(zip(keys, idx))}
            out.append(_base(spec, f'{base_name} · {kind} #{n}', kind,
                             'Field values: ' + ', '.join(f'{k}={json.dumps(v)}' for (k, v), i in zip(vals.items(), idx) if i),
                             body=json.dumps({**fields, **vals}, indent=2), script=_script('', ('server does not crash (no 5xx)', 'pm.response.code < 500'))))

    if 'bulk' in groups and fields is not None:
        out.append(_base(spec, f'{base_name} · bulk array of 50', 'bulk', 'The body is sent as an array of 50 copies.',
                         body=json.dumps([fields] * 50), script=_script('', ('server does not crash (no 5xx)', 'pm.response.code < 500'))))
        out.append(_base(spec, f'{base_name} · large payload', 'bulk', 'Each text field is sent 5,000 characters long.',
                         body=json.dumps({k: ('y' * 5000 if isinstance(v, str) else v) for k, v in fields.items()}),
                         script=_script('', ('server does not crash (no 5xx)', 'pm.response.code < 500'))))

    if 'pagination' in groups and method == 'GET':
        base_url = spec['url']
        sep = '&' if '?' in base_url else '?'
        for tag, query in (('page 0', 'page=0'), ('negative page', 'page=-1'), ('huge page', 'page=99999999'),
                           ('limit 0', 'limit=0'), ('huge limit', 'limit=1000000'), ('non-numeric page', 'page=abc'),
                           ('unknown sort field', 'sort=__nope__'), ('unknown filter', 'filter=__nope__')):
            out.append(_base(spec, f'{base_name} · {tag}', 'pagination', f'The query "{query}" is added.',
                             url=f'{base_url}{sep}{query}', script=_script('', ('server does not crash (no 5xx)', 'pm.response.code < 500'))))

    if 'idempotency' in groups:
        key = 'idem_' + ''.join(ch if ch.isalnum() else '_' for ch in base_name)[:40]
        first = ('pm.collectionVariables.set(' + _js(key) + ', String(pm.response.code));\n'
                 'pm.test("first call recorded", function () { pm.expect(pm.response.code < 500).to.be.true; });')
        second = ('pm.test("repeat gives the same result (idempotent)", function () {\n'
                  '    pm.expect(String(pm.response.code), "status of the repeat").to.equal(pm.collectionVariables.get(' + _js(key) + '));\n'
                  '});')
        out.append(_base(spec, f'{base_name} · idempotency call 1', 'idempotency', 'The request is sent for the first time.', script=first))
        out.append(_base(spec, f'{base_name} · idempotency call 2', 'idempotency',
                         'The identical request is repeated; the response status must be the same.', script=second))

    if 'jwt' in groups and auth_type == 'bearer':
        for tag, token in (('expired token', EXPIRED_JWT), ('alg=none token', NONE_JWT),
                           ('empty token', ''), ('malformed token', 'not-a-jwt')):
            bad = {**auth, 'token': token}
            out.append(_base(spec, f'{base_name} · {tag}', 'jwt',
                             f'The request is sent with an {tag}; it must be rejected.', auth=bad,
                             expected=auth_status, low=400, high=403, label=f'rejects an {tag}'))

    if 'idor' in groups:
        import re
        m = list(re.finditer(r'/(\d+)(?=/|\?|$)', spec['url']))
        if m:
            last = m[-1]
            for tag, new in (('another id', str(int(last.group(1)) + 1)), ('id 0', '0')):
                url = spec['url'][:last.start(1)] + new + spec['url'][last.end(1):]
                out.append(_base(spec, f'{base_name} · IDOR {tag}', 'idor',
                                 f'The id in the URL is changed to {new}; another record must not be readable or changeable.',
                                 url=url, expected=403, low=400, high=404, label='refuses access to another record'))

    if 'secheaders' in groups:
        out.append(_base(spec, f'{base_name} · security headers', 'secheaders',
                         'The request as imported; the response is checked for security headers.',
                         script=_script('', ('X-Content-Type-Options: nosniff is set',
                                             'pm.response.headers.has("X-Content-Type-Options", "nosniff")'),
                                        ('server does not leak X-Powered-By', '!pm.response.headers.has("X-Powered-By")'))))

    if 'cors' in groups:
        evil = 'https://evil.example'
        hdrs = [h for h in headers if str(h.get('key', '')).lower() != 'origin'] + \
               [{'key': 'Origin', 'value': evil, 'enabled': True}]
        out.append(_base(spec, f'{base_name} · CORS untrusted origin', 'cors',
                         'The request is sent with an untrusted Origin header.', headers=hdrs,
                         script=_script('', ('untrusted origin is not allowed',
                                             f'!pm.response.headers.has("Access-Control-Allow-Origin", {_js(evil)})'),
                                        ('wildcard origin is not combined with credentials',
                                         '!(pm.response.headers.has("Access-Control-Allow-Origin", "*") '
                                         '&& pm.response.headers.has("Access-Control-Allow-Credentials", "true"))'))))

    if 'contract' in groups:
        checks = [('content type is JSON', 'pm.response.headers.has("Content-Type") && /json/i.test(pm.response.headers.get("Content-Type") || "")')]
        sample = spec.get('sample_response')
        script = _script('', *checks)
        if isinstance(sample, (dict, list)):
            script += ('\npm.test("response matches the contract (fields, types, no unexpected or missing fields)", function () {\n'
                       f'    pm.response.to.have.jsonSchema({json.dumps(infer_schema(sample))});\n}});')
        out.append(_base(spec, f'{base_name} · contract', 'contract',
                         'The request as imported; the response shape is checked against the contract.', script=script))

    if 'sensitive' in groups:
        cond = ' && '.join(f'!t.includes({_js(w)})' for w in LEAK_WORDS)
        out.append(_base(spec, f'{base_name} · sensitive data exposure', 'sensitive',
                         'The request as imported; the response must not leak secrets or stack traces.',
                         script=_script('', ('response does not leak secrets or stack traces',
                                             f'(function(t){{ return {cond}; }})(pm.response.text().toLowerCase())'))))
    return out


def summary(scenarios):
    """Count per group, for the preview."""
    counts = {}
    for s in scenarios:
        counts[s['kind']] = counts.get(s['kind'], 0) + 1
    return counts
