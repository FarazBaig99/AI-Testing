"""Autofill: look at a request's body fields, work out what kind of value each parameter wants, and
generate a realistic random value for it.

The type of a field is decided, in order, by:
  1. a type the user confirmed before (the learned store, ApiParamType) - passed in as `learned`,
  2. the parameter's name (iataCode -> code, email -> email, isActive -> boolean, ...),
  3. the JSON type of the value already in the body (25 -> integer, true -> boolean, "x" -> string).

Random values are produced with the same generators the LOV library uses, so names, emails, dates,
phone numbers and UUIDs all look real.
"""
import re

from . import lov

# the types a field can be, with a friendly label for the picker (order = how the dropdown lists them)
TYPES = [
    ('string', 'Text'),
    ('integer', 'Integer'),
    ('number', 'Decimal number'),
    ('boolean', 'True / false'),
    ('email', 'Email'),
    ('name', 'Person name'),
    ('firstName', 'First name'),
    ('lastName', 'Last name'),
    ('phone', 'Phone number'),
    ('date', 'Date'),
    ('datetime', 'Date & time'),
    ('uuid', 'UUID'),
    ('code', 'Short code'),
    ('city', 'City'),
    ('country', 'Country'),
    ('company', 'Company name'),
    ('text', 'Sentence'),
    ('reference', 'From an API'),
]
TYPE_KEYS = {t for t, _ in TYPES}

# type -> (LOV produce kind, options) used to generate a value
_GEN = {
    'string': ('generated', {'kind': 'text', 'min_len': 5, 'max_len': 9, 'charset': 'letters'}),
    'integer': ('generated', {'kind': 'number', 'min': 1, 'max': 10000, 'decimals': 0}),
    'number': ('generated', {'kind': 'number', 'min': 1, 'max': 100000, 'decimals': 2}),
    'boolean': ('generated', {'kind': 'boolean', 'style': 'true/false'}),
    'email': ('generated', {'kind': 'email', 'length': 8, 'domain': 'example.com'}),
    'name': ('generated', {'kind': 'name', 'part': 'full'}),
    'firstName': ('generated', {'kind': 'name', 'part': 'first'}),
    'lastName': ('generated', {'kind': 'name', 'part': 'last'}),
    'phone': ('generated', {'kind': 'phone', 'pattern': '03#########'}),
    'date': ('date', {'base': 'current', 'format': 'yyyy-MM-dd', 'time': 'current', 'timezone': 'auto'}),
    'datetime': ('date', {'base': 'current', 'format': "yyyy-MM-dd'T'HH:mm:ss", 'time': 'current', 'timezone': 'auto'}),
    'uuid': ('generated', {'kind': 'uuid'}),
    'code': ('generated', {'kind': 'text', 'min_len': 3, 'max_len': 6, 'charset': 'alnum_upper'}),
    'city': ('generated', {'kind': 'city'}),
    'country': ('generated', {'kind': 'country'}),
    'company': ('generated', {'kind': 'company'}),
    'text': ('generated', {'kind': 'lorem', 'min_words': 3, 'max_words': 8}),
}

# types whose value should stay a JSON number / boolean when written back into a JSON body
NUMERIC_TYPES = {'integer', 'number'}
BOOLEAN_TYPES = {'boolean'}

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?')


def _norm(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def _name_type(name):
    """A specialised type guessed from the parameter name, or None."""
    n = _norm(name)
    if not n:
        return None
    if 'email' in n:
        return 'email'
    if any(k in n for k in ('phone', 'mobile', 'msisdn', 'cellno', 'contactno')):
        return 'phone'
    if 'uuid' in n or 'guid' in n:
        return 'uuid'
    if 'timestamp' in n or 'datetime' in n:
        return 'datetime'
    if ('date' in n or n.endswith('dob') or n == 'dob' or n.endswith('at')
            or 'expiry' in n or 'validfrom' in n or 'validto' in n or 'issued' in n):
        return 'date'
    if 'firstname' in n or n in ('fname', 'givenname'):
        return 'firstName'
    if 'lastname' in n or 'surname' in n or n == 'lname':
        return 'lastName'
    if ('legalname' in n or 'companyname' in n or 'businessname' in n or n == 'company'
            or 'organization' in n or 'organisation' in n or 'orgname' in n):
        return 'company'
    if 'country' in n:
        return 'country'
    if 'city' in n:
        return 'city'
    if 'name' in n:
        return 'name'
    if n.startswith('is') or n.startswith('has') or 'enabled' in n or 'active' in n or n.endswith('flag'):
        return 'boolean'
    if any(k in n for k in ('amount', 'price', 'weight', 'balance', 'rate', 'latitude', 'longitude',
                            'percentage', 'tax', 'fee')):
        return 'number'
    if any(k in n for k in ('count', 'qty', 'quantity', 'age', 'year', 'number', 'num', 'length',
                            'size', 'total', 'seq', 'id')):
        return 'integer'
    if 'code' in n:
        return 'code'
    return None


def _value_type(value):
    """A type guessed from the JSON value already in the body, or None."""
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str) and value.strip():
        v = value.strip()
        if _EMAIL_RE.match(v):
            return 'email'
        if _UUID_RE.match(v):
            return 'uuid'
        if _DATE_RE.match(v):
            return 'datetime' if ('T' in v or ' ' in v) else 'date'
    return None


def detect_type(name, value, learned=None):
    """Decide a field's type: a learned type wins, then the name, then the value, else plain text."""
    if learned and learned in TYPE_KEYS:
        return learned
    if isinstance(value, bool):          # a true/false value is a strong signal, override name guesses
        return 'boolean'
    by_name = _name_type(name)
    if by_name:
        return by_name
    by_value = _value_type(value)
    if by_value:
        return by_value
    return 'string'


def generate_value(data_type, intel=None):
    """A fresh random value for a type, as a Python value (str, int, float or bool). When the brain has learned
    a length range for this field (`intel`), the value is kept within it - so a 7-char `ntn` stays 7 chars."""
    intel = intel or {}
    produces, options = _GEN.get(data_type, _GEN['string'])
    options = dict(options)
    try:
        mx = int(intel['max_len']) if intel.get('max_len') else None
        mn = int(intel['min_len']) if intel.get('min_len') else None
    except (TypeError, ValueError):
        mx = mn = None
    if data_type == 'integer' and mx:
        digits = max(1, mn or mx)
        options = {'kind': 'number', 'min': 10 ** (digits - 1) if digits > 1 else 0, 'max': 10 ** mx - 1, 'decimals': 0}
    elif data_type in ('string', 'text', 'code') and (mx or intel.get('charset') in ('digits', 'alnum', 'alpha')):
        options['kind'] = options.get('kind', 'text')
        if mx:
            options['min_len'], options['max_len'] = (mn or mx), mx
        cs = intel.get('charset')
        if cs in ('digits', 'alnum', 'alpha'):
            options['charset'] = {'digits': 'digits', 'alnum': 'alnum', 'alpha': 'letters'}[cs]
    raw = lov.make(produces, options, [])
    if data_type in NUMERIC_TYPES:
        try:
            num = float(raw)
            return int(num) if data_type == 'integer' or num == int(num) else num
        except (TypeError, ValueError):
            return raw
    if data_type in BOOLEAN_TYPES:
        return str(raw).strip().lower() in ('true', '1', 'yes', 'y')
    return raw
