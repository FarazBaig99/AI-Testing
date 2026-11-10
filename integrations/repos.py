"""Repository connectors: each reads one repository of its provider into the same normalized shape.

    {'name', 'default_branch',
     'deployments':   [{environment_name, ref, sha, status, deployed_at, deployed_by, url}],
     'pull_requests': [{number, title, state (open/merged/closed), author, source_branch, target_branch, url,
                        created_on, updated_on, merged_on, additions, deletions, files: [{path, status, ...}]}],
     'pipelines':     [{name, status, branch, run_at, url}]}

`http` is injectable (same signature as delivery's http_json) so tests use fake API answers.
"""
import re
from datetime import timedelta, timezone as dt_timezone
from urllib.parse import quote, urlsplit

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from delivery.connectors.base import ConnectorError, basic_auth, http_json

PR_LIMIT = 15
FILE_LIMIT = 100


def when(value):
    if not value:
        return None
    parsed = parse_datetime(str(value).replace('Z', '+00:00'))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def strip_ref(ref):
    return re.sub(r'^refs/(heads|tags)/', '', ref or '')


class RepoConnector:
    provider = ''
    label = ''
    example = ''
    token_help = ''
    do_not_call_in_templates = True

    def __init__(self, repository, http=None):
        self.repo = repository
        self.http = http or http_json
        self.warnings = []

    def get(self, url):
        return self.http('GET', url, headers=self.headers())

    def headers(self):
        return {}

    def fetch(self):
        raise NotImplementedError


class GitHub(RepoConnector):
    provider = 'github'
    label = 'GitHub'
    example = 'https://github.com/owner/repo'
    token_help = 'Personal access token with repo (read) and actions (read) access.'

    def __init__(self, repository, http=None):
        super().__init__(repository, http)
        parts = urlsplit(repository.url.strip())
        path = parts.path.strip('/').removesuffix('.git').split('/')
        if not parts.hostname or len(path) < 2:
            raise ConnectorError(f'Use a repository URL like {self.example}')
        self.full = f'{path[0]}/{path[1]}'
        host = parts.hostname
        default_api = 'https://api.github.com' if host == 'github.com' else f'{parts.scheme}://{host}/api/v3'
        self.api = f'{(repository.api_url or default_api).rstrip("/")}/repos/{self.full}'

    def headers(self):
        h = {'Accept': 'application/vnd.github+json'}
        if self.repo.token:
            h['Authorization'] = f'Bearer {self.repo.token}'
        return h

    def fetch(self):
        info = self.get(self.api)
        result = {'name': info.get('full_name') or self.full, 'default_branch': info.get('default_branch', ''),
                  'deployments': [], 'pull_requests': [], 'pipelines': []}
        latest = {}
        for d in self.get(f'{self.api}/deployments?per_page=50') or []:
            latest.setdefault(d.get('environment') or 'unknown', d)
        for env, d in latest.items():
            statuses = self.get(f'{self.api}/deployments/{d["id"]}/statuses?per_page=1') or []
            status = statuses[0] if statuses else {}
            result['deployments'].append({
                'environment_name': env, 'ref': d.get('ref', ''), 'sha': d.get('sha', ''),
                'status': status.get('state', 'created'), 'deployed_at': when(status.get('created_at') or d.get('created_at')),
                'deployed_by': (d.get('creator') or {}).get('login', ''),
                'url': status.get('environment_url') or status.get('target_url') or ''})
        for pr in (self.get(f'{self.api}/pulls?state=all&sort=updated&direction=desc&per_page={PR_LIMIT}') or [])[:PR_LIMIT]:
            files = self.get(f'{self.api}/pulls/{pr["number"]}/files?per_page={FILE_LIMIT}') or []
            result['pull_requests'].append({
                'number': str(pr['number']), 'title': pr.get('title', ''),
                'state': 'merged' if pr.get('merged_at') else ('open' if pr.get('state') == 'open' else 'closed'),
                'author': (pr.get('user') or {}).get('login', ''),
                'source_branch': (pr.get('head') or {}).get('ref', ''), 'target_branch': (pr.get('base') or {}).get('ref', ''),
                'url': pr.get('html_url', ''), 'created_on': when(pr.get('created_at')),
                'updated_on': when(pr.get('updated_at')), 'merged_on': when(pr.get('merged_at')),
                'additions': sum(f.get('additions', 0) for f in files), 'deletions': sum(f.get('deletions', 0) for f in files),
                'files': [{'path': f.get('filename', ''), 'status': f.get('status', ''), 'additions': f.get('additions', 0),
                           'deletions': f.get('deletions', 0)} for f in files[:FILE_LIMIT]]})
        try:
            runs = (self.get(f'{self.api}/actions/runs?per_page=50') or {}).get('workflow_runs', [])
        except ConnectorError as e:
            runs = []
            self.warnings.append(f'Pipelines not read: {e}')
        seen = set()
        for run in runs:
            if run.get('name') in seen:
                continue
            seen.add(run.get('name'))
            result['pipelines'].append({'name': run.get('name', ''), 'status': run.get('conclusion') or run.get('status', ''),
                                        'branch': run.get('head_branch', ''), 'run_at': when(run.get('created_at')),
                                        'url': run.get('html_url', '')})
        return result


