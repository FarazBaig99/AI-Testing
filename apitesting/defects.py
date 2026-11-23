"""Create a defect (bug) in ClickUp or Jira from a failed API test result.

Pure HTTP with the standard library; nothing here touches the database. Each function returns
(ok, link_or_message). The settings object only needs the attributes of models.IntegrationSettings.
"""
import base64
import json
import urllib.error
import urllib.request

TIMEOUT = 20
MAX_DESCRIPTION = 15000


def _post(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json', **headers},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', 'replace')[:500]
        except Exception:
            body = ''
        return e.code, {'error': body or e.reason}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {'error': str(e)}


def describe_result(result, run=None):
    """Plain-text bug description for a RunResult (or any object with the same attributes)."""
    lines = [f'API: {result.method} {result.url}',
             f'Expected: {result.expected or "2xx"}',
             f'Actual status: {result.status_code if result.status_code is not None else "no response"}',
             f'Response time: {result.ms if result.ms is not None else "-"} ms']
    if run is not None:
        lines.append(f'Collection run: #{run.pk} ({run.collection})')
    if result.error:
        lines += ['', 'Problems:', result.error]
    failed = [t for t in (result.tests or []) if not t.get('passed')]
    if failed:
        lines += ['', 'Failed tests:'] + [f'- {t.get("name")}: {t.get("error", "")}' for t in failed]
    snap = result.request_snapshot or {}
    if snap.get('body'):
        lines += ['', 'Request body:', str(snap['body'])[:3000]]
    if result.response_body:
        lines += ['', 'Response body:', result.response_body[:3000]]
    return '\n'.join(lines)[:MAX_DESCRIPTION]


def create_clickup(settings, title, description):
    if not (settings.clickup_token and settings.clickup_list_id):
        return False, 'ClickUp is not configured (token and list id).'
    status, data = _post(f'https://api.clickup.com/api/v2/list/{settings.clickup_list_id.strip()}/task',
                         {'Authorization': settings.clickup_token.strip()},
                         {'name': title[:250], 'description': description, 'tags': ['api-test']})
    if status in (200, 201) and data.get('url'):
        return True, data['url']
    return False, f'ClickUp refused the task ({status}): {data.get("err") or data.get("error") or data}'


def create_jira(settings, title, description):
    if not (settings.jira_url and settings.jira_email and settings.jira_token and settings.jira_project_key):
        return False, 'Jira is not configured (URL, e-mail, API token and project key).'
    base = settings.jira_url.strip().rstrip('/')
    auth = base64.b64encode(f'{settings.jira_email.strip()}:{settings.jira_token.strip()}'.encode()).decode()
    doc = {'type': 'doc', 'version': 1, 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': line or ' '}]} for line in description.split('\n')[:300]]}
    status, data = _post(f'{base}/rest/api/3/issue', {'Authorization': f'Basic {auth}'},
                         {'fields': {'project': {'key': settings.jira_project_key.strip()}, 'summary': title[:250],
                                     'description': doc, 'issuetype': {'name': settings.jira_issue_type or 'Bug'},
                                     'labels': ['api-test']}})
    if status in (200, 201) and data.get('key'):
        return True, f'{base}/browse/{data["key"]}'
    return False, f'Jira refused the issue ({status}): {data.get("errors") or data.get("errorMessages") or data.get("error") or data}'


def create_defect(settings, target, title, description):
    """target is 'clickup' or 'jira'."""
    if target == 'clickup':
        return create_clickup(settings, title, description)
    if target == 'jira':
        return create_jira(settings, title, description)
    return False, 'Unknown defect tracker.'
