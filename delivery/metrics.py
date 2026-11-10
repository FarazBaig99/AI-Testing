"""Layer 3, delivery intelligence: progress, velocity, bug trend, QA readiness and rule-based insights."""
from datetime import date, timedelta

from django.db.models import Count, Q, Sum

from .models import Dependency, Release, Sprint, WorkItem

LIVE = Q(items__removed=False)
OPEN_BUG = Q(kind='bug') & ~Q(category='done')
SERIOUS = Q(severity__in=['Critical', 'Blocker', 'High', 'Highest', 'Major']) | Q(priority__in=['Highest', 'Urgent', 'Critical', 'Blocker'])


def items(project):
    return WorkItem.objects.filter(project=project, removed=False)


def pct(part, whole):
    return round(part * 100 / whole) if whole else None


def _annotate(qs):
    return qs.annotate(
        n_items=Count('items', filter=LIVE & ~Q(items__kind='epic'), distinct=True),
        n_done=Count('items', filter=LIVE & ~Q(items__kind='epic') & Q(items__category='done'), distinct=True),
        n_stories=Count('items', filter=LIVE & Q(items__kind='story'), distinct=True),
        n_stories_done=Count('items', filter=LIVE & Q(items__kind='story', items__category='done'), distinct=True),
        n_verified=Count('items', filter=LIVE & Q(items__kind='story', items__qa_status='Passed'), distinct=True),
        n_bugs=Count('items', filter=LIVE & Q(items__kind='bug'), distinct=True),
        n_bugs_open=Count('items', filter=LIVE & Q(items__kind='bug') & ~Q(items__category='done'), distinct=True),
        points=Sum('items__story_points', filter=LIVE),
        points_done=Sum('items__story_points', filter=LIVE & Q(items__category='done')),
    )


def _progress(obj):
    obj.progress = pct(obj.n_done, obj.n_items)
    return obj


def releases(project):
    return [_progress(r) for r in _annotate(Release.objects.filter(project=project, removed=False))]


def sprints(project):
    today = date.today()
    result = []
    for s in _annotate(Sprint.objects.filter(project=project, removed=False).select_related('release', 'connection')):
        _progress(s)
        s.days_left = (s.end_date - today).days if s.end_date and s.state == 'active' else None
        if s.start_date and s.end_date and s.end_date > s.start_date:
            elapsed = (min(max(today, s.start_date), s.end_date) - s.start_date).days
            s.time_pct = round(elapsed * 100 / (s.end_date - s.start_date).days)
        else:
            s.time_pct = None
        # work done measured by points when the sprint is estimated, else by item count
        s.done_pct = pct(s.points_done or 0, s.points) if s.points else s.progress
        result.append(s)
    return result


def qa_verification(project):
    """Share of finished user stories QA has passed; Failed / Blocked stories are the open QA issues."""
    stories = items(project).filter(kind='story')
    done = stories.filter(category='done').count()
    counts = dict(stories.values_list('qa_status').annotate(n=Count('id')).order_by())
    return {'rate': pct(stories.filter(category='done', qa_status='Passed').count(), done), 'done': done,
            'passed': counts.get('Passed', 0), 'failed': counts.get('Failed', 0), 'blocked': counts.get('Blocked', 0)}


def sprint_detail(project, sprint):
    """Everything of one sprint as a tree: each user story with its tasks, subtasks and bugs below it.

    The sprint's scope is its own items plus every descendant of its stories (subtasks often have no sprint
    of their own in the tools). Items of the sprint that are not under one of its stories are listed apart.
    """
    live = list(items(project).select_related('assignee', 'parent', 'qa_by'))
    children = {}
    for i in live:
        if i.parent_id:
            children.setdefault(i.parent_id, []).append(i)
    in_sprint = [i for i in live if i.sprint_id == sprint.pk and i.kind != 'epic']
    stories = [i for i in in_sprint if i.kind == 'story']

    def below(item, depth=1):
        rows = []
        for child in sorted(children.get(item.pk, []), key=lambda c: (c.kind != 'bug', c.external_key)):
            child.depth = depth
            rows.append(child)
            rows += below(child, depth + 1)
        return rows

    tree, placed = [], set()
    for story in stories:
        story.rows = below(story)
        placed.add(story.pk)
        placed.update(r.pk for r in story.rows)
        tree.append(story)
    loose = [i for i in in_sprint if i.pk not in placed]
    scope = tree + [r for s in tree for r in s.rows] + loose
    return {'tree': tree, 'other_tasks': [i for i in loose if i.kind != 'bug'],
            'other_bugs': [i for i in loose if i.kind == 'bug'], 'scope': scope}


