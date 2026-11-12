"""LOV (list of values): named generators used as {{name}} in a request.

Every send fills the token with a fresh value: a random name or email, a number or text of a given length,
today's date plus an offset, a counter, a pick from a list... Each LOV has a kind, kind options and an
optional prefix / suffix. `generate(lov)` returns the value; `example(kind, options)` previews one.
"""
import calendar
import random
import re
import string
import time
import uuid
from datetime import datetime, timedelta

FIRST_NAMES = ['Ahmed', 'Ali', 'Ayesha', 'Bilal', 'Danish', 'Fatima', 'Faraz', 'Hamza', 'Hina', 'Imran', 'Iqra',
               'Kamran', 'Khadija', 'Maryam', 'Nadia', 'Omar', 'Rabia', 'Saad', 'Sana', 'Sara', 'Tariq', 'Usman',
               'Zain', 'Zara', 'Adam', 'Daniel', 'Emma', 'Ethan', 'Hannah', 'Jacob', 'Liam', 'Maya', 'Noah', 'Olivia',
               'Sophia', 'William', 'Aisha', 'Hassan', 'Mustafa', 'Yusuf']
LAST_NAMES = ['Khan', 'Ahmed', 'Baig', 'Malik', 'Raza', 'Sheikh', 'Siddiqui', 'Qureshi', 'Butt', 'Chaudhry', 'Mirza',
              'Hussain', 'Shah', 'Farooq', 'Akhtar', 'Abbasi', 'Rizvi', 'Javed', 'Iqbal', 'Hashmi', 'Smith', 'Brown',
              'Taylor', 'Wilson', 'Davies', 'Evans', 'Thomas', 'Roberts', 'Walker', 'Wright', 'Patel', 'Lee']
COMPANY_WORDS = ['Global', 'Prime', 'Blue', 'Silver', 'Apex', 'Nova', 'Summit', 'Crescent', 'United', 'Pioneer',
                 'Eastern', 'Metro', 'Royal', 'Alpha', 'Horizon', 'Pak', 'Star', 'Atlas']
COMPANY_TYPES = ['Logistics', 'Cargo', 'Traders', 'Enterprises', 'Industries', 'Solutions', 'Freight', 'Exports',
                 'Textiles', 'Shipping', 'Technologies', 'Holdings']
LOREM = ('lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore '
         'magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo '
         'consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur').split()
CITIES = ['Karachi', 'Lahore', 'Islamabad', 'Rawalpindi', 'Faisalabad', 'Multan', 'Peshawar', 'Quetta', 'Sialkot',
          'Hyderabad', 'Gujranwala', 'Dubai', 'London', 'Istanbul', 'Singapore']
COUNTRIES = ['Pakistan', 'United Arab Emirates', 'United Kingdom', 'Turkey', 'Singapore', 'Saudi Arabia', 'China',
             'Germany', 'United States', 'Malaysia']
CHARSETS = {
    'letters': string.ascii_letters, 'lower': string.ascii_lowercase, 'upper': string.ascii_uppercase,
    'digits': string.digits, 'alnum': string.ascii_letters + string.digits,
    'alnum_upper': string.ascii_uppercase + string.digits, 'alnum_lower': string.ascii_lowercase + string.digits,
    'hex': '0123456789abcdef',
}

