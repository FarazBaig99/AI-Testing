"""Demo Source: a sample project (3 releases, 6 sprints, epics, stories, tasks, bugs) with dates around today,
so the delivery screens can be seen without any tool account. Every sync moves a few items forward,
so the sync history shows real updates.
"""
from datetime import date, timedelta

from .base import Connector, state_from_dates

PEOPLE = [('ayesha', 'Ayesha Malik'), ('bilal', 'Bilal Ahmed'), ('sana', 'Sana Raza'), ('usman', 'Usman Tariq'),
          ('hina', 'Hina Shah')]
EPICS = ['Customer Onboarding', 'Payments & Billing', 'Reporting Dashboard', 'Mobile Experience']
STORIES = ['Sign up with email', 'Verify phone number', 'Upload KYC documents', 'Card payment checkout',
           'Refund a payment', 'Monthly invoice PDF', 'Sales summary widget', 'Export report to Excel',
           'Filter report by date', 'Push notifications', 'Offline mode for orders', 'Dark mode support']
BUGS = [('Login fails with special characters in password', 'Critical'), ('Invoice total rounds wrongly', 'High'),
        ('Report export times out above 10k rows', 'High'), ('Typo on the checkout page', 'Low'),
        ('Push notification shown twice', 'Medium'), ('Date filter ignores time zone', 'Medium'),
        ('Orders lost when the app goes offline', 'High')]
# the story (index in STORIES) each bug was raised against; None = a stand-alone bug in Sprint 3
BUG_STORY = [0, 5, 7, None, 9, 8, 10]
STATUS = {'todo': 'To Do', 'in_progress': 'In Progress', 'done': 'Done'}


class DemoConnector(Connector):
    tool = 'demo'
    label = 'Demo Source'
    pick_label = 'Sample project'
    about = 'Sample data to try Project Delivery without a tool account.'

    def list_projects(self):
        return [('demo-shop', 'Demo Shop (sample)')]

    def fetch(self):
        today = date.today()
        rounds = self.connection.runs.filter(status__in=['Success', 'Warning']).count()
        sprint_start = today - timedelta(days=14 * 4 + 5)  # sprint 5 of 6 is running today
        sprints, releases = [], []
        for n in range(6):
            start = sprint_start + timedelta(days=14 * n)
            end = start + timedelta(days=13)
            sprints.append({'external_id': f's{n + 1}', 'name': f'Sprint {n + 1}', 'state': state_from_dates(start, end, today),
                            'start_date': start, 'end_date': end, 'goal': f'Sprint {n + 1} goal',
                            'release': f'r{n // 2 + 1}', 'url': ''})
        for n in range(3):
            start, end = sprints[n * 2]['start_date'], sprints[n * 2 + 1]['end_date']
            status = 'Released' if end < today else ('In Progress' if start <= today else 'Planned')
            releases.append({'external_id': f'r{n + 1}', 'external_key': '', 'name': f'Release {n + 1}.0',
                             'status': status, 'start_date': start, 'release_date': end,
                             'description': f'Release {n + 1}.0 of Demo Shop', 'url': ''})

        items = []

        def add(eid, source_type, title, sprint_no, person, points=None, parent=None, severity='', priority='Medium'):
            sprint = sprints[sprint_no] if sprint_no is not None else None
            # finished sprints are done; the running one advances with every sync; future ones are open
            if sprint is None or sprint['state'] == 'future':
                category = 'todo'
            elif sprint['state'] == 'closed':
                category = 'done' if len(items) % 9 else 'in_progress'  # a few carried-over items
            else:
                step = (len(items) + rounds) % 4
                category = 'done' if step < min(1 + rounds, 3) else ('in_progress' if step < 3 else 'todo')
            resolved = (min(sprint['end_date'], today) if category == 'done' and sprint else None)
            items.append({
                'external_id': eid, 'external_key': f'DEMO-{len(items) + 1}', 'source_type': source_type,
                'title': title, 'status': STATUS[category], 'category': category, 'priority': priority,
                'severity': severity, 'story_points': points, 'assignee': person,
                'release': sprint['release'] if sprint else None, 'sprint': sprint['external_id'] if sprint else None,
                'parent': parent, 'labels': '', 'created_on': min(sprint['start_date'] if sprint else today, today) - timedelta(days=3),
                'updated_on': today, 'resolved_on': resolved, 'due_date': sprint['end_date'] if sprint else None,
                'url': '',
            })

        for n, epic in enumerate(EPICS):
            add(f'e{n}', 'Epic', epic, None, PEOPLE[n][0], priority='High')
        for n, story in enumerate(STORIES):
            sprint_no = n // 2
            add(f'st{n}', 'Story', story, sprint_no, PEOPLE[n % 5][0], points=[3, 5, 8, 2, 5, 3][n % 6], parent=f'e{n // 3}')
            add(f't{n}a', 'Task', f'Build API for: {story.lower()}', sprint_no, PEOPLE[(n + 1) % 5][0], parent=f'st{n}')
            add(f't{n}b', 'Sub-task', f'Write tests for: {story.lower()}', sprint_no, PEOPLE[(n + 2) % 5][0], parent=f't{n}a')
        for n, (bug, severity) in enumerate(BUGS):
            story = BUG_STORY[n]
            add(f'b{n}', 'Bug', bug, story // 2 if story is not None else 2, PEOPLE[(n + 3) % 5][0], severity=severity,
                parent=f'st{story}' if story is not None else None,
                priority='Highest' if severity == 'Critical' else 'High' if severity == 'High' else 'Medium')
        add('st-backlog', 'Story', 'Loyalty points program', None, None, points=13, parent='e1')

        return {
            'members': [{'external_id': key, 'name': name, 'email': f'{key}@demo.test'} for key, name in PEOPLE],
            'releases': releases, 'sprints': sprints, 'items': items,
            'dependencies': [{'source': 'st3', 'target': 'st4', 'type': 'blocks'},
                             {'source': 'st0', 'target': 'st2', 'type': 'blocks'},
                             {'source': 'st8', 'target': 'st9', 'type': 'blocks'},
                             {'source': 'b2', 'target': 'st7', 'type': 'blocks'},
                             {'source': 'st10', 'target': 'st11', 'type': 'blocks'},
                             {'source': 'st6', 'target': 'st7', 'type': 'relates'}],
        }