class GitLab(RepoConnector):
    provider = 'gitlab'
    label = 'GitLab'
    example = 'https://gitlab.com/group/project'
    token_help = 'Personal or project access token with read_api scope.'

    def __init__(self, repository, http=None):
        super().__init__(repository, http)
        parts = urlsplit(repository.url.strip())
        path = parts.path.strip('/').removesuffix('.git')
        if not parts.hostname or '/' not in path:
            raise ConnectorError(f'Use a project URL like {self.example}')
        self.path = path.split('/-/')[0]
        base = repository.api_url or f'{parts.scheme}://{parts.netloc}'
        self.api = f'{base.rstrip("/")}/api/v4/projects/{quote(self.path, safe="")}'

    def headers(self):
        return {'PRIVATE-TOKEN': self.repo.token} if self.repo.token else {}

    def fetch(self):
        info = self.get(self.api)
        result = {'name': info.get('path_with_namespace') or self.path, 'default_branch': info.get('default_branch', ''),
                  'deployments': [], 'pull_requests': [], 'pipelines': []}
        latest = {}
        for d in self.get(f'{self.api}/deployments?order_by=updated_at&sort=desc&per_page=50') or []:
            latest.setdefault((d.get('environment') or {}).get('name', 'unknown'), d)
        for env, d in latest.items():
            result['deployments'].append({
                'environment_name': env, 'ref': d.get('ref', ''), 'sha': d.get('sha', ''), 'status': d.get('status', ''),
                'deployed_at': when(d.get('updated_at') or d.get('created_at')),
                'deployed_by': (d.get('user') or {}).get('username', ''),
                'url': (d.get('environment') or {}).get('external_url') or ''})
        states = {'opened': 'open', 'merged': 'merged'}
        for mr in (self.get(f'{self.api}/merge_requests?state=all&order_by=updated_at&per_page={PR_LIMIT}') or [])[:PR_LIMIT]:
            changes = (self.get(f'{self.api}/merge_requests/{mr["iid"]}/changes') or {}).get('changes', [])
            result['pull_requests'].append({
                'number': str(mr['iid']), 'title': mr.get('title', ''), 'state': states.get(mr.get('state'), 'closed'),
                'author': (mr.get('author') or {}).get('username', ''), 'source_branch': mr.get('source_branch', ''),
                'target_branch': mr.get('target_branch', ''), 'url': mr.get('web_url', ''),
                'created_on': when(mr.get('created_at')), 'updated_on': when(mr.get('updated_at')),
                'merged_on': when(mr.get('merged_at')), 'additions': 0, 'deletions': 0,
                'files': [{'path': c.get('new_path', ''), 'additions': 0, 'deletions': 0,
                           'status': 'added' if c.get('new_file') else 'removed' if c.get('deleted_file')
                           else 'renamed' if c.get('renamed_file') else 'modified'} for c in changes[:FILE_LIMIT]]})
        seen = set()
        for p in self.get(f'{self.api}/pipelines?per_page=30') or []:
            if p.get('ref') in seen:
                continue
            seen.add(p.get('ref'))
            result['pipelines'].append({'name': f'Pipeline · {p.get("ref", "")}', 'status': p.get('status', ''),
                                        'branch': p.get('ref', ''), 'run_at': when(p.get('updated_at')),
                                        'url': p.get('web_url', '')})
        return result


