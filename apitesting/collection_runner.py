"""Runs a collection: functional (each request once per iteration, pass/fail on the status code) or performance
(virtual users for a duration / number of iterations). Runs execute in a background thread inside the server;
the in-process scheduler starts runs that are due."""
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.utils import timezone

from . import runner
from .models import CollectionRequest, CollectionRun, RunResult, RunSchedule

log = logging.getLogger(__name__)

MAX_ITERATIONS = 100
MAX_DELAY_MS = 60000
MAX_VUS = 100
MAX_DURATION = 1800
MAX_VU_ITERATIONS = 10000
BODY_LIMIT = 20000          # response body kept per functional result
SAMPLE_CAP = 50000          # response times kept per API for percentiles (reservoir sampling)
SPEC_FIELDS = ['method', 'url', 'params', 'headers', 'auth', 'body_mode', 'raw_language', 'body', 'form_data',
               'urlencoded', 'graphql_query', 'graphql_variables', 'verify_ssl']


# ---------- pass rule ----------

def expected_status(item):
    """The status code a request must return; None means any 2xx."""
    if item.expected_status:
        return item.expected_status
    if item.source_id and item.source:
        return item.source.expected_status
    return None


def expected_label(item):
    code = expected_status(item)
    return str(code) if code else '2xx'


def is_pass(result, expected):
    if not result.get('ok'):
        return False
    return result['status'] == expected if expected else 200 <= result['status'] < 300


def _spec(item):
    return {f: getattr(item, f) for f in SPEC_FIELDS}


def _ordered_items(collection, ids):
    """Requests of the collection that are in `ids`, in the collection's order."""
    wanted = {int(i) for i in ids}
    return [r for r in collection.requests.select_related('environment', 'source') if r.pk in wanted]


def _stop_requested(run):
    return CollectionRun.objects.filter(pk=run.pk, stop_requested=True).exists()


# ---------- functional ----------

def run_functional(run, sender=None):
    send = sender or runner.send
    cfg = run.config
    items = _ordered_items(run.collection, cfg.get('items', []))
    iterations = cfg.get('iterations', 1)
    summary = {'planned': len(items) * iterations, 'done': 0, 'passed': 0, 'failed': 0, 'total_ms': 0,
               'iterations': iterations, 'current_iteration': 0}
    stopped = False
    for iteration in range(1, iterations + 1):
        summary['current_iteration'] = iteration
        for position, item in enumerate(items, 1):
            if _stop_requested(run):
                stopped = True
                break
            expected = expected_status(item)
            result = send(_spec(item), item.environment)
            passed = is_pass(result, expected)
            body = result.get('body') or ''
            RunResult.objects.create(
                run=run, request=item, iteration=iteration, position=position, name=item.name, method=item.method,
                url=result.get('url') or item.url, expected=expected_label(item), status_code=result.get('status'),
                ms=result.get('ms'), size=result.get('size'), passed=passed, error=result.get('error', ''),
                response_headers=result.get('headers', []),
                response_body=body[:BODY_LIMIT] + ('\n... (cut)' if len(body) > BODY_LIMIT else ''))
            summary['done'] += 1
            summary['passed' if passed else 'failed'] += 1
            summary['total_ms'] += result.get('ms') or 0
            summary['avg_ms'] = round(summary['total_ms'] / summary['done'])
            run.summary = summary
            run.save(update_fields=['summary'])
            if not passed and cfg.get('stop_on_fail'):
                stopped = True
                summary['stopped_on'] = item.name
                break
            if cfg.get('delay_ms'):
                time.sleep(cfg['delay_ms'] / 1000)
        if stopped:
            break
    run.summary = summary
    if stopped and _stop_requested(run):
        return 'stopped'
    return 'passed' if summary['failed'] == 0 and summary['done'] else 'failed'


# ---------- performance ----------

def _percentile(sorted_values, pct):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo))


