"""LOV (list of values) categories, ATAS-style: {{name}} (or an alias) is filled when a request is sent.

A category produces one of four things:
  defined   - one of the values listed in the category (random, or the next one each time)
  unique    - a fresh value that has never been used before: an Integer / Alphanumeric / Alphabets fragment
              of a given length, alone or as prefix / suffix of one of the listed base values; every value
              handed out is remembered per project (ApiLovUsedValue) so it is never produced twice
  date      - the current date, or a date days / weeks / months ahead or back, written in a date format
              from the formats library (Java-style patterns like yyyy-MM-dd'T'HH:mm:ss), with the time of
              day and timezone chosen
  generated - a built-in generator: random name, text, number, email, phone, UUID, counter, true/false...
"""
import calendar
import random
import re
import string
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, available_timezones

NAME_RE = re.compile(r'^[\w.\-]{1,200}$')

PRODUCES = [
    ('defined', 'Defined values', 'One of the values listed in this category.'),
    ('unique', 'Unique value', 'A fresh value that has never been used, generated on every send.'),
    ('date', 'Date', 'The current date, or a date days, weeks or months away, written in the format you choose.'),
    ('generated', 'Generated value', 'A random name, text, number, email, phone, UUID, counter...'),
]

# ---------- date formats (Java / .NET style patterns, like ATAS) ----------

BUILTIN_DATE_FORMATS = [
    ('ACS date-time', "yyyy-MM-dd'T'HH:mm:ss"),
    ('ISO date', 'yyyy-MM-dd'),
    ('ISO 8601 date-time (Z for UTC)', "yyyy-MM-dd'T'HH:mm:ss.fffK"),
    ('ISO date-time with offset', "yyyy-MM-dd'T'HH:mm:sszzz"),
    ('Date-time with a space', 'yyyy-MM-dd HH:mm:ss'),
    ('Day/Month/Year', 'dd/MM/yyyy'),
    ('Month/Day/Year', 'MM/dd/yyyy'),
    ('Day-Mon-Year', 'dd-MMM-yyyy'),
    ('Compact date', 'yyyyMMdd'),
    ('Epoch seconds', 'X'),
    ('Epoch milliseconds', 'x'),
]
_TOKEN_RE = re.compile(r"'([^']*)'|(y+|M+|d+|H+|h+|m+|s+|f+|S+|a|E+|K|z+|X|x|Z)|(.)", re.S)


def _offset(dt):
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = '+' if total >= 0 else '-'
    total = abs(total)
    return sign, total // 3600, (total % 3600) // 60


def format_pattern(dt, pattern):
    """Write an aware datetime in a Java-style pattern: yyyy MM dd HH mm ss fff a K zzz X x MMM EEE and
    'quoted literals'. Unknown letters are written as they are."""
    out = []
    for quoted, token, other in _TOKEN_RE.findall(pattern or ''):
        if other:
            out.append(other)
            continue
        if quoted is not None and token == '' and quoted != '':
            out.append(quoted)
            continue
        if token == '' and quoted == '':
            out.append("'")
            continue
        c, n = token[0], len(token)
        if c == 'y':
            out.append(f'{dt.year:04d}' if n != 2 else f'{dt.year % 100:02d}')
        elif c == 'M':
            out.append(dt.strftime('%B') if n >= 4 else dt.strftime('%b') if n == 3 else f'{dt.month:0{n}d}')
        elif c == 'd':
            out.append(f'{dt.day:0{n}d}')
        elif c == 'H':
            out.append(f'{dt.hour:0{n}d}')
        elif c == 'h':
            out.append(f'{(dt.hour % 12) or 12:0{n}d}')
        elif c == 'm':
            out.append(f'{dt.minute:0{n}d}')
        elif c == 's':
            out.append(f'{dt.second:0{n}d}')
        elif c in 'fS':
            out.append(f'{dt.microsecond:06d}'[:n])
        elif c == 'a':
            out.append('PM' if dt.hour >= 12 else 'AM')
        elif c == 'E':
            out.append(dt.strftime('%A') if n >= 4 else dt.strftime('%a'))
        elif c == 'K':
            sign, h, m = _offset(dt)
            out.append('Z' if (h, m) == (0, 0) else f'{sign}{h:02d}:{m:02d}')
        elif c == 'z':
            sign, h, m = _offset(dt)
            out.append(f'{sign}{h:02d}:{m:02d}')
        elif c == 'Z':
            sign, h, m = _offset(dt)
            out.append(f'{sign}{h:02d}{m:02d}')
        elif c == 'X':
            out.append(str(int(dt.timestamp())))
        elif c == 'x':
            out.append(str(int(dt.timestamp() * 1000)))
        else:
            out.append(token)
    return ''.join(out)