# kind -> (label, [(option, type, default, help)]) ; types: number, text, select:a|b|c, textarea
KINDS = {
    'name': ('Random name', [('part', 'select:full|first|last', 'full', 'Which part of the name')]),
    'text': ('Random text', [('min_len', 'number', 5, 'Minimum length'), ('max_len', 'number', 10, 'Maximum length'),
                             ('charset', 'select:letters|lower|upper|alnum|alnum_upper|alnum_lower|digits|hex', 'letters',
                              'Characters to use')]),
    'number': ('Random number', [('min', 'number', 1, 'Smallest value'), ('max', 'number', 1000, 'Largest value'),
                                 ('decimals', 'number', 0, 'Decimal places (0 = whole number)'),
                                 ('pad', 'number', 0, 'Zero-pad to this many digits (0 = no padding)')]),
    'email': ('Random email', [('length', 'number', 8, 'Letters before the @'),
                               ('domain', 'text', 'example.com', 'Domain after the @')]),
    'phone': ('Phone number', [('pattern', 'text', '03#########', 'Every # becomes a random digit')]),
    'date': ('Date / time', [('offset', 'number', 0, 'Add this many units (negative = past)'),
                             ('unit', 'select:days|hours|minutes|months|years', 'days', 'Unit of the offset'),
                             ('format', 'text', '%Y-%m-%d', 'Python strftime format, e.g. %Y-%m-%d or %d/%m/%Y %H:%M')]),
    'timestamp': ('Unix timestamp', [('offset', 'number', 0, 'Add this many units (negative = past)'),
                                     ('unit', 'select:days|hours|minutes|seconds', 'days', 'Unit of the offset'),
                                     ('precision', 'select:seconds|milliseconds', 'seconds', 'Seconds or milliseconds')]),
    'uuid': ('UUID', []),
    'sequence': ('Sequence (counter)', [('start', 'number', 1, 'First value'), ('step', 'number', 1, 'Added on every send'),
                                        ('pad', 'number', 0, 'Zero-pad to this many digits (0 = no padding)')]),
    'list': ('Pick from a list', [('values', 'textarea', '', 'One value per line'),
                                  ('mode', 'select:random|sequential', 'random', 'Random pick, or next value on every send')]),
    'boolean': ('True / false', [('style', 'select:true/false|1/0|yes/no|Y/N', 'true/false', 'How it is written')]),
    'lorem': ('Lorem text', [('min_words', 'number', 3, 'Fewest words'), ('max_words', 'number', 8, 'Most words')]),
    'company': ('Company name', []),
    'city': ('City', []),
    'country': ('Country', []),
}
COMMON_OPTIONS = [('prefix', 'text', '', 'Added before the value'), ('suffix', 'text', '', 'Added after the value')]
NAME_RE = re.compile(r'^[\w.\-]{1,200}$')


def clean_options(kind, options):
    """Only the options the kind knows, typed, with defaults filled in."""
    options = options if isinstance(options, dict) else {}
    out = {}
    for key, typ, default, _ in KINDS.get(kind, ('', []))[1] + COMMON_OPTIONS:
        raw = options.get(key, default)
        if typ == 'number':
            try:
                num = float(raw)
                out[key] = int(num) if num == int(num) else num
            except (TypeError, ValueError):
                out[key] = default
        elif typ.startswith('select:'):
            choices = typ[7:].split('|')
            out[key] = raw if raw in choices else default
        else:
            out[key] = str(raw if raw is not None else '')[:2000]
    return out


def validate(kind, options):
    """An error message for impossible options, or ''."""
    if kind not in KINDS:
        return 'Unknown LOV type.'
    o = clean_options(kind, options)
    if kind == 'text' and not (1 <= o['min_len'] <= o['max_len'] <= 5000):
        return 'Text: min length must be between 1 and the max length (up to 5000).'
    if kind == 'number' and o['min'] > o['max']:
        return 'Number: the smallest value is bigger than the largest.'
    if kind == 'lorem' and not (1 <= o['min_words'] <= o['max_words'] <= 1000):
        return 'Lorem: fewest words must be between 1 and the most words (up to 1000).'
    if kind == 'list' and not [v for v in o['values'].splitlines() if v.strip()]:
        return 'List: add at least one value (one per line).'
    if kind == 'date':
        try:
            datetime.now().strftime(o['format'] or '%Y-%m-%d')
        except (ValueError, TypeError):
            return 'Date: the format is not a valid strftime pattern.'
    if kind == 'phone' and '#' not in o['pattern']:
        return 'Phone: the pattern needs at least one # for a random digit.'
    return ''


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def shifted_now(offset, unit):
    now = datetime.now()
    if unit == 'months':
        return _add_months(now, int(offset))
    if unit == 'years':
        return _add_months(now, int(offset) * 12)
    return now + timedelta(**{unit: offset})