def current_sprint(sprint_list):
    active = [s for s in sprint_list if s.state == 'active']
    return active[0] if active else None


def timeline(release_list, sprint_list):
    """Bars positioned in % of the span from the first start to the last end, plus today's position."""
    rows = [('release', r, r.start_date or r.release_date, r.release_date) for r in release_list] + \
           [('sprint', s, s.start_date, s.end_date) for s in sprint_list]
    rows = [(kind, obj, start, end or start) for kind, obj, start, end in rows if start]
    if not rows:
        return None
    first = min(start for _, _, start, _ in rows)
    last = max(end for _, _, _, end in rows)
    span = max((last - first).days, 1)
    bars = [{'kind': kind, 'obj': obj, 'start': start, 'end': end,
             'left': (start - first).days * 100 / span,
             'width': max((end - start).days * 100 / span, 1.2)} for kind, obj, start, end in rows]
    today = date.today()
    return {'bars': bars, 'first': first, 'last': last,
            'today': (today - first).days * 100 / span if first <= today <= last else None}


def velocity(sprint_list):
    """Committed vs completed story points (or items when nothing is estimated) per started sprint."""
    started = [s for s in sprint_list if s.state in ('closed', 'active')][-8:]
    by_points = any(s.points for s in started)
    rows = [{'sprint': s, 'committed': (s.points or 0) if by_points else s.n_items,
             'completed': (s.points_done or 0) if by_points else s.n_done} for s in started]
    closed = [r['completed'] for r, s in zip(rows, started) if s.state == 'closed']
    _heights(rows, 'committed', 'completed')
    return {'rows': rows, 'unit': 'points' if by_points else 'items',
            'average': round(sum(closed) / len(closed), 1) if closed else None}


def _heights(rows, *keys):
    """Bar heights in % of the largest value, for the CSS bar charts."""
    top = max([r[k] for r in rows for k in keys] + [1])
    for r in rows:
        for k in keys:
            r[f'{k}_h'] = round(r[k] * 100 / top, 1)


def bug_trend(project, sprint_list):
    """Bugs reported vs resolved inside each started sprint's dates."""
    bugs = list(items(project).filter(kind='bug').values('created_on', 'resolved_on'))
    rows = []
    for s in [s for s in sprint_list if s.state in ('closed', 'active') and s.start_date and s.end_date][-8:]:
        within = lambda d: d and s.start_date <= d <= s.end_date
        rows.append({'sprint': s, 'created': sum(1 for b in bugs if within(b['created_on'])),
                     'resolved': sum(1 for b in bugs if within(b['resolved_on']))})
    _heights(rows, 'created', 'resolved')
    return {'rows': rows}


def readiness(project, release):
    """QA readiness of one release: 100 when every story is done, no serious bug is open and nothing is blocked."""
    rel_items = items(project).filter(release=release)
    stories = rel_items.filter(kind='story')
    bugs = rel_items.filter(kind='bug')
    n_stories, n_stories_done = stories.count(), stories.filter(category='done').count()
    serious_open = bugs.filter(OPEN_BUG & SERIOUS).count()
    bugs_open = bugs.filter(OPEN_BUG).count()
    blocked = Dependency.objects.filter(project=project, type='blocks', target__release=release, target__removed=False,
                                        source__removed=False).exclude(source__category='done').count()
    checks = [
        ('Stories completed', f'{n_stories_done}/{n_stories}', pct(n_stories_done, n_stories) or 0, 50),
        ('No open critical / high bugs', f'{serious_open} open', 100 if serious_open == 0 else 0, 25),
        ('Bugs closed', f'{bugs.count() - bugs_open}/{bugs.count()}', pct(bugs.count() - bugs_open, bugs.count()) if bugs.count() else 100, 15),
        ('No blocked items', f'{blocked} blocked', 100 if blocked == 0 else 0, 10),
    ]
    score = round(sum(value * weight for _, _, value, weight in checks) / 100)
    level = 'Ready' if score >= 90 else 'Almost ready' if score >= 70 else 'Not ready'
    css = 'success' if score >= 90 else 'warning' if score >= 70 else 'danger'
    return {'release': release, 'score': score, 'level': level, 'css': css,
            'checks': [{'label': l, 'value': v, 'ok': s == 100} for l, v, s, _ in checks]}