def _add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


def local_zone():
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:
        return dt_timezone.utc


def zone_for(name):
    if not name or name == 'auto':
        return local_zone()
    try:
        return ZoneInfo(name)
    except Exception:
        return local_zone()


def timezone_choices():
    common = ['Asia/Karachi', 'UTC', 'Asia/Dubai', 'Asia/Riyadh', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Shanghai',
              'Europe/London', 'Europe/Berlin', 'Europe/Istanbul', 'America/New_York', 'America/Chicago',
              'America/Los_Angeles', 'Australia/Sydney']
    rest = sorted(z for z in available_timezones() if z not in common and '/' in z and not z.startswith('Etc/'))
    return common + rest


def date_value(o):
    """The date a Date category writes right now (o = cleaned options)."""
    now = datetime.now(zone_for(o['timezone']))
    amount = int(o['amount']) if o['base'] != 'current' else 0
    if o['base'] == 'past':
        amount = -amount
    if o['unit'] == 'months':
        dt = _add_months(now, amount)
    elif o['unit'] == 'weeks':
        dt = now + timedelta(weeks=amount)
    else:
        dt = now + timedelta(days=amount)
    if o['time'] == 'start':
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif o['time'] == 'end':
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return format_pattern(dt, o['format'] or 'yyyy-MM-dd')


# ---------- generated values ----------

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

# generated kind -> (label, [(option, type, default, help)]) ; types: number, text, select:a|b|c
GENERATED_KINDS = {
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
    'uuid': ('UUID', []),
    'sequence': ('Sequence (counter)', [('start', 'number', 1, 'First value'), ('step', 'number', 1, 'Added on every send'),
                                        ('pad', 'number', 0, 'Zero-pad to this many digits (0 = no padding)')]),
    'boolean': ('True / false', [('style', 'select:true/false|1/0|yes/no|Y/N', 'true/false', 'How it is written')]),
    'lorem': ('Lorem text', [('min_words', 'number', 3, 'Fewest words'), ('max_words', 'number', 8, 'Most words')]),
    'company': ('Company name', []),
    'city': ('City', []),
    'country': ('Country', []),
}

# options of every produce type (the generated kind options come from GENERATED_KINDS)
OPTIONS = {
    'defined': [('mode', 'select:random|sequential', 'random', 'A random value, or the next one on every send')],
    'unique': [('data_type', 'select:integer|alphanumeric|alphabets', 'alphanumeric', 'Characters of the unique fragment'),
               ('position', 'select:prefix|suffix|full_random', 'full_random',
                'Put the fragment before or after one of the listed values, or use the fragment alone'),
               ('case', 'select:upper|lower', 'upper', 'Letter case of the fragment'),
               ('length', 'number', 6, 'Length of the fragment')],
    'date': [('base', 'select:current|future|past', 'current', 'Today, or a date ahead / back'),
             ('amount', 'number', 1, 'How far ahead or back'),
             ('unit', 'select:days|weeks|months', 'days', 'Unit of the distance'),
             ('format', 'text', 'yyyy-MM-dd', 'A pattern from the date formats library'),
             ('time', 'select:current|start|end', 'current', 'Current time, start of day (00:00:00) or end of day (23:59:59)'),
             ('timezone', 'text', 'auto', 'auto = the timezone of this server, or a zone like Asia/Karachi')],
    'generated': [('kind', 'select:' + '|'.join(GENERATED_KINDS), 'name', 'What to generate'),
                  ('prefix', 'text', '', 'Added before the value'), ('suffix', 'text', '', 'Added after the value')],
}


