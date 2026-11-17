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
