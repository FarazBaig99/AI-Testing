"""Jira Cloud: REST API v3 (projects, versions, issues) + Agile API 1.0 (boards, sprints).

Versions are releases, board sprints are sprints, the issue type decides the kind (Epic / Story / Task / Sub-task / Bug).
"""
from urllib.parse import quote

from .base import Connector, ConnectorError, basic_auth, to_date

CATEGORY = {'new': 'todo', 'indeterminate': 'in_progress', 'done': 'done'}
SPRINT_FIELD = 'com.pyxis.greenhopper.jira:gh-sprint'
POINT_FIELDS = ('story points', 'story point estimate')


class JiraConnector(Connector):
    tool = 'jira'
    label = 'Jira'
    fields = [('base_url', 'Site URL', 'https://your-company.atlassian.net', 'Your Jira Cloud address'),
              ('account', 'Account email', 'name@company.com', 'Email of the Atlassian account that owns the token'),
              ('token', 'API token', '', 'Create one at id.atlassian.com → Security → API tokens')]
    pick_label = 'Jira project'
    about = 'Versions → Releases, board sprints → Sprints, issue types → Epic / Story / Task / Subtask / Bug.'

    def get(self, path, **kwargs):
        return self.http('GET', self.base + path, self.headers, **kwargs)

    @property
    def base(self):
        return self.clean_url(self.connection.base_url)

    @property
    def headers(self):
        return {'Authorization': basic_auth(self.connection.account.strip(), self.connection.token.strip())}

    def list_projects(self):
        self.check_config()
        projects, start = [], 0
        while True:
            page = self.get(f'/rest/api/3/project/search?maxResults=100&startAt={start}')
            values = page.get('values', [])
            projects += [(p['key'], f'{p["name"]} ({p["key"]})') for p in values]
            start += len(values)
            if page.get('isLast', True) or not values:
                return projects

    def fetch(self):
        self.check_config()
        key = self.connection.external_id
        fields = self.get('/rest/api/3/field')
        sprint_field = next((f['id'] for f in fields if f.get('schema', {}).get('custom') == SPRINT_FIELD), None)
        point_fields = [f['id'] for f in fields if f.get('name', '').lower() in POINT_FIELDS]
        severity_field = next((f['id'] for f in fields if f.get('name', '').lower() == 'severity'), None)

        releases = [self.release(v) for v in self.get(f'/rest/api/3/project/{quote(key)}/versions')]
        sprints = {s['external_id']: s for s in self.board_sprints(key)}
        members, items, dependencies = {}, [], []

        wanted = ['summary', 'status', 'issuetype', 'priority', 'assignee', 'fixVersions', 'parent', 'labels',
                  'created', 'updated', 'resolutiondate', 'duedate', 'issuelinks']
        wanted += [f for f in [sprint_field, severity_field, *point_fields] if f]
        token = None
        while True:
            body = {'jql': f'project = "{key}" ORDER BY key', 'fields': wanted, 'maxResults': 100}
            if token:
                body['nextPageToken'] = token
            page = self.http('POST', self.base + '/rest/api/3/search/jql', self.headers, body=body)
            for issue in page.get('issues', []):
                f = issue['fields']
                assignee = f.get('assignee')
                if assignee:
                    members[assignee['accountId']] = {'external_id': assignee['accountId'],
                                                      'name': assignee.get('displayName', ''),
                                                      'email': assignee.get('emailAddress', '')}
                sprint_values = (f.get(sprint_field) or []) if sprint_field else []
                for s in sprint_values:  # sprints of boards we could not read still show up on issues
                    sprints.setdefault(str(s['id']), self.sprint(s))
                points = next((f[p] for p in point_fields if f.get(p) is not None), None)
                severity = f.get(severity_field) if severity_field else None
                items.append({
                    'external_id': issue['id'], 'external_key': issue['key'],
                    'source_type': f['issuetype']['name'], 'title': f.get('summary') or issue['key'],
                    'status': f['status']['name'],
                    'category': CATEGORY.get(f['status'].get('statusCategory', {}).get('key'), 'in_progress'),
                    'priority': (f.get('priority') or {}).get('name', ''),
                    'severity': severity.get('value', '') if isinstance(severity, dict) else (severity or ''),
                    'story_points': points,
                    'assignee': assignee['accountId'] if assignee else None,
                    'release': f['fixVersions'][0]['id'] if f.get('fixVersions') else None,
                    'sprint': str(sprint_values[-1]['id']) if sprint_values else None,
                    'parent': (f.get('parent') or {}).get('id'),
                    'labels': ', '.join(f.get('labels') or []),
                    'created_on': to_date(f.get('created')), 'updated_on': to_date(f.get('updated')),
                    'resolved_on': to_date(f.get('resolutiondate')), 'due_date': to_date(f.get('duedate')),
                    'url': f'{self.base}/browse/{issue["key"]}',
                })
                for link in f.get('issuelinks') or []:
                    kind = 'blocks' if link['type']['name'].lower() == 'blocks' else 'relates'
                    if 'outwardIssue' in link:
                        dependencies.append({'source': issue['id'], 'target': link['outwardIssue']['id'], 'type': kind})
                    elif 'inwardIssue' in link:
                        dependencies.append({'source': link['inwardIssue']['id'], 'target': issue['id'], 'type': kind})
            token = page.get('nextPageToken')
            if page.get('isLast', True) or not token:
                break
        return {'members': list(members.values()), 'releases': releases, 'sprints': list(sprints.values()),
                'items': items, 'dependencies': dependencies}

    def board_sprints(self, key):
        try:
            boards = self.get(f'/rest/agile/1.0/board?projectKeyOrId={quote(key)}&maxResults=50').get('values', [])
        except ConnectorError as e:
            self.warnings.append(f'Boards could not be read, sprints come from issues only ({e}).')
            return []
        sprints = []
        for board in boards:
            if board.get('type') != 'scrum':
                continue  # kanban boards have no sprints
            start = 0
            while True:
                page = self.get(f'/rest/agile/1.0/board/{board["id"]}/sprint?startAt={start}&maxResults=50')
                values = page.get('values', [])
                sprints += [self.sprint(s) for s in values]
                start += len(values)
                if page.get('isLast', True) or not values:
                    break
        return sprints

    def release(self, v):
        if v.get('archived'):
            status = 'Archived'
        elif v.get('released'):
            status = 'Released'
        else:
            status = 'In Progress' if v.get('startDate') else 'Planned'
        return {'external_id': str(v['id']), 'external_key': '', 'name': v['name'], 'status': status,
                'start_date': to_date(v.get('startDate')), 'release_date': to_date(v.get('releaseDate')),
                'description': v.get('description', ''),
                'url': f'{self.base}/projects/{self.connection.external_id}/versions/{v["id"]}'}

    def sprint(self, s):
        return {'external_id': str(s['id']), 'name': s['name'], 'state': s.get('state', 'future').lower(),
                'start_date': to_date(s.get('startDate')), 'end_date': to_date(s.get('endDate')),
                'goal': s.get('goal') or '', 'release': None, 'url': ''}
