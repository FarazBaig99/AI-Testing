"""Sync engine: fetch from the connector, validate, then create / update / mark removed in one transaction.

Running a sync twice never duplicates anything: records are matched on (connection, external_id).
Records that no longer come from the tool are marked `removed` (kept for history), not deleted.
Every created / updated / removed record is written to SyncChange with its old → new values.
"""
from django.db import transaction
from django.db.models import Model
from django.utils import timezone

from projects.models import ProjectActivity

from .connectors import ConnectorError, connector_for, guess_kind
from .models import Dependency, Release, Sprint, SyncChange, SyncRun, TeamMember, WorkItem

KIND_VALUES = {k for k, _ in WorkItem.KINDS}


def _show(value):
    if value is None:
        return ''
    if isinstance(value, Model):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


class Sync:
    def __init__(self, connection, user, http=None):
        self.connection = connection
        self.project = connection.project
        self.user = user
        self.connector = connector_for(connection, http=http)
        self.changes = []  # SyncChange rows, saved at the end
        self.counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'removed': 0}
        self.warnings = []

    def run(self):
        c = self.connection
        run = SyncRun.objects.create(connection=c, project=self.project, tool=c.tool, source_name=c.external_name,
                                     started_by=self.user)
        try:
            if not c.is_linked:
                raise ConnectorError('Pick the project to sync first.')
            data = self.connector.fetch()
            self.warnings = list(self.connector.warnings)
            data = self.validate(data)
            with transaction.atomic():
                self.save(data)
            run.status = 'Warning' if self.warnings else 'Success'
            run.summary = {name: len(rows) for name, rows in data.items()}
        except ConnectorError as e:
            run.status, run.error = 'Failed', str(e)
        except Exception as e:  # an unexpected answer from the tool must not break the page
            run.status, run.error = 'Failed', f'Unexpected error: {type(e).__name__}: {e}'
        run.finished_at = timezone.now()
        run.warnings = self.warnings[:100]
        for name, value in self.counts.items():
            setattr(run, name, value if run.status != 'Failed' else 0)
        run.save()
        if run.status != 'Failed':
            for change in self.changes:
                change.run = run
            SyncChange.objects.bulk_create(self.changes, batch_size=500)
        c.last_synced_at, c.last_status = run.finished_at, run.status
        c.save(update_fields=['last_synced_at', 'last_status'])
        message = (f'{c.get_tool_display()} sync of "{c.external_name}": {run.status}'
                   + (f' ({run.created} new, {run.updated} updated, {run.removed} removed)' if run.status != 'Failed' else ''))
        ProjectActivity.objects.create(project=self.project, user=self.user, category='delivery',
                                       message=message[:500], reason=run.error[:1000])
        return run

    # ---------- validation ----------
    def validate(self, data):
        problems = {}

        def problem(text):
            problems[text] = problems.get(text, 0) + 1

        clean = {}
        for name in ['members', 'releases', 'sprints', 'items', 'dependencies']:
            rows = data.get(name) or []
            if name == 'dependencies':
                clean[name] = rows
                continue
            label = 'title' if name == 'items' else 'name'
            kept, seen = [], set()
            for row in rows:
                if not row.get('external_id') or not str(row.get(label) or '').strip():
                    problem(f'{name}: skipped a record without id or {label}')
                elif row['external_id'] in seen:
                    problem(f'{name}: skipped a duplicate record')
                else:
                    seen.add(row['external_id'])
                    kept.append(row)
            clean[name] = kept

        for r in clean['releases']:
            if r.get('start_date') and r.get('release_date') and r['release_date'] < r['start_date']:
                problem(f'Release "{r["name"]}" ends before it starts')
        for s in clean['sprints']:
            if s.get('start_date') and s.get('end_date') and s['end_date'] < s['start_date']:
                problem(f'Sprint "{s["name"]}" ends before it starts')
        ids = {name: {r['external_id'] for r in clean[name]} for name in ['members', 'releases', 'sprints', 'items']}
        for s in clean['sprints']:
            if s.get('release') and s['release'] not in ids['releases']:
                s['release'] = None
        for item in clean['items']:
            for field, pool in [('sprint', 'sprints'), ('release', 'releases'), ('assignee', 'members'),
                                ('parent', 'items')]:
                if item.get(field) and item[field] not in ids[pool]:
                    if field == 'parent':
                        problem('items: parent outside the synced project was left empty')
                    item[field] = None
            if item.get('story_points') is not None:
                try:
                    item['story_points'] = float(item['story_points'])
                except (TypeError, ValueError):
                    problem('items: story points that are not numbers were left empty')
                    item['story_points'] = None
        clean['dependencies'] = [d for d in clean['dependencies']
                                 if d['source'] in ids['items'] and d['target'] in ids['items'] and d['source'] != d['target']]
        self.warnings += [f'{text} ({n}×)' if n > 1 else text for text, n in problems.items()]
        return clean

    # ---------- saving ----------
    def kind(self, source_type):
        mapped = self.connection.mapping.get('types', {}).get((source_type or '').strip().lower())
        return mapped if mapped in KIND_VALUES else guess_kind(source_type)

    def upsert(self, model, entity, rows, values):
        """values(row) -> model field values. Returns {external_id: instance} of the rows."""
        existing = {o.external_id: o for o in model.objects.filter(connection=self.connection)}
        result = {}
        for row in rows:
            new = values(row)
            obj = existing.pop(row['external_id'], None)
            if obj is None:
                obj = model.objects.create(connection=self.connection, project=self.project,
                                           external_id=row['external_id'], **new)
                self.record(entity, obj, 'created')
            else:
                diff = {f: [_show(getattr(obj, f)), _show(v)] for f, v in new.items() if getattr(obj, f) != v}
                if obj.removed:
                    diff['removed'] = ['yes', 'no']
                    obj.removed = False
                if diff:
                    for f, v in new.items():
                        setattr(obj, f, v)
                    obj.save()
                    self.record(entity, obj, 'updated', diff)
                else:
                    self.counts['unchanged'] += 1
            result[row['external_id']] = obj
        for obj in existing.values():
            if not obj.removed:
                obj.removed = True
                obj.save(update_fields=['removed'])
                self.record(entity, obj, 'removed')
        return result

    def record(self, entity, obj, change, fields=None):
        self.counts[change] += 1
        self.changes.append(SyncChange(entity=entity, external_key=getattr(obj, 'external_key', '') or '',
                                       title=str(getattr(obj, 'title', '') or obj.name)[:500], change=change,
                                       fields=fields or {}))

    def save(self, data):
        members = self.upsert(TeamMember, 'Team member', data['members'], lambda r: {
            'name': r['name'][:200], 'email': (r.get('email') or '')[:200]})
        releases = self.upsert(Release, 'Release', data['releases'], lambda r: {
            'name': r['name'][:300], 'external_key': (r.get('external_key') or '')[:100],
            'status': r.get('status') or 'Planned', 'start_date': r.get('start_date'),
            'release_date': r.get('release_date'), 'description': r.get('description') or '', 'url': r.get('url') or ''})
        sprints = self.upsert(Sprint, 'Sprint', data['sprints'], lambda r: {
            'name': r['name'][:300], 'state': r.get('state') or 'future', 'start_date': r.get('start_date'),
            'end_date': r.get('end_date'), 'goal': r.get('goal') or '', 'url': r.get('url') or '',
            'release': releases.get(r.get('release'))})
        parents = {r['external_id']: r.get('parent') for r in data['items']}
        known = {o.external_id: o for o in WorkItem.objects.filter(connection=self.connection)}
        items = self.upsert(WorkItem, 'Work item', data['items'], lambda r: {
            'kind': self.kind(r.get('source_type')), 'source_type': (r.get('source_type') or '')[:100],
            'external_key': (r.get('external_key') or '')[:100], 'title': r['title'][:500],
            'status': (r.get('status') or '')[:100],
            'category': r.get('category') if r.get('category') in ('todo', 'in_progress', 'done') else 'todo',
            'priority': (r.get('priority') or '')[:50], 'severity': (r.get('severity') or '')[:50],
            'story_points': r.get('story_points'), 'assignee': members.get(r.get('assignee')),
            'release': releases.get(r.get('release')), 'sprint': sprints.get(r.get('sprint')),
            'labels': (r.get('labels') or '')[:500], 'created_on': r.get('created_on'),
            'updated_on': r.get('updated_on'), 'resolved_on': r.get('resolved_on'), 'due_date': r.get('due_date'),
            'url': (r.get('url') or '')[:500],
            # parent is set below, once every item exists; the old one is kept here so it is not seen as a change
            'parent': known[r['external_id']].parent if r['external_id'] in known else None})

        by_key = {(c.entity, c.title, c.external_key): c for c in self.changes if c.entity == 'Work item'}
        for ext, obj in items.items():
            parent = items.get(parents.get(ext))
            if obj.parent_id != (parent.pk if parent else None):
                old = obj.parent
                obj.parent = parent
                obj.save(update_fields=['parent'])
                change = by_key.get(('Work item', str(obj.title)[:500], obj.external_key or ''))
                if change is None:
                    self.counts['unchanged'] -= 1
                    self.record('Work item', obj, 'updated', {'parent': [_show(old), _show(parent)]})
                elif change.change == 'updated':
                    change.fields['parent'] = [_show(old), _show(parent)]

        Dependency.objects.filter(connection=self.connection).delete()
        unique = {(d['source'], d['target'], d.get('type') or 'blocks') for d in data['dependencies']}
        Dependency.objects.bulk_create([
            Dependency(connection=self.connection, project=self.project, source=items[s], target=items[t],
                       type=k if k in ('blocks', 'relates') else 'relates') for s, t, k in unique])


def run_sync(connection, user, http=None):
    return Sync(connection, user, http=http).run()
