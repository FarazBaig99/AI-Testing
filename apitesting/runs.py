"""Collection runner pages: run now (functional / performance), schedules, run history and reports, mail settings."""
from datetime import time as dtime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import collection_runner as engine
from .mail import MailError, send_mail, split_emails
from .models import ApiCollection, CollectionRun, MailSettings, RunSchedule
from .schedules import WEEKDAYS, CronError, describe, next_run, parse_cron


def _int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _emails(request, errors):
    valid, invalid = split_emails(request.POST.get('emails', ''))
    if invalid:
        errors.append('Not valid e-mail address: ' + ', '.join(invalid))
    when = request.POST.get('email_when')
    return ', '.join(valid), when if when in dict(RunSchedule.EMAIL_WHEN) else 'always'


def functional_config(request, collection, errors):
    ids = {int(i) for i in request.POST.getlist('items') if i.isdigit()}
    items = [r for r in collection.requests.all() if r.pk in ids]
    if not items:
        errors.append('Tick at least one request to run.')
    for item in items:          # the expected status typed on the runner is kept on the request
        raw = request.POST.get(f'expected_{item.pk}', '').strip()
        code = _int(raw, None, 100, 599) if raw else None
        if code != item.expected_status:
            item.expected_status = code
            item.save(update_fields=['expected_status'])
    return {'items': [i.pk for i in items],
            'iterations': _int(request.POST.get('iterations'), 1, 1, engine.MAX_ITERATIONS),
            'delay_ms': _int(request.POST.get('delay_ms'), 0, 0, engine.MAX_DELAY_MS),
            'stop_on_fail': request.POST.get('stop_on_fail') == 'on',
            'keep_variables': request.POST.get('keep_variables') == 'on'}


def performance_config(request, collection, errors):
    ids = {int(i) for i in request.POST.getlist('perf_items') if i.isdigit()}
    items = [r for r in collection.requests.all() if r.pk in ids]
    if not items:
        errors.append('Tick at least one API for the performance run.')
    duration = _int(request.POST.get('duration'), 60, 5, engine.MAX_DURATION)
    p95 = request.POST.get('max_p95', '').strip()
    return {'items': [{'id': i.pk, 'mode': 'parallel' if request.POST.get(f'mode_{i.pk}') == 'parallel' else 'sequence'}
                      for i in items],
            'vus': _int(request.POST.get('vus'), 1, 1, engine.MAX_VUS),
            'stop_by': 'iterations' if request.POST.get('stop_by') == 'iterations' else 'duration',
            'duration': duration,
            'iterations': _int(request.POST.get('vu_iterations'), 1, 1, engine.MAX_VU_ITERATIONS),
            'ramp_up': _int(request.POST.get('ramp_up'), 0, 0, duration),
            'max_error_pct': _int(request.POST.get('max_error_pct'), 1, 0, 100),
            'max_p95': _int(p95, None, 1, 600000) if p95 else None}


def apply_timing(schedule, request, errors):
    """Frequency fields of the schedule form."""
    freq = request.POST.get('frequency')
    schedule.frequency = freq if freq in dict(RunSchedule.FREQUENCIES) else 'daily'
    schedule.interval_minutes = _int(request.POST.get('interval_minutes'), 60, 5, 10080)
    schedule.minute = _int(request.POST.get('minute'), 0, 0, 59)
    raw = request.POST.get('time', '')
    try:
        h, m = raw.split(':')[:2]
        schedule.time = dtime(int(h), int(m))
    except ValueError:
        schedule.time = dtime(9, 0)
        if schedule.frequency in ('daily', 'weekly'):
            errors.append('Pick a time for the schedule.')
    schedule.weekdays = sorted({int(d) for d in request.POST.getlist('weekdays') if d in '0123456'})
    if schedule.frequency == 'weekly' and not schedule.weekdays:
        errors.append('Tick at least one day for a weekly schedule.')
    schedule.cron = request.POST.get('cron', '').strip()
    if schedule.frequency == 'cron':
        try:
            parse_cron(schedule.cron)
        except CronError as exc:
            errors.append(str(exc))


def _start(collection, kind, config, request, emails, when):
    run = CollectionRun.objects.create(collection=collection, kind=kind, config=config, emails=emails,
                                       email_when=when, created_by=request.user)
    engine.start_run(run)
    return run