class AzureRepos(RepoConnector):
    provider = 'azure'
    label = 'Azure DevOps'
    example = 'https://dev.azure.com/organization/project/_git/repository'
    token_help = 'Personal access token with Code (read), Build (read) and Environment (read) scopes.'
    VERSION = 'api-version=7.0'

    def __init__(self, repository, http=None):
        super().__init__(repository, http)
        parts = urlsplit(repository.url.strip())
        path = [p for p in parts.path.split('/') if p]
        if parts.hostname and parts.hostname.endswith('visualstudio.com'):
            path = [parts.hostname.split('.')[0]] + path
        if '_git' not in path or path.index('_git') < 2 or len(path) <= path.index('_git') + 1:
            raise ConnectorError(f'Use a repository URL like {self.example}')
        i = path.index('_git')
        self.org, self.project, self.name = path[0], path[i - 1], path[i + 1]
        self.base = (repository.api_url or f'https://dev.azure.com/{self.org}').rstrip('/')
        self.api = f'{self.base}/{quote(self.project)}/_apis'
        self.web = f'{self.base}/{quote(self.project)}/_git/{quote(self.name)}'

    def headers(self):
        return {'Authorization': basic_auth('', self.repo.token)} if self.repo.token else {}

    def fetch(self):
        info = self.get(f'{self.api}/git/repositories/{quote(self.name)}?{self.VERSION}')
        repo_id = info.get('id', self.name)
        result = {'name': f'{self.project}/{info.get("name", self.name)}', 'default_branch': strip_ref(info.get('defaultBranch')),
                  'deployments': [], 'pull_requests': [], 'pipelines': []}
        states = {'active': 'open', 'completed': 'merged'}
        prs = self.get(f'{self.api}/git/repositories/{repo_id}/pullrequests?searchCriteria.status=all'
                       f'&$top={PR_LIMIT}&{self.VERSION}').get('value', [])
        for pr in prs[:PR_LIMIT]:
            pr_id = pr['pullRequestId']
            files = []
            iterations = self.get(f'{self.api}/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations?{self.VERSION}').get('value', [])
            if iterations:
                changes = self.get(f'{self.api}/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations/'
                                   f'{iterations[-1]["id"]}/changes?{self.VERSION}').get('changeEntries', [])
                files = [{'path': (c.get('item') or {}).get('path', ''), 'status': c.get('changeType', ''),
                          'additions': 0, 'deletions': 0} for c in changes[:FILE_LIMIT]]
            closed = when(pr.get('closedDate'))
            result['pull_requests'].append({
                'number': str(pr_id), 'title': pr.get('title', ''), 'state': states.get(pr.get('status'), 'closed'),
                'author': (pr.get('createdBy') or {}).get('displayName', ''),
                'source_branch': strip_ref(pr.get('sourceRefName')), 'target_branch': strip_ref(pr.get('targetRefName')),
                'url': f'{self.web}/pullrequest/{pr_id}', 'created_on': when(pr.get('creationDate')),
                'updated_on': closed or when(pr.get('creationDate')),
                'merged_on': closed if pr.get('status') == 'completed' else None,
                'additions': 0, 'deletions': 0, 'files': files})
        try:
            builds = self.get(f'{self.api}/build/builds?$top=30&repositoryId={repo_id}&repositoryType=TfsGit'
                              f'&{self.VERSION}').get('value', [])
        except ConnectorError as e:
            builds = []
            self.warnings.append(f'Pipelines not read: {e}')
        seen = set()
        for b in builds:
            name = (b.get('definition') or {}).get('name', '')
            if name in seen:
                continue
            seen.add(name)
            result['pipelines'].append({'name': name, 'status': b.get('result') or b.get('status', ''),
                                        'branch': strip_ref(b.get('sourceBranch')),
                                        'run_at': when(b.get('finishTime') or b.get('queueTime')),
                                        'url': ((b.get('_links') or {}).get('web') or {}).get('href', '')})
        try:
            envs = self.get(f'{self.api}/distributedtask/environments?api-version=7.1-preview.1').get('value', [])
            for env in envs:
                records = self.get(f'{self.api}/distributedtask/environments/{env["id"]}/environmentdeploymentrecords'
                                   f'?top=1&api-version=7.1-preview.1').get('value', [])
                if records:
                    r = records[0]
                    result['deployments'].append({
                        'environment_name': env.get('name', ''), 'ref': (r.get('definition') or {}).get('name', ''),
                        'sha': '', 'status': r.get('result', ''), 'deployed_at': when(r.get('finishTime') or r.get('startTime')),
                        'deployed_by': (r.get('owner') or {}).get('name', ''),
                        'url': ((r.get('owner') or {}).get('_links', {}).get('web') or {}).get('href', '')})
        except ConnectorError as e:
            self.warnings.append(f'Deployments not read: {e}')
        return result


