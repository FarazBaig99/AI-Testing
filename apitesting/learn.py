"""The API Testing "understanding brain": a rule-based learner that watches real sends and remembers what it
sees, so Autofill gets smarter over time.

From each send it learns two things:
  * field intelligence  - for every field in a known-good (2xx) request body it records the value's length
                          range, character set and a few sample values, and the entity a `...Id` field refers to.
  * entity pools        - when an endpoint returns a list of objects with an `id`, it remembers those ids
                          (with a human label) under the resource's name, so a `cityId` field can later be
                          filled with a real id handed out by the cities API.

Everything is heuristic and offline (no model call); it just accumulates evidence row by row.
"""
import json
import re

from . import autofill
from .models import ApiEntityPool, ApiParamType

MAX_SAMPLES = 8
MAX_POOL = 60
LIST_KEYS = ('data', 'items', 'results', 'content', 'records', 'list', 'rows', 'payload')
LABEL_KEYS = ('name', 'label', 'title', 'displayName', 'code', 'description', 'value')
# path segments that are not resource names
SKIP_SEGMENTS = {'api', 'v1', 'v2', 'v3', 'rest', 'metadata', 'ums', 'backoffice-server', 'backoffice-client',
                 'search', 'list', 'all', 'lookup', 'lookups', 'master', 'public', 'internal'}


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


def singularize(word):
    w = (word or '').lower()
    if w.endswith('ies') and len(w) > 3:
        return w[:-3] + 'y'
    if w.endswith(('ses', 'xes', 'zes', 'ches', 'shes')):
        return w[:-2]
    if w.endswith('s') and not w.endswith('ss') and len(w) > 1:
        return w[:-1]
    return w


def entity_from_url(url):
    """The resource an endpoint is about, from its path: /api/v1/metadata/cities -> 'city'."""
    path = re.sub(r'[?#].*$', '', url or '')
    segs = [s for s in path.split('/') if re.search(r'[a-zA-Z]', s) and '{' not in s and '{{' not in s]
    segs = [s for s in segs if s.lower() not in SKIP_SEGMENTS and not re.fullmatch(r'v?\d+', s)]
    return singularize(segs[-1]) if segs else ''


def entity_of_field(name):
    """The entity a `...Id` field points at: cityId -> 'city', incorporationCountryId -> 'incorporationcountry'."""
    m = re.match(r'^(.*?)_?[iI][dD]$', str(name or ''))
    base = _norm(m.group(1)) if m and m.group(1) else ''
    return singularize(base) if base else ''


def pool_for_field(name, pools):
    """Pick the entity pool a field should draw from. `pools` is {entity: ApiEntityPool}. Returns (entity, pool)
    or None. Matches exactly, then by the longest entity that is contained in (or contains) the field's base."""
    base = entity_of_field(name)
    if not base:
        return None
    if base in pools:
        return base, pools[base]
    best = None
    for ent in pools:
        if len(ent) >= 3 and (ent in base or base in ent) and (best is None or len(ent) > len(best)):
            best = ent
    return (best, pools[best]) if best else None


def _charset(s):
    if re.fullmatch(r'\d+', s):
        return 'digits'
    if re.fullmatch(r'[A-Za-z]+', s):
        return 'alpha'
    if re.fullmatch(r'[A-Za-z0-9]+', s):
        return 'alnum'
    return 'other'


def _merge_charset(old, s):
    cur = _charset(s)
    if not old or old == cur:
        return cur
    order = ['digits', 'alpha', 'alnum', 'other']            # widen to the looser set
    return order[max(order.index(old), order.index(cur))] if old in order and cur in order else 'other'


def _leaves(node, key=None, out=None):
    """Every scalar (name, value) under a parsed JSON value; the name is the key it sits under."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            _leaves(v, k, out)
    elif isinstance(node, list):
        for v in node:
            _leaves(v, key, out)
    elif key is not None:
        out.append((key, node))
    return out


def _response_list(body):
    """A list of records from a response body, through the common pagination wrappers."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in LIST_KEYS:
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in LIST_KEYS:
                    if isinstance(v.get(k2), list):
                        return v[k2]
    return None


def _label_of(obj):
    for k in LABEL_KEYS:
        v = obj.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v)[:80]
    return ''


def _id_of(obj):
    for k in ('id', 'Id', 'ID', 'uuid', 'guid', 'key', 'code'):
        if k in obj and isinstance(obj[k], (str, int)):
            return obj[k]
    return None


def _type_for(name, value, charset):
    """The data type to learn: the usual name/value guess, sharpened by the observed character set."""
    t = autofill.detect_type(name, value)
    if t == 'string':
        if charset == 'digits':
            return 'integer'
        if charset == 'alnum':
            return 'code'
    return t