def _core(kind, o, state):
    """The value before prefix / suffix. `state` is the LOV's persistent dict (counters)."""
    if kind == 'name':
        first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        return {'first': first, 'last': last}.get(o['part'], f'{first} {last}')
    if kind == 'text':
        return ''.join(random.choices(CHARSETS[o['charset']], k=random.randint(o['min_len'], o['max_len'])))
    if kind == 'number':
        if o['decimals'] > 0:
            return f"{random.uniform(o['min'], o['max']):.{min(int(o['decimals']), 10)}f}"
        value = str(random.randint(int(o['min']), int(o['max'])))
        return value.zfill(int(o['pad'])) if o['pad'] else value
    if kind == 'email':
        local = ''.join(random.choices(string.ascii_lowercase + string.digits, k=max(1, min(int(o['length']), 64))))
        return f"{local}@{o['domain'] or 'example.com'}"
    if kind == 'phone':
        return re.sub('#', lambda _: random.choice(string.digits), o['pattern'])
    if kind == 'date':
        return shifted_now(o['offset'], o['unit']).strftime(o['format'] or '%Y-%m-%d')
    if kind == 'timestamp':
        seconds = time.time() + timedelta(**{o['unit']: o['offset']}).total_seconds()
        return str(int(seconds * 1000)) if o['precision'] == 'milliseconds' else str(int(seconds))
    if kind == 'uuid':
        return str(uuid.uuid4())
    if kind == 'sequence':
        current = state.get('next', o['start'])
        state['next'] = current + o['step']
        value = str(current)
        return value.zfill(int(o['pad'])) if o['pad'] else value
    if kind == 'list':
        values = [v.strip() for v in o['values'].splitlines() if v.strip()]
        if o['mode'] == 'sequential':
            pos = int(state.get('pos', 0)) % len(values)
            state['pos'] = pos + 1
            return values[pos]
        return random.choice(values)
    if kind == 'boolean':
        yes, no = o['style'].split('/')
        return random.choice([yes, no])
    if kind == 'lorem':
        return ' '.join(random.choices(LOREM, k=random.randint(o['min_words'], o['max_words'])))
    if kind == 'company':
        return f'{random.choice(COMPANY_WORDS)} {random.choice(COMPANY_TYPES)}'
    if kind == 'city':
        return random.choice(CITIES)
    if kind == 'country':
        return random.choice(COUNTRIES)
    return ''


def make(kind, options, state=None):
    o = clean_options(kind, options)
    return f"{o['prefix']}{_core(kind, o, state if state is not None else {})}{o['suffix']}"


def example(kind, options, count=3):
    """Preview values; counters are not advanced (a throw-away state is used)."""
    state = {}
    try:
        return [make(kind, options, state) for _ in range(count)]
    except Exception as exc:          # a bad format etc.: show the problem instead of a 500
        return [f'error: {exc}']


def generate(lov):
    """A fresh value for a stored LOV; counters (sequence, sequential list) are saved on the LOV."""
    state = dict(lov.state or {})
    value = make(lov.kind, lov.options, state)
    if state != (lov.state or {}):
        lov.state = state
        lov.save(update_fields=['state'])
    return value


class Registry:
    """The LOVs a request can use (all projects + its project), each generated once per send."""

    def __init__(self, lovs=()):
        self.lovs = {l.name: l for l in lovs}
        self.values = {}

    @classmethod
    def for_project(cls, project=None):
        from .models import ApiLov
        from django.db.models import Q
        qs = ApiLov.objects.filter(enabled=True)
        qs = qs.filter(Q(project=None) | Q(project=project)) if project is not None else qs.filter(project=None)
        return cls(qs)

    def __contains__(self, name):
        return name in self.lovs

    def value(self, name):
        if name not in self.values:
            self.values[name] = generate(self.lovs[name])
        return self.values[name]

    def reset(self):
        self.values = {}

    def used(self):
        return dict(self.values)