def workload(project):
    rows = list(items(project).exclude(kind='epic').filter(assignee__isnull=False).values(
        'assignee', 'assignee__name').annotate(
        open=Count('id', filter=~Q(category='done')),
        in_progress=Count('id', filter=Q(category='in_progress')),
        done=Count('id', filter=Q(category='done')),
        open_points=Sum('story_points', filter=~Q(category='done')),
        bugs=Count('id', filter=OPEN_BUG),
        active=Count('id', filter=Q(sprint__state='active') & ~Q(category='done')),
    ).order_by('-open', 'assignee__name'))
    average = sum(r['open'] for r in rows) / len(rows) if rows else 0
    most = max([r['open'] for r in rows] + [1])
    for r in rows:
        r['load'] = round(r['open'] * 100 / most)
        r['overloaded'] = r['open'] >= 5 and r['open'] > average * 1.5
    unassigned = items(project).exclude(kind='epic').filter(assignee__isnull=True).exclude(category='done').count()
    return {'rows': rows, 'average': round(average, 1), 'unassigned': unassigned}


def insights(project, connections, release_list, sprint_list):
    """Rule-based delivery risks, most serious first. level: danger / warning / info."""
    found = []
    today = date.today()
    add = lambda level, title, detail, section: found.append(
        {'level': level, 'title': title, 'detail': detail, 'section': section})
    live = items(project)

    sprint = current_sprint(sprint_list)
    if sprint and sprint.time_pct is not None and sprint.done_pct is not None:
        gap = sprint.time_pct - sprint.done_pct
        if gap >= 20:
            add('danger' if gap >= 40 else 'warning', f'{sprint.name} is behind schedule',
                f'{sprint.time_pct}% of the time has passed but only {sprint.done_pct}% of the work is done.', 'sprints')
    for s in sprint_list:
        if s.state == 'closed' and s.n_items and s.n_done < s.n_items:
            add('info', f'{s.n_items - s.n_done} unfinished item(s) in closed {s.name}',
                'They were carried over or left open; review them in the next planning.', 'sprints')
    for r in release_list:
        if r.status in ('Released', 'Archived') or not r.release_date or not r.n_items:
            continue
        days = (r.release_date - today).days
        if days <= 14 and (r.progress or 0) < 80:
            when = f'was due {-days} day(s) ago' if days < 0 else f'is due in {days} day(s)'
            add('danger' if days < 7 else 'warning', f'{r.name} at risk',
                f'The release {when} and only {r.progress or 0}% of its work is done.', 'releases')
    serious = live.filter(OPEN_BUG & SERIOUS).count()
    if serious:
        add('danger', f'{serious} critical / high bug(s) open', 'Fix or re-triage them before the next release.', 'bugs')
    unestimated = live.filter(kind='story', story_points__isnull=True, sprint__state__in=['active', 'future']).count()
    if unestimated:
        add('warning', f'{unestimated} planned stor(y/ies) without story points',
            'Estimates are needed for reliable velocity and sprint planning.', 'stories')
    blocked = Dependency.objects.filter(project=project, type='blocks', target__sprint__state='active',
                                        target__removed=False, source__removed=False).exclude(source__category='done')
    if blocked.exists():
        add('warning', f'{blocked.count()} item(s) in the running sprint are blocked',
            'Another item that blocks them is not done yet.', 'milestones')
    for row in workload(project)['rows']:
        if row['overloaded']:
            add('warning', f'{row["assignee__name"]} may be overloaded',
                f'{row["open"]} open items, well above the team average.', 'team')
    unassigned = live.filter(sprint__state='active', assignee__isnull=True).exclude(kind='epic').exclude(category='done').count()
    if unassigned:
        add('info', f'{unassigned} item(s) in the running sprint have no assignee', 'Assign an owner for each.', 'tasks')
    backlog = live.filter(kind='story', sprint__isnull=True).exclude(category='done').count()
    if backlog:
        add('info', f'{backlog} stor(y/ies) in the backlog', 'Not planned in any sprint yet.', 'stories')

    if not connections:
        add('info', 'No tool connected', 'Connect Jira, ClickUp or the Demo Source to start syncing.', 'integration')
    for c in connections:
        if c.last_status == 'Failed':
            add('danger', f'Last {c.get_tool_display()} sync failed', 'Open Sync History for the error.', 'history')
        elif c.is_linked and (not c.last_synced_at or c.last_synced_at.date() < today - timedelta(days=7)):
            add('warning', f'{c.get_tool_display()} data is not fresh',
                'Not synced in the last 7 days.' if c.last_synced_at else 'Never synced yet.', 'integration')
    order = {'danger': 0, 'warning': 1, 'info': 2}
    return sorted(found, key=lambda f: order[f['level']])
