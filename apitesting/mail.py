"""Run report e-mails through the SMTP server saved in Mail Settings (Python's smtplib)."""
import re
import smtplib
import ssl
from email.message import EmailMessage

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.urls import reverse

from .models import MailSettings

SMTP_TIMEOUT = 20


class MailError(Exception):
    pass


def split_emails(text):
    """'a@x.com, b@y.com; c@z.com' -> (valid list, invalid list)."""
    valid, invalid = [], []
    for part in re.split(r'[,;\s]+', text or ''):
        if not part:
            continue
        try:
            validate_email(part)
            if part.lower() not in (v.lower() for v in valid):
                valid.append(part)
        except ValidationError:
            invalid.append(part)
    return valid, invalid


def _connect(settings):
    if settings.security == 'ssl':
        return smtplib.SMTP_SSL(settings.host, settings.port, timeout=SMTP_TIMEOUT,
                                context=ssl.create_default_context())
    server = smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT)
    if settings.security == 'tls':
        server.starttls(context=ssl.create_default_context())
    return server


def send_mail(to, subject, text, html=None, settings=None, smtp=None):
    """Send one message. `smtp` replaces the connection factory (tests). Raises MailError."""
    settings = settings or MailSettings.load()
    if not settings.is_configured:
        raise MailError('Mail settings are not configured.')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = settings.from_email or settings.username
    msg['To'] = ', '.join(to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype='html')
    try:
        server = (smtp or _connect)(settings)
        try:
            if settings.username:
                server.login(settings.username, settings.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        raise MailError('The SMTP server rejected the username or password.')
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f'Could not send e-mail: {exc}')


def report_context(run, settings=None):
    settings = settings or MailSettings.load()
    base = (settings.site_url or '').rstrip('/')
    return {'run': run, 'summary': run.summary, 'collection': run.collection,
            'failed': run.results.filter(passed=False)[:50] if run.kind == 'functional' else [],
            'link': base + reverse('api_run_detail', args=[run.pk]) if base else ''}


def report_subject(run):
    s = run.summary or {}
    if run.kind == 'performance':
        t = s.get('totals', {})
        detail = f"{t.get('requests', 0)} requests, p95 {t.get('p95') or '-'} ms, errors {t.get('error_pct', 0)}%"
    else:
        detail = f"{s.get('passed', 0)}/{s.get('done', 0)} passed"
    return f"[BaigNova] {run.collection.name}: {run.get_kind_display()} run {run.get_status_display().upper()} ({detail})"


def send_run_report(run, smtp=None):
    """E-mail the report if the run asks for it; the outcome is saved in run.email_status."""
    to, _ = split_emails(run.emails)
    if not to or run.email_when == 'never':
        return
    if run.email_when == 'failure' and run.status == 'passed':
        run.email_status = 'Not sent: the run passed (report only on failure).'
    else:
        settings = MailSettings.load()
        context = report_context(run, settings)
        try:
            send_mail(to, report_subject(run), render_to_string('apitesting/email_report.txt', context),
                      render_to_string('apitesting/email_report.html', context), settings=settings, smtp=smtp)
            run.email_status = 'Sent to ' + ', '.join(to)
        except MailError as exc:
            run.email_status = f'Not sent: {exc}'
    run.save(update_fields=['email_status'])