def _learn_field(name, value):
    if value is None or isinstance(value, (dict, list, bool)):
        return
    s = str(value)
    if not s or '{{' in s:
        return
    charset = _charset(s)
    pt, _ = ApiParamType.objects.get_or_create(name=name[:200], defaults={'data_type': _type_for(name, value, charset)})
    intel = dict(pt.intel or {})
    L = len(s)
    intel['count'] = int(intel.get('count', 0)) + 1
    intel['min_len'] = min(int(intel.get('min_len', L)), L)
    intel['max_len'] = max(int(intel.get('max_len', L)), L)
    intel['charset'] = _merge_charset(intel.get('charset'), s)
    samples = [x for x in (intel.get('samples') or []) if x != s]
    samples.insert(0, s)
    intel['samples'] = samples[:MAX_SAMPLES]
    ent = entity_of_field(name)
    if ent:
        intel['entity'] = ent
    pt.intel = intel
    fields = ['intel']
    if pt.data_type in ('', 'string'):                       # sharpen a still-generic type, never override a specific one
        better = _type_for(name, value, intel['charset'])
        if better != pt.data_type:
            pt.data_type, fields = better, ['intel', 'data_type']
    pt.save(update_fields=fields)


def _learn_pool(url, body):
    entity = entity_from_url(url)
    rows = _response_list(body)
    if not entity or not rows:
        return
    pool = []
    for obj in rows[:MAX_POOL]:
        if isinstance(obj, dict):
            i = _id_of(obj)
            if i is not None:
                pool.append({'id': i, 'label': _label_of(obj)})
    if pool:
        ApiEntityPool.objects.update_or_create(
            entity=entity[:120], defaults={'source_url': (url or '')[:2000], 'values': pool})


def observe(spec, result):
    """Learn from one send. Never raises: a send must not fail because learning did."""
    try:
        spec = spec or {}
        result = result or {}
        ok = bool(result.get('ok')) and (result.get('status') or 0) < 400
        if ok and spec.get('body_mode') == 'raw':
            try:
                body = json.loads(spec.get('body') or 'null')
            except (ValueError, TypeError):
                body = None
            if isinstance(body, (dict, list)):
                for name, value in _leaves(body):
                    _learn_field(name, value)
        _learn_pool(result.get('url') or spec.get('url'), result.get('body') or '')
    except Exception:
        pass


# ---------- training the brain directly: from an uploaded document, or manual entries ----------

FREE_TYPE = {
    'int': 'integer', 'integer': 'integer', 'long': 'integer', 'bigint': 'integer', 'number': 'number',
    'decimal': 'number', 'float': 'number', 'double': 'number', 'bool': 'boolean', 'boolean': 'boolean',
    'string': 'string', 'text': 'string', 'varchar': 'string', 'char': 'string', 'str': 'string',
    'email': 'email', 'date': 'date', 'datetime': 'datetime', 'timestamp': 'datetime', 'uuid': 'uuid',
    'guid': 'uuid', 'phone': 'phone', 'city': 'city', 'country': 'country', 'name': 'name', 'code': 'code',
}


def _map_free_type(word):
    w = re.sub(r'[^a-z0-9]', '', str(word or '').lower())
    return FREE_TYPE.get(w, 'string')


def _type_from_schema(prop):
    t, fmt = prop.get('type'), (prop.get('format') or '').lower()
    if isinstance(t, list):
        t = next((x for x in t if x != 'null'), 'string')
    if t == 'string':
        return {'email': 'email', 'date': 'date', 'date-time': 'datetime', 'datetime': 'datetime',
                'uuid': 'uuid', 'guid': 'uuid'}.get(fmt, 'string')
    return {'integer': 'integer', 'number': 'number', 'boolean': 'boolean'}.get(t, 'string')


def _props_to_params(props, required=()):
    out = {}
    for name, prop in (props or {}).items():
        if not isinstance(prop, dict):
            continue
        if prop.get('type') == 'object' and isinstance(prop.get('properties'), dict):
            out.update(_props_to_params(prop['properties'], prop.get('required', [])))
            continue
        if prop.get('type') == 'array':
            continue
        info = {'data_type': _type_from_schema(prop)}
        if str(prop.get('maxLength', '')).isdigit():
            info['max_len'] = int(prop['maxLength'])
        if str(prop.get('minLength', '')).isdigit():
            info['min_len'] = int(prop['minLength'])
        if isinstance(prop.get('enum'), list) and prop['enum']:
            info['samples'] = [str(x) for x in prop['enum'][:MAX_SAMPLES]]
        if re.search(r'\d|\[0-9\]|digit', prop.get('pattern') or ''):
            info['charset'] = 'digits'
        if name in (required or []):
            info['required'] = True
        out[str(name)] = info
    return out