class DemoRepo(RepoConnector):
    """Sample repository so the screens can be tried without a token; deployments use the project's environments."""
    provider = 'demo'
    label = 'Demo Repository'
    example = 'demo/any-name'
    token_help = 'No token needed.'

    def fetch(self):
        now = timezone.now().replace(microsecond=0)
        envs = [e.name for e in self.repo.project.api_environments.all()] or ['DEV', 'SIT', 'UAT']
        envs = envs + ['PROD'] if not any('prod' in e.lower() for e in envs) else envs
        shas = ['9f2c1ab7e4d0', '4be81c09a7f3', '1d77e0c2b5aa', '7a0c3e91fd42']
        deployments = []
        for i, env in enumerate(envs):
            prod = 'prod' in env.lower()
            deployments.append({
                'environment_name': env, 'ref': 'v2.3.0' if prod else ('main' if i == 0 else 'release/2.4'),
                'sha': shas[3] if prod else shas[min(i, 2)], 'status': 'success',
                'deployed_at': now - timedelta(days=9 if prod else i * 2, hours=i),
                'deployed_by': 'release-bot' if prod else 'ci-pipeline', 'url': ''})
        files = [
            [('src/orders/checkout.py', 'modified', 42, 8), ('src/orders/tests/test_checkout.py', 'added', 120, 0)],
            [('src/payments/gateway.py', 'modified', 15, 30), ('config/payment.yml', 'modified', 2, 2)],
            [('src/catalog/search.py', 'modified', 60, 12)],
            [('src/auth/login.py', 'modified', 5, 1), ('src/auth/otp.py', 'added', 88, 0), ('docs/auth.md', 'modified', 10, 3)],
            [('src/cart/cart.py', 'removed', 0, 140)],
        ]
        titles = ['Checkout: apply coupon before tax', 'Payment gateway timeout handling', 'Faster product search',
                  'Login with OTP', 'Remove legacy cart service']
        states = ['open', 'open', 'merged', 'merged', 'closed']
        prs = []
        for n, (title, state, changed) in enumerate(zip(titles, states, files), start=101):
            updated = now - timedelta(days=n - 101)
            prs.append({'number': str(n), 'title': title, 'state': state, 'author': ['sara', 'bilal', 'omer', 'hina', 'ali'][n - 101],
                        'source_branch': f'feature/{title.split()[0].lower().strip(":")}-{n}', 'target_branch': 'main',
                        'url': '', 'created_on': updated - timedelta(days=2), 'updated_on': updated,
                        'merged_on': updated if state == 'merged' else None,
                        'additions': sum(f[2] for f in changed), 'deletions': sum(f[3] for f in changed),
                        'files': [{'path': p, 'status': s, 'additions': a, 'deletions': d} for p, s, a, d in changed]})
        pipelines = [{'name': 'CI · Build & Unit Tests', 'status': 'success', 'branch': 'main', 'run_at': now - timedelta(hours=3), 'url': ''},
                     {'name': 'Playwright E2E Nightly Regression', 'status': 'failure', 'branch': 'main', 'run_at': now - timedelta(hours=10), 'url': ''},
                     {'name': 'Deploy to staging', 'status': 'success', 'branch': 'release/2.4', 'run_at': now - timedelta(days=2), 'url': ''}]
        return {'name': self.repo.url.strip() or 'demo/demo-repository', 'default_branch': 'main',
                'deployments': deployments, 'pull_requests': prs, 'pipelines': pipelines}


CONNECTORS = {c.provider: c for c in (GitHub, GitLab, AzureRepos, DemoRepo)}


def sync_repository(repo, http=None):
    """Reads the repository and replaces its deployments, pull requests and pipelines. A failure keeps the old data."""
    from .models import Deployment, Pipeline, PullRequest
    try:
        connector = CONNECTORS[repo.provider](repo, http)
        data = connector.fetch()
    except ConnectorError as e:
        repo.last_status, repo.last_error = 'Failed', str(e)[:500]
        repo.last_synced_at = timezone.now()
        repo.save(update_fields=['last_status', 'last_error', 'last_synced_at'])
        return {'ok': False, 'message': str(e)}
    with transaction.atomic():
        repo.deployments.all().delete()
        repo.pull_requests.all().delete()
        repo.pipelines.all().delete()
        Deployment.objects.bulk_create(Deployment(repository=repo, **d) for d in data['deployments'])
        PullRequest.objects.bulk_create(PullRequest(repository=repo, **p) for p in data['pull_requests'])
        Pipeline.objects.bulk_create(Pipeline(repository=repo, **p) for p in data['pipelines'])
        repo.name = data['name'][:300]
        repo.default_branch = data['default_branch'][:200]
        repo.last_status = 'Warning' if connector.warnings else 'Success'
        repo.last_error = '; '.join(connector.warnings)[:500]
        repo.last_synced_at = timezone.now()
        repo.save()
    return {'ok': True, 'message': f'{len(data["deployments"])} environment deployment(s), '
                                   f'{len(data["pull_requests"])} pull request(s), {len(data["pipelines"])} pipeline(s).'
                                   + (f' Warning: {repo.last_error}' if connector.warnings else '')}