def option_specs(produces, options=None):
    specs = list(OPTIONS.get(produces, []))
    if produces == 'generated':
        kind = (options or {}).get('kind', 'name')
        specs += GENERATED_KINDS.get(kind, GENERATED_KINDS['name'])[1]
    return specs


def clean_options(produces, options):
    """Only the options the produce type knows, typed, with defaults filled in."""
    options = options if isinstance(options, dict) else {}
    out = {}
    for key, typ, default, _ in option_specs(produces, options):
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


def clean_values(values):
    out, seen = [], set()
    for v in values if isinstance(values, list) else []:
        v = str(v).strip()[:500]
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def validate(produces, options, values):
    """An error message for impossible settings, or ''."""
    if produces not in dict((p, l) for p, l, _ in PRODUCES):
        return 'Unknown produce type.'
    o = clean_options(produces, options)
    if produces == 'defined' and not values:
        return 'Defined values: add at least one value.'
    if produces == 'unique':
        if not 1 <= o['length'] <= 100:
            return 'Unique value: the length must be between 1 and 100.'
        if o['position'] != 'full_random' and not values:
            return 'Unique value: add the base values the fragment is put before / after, or choose "Generate complete random value".'
    if produces == 'date':
        if o['base'] != 'current' and o['amount'] < 0:
            return 'Date: the distance cannot be negative; choose Past date instead.'
        if o['timezone'] not in ('auto', '') and o['timezone'] not in available_timezones():
            return 'Date: unknown timezone.'
        if not o['format'].strip():
            return 'Date: choose a format.'
    if produces == 'generated':
        kind = o['kind']
        if kind == 'text' and not (1 <= o['min_len'] <= o['max_len'] <= 5000):
            return 'Text: min length must be between 1 and the max length (up to 5000).'
        if kind == 'number' and o['min'] > o['max']:
            return 'Number: the smallest value is bigger than the largest.'
        if kind == 'lorem' and not (1 <= o['min_words'] <= o['max_words'] <= 1000):
            return 'Lorem: fewest words must be between 1 and the most words (up to 1000).'
        if kind == 'phone' and '#' not in o['pattern']:
            return 'Phone: the pattern needs at least one # for a random digit.'
    return ''


def summary(produces, o):
    """Short description of the configuration, shown on the category card."""
    if produces == 'defined':
        return 'random pick' if o['mode'] == 'random' else 'next value on every send'
    if produces == 'unique':
        pos = {'prefix': 'Prefix', 'suffix': 'Suffix', 'full_random': 'Generate complete random value'}[o['position']]
        return f"{pos}, {o['data_type'].capitalize()}, length {o['length']}"
    if produces == 'date':
        base = {'current': 'Current date', 'future': f"{o['amount']} {o['unit']} ahead", 'past': f"{o['amount']} {o['unit']} back"}[o['base']]
        return f"{base} · {o['format']}" + (' · start of day' if o['time'] == 'start' else ' · end of day' if o['time'] == 'end' else '')
    return GENERATED_KINDS.get(o['kind'], ('?',))[0] + (f" · prefix {o['prefix']}" if o['prefix'] else '') + (f" · suffix {o['suffix']}" if o['suffix'] else '')


# ---------- making values ----------

def _fragment(o):
    chars = {'integer': string.digits, 'alphabets': string.ascii_uppercase,
             'alphanumeric': string.ascii_uppercase + string.digits}[o['data_type']]
    frag = ''.join(random.choices(chars, k=int(o['length'])))
    return frag.lower() if o['case'] == 'lower' else frag


def _unique_candidate(o, values):
    frag = _fragment(o)
    if o['position'] == 'full_random' or not values:
        return frag
    base = random.choice(values)
    return frag + base if o['position'] == 'prefix' else base + frag