class Stats:
    """Thread-safe counters for a performance run."""

    def __init__(self, items):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.per = {i.pk: {'name': i.name, 'method': i.method, 'count': 0, 'errors': 0, 'sum': 0,
                           'min': None, 'max': None, 'samples': [], 'codes': {}} for i in items}
        self.buckets = {}       # second -> [count, errors, sum_ms]
        self.active_vus = 0

    def record(self, item_id, ms, ok, code):
        second = int(time.monotonic() - self.started)
        with self.lock:
            p = self.per[item_id]
            p['count'] += 1
            p['errors'] += 0 if ok else 1
            p['sum'] += ms
            p['min'] = ms if p['min'] is None else min(p['min'], ms)
            p['max'] = ms if p['max'] is None else max(p['max'], ms)
            if len(p['samples']) < SAMPLE_CAP:
                p['samples'].append(ms)
            else:
                j = random.randrange(p['count'])
                if j < SAMPLE_CAP:
                    p['samples'][j] = ms
            key = str(code or 'error')
            p['codes'][key] = p['codes'].get(key, 0) + 1
            b = self.buckets.setdefault(second, [0, 0, 0])
            b[0] += 1
            b[1] += 0 if ok else 1
            b[2] += ms

    @staticmethod
    def _metrics(count, errors, total, samples, lo, hi, elapsed):
        s = sorted(samples)
        return {'requests': count, 'errors': errors, 'error_pct': round(errors * 100 / count, 2) if count else 0,
                'rps': round(count / elapsed, 2) if elapsed else 0, 'avg': round(total / count) if count else None,
                'min': lo, 'max': hi, 'p50': _percentile(s, 50), 'p90': _percentile(s, 90),
                'p95': _percentile(s, 95), 'p99': _percentile(s, 99)}

    def snapshot(self):
        with self.lock:
            elapsed = max(time.monotonic() - self.started, 0.001)
            apis, all_samples, codes = [], [], {}
            count = errors = total = 0
            lo = hi = None
            for item_id, p in self.per.items():
                apis.append(dict(self._metrics(p['count'], p['errors'], p['sum'], p['samples'], p['min'], p['max'],
                                               elapsed), id=item_id, name=p['name'], method=p['method'],
                                 codes=dict(p['codes'])))
                count, errors, total = count + p['count'], errors + p['errors'], total + p['sum']
                all_samples += p['samples']
                if p['min'] is not None:
                    lo = p['min'] if lo is None else min(lo, p['min'])
                    hi = p['max'] if hi is None else max(hi, p['max'])
                for k, v in p['codes'].items():
                    codes[k] = codes.get(k, 0) + v
            last = max(self.buckets) if self.buckets else -1
            series = [{'t': s, 'count': self.buckets.get(s, [0, 0, 0])[0], 'errors': self.buckets.get(s, [0, 0, 0])[1],
                       'avg': round(self.buckets[s][2] / self.buckets[s][0]) if s in self.buckets else None}
                      for s in range(last + 1)]
            return {'totals': self._metrics(count, errors, total, all_samples, lo, hi, elapsed), 'apis': apis,
                    'series': series, 'codes': codes, 'elapsed': round(elapsed, 1), 'active_vus': self.active_vus}


def _steps(items, modes):
    """Group consecutive 'parallel' requests so they are fired together; others go one by one."""
    steps = []
    for item in items:
        parallel = modes.get(str(item.pk)) == 'parallel'
        if parallel and steps and steps[-1][0]:
            steps[-1][1].append(item)
        else:
            steps.append((parallel, [item]))
    return [group for _, group in steps]


