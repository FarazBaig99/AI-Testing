"""Common parts of every tool connector.

A connector does two things for its Connection:
  list_projects() -> [(external_id, name)]   the projects the user can pick when linking
  fetch()         -> normalized dict          everything of the picked project, in the shape below

    {'members':      [{external_id, name, email}],
     'releases':     [{external_id, external_key, name, status, start_date, release_date, description, url}],
     'sprints':      [{external_id, name, state, start_date, end_date, goal, release, url}],
     'items':        [{external_id, external_key, source_type, title, status, category, priority, severity,
                       story_points, assignee, release, sprint, parent, labels, created_on, updated_on,
                       resolved_on, due_date, url}],
     'dependencies': [{source, target, type}]}

References between records (item.sprint, sprint.release, dependency.source...) are external ids.
The sync engine maps `source_type` to a WorkItem kind, validates and saves.
"""
import base64
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timezone


class ConnectorError(Exception):
    """A problem the user can fix (bad token, wrong URL, missing access); shown as is."""


def http_json(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        'Accept': 'application/json', **({'Content-Type': 'application/json'} if data else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')[:300]
        if e.code in (401, 403):
            raise ConnectorError(f'Access denied ({e.code}): check the API token and its permissions.') from e
        if e.code == 404:
            raise ConnectorError(f'Not found (404): {url}') from e
        if e.code == 429:
            raise ConnectorError('Rate limit reached (429): wait a minute and sync again.') from e
        raise ConnectorError(f'HTTP {e.code} from {url}: {detail}') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectorError(f'Could not reach {url}: {getattr(e, "reason", e)}') from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError as e:
        raise ConnectorError(f'{url} did not return JSON. Check the site URL.') from e


def basic_auth(user, password):
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


def to_date(value):
    """ISO date / datetime string, or epoch milliseconds (as int or string) -> date."""
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def state_from_dates(start, end, today=None):
    today = today or date.today()
    if end and end < today:
        return 'closed'
    if start and start <= today:
        return 'active'
    return 'future'


# the WorkItem kind a source type means when the connection has no mapping for it
KIND_WORDS = [('bug', 'bug'), ('defect', 'bug'), ('epic', 'epic'), ('sub', 'subtask'), ('story', 'story'),
              ('feature', 'story'), ('requirement', 'story')]


def guess_kind(source_type):
    text = (source_type or '').lower()
    return next((kind for word, kind in KIND_WORDS if word in text), 'task')


class Connector:
    tool = ''
    label = ''
    # credential fields shown when connecting: (Connection field, label, placeholder, help)
    fields = []
    pick_label = 'Project'
    phase = 1  # tools of later phases are listed but cannot be connected yet
    about = ''
    do_not_call_in_templates = True  # templates read the class attributes; calling the class would need a connection

    def __init__(self, connection, http=None):
        self.connection = connection
        self.http = http or http_json
        self.warnings = []

    def check_config(self):
        for name, label, _, _ in self.fields:
            if not getattr(self.connection, name).strip():
                raise ConnectorError(f'{label} is required.')

    def list_projects(self):
        raise NotImplementedError

    def fetch(self):
        raise NotImplementedError

    def setting(self, name, default=''):
        return self.connection.mapping.get(name) or default

    @staticmethod
    def clean_url(url):
        url = url.strip().rstrip('/')
        if not re.match(r'^https?://', url):
            url = 'https://' + url
        return url
