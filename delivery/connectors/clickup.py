"""ClickUp: API v2. The user picks a Space; everything in it is synced.

ClickUp has no built-in releases, so folder and list names decide (keywords can be changed in the mapping):
  a folder named like "Release ..."  -> a release; its lists are that release's sprints if named "Sprint ..."
  a folder named like "Sprint ..."   -> each of its lists is a sprint (ClickUp Sprints ClickApp)
  a list named "Sprint ..." or "Release ..." anywhere -> a sprint / release
Custom task types (or a tag such as "bug" / "story" / "epic") decide the kind; a task with a parent is a subtask.
"""
from .base import Connector, ConnectorError, guess_kind, state_from_dates, to_date

API = 'https://api.clickup.com/api/v2'
CATEGORY = {'open': 'todo', 'custom': 'in_progress', 'done': 'done', 'closed': 'done'}


class ClickUpConnector(Connector):
    tool = 'clickup'
    label = 'ClickUp'
    fields = [('token', 'API token', 'pk_...', 'ClickUp → Settings → Apps → API Token')]
    pick_label = 'Workspace › Space'
    about = 'Release / Sprint folders and lists → Releases and Sprints, task types or tags → Story / Bug / Epic.'

    def get(self, path):
        return self.http('GET', API + path, {'Authorization': self.connection.token.strip()})

    def list_projects(self):
        self.check_config()
        spaces = []
        for team in self.get('/team').get('teams', []):
            for space in self.get(f'/team/{team["id"]}/space?archived=false').get('spaces', []):
                spaces.append((f'{team["id"]}:{space["id"]}', f'{team["name"]} › {space["name"]}'))
        return spaces

    def fetch(self):
        self.check_config()
        try:
            team_id, space_id = self.connection.external_id.split(':')
        except ValueError:
            raise ConnectorError('Pick the ClickUp space again.')
        sprint_word = self.setting('sprint_keyword', 'sprint').lower()
        release_word = self.setting('release_keyword', 'release').lower()
        try:
            custom_types = {str(t['id']): t['name'] for t in self.get(f'/team/{team_id}/custom_item').get('custom_items', [])}
        except ConnectorError:
            custom_types = {}

        releases, sprints = [], []
        # list id -> (sprint external id, release external id) given to the tasks of that list
        list_links = {}

        def add_release(external_id, name, start, end, url=''):
            releases.append({'external_id': external_id, 'external_key': '', 'name': name,
                             'status': release_status(start, end), 'start_date': start, 'release_date': end,
                             'description': '', 'url': url})
            return external_id

        def add_sprint(lst, release=None):
            start, end = to_date(lst.get('start_date')), to_date(lst.get('due_date'))
            sprints.append({'external_id': f'list-{lst["id"]}', 'name': lst['name'],
                            'state': state_from_dates(start, end), 'start_date': start, 'end_date': end,
                            'goal': '', 'release': release, 'url': ''})
            return f'list-{lst["id"]}'

        folders = self.get(f'/space/{space_id}/folder?archived=false').get('folders', [])
        loose = self.get(f'/space/{space_id}/list?archived=false').get('lists', [])
        all_lists = [lst for folder in folders for lst in folder.get('lists', [])] + loose
        for folder in folders:
            name = folder['name'].lower()
            lists = folder.get('lists', [])
            if release_word in name:
                dates = [d for lst in lists for d in (to_date(lst.get('start_date')), to_date(lst.get('due_date'))) if d]
                release = add_release(f'folder-{folder["id"]}', folder['name'], min(dates, default=None),
                                      max(dates, default=None))
                for lst in lists:
                    sprint = add_sprint(lst, release) if sprint_word in lst['name'].lower() else None
                    list_links[lst['id']] = (sprint, release)
            elif sprint_word in name:
                for lst in lists:
                    list_links[lst['id']] = (add_sprint(lst), None)
        for lst in all_lists:
            if lst['id'] in list_links:
                continue
            name = lst['name'].lower()
            if sprint_word in name:
                list_links[lst['id']] = (add_sprint(lst), None)
            elif release_word in name:
                list_links[lst['id']] = (None, add_release(f'list-{lst["id"]}', lst['name'],
                                                           to_date(lst.get('start_date')), to_date(lst.get('due_date'))))
            else:
                list_links[lst['id']] = (None, None)

        members, items, dependencies, seen = {}, [], [], set()
        for lst in all_lists:
            sprint, release = list_links[lst['id']]
            page = 0
            while True:
                data = self.get(f'/list/{lst["id"]}/task?page={page}&subtasks=true&include_closed=true&archived=false')
                for task in data.get('tasks', []):
                    if task['id'] in seen:  # a task can live in several lists
                        continue
                    seen.add(task['id'])
                    items.append(self.item(task, sprint, release, custom_types, members))
                    for dep in task.get('dependencies') or []:
                        if dep.get('task_id') and dep.get('depends_on'):
                            dependencies.append({'source': dep['depends_on'], 'target': dep['task_id'], 'type': 'blocks'})
                if data.get('last_page', True) or not data.get('tasks'):
                    break
                page += 1
        return {'members': list(members.values()), 'releases': releases, 'sprints': sprints, 'items': items,
                'dependencies': dependencies}

    def item(self, task, sprint, release, custom_types, members):
        tags = [t['name'] for t in task.get('tags') or []]
        source_type = custom_types.get(str(task.get('custom_item_id') or ''), '')
        if not source_type:
            source_type = next((t for t in tags if guess_kind(t) != 'task'), 'Task')
        if task.get('parent') and source_type == 'Task':
            source_type = 'Subtask'
        assignees = task.get('assignees') or []
        for a in assignees:
            members[str(a['id'])] = {'external_id': str(a['id']), 'name': a.get('username') or a.get('email', ''),
                                     'email': a.get('email', '')}
        status = task.get('status') or {}
        return {
            'external_id': task['id'], 'external_key': task.get('custom_id') or task['id'],
            'source_type': source_type, 'title': task['name'], 'status': status.get('status', ''),
            'category': CATEGORY.get(status.get('type'), 'in_progress'),
            'priority': ((task.get('priority') or {}).get('priority') or '').title(), 'severity': '',
            'story_points': task.get('points'),
            'assignee': str(assignees[0]['id']) if assignees else None,
            'release': release, 'sprint': sprint, 'parent': task.get('parent'),
            'labels': ', '.join(tags),
            'created_on': to_date(task.get('date_created')), 'updated_on': to_date(task.get('date_updated')),
            'resolved_on': to_date(task.get('date_done') or task.get('date_closed')),
            'due_date': to_date(task.get('due_date')), 'url': task.get('url', ''),
        }


def release_status(start, end):
    return {'closed': 'Released', 'active': 'In Progress', 'future': 'Planned'}[state_from_dates(start, end)]