@login_required
def collection_run(request, collection_id):
    collection = get_object_or_404(ApiCollection, pk=collection_id)
    tab = request.GET.get('tab', 'functional')
    if request.method == 'POST':
        action = request.POST.get('action', '')
        errors = []
        if action in ('run_functional', 'schedule_functional', 'run_performance', 'schedule_performance'):
            kind = 'performance' if action.endswith('performance') else 'functional'
            config = (performance_config if kind == 'performance' else functional_config)(request, collection, errors)
            emails, when = _emails(request, errors)
            if action.startswith('schedule'):
                schedule = RunSchedule(collection=collection, kind=kind, config=config, emails=emails, email_when=when,
                                       created_by=request.user,
                                       name=request.POST.get('schedule_name', '').strip()[:200]
                                       or f'{collection.name} {kind}')
                apply_timing(schedule, request, errors)
                if not errors:
                    schedule.next_run_at = next_run(schedule)
                    schedule.save()
                    messages.success(request, f'Schedule "{schedule.name}" saved: {describe(schedule)}.')
                    return redirect(f"{reverse('api_collection_run', args=[collection.pk])}?tab=schedules")
            elif not errors:
                run = _start(collection, kind, config, request, emails, when)
                return redirect('api_run_detail', run_id=run.pk)
            for e in errors:
                messages.error(request, e)
            return redirect(f"{reverse('api_collection_run', args=[collection.pk])}?tab={kind}")

        schedule = get_object_or_404(RunSchedule, pk=request.POST.get('schedule'), collection=collection)
        if action == 'toggle_schedule':
            schedule.enabled = not schedule.enabled
            schedule.next_run_at = next_run(schedule) if schedule.enabled else None
            schedule.save(update_fields=['enabled', 'next_run_at'])
            messages.success(request, f'Schedule "{schedule.name}" {"turned on" if schedule.enabled else "paused"}.')
        elif action == 'delete_schedule':
            schedule.delete()
            messages.success(request, f'Schedule "{schedule.name}" deleted.')
        elif action == 'run_schedule_now':
            run = _start(collection, schedule.kind, schedule.config, request, schedule.emails, schedule.email_when)
            run.schedule = schedule
            run.save(update_fields=['schedule'])
            return redirect('api_run_detail', run_id=run.pk)
        elif action == 'edit_schedule':
            errors = []
            schedule.name = request.POST.get('schedule_name', '').strip()[:200] or schedule.name
            apply_timing(schedule, request, errors)
            schedule.emails, schedule.email_when = _emails(request, errors)
            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                schedule.next_run_at = next_run(schedule) if schedule.enabled else None
                schedule.save()
                messages.success(request, f'Schedule "{schedule.name}" saved: {describe(schedule)}.')
        return redirect(f"{reverse('api_collection_run', args=[collection.pk])}?tab=schedules")

    requests_ = list(collection.requests.select_related('environment', 'project', 'source'))
    for r in requests_:
        r.expected_default = r.source.expected_status if r.source else ''
    schedules = list(collection.schedules.all())
    for s in schedules:
        s.description = describe(s)
    return render(request, 'apitesting/collection_run.html', {
        'collection': collection, 'requests': requests_, 'tab': tab, 'schedules': schedules,
        'runs': collection.runs.select_related('schedule', 'created_by')[:50],
        'weekdays': list(enumerate(WEEKDAYS)), 'frequencies': RunSchedule.FREQUENCIES,
        'email_when': RunSchedule.EMAIL_WHEN, 'mail': MailSettings.load(), 'limits': engine,
    })


@login_required
def run_detail(request, run_id):
    run = get_object_or_404(CollectionRun.objects.select_related('collection', 'schedule', 'created_by'), pk=run_id)
    return render(request, 'apitesting/run_detail.html', {
        'run': run, 'results': run.results.all() if run.kind == 'functional' else [],
        'config_items': {r.pk: r for r in run.collection.requests.all()},
    })


@login_required
def run_status(request, run_id):
    """Live progress for the run page (polled every second while the run is going)."""
    run = get_object_or_404(CollectionRun, pk=run_id)
    after = _int(request.GET.get('after'), 0, 0, 10 ** 12)
    results = [{'id': r.pk, 'iteration': r.iteration, 'position': r.position, 'name': r.name, 'method': r.method,
                'url': r.url, 'expected': r.expected, 'status_code': r.status_code, 'ms': r.ms, 'size': r.size,
                'passed': r.passed, 'error': r.error, 'headers': r.response_headers, 'body': r.response_body,
                'tests': r.tests, 'console': r.console}
               for r in run.results.filter(pk__gt=after)[:500]]
    return JsonResponse({'status': run.status, 'status_label': run.get_status_display(), 'active': run.is_active,
                         'summary': run.summary, 'error': run.error, 'email_status': run.email_status,
                         'duration': run.duration_s, 'results': results})


@login_required
@require_POST
def run_stop(request, run_id):
    run = get_object_or_404(CollectionRun, pk=run_id)
    if run.is_active:
        run.stop_requested = True
        run.save(update_fields=['stop_requested'])
        messages.info(request, 'Stopping the run...')
    return redirect('api_run_detail', run_id=run.pk)


@login_required
def mail_settings(request, smtp=None):
    settings = MailSettings.load()
    if request.method == 'POST':
        if request.POST.get('action') == 'test':
            to, invalid = split_emails(request.POST.get('test_to', ''))
            if not to or invalid:
                messages.error(request, 'Type one valid e-mail address for the test.')
            else:
                try:
                    send_mail(to, '[BaigNova] Test e-mail',
                              'Mail settings work. Collection run reports will be sent from this address.',
                              settings=settings, smtp=smtp)
                    messages.success(request, f'Test e-mail sent to {to[0]}.')
                except MailError as exc:
                    messages.error(request, str(exc))
            return redirect('api_mail_settings')
        settings.host = request.POST.get('host', '').strip()[:255]
        settings.port = _int(request.POST.get('port'), 587, 1, 65535)
        security = request.POST.get('security')
        settings.security = security if security in dict(MailSettings._meta.get_field('security').choices) else 'tls'
        settings.username = request.POST.get('username', '').strip()[:255]
        if request.POST.get('password'):          # blank keeps the saved password
            settings.password = request.POST['password'][:255]
        if request.POST.get('clear_password') == 'on':
            settings.password = ''
        from_email = request.POST.get('from_email', '').strip()
        if from_email and split_emails(from_email)[1]:
            messages.error(request, 'The From address is not a valid e-mail address.')
            return redirect('api_mail_settings')
        settings.from_email = from_email[:255]
        settings.site_url = request.POST.get('site_url', '').strip()[:255] or 'http://127.0.0.1:8000'
        settings.save()
        messages.success(request, 'Mail settings saved.')
        return redirect('api_mail_settings')
    return render(request, 'apitesting/mail_settings.html', {'settings': settings,
                                                            'next': request.GET.get('next', '')})
