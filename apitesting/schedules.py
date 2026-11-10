"""When a collection schedule runs next. Times are in the server's local time zone (settings.TIME_ZONE)."""
from datetime import datetime, time, timedelta

from django.utils import timezone

WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


class CronError(ValueError):
    pass


def _field(text, low, high, name):
    """Values allowed by one cron field: *, */n, a, a-b, a-b/n, lists of these."""
    values = set()
    for part in text.split(','):
        step = 1
        if '/' in part:
            part, step_text = part.split('/', 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f'Bad step in the {name} field.')
            step = int(step_text)
        if part == '*':
            start, end = low, high
        elif '-' in part:
            a, b = part.split('-', 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f'Bad range in the {name} field.')
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step > 1:
                end = high
        else:
            raise CronError(f'Bad value "{part}" in the {name} field.')
        if start < low or end > high or start > end:
            raise CronError(f'The {name} field must be between {low} and {high}.')
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr):
    """'m h dom mon dow' -> (minutes, hours, days, months, weekdays, dom_any, dow_any). dow: 0/7 = Sunday."""
    parts = (expr or '').split()
    if len(parts) != 5:
        raise CronError('A cron expression has 5 fields: minute hour day-of-month month day-of-week.')
    minutes = _field(parts[0], 0, 59, 'minute')
    hours = _field(parts[1], 0, 23, 'hour')
    days = _field(parts[2], 1, 31, 'day-of-month')
    months = _field(parts[3], 1, 12, 'month')
    dows = {d % 7 for d in _field(parts[4], 0, 7, 'day-of-week')}
    return minutes, hours, days, months, dows, parts[2] == '*', parts[4] == '*'


def next_cron(expr, after):
    """First local datetime strictly after `after` that matches the expression."""
    minutes, hours, days, months, dows, dom_any, dow_any = parse_cron(expr)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for _ in range(366 * 5):
        dom_ok, dow_ok = day.day in days, (day.weekday() + 1) % 7 in dows
        # like cron: when both day fields are restricted, either one may match
        day_ok = (dom_ok or dow_ok) if not (dom_any or dow_any) else (dom_ok and dow_ok)
        if day.month in months and day_ok:
            for h in sorted(hours):
                for m in sorted(minutes):
                    candidate = timezone.make_aware(datetime.combine(day, time(h, m)))
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    raise CronError('This cron expression never runs.')


def next_run(schedule, after=None):
    """Next time the schedule is due after `after` (default: now), as an aware datetime."""
    now = timezone.localtime(after or timezone.now())
    freq = schedule.frequency
    if freq == 'interval':
        return now.replace(second=0, microsecond=0) + timedelta(minutes=max(1, schedule.interval_minutes))
    if freq == 'hourly':
        candidate = now.replace(minute=schedule.minute % 60, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(hours=1)
    if freq == 'cron':
        return next_cron(schedule.cron, now)
    at = schedule.time or time(9, 0)
    allowed = set(schedule.weekdays or []) if freq == 'weekly' else set(range(7))
    if not allowed:
        allowed = set(range(7))
    day = now.date()
    for _ in range(8):
        if day.weekday() in allowed:
            candidate = timezone.make_aware(datetime.combine(day, at))
            if candidate > now:
                return candidate
        day += timedelta(days=1)
    return None


def describe(schedule):
    """Plain words, e.g. 'Weekly on Mon, Wed at 09:00'."""
    freq = schedule.frequency
    if freq == 'interval':
        return f'Every {schedule.interval_minutes} minutes'
    if freq == 'hourly':
        return f'Hourly at :{schedule.minute:02d}'
    if freq == 'cron':
        return f'Cron {schedule.cron}'
    at = schedule.time.strftime('%H:%M') if schedule.time else '09:00'
    if freq == 'weekly':
        days = ', '.join(WEEKDAYS[d] for d in sorted(schedule.weekdays or [])) or 'every day'
        return f'Weekly on {days} at {at}'
    return f'Daily at {at}'