def _find_schemas(obj, acc):
    """Every JSON-Schema / OpenAPI schema object (one with a properties map) anywhere in the document."""
    if isinstance(obj, dict):
        if isinstance(obj.get('properties'), dict):
            acc.append((obj['properties'], obj.get('required', [])))
        for v in obj.values():
            _find_schemas(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _find_schemas(v, acc)


def _csv_params(text):
    import csv
    import io
    out = {}
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or '').strip().lower(): (v or '').strip() for k, v in raw.items()}
        name = row.get('name') or row.get('parameter') or row.get('field') or row.get('key')
        if not name:
            continue
        info = {}
        dt = row.get('type') or row.get('data_type') or row.get('datatype')
        if dt:
            info['data_type'] = _map_free_type(dt)
        for k in ('max_length', 'maxlength', 'max_len', 'maxlen', 'max'):
            if row.get(k, '').replace('.', '', 1).isdigit():
                info['max_len'] = int(float(row[k]))
                break
        for k in ('min_length', 'minlength', 'min_len', 'minlen', 'min'):
            if row.get(k, '').replace('.', '', 1).isdigit():
                info['min_len'] = int(float(row[k]))
                break
        if row.get('required', '').lower() in ('1', 'true', 'yes', 'y'):
            info['required'] = True
        sample_raw = row.get('samples') or row.get('example') or row.get('examples') or row.get('values')
        if sample_raw:
            info['samples'] = [x.strip() for x in re.split(r'[|;]', sample_raw) if x.strip()][:MAX_SAMPLES]
        out[name] = info
    return out


def extract_params(text, filename=''):
    """Pull a {name: {data_type, max_len, min_len, charset, samples, required}} map out of an uploaded
    document: an OpenAPI / Swagger spec, a JSON Schema, a plain example JSON body, or a CSV."""
    text = (text or '').strip()
    if not text:
        return {}
    first = text.splitlines()[0]
    if filename.lower().endswith('.csv') or (',' in first and not text.lstrip().startswith(('{', '['))):
        return _csv_params(text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    schemas = []
    _find_schemas(data, schemas)
    if schemas:
        params = {}
        for props, req in schemas:
            params.update(_props_to_params(props, req))
        return params
    if isinstance(data, (dict, list)):
        params = {}
        for name, value in _leaves(data):
            info = {'data_type': autofill.detect_type(name, value)}
            s = str(value)
            if s and '{{' not in s and not isinstance(value, bool):
                info.update(max_len=len(s), min_len=len(s), charset=_charset(s), samples=[s])
            params[str(name)] = info
        return params
    return {}


def apply_params(params, source='manual'):
    """Write trained parameter info into the brain (ApiParamType + intel). Merges with what is already known."""
    n = 0
    for name, info in (params or {}).items():
        name = str(name).strip()[:200]
        if not name or not isinstance(info, dict):
            continue
        pt, _ = ApiParamType.objects.get_or_create(name=name, defaults={'data_type': info.get('data_type') or 'string'})
        if info.get('data_type'):
            pt.data_type = info['data_type']
        intel = dict(pt.intel or {})
        for k in ('min_len', 'max_len', 'charset'):
            if info.get(k) not in (None, ''):
                intel[k] = info[k]
        if info.get('samples'):
            keep = [x for x in (intel.get('samples') or []) if x not in info['samples']]
            intel['samples'] = (list(info['samples']) + keep)[:MAX_SAMPLES]
        if info.get('required') is not None:
            intel['required'] = bool(info['required'])
        ent = entity_of_field(name)
        if ent:
            intel['entity'] = ent
        intel['source'] = source
        pt.intel = intel
        pt.save()
        n += 1
    return n


def known_params(names):
    """What the brain currently knows about each of these parameter names (for the training panel)."""
    rows = {pt.name: pt for pt in ApiParamType.objects.filter(name__in=list(names))}
    out = []
    for name in names:
        pt = rows.get(name)
        intel = (pt.intel if pt else {}) or {}
        out.append({'name': name, 'data_type': pt.data_type if pt else '',
                    'min_len': intel.get('min_len'), 'max_len': intel.get('max_len'),
                    'charset': intel.get('charset', ''), 'required': bool(intel.get('required')),
                    'samples': intel.get('samples') or [], 'entity': intel.get('entity', ''),
                    'known': pt is not None})
    return out