def _generated(o, state):
    kind = o['kind']
    if kind == 'name':
        first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        core = {'first': first, 'last': last}.get(o['part'], f'{first} {last}')
    elif kind == 'text':
        core = ''.join(random.choices(CHARSETS[o['charset']], k=random.randint(o['min_len'], o['max_len'])))
    elif kind == 'number':
        if o['decimals'] > 0:
            core = f"{random.uniform(o['min'], o['max']):.{min(int(o['decimals']), 10)}f}"
        else:
            core = str(random.randint(int(o['min']), int(o['max'])))
            core = core.zfill(int(o['pad'])) if o['pad'] else core
    elif kind == 'email':
        local = ''.join(random.choices(string.ascii_lowercase + string.digits, k=max(1, min(int(o['length']), 64))))
        core = f"{local}@{o['domain'] or 'example.com'}"
    elif kind == 'phone':
        core = re.sub('#', lambda _: random.choice(string.digits), o['pattern'])
    elif kind == 'uuid':
        core = str(uuid.uuid4())
    elif kind == 'sequence':
        current = state.get('next', o['start'])
        state['next'] = current + o['step']
        core = str(current).zfill(int(o['pad'])) if o['pad'] else str(current)
    elif kind == 'boolean':
        core = random.choice(o['style'].split('/'))
    elif kind == 'lorem':
        core = ' '.join(random.choices(LOREM, k=random.randint(o['min_words'], o['max_words'])))
    elif kind == 'company':
        core = f'{random.choice(COMPANY_WORDS)} {random.choice(COMPANY_TYPES)}'
    elif kind == 'city':
        core = random.choice(CITIES)
    elif kind == 'country':
        core = random.choice(COUNTRIES)
    else:
        core = ''
    return f"{o['prefix']}{core}{o['suffix']}"


def make(produces, options, values, state=None, is_used=None):
    """One value. `state` keeps counters; `is_used(value)` says whether a unique candidate was handed out before."""
    o = clean_options(produces, options)
    state = state if state is not None else {}
    if produces == 'defined':
        if not values:
            return ''
        if o['mode'] == 'sequential':
            pos = int(state.get('pos', 0)) % len(values)
            state['pos'] = pos + 1
            return values[pos]
        return random.choice(values)
    if produces == 'unique':
        for _ in range(200):
            candidate = _unique_candidate(o, values)
            if is_used is None or not is_used(candidate):
                return candidate
        raise ValueError('No unused value is left; make the fragment longer or reset the used values.')
    if produces == 'date':
        return date_value(o)
    return _generated(o, state)


def example(produces, options, values, count=3):
    """Preview values; counters are not advanced and nothing is marked used."""
    try:
        return [make(produces, options, values, {}) for _ in range(count)]
    except Exception as exc:
        return [f'error: {exc}']


def generate(lov, project=None):
    """A fresh value for a stored category; counters are saved, unique values are remembered per project."""
    from .models import ApiLovUsedValue
    state = dict(lov.state or {})
    values = list(lov.values or [])
    if lov.produces == 'unique':
        used = set(ApiLovUsedValue.objects.filter(lov=lov).values_list('value', flat=True))
        value = make('unique', lov.options, values, state, is_used=lambda v: v in used)
        ApiLovUsedValue.objects.get_or_create(lov=lov, value=value, defaults={'project': project})
        return value
    value = make(lov.produces, lov.options, values, state)
    if state != (lov.state or {}):
        lov.state = state
        lov.save(update_fields=['state'])
    return value


def alias_list(text):
    return [a for a in dict.fromkeys(x.strip() for x in re.split(r'[,\n;]+', text or '')) if a]


class Registry:
    """The enabled categories a request can use, by name and alias, each generated once per send."""

    def __init__(self, lovs=(), project=None):
        self.project = project
        self.lovs = {}
        for l in lovs:
            self.lovs[l.name] = l
            for alias in alias_list(l.aliases):
                self.lovs.setdefault(alias, l)
        self.values = {}

    @classmethod
    def load(cls, project=None):
        from .models import ApiLov
        return cls(ApiLov.objects.filter(enabled=True), project)

    def __contains__(self, name):
        return name in self.lovs

    def value(self, name):
        lov = self.lovs[name]
        if lov.name not in self.values:
            self.values[lov.name] = generate(lov, self.project)
        return self.values[lov.name]

    def reset(self):
        self.values = {}

    def used(self):
        return dict(self.values)