def run_performance(run, sender=None, tick=1.0):
    send = sender or runner.send
    cfg = run.config
    items = _ordered_items(run.collection, [e['id'] for e in cfg.get('items', [])])
    modes = {str(e['id']): e.get('mode', 'sequence') for e in cfg.get('items', [])}
    vus, ramp_up = cfg.get('vus', 1), cfg.get('ramp_up', 0)
    by_duration = cfg.get('stop_by', 'duration') == 'duration'
    duration, vu_iterations = cfg.get('duration', 60), cfg.get('iterations', 1)
    prepared = {i.pk: (_spec(i), i.environment, expected_status(i)) for i in items}
    steps = _steps(items, modes)
    stats = Stats(items)
    stop = threading.Event()
    deadline = stats.started + duration if by_duration else None

    def fire(item):
        spec, env, expected = prepared[item.pk]
        result = send(spec, env)
        stats.record(item.pk, result.get('ms') or 0, is_pass(result, expected), result.get('status'))

    def virtual_user(index):
        if ramp_up and stop.wait(ramp_up * index / vus):
            return
        with stats.lock:
            stats.active_vus += 1
        pool = ThreadPoolExecutor(max_workers=max(len(g) for g in steps)) if any(len(g) > 1 for g in steps) else None
        try:
            done = 0
            while not stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                if not deadline and done >= vu_iterations:
                    break
                for group in steps:
                    if stop.is_set() or (deadline and time.monotonic() >= deadline):
                        break
                    if len(group) == 1:
                        fire(group[0])
                    else:
                        list(pool.map(fire, group))
                done += 1
        finally:
            if pool:
                pool.shutdown(wait=True)
            with stats.lock:
                stats.active_vus -= 1

    threads = [threading.Thread(target=virtual_user, args=(i,), daemon=True) for i in range(vus)]
    for t in threads:
        t.start()
    stopped = False
    while any(t.is_alive() for t in threads):
        time.sleep(tick)
        if _stop_requested(run):
            stopped = True
            stop.set()
        if deadline and time.monotonic() > deadline + runner.TIMEOUT + 5:
            stop.set()          # a hung request must not keep the run open
        run.summary = dict(stats.snapshot(), config_vus=vus)
        run.save(update_fields=['summary'])
    for t in threads:
        t.join()
    summary = dict(stats.snapshot(), config_vus=vus)
    totals = summary['totals']
    reasons = []
    if totals['requests'] == 0:
        reasons.append('No request was sent.')
    if totals['error_pct'] > cfg.get('max_error_pct', 1):
        reasons.append(f"Error rate {totals['error_pct']}% is above {cfg.get('max_error_pct', 1)}%.")
    if cfg.get('max_p95') and (totals['p95'] or 0) > cfg['max_p95']:
        reasons.append(f"p95 {totals['p95']} ms is above {cfg['max_p95']} ms.")
    summary['fail_reasons'] = reasons
    run.summary = summary
    if stopped:
        return 'stopped'
    return 'failed' if reasons else 'passed'


# ---------- execution ----------

def execute(run, sender=None, smtp=None):
    """Run synchronously (the background thread calls this). Returns the run."""
    from .mail import send_run_report
    run.status, run.started_at = 'running', timezone.now()
    run.save(update_fields=['status', 'started_at'])
    try:
        if run.kind == 'performance':
            run.status = run_performance(run, sender)
        else:
            run.status = run_functional(run, sender)
    except Exception as exc:       # the run must always end with a status
        log.exception('Collection run %s failed', run.pk)
        run.status, run.error = 'error', f'{type(exc).__name__}: {exc}'
    run.finished_at = timezone.now()
    run.save(update_fields=['status', 'error', 'summary', 'finished_at'])
    send_run_report(run, smtp=smtp)
    return run


def _background(run_id):
    close_old_connections()
    try:
        execute(CollectionRun.objects.select_related('collection').get(pk=run_id))
    finally:
        connection.close()


def start_run(run):
    threading.Thread(target=_background, args=(run.pk,), daemon=True, name=f'collection-run-{run.pk}').start()


# ---------- scheduler ----------

_scheduler_started = False


def tick(now=None, start=start_run):
    """Start every enabled schedule that is due. Returns the runs started."""
    from .schedules import next_run
    now = now or timezone.now()
    started = []
    for schedule in RunSchedule.objects.filter(enabled=True, next_run_at__lte=now).select_related('collection'):
        schedule.last_run_at = now
        schedule.next_run_at = next_run(schedule, now)
        schedule.save(update_fields=['last_run_at', 'next_run_at'])
        if schedule.runs.filter(status__in=('queued', 'running')).exists():
            continue        # the previous run of this schedule is still going
        run = CollectionRun.objects.create(collection=schedule.collection, schedule=schedule, kind=schedule.kind,
                                           trigger='schedule', config=schedule.config, emails=schedule.emails,
                                           email_when=schedule.email_when, created_by=schedule.created_by)
        start(run)
        started.append(run)
    return started


def _scheduler_loop():
    time.sleep(3)
    close_old_connections()
    # runs that were going when the server stopped can never finish
    CollectionRun.objects.filter(status__in=('queued', 'running')).update(
        status='error', error='The server stopped during this run.', finished_at=timezone.now())
    while True:
        try:
            close_old_connections()
            tick()
        except Exception:
            log.exception('Collection scheduler tick failed')
        finally:
            connection.close()
        time.sleep(30)


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True, name='collection-scheduler').start()
