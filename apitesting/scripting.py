"""Variables ({{name}}) and Postman-style scripts (pre-request / post-response) for API Testing.

Scripts run in Node.js (apitesting/js/sandbox.js) with code generation, file system and child processes blocked.
One Node process serves a whole workspace send or collection run; ScriptEngine talks to it line by line.
"""
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings

from . import lov as lov_module, runner

SANDBOX = Path(__file__).resolve().parent / 'js' / 'sandbox.js'
ENGINE_TIMEOUT = 15          # seconds for one script event, Node start included
VAR_RE = re.compile(r'\{\{\s*([\w.\-$]+)\s*\}\}')
WINDOWS_NODE = r'C:\Program Files\nodejs\node.exe'


class ScriptError(Exception):
    pass


def find_node():
    path = getattr(settings, 'NODE_PATH', '') or shutil.which('node')
    if not path and os.path.exists(WINDOWS_NODE):
        path = WINDOWS_NODE
    return path


class ScriptEngine:
    """A sandboxed Node process. Use as a context manager, or call close()."""

    def __init__(self):
        self.proc = None
        self.lines = None
        self.lock = threading.Lock()
        self.counter = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _start(self):
        node = find_node()
        if not node:
            raise ScriptError('Node.js was not found on the BaigNova server, so scripts cannot run. '
                              'Install Node.js or set NODE_PATH in settings.')
        env = {k: os.environ[k] for k in ('SYSTEMROOT', 'SystemRoot', 'TEMP', 'TMP') if k in os.environ}
        self.proc = subprocess.Popen(
            [node, '--disallow-code-generation-from-strings', '--permission', f'--allow-fs-read={SANDBOX}',
             str(SANDBOX)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(SANDBOX.parent),
            env=env, text=True, encoding='utf-8', bufsize=1)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.lines), daemon=True).start()

    @staticmethod
    def _read(proc, lines):
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def run(self, payload):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.counter += 1
            payload = dict(payload, id=self.counter)
            try:
                self.proc.stdin.write(json.dumps(payload, default=str) + '\n')
                self.proc.stdin.flush()
            except OSError as exc:
                self._kill()
                raise ScriptError(f'The script engine stopped: {exc}')
            deadline = time.monotonic() + ENGINE_TIMEOUT
            while True:
                try:
                    line = self.lines.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    self._kill()
                    raise ScriptError(f'Scripts did not finish within {ENGINE_TIMEOUT} s and were stopped.')
                if line is None:
                    self._kill()
                    raise ScriptError('The script engine stopped unexpectedly.')
                try:
                    out = json.loads(line)
                except ValueError:
                    continue
                if out.get('id') == payload['id']:
                    return out

    def _kill(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self._kill()
        self.proc = None


# ---------- variables ----------

def rows_to_dict(rows):
    return {r['key']: r.get('value', '') for r in rows or [] if r.get('enabled', True) and str(r.get('key', '')).strip()}


def dict_to_rows(values, old_rows):
    """Write changed values back into the stored rows; disabled rows are kept, unset keys removed."""
    out, seen = [], set()
    for r in old_rows or []:
        key = r.get('key', '')
        if not r.get('enabled', True):
            out.append(r)
            continue
        if key in values:
            value = values[key]
            out.append(dict(r, value=value if isinstance(value, str) else json.dumps(value)))
            seen.add(key)
    for key, value in values.items():
        if key not in seen and key not in {r.get('key') for r in out}:
            out.append({'key': key, 'value': value if isinstance(value, str) else json.dumps(value), 'enabled': True})
    return out


def dynamic_value(name):
    if name in ('$guid', '$randomUUID'):
        return str(uuid.uuid4())
    if name == '$timestamp':
        return str(int(time.time()))
    if name == '$isoTimestamp':
        return datetime.now(dt_timezone.utc).isoformat().replace('+00:00', 'Z')
    if name == '$randomInt':
        return str(random.randint(0, 1000))
    return None


class Scopes:
    """environment > collection > project > globals, with run-local values on top (pm.variables).

    Project variables work in every environment of the project; globals everywhere."""

    def __init__(self, environment=None, collection=None, globals_=None, local=None, env=None, project=None,
                 lovs=None):
        self.environment = dict(environment or {})
        self.collection = dict(collection or {})
        self.project = dict(project or {})
        self.globals = dict(globals_ or {})
        self.local = dict(local or {})
        self.lovs = lovs if lovs is not None else lov_module.Registry()     # {{lovName}}: generated per send
        self.injected = set()
        if env is not None and 'baseUrl' not in self.environment:
            self.environment['baseUrl'] = env.base_url.rstrip('/')
            self.injected.add('baseUrl')

    def lookup(self, name):
        for scope in (self.local, self.environment, self.collection, self.project, self.globals):
            if name in scope:
                value = scope[name]
                return value if isinstance(value, str) else json.dumps(value)
        if name in self.lovs:
            return self.lovs.value(name)
        return dynamic_value(name)

    def prepare_lovs(self, spec):
        """Generate the LOVs the spec mentions, so scripts see the same values the request is sent with."""
        self.lovs.reset()
        names = set()

        def walk(value):
            if isinstance(value, str):
                names.update(m.group(1) for m in VAR_RE.finditer(value))
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
        walk(spec)
        for name in names:
            if name in self.lovs:
                self.lookup(name)          # a variable of the same name wins; otherwise the LOV is generated now
        return self.lovs.used()

    def substitute(self, value, depth=0):
        if isinstance(value, str):
            def repl(m):
                found = self.lookup(m.group(1))
                return m.group(0) if found is None else found
            for _ in range(3):          # variables may hold other {{variables}}
                new = VAR_RE.sub(repl, value)
                if new == value:
                    break
                value = new
            return value
        if isinstance(value, list):
            return [self.substitute(v) for v in value]
        if isinstance(value, dict):
            return {k: self.substitute(v) for k, v in value.items()}
        return value

    def for_script(self):
        env = {k: v for k, v in self.environment.items()}
        return {'environment': env, 'collection': self.collection, 'project': self.project, 'globals': self.globals,
                'local': self.local, 'lov': self.lovs.used()}

    def update(self, values):
        if not values:
            return
        self.environment = dict(values.get('environment', self.environment))
        self.collection = dict(values.get('collection', self.collection))
        self.project = dict(values.get('project', self.project))
        self.globals = dict(values.get('globals', self.globals))
        self.local = dict(values.get('local', self.local))


def project_rows(env=None, project=None):
    """The stored project-level variable rows of a project (or of an environment's project)."""
    from .models import ProjectVariables
    project = project or (env.project if env is not None else None)
    if project is None:
        return []
    pv = ProjectVariables.objects.filter(project=project).first()
    return list(pv.variables) if pv else []


def load_scopes(env=None, collection=None, globals_obj=None):
    from .models import ApiGlobals
    globals_obj = globals_obj or ApiGlobals.load()
    return Scopes(rows_to_dict(env.variables) if env else {}, rows_to_dict(collection.variables) if collection else {},
                  rows_to_dict(globals_obj.variables), env=env, project=rows_to_dict(project_rows(env)),
                  lovs=lov_module.Registry.for_project(env.project if env else None))


def save_project_scope(values, project):
    from .models import ProjectVariables
    if project is None:
        return
    pv = ProjectVariables.for_project(project)
    rows = dict_to_rows(values, pv.variables)
    if rows != pv.variables:
        pv.variables = rows
        pv.save(update_fields=['variables'])


def save_scopes(scopes, env=None, collection=None, save_globals=True, save_project=True):
    """Keep the values scripts set (like Postman's current value)."""
    from .models import ApiGlobals
    if env is not None:
        values = {k: v for k, v in scopes.environment.items()
                  if k not in scopes.injected or v != env.base_url.rstrip('/')}
        rows = dict_to_rows(values, env.variables)
        if rows != env.variables:
            env.variables = rows
            env.save(update_fields=['variables'])
        if save_project:
            save_project_scope(scopes.project, env.project)
    if collection is not None:
        rows = dict_to_rows(scopes.collection, collection.variables)
        if rows != collection.variables:
            collection.variables = rows
            collection.save(update_fields=['variables'])
    if not save_globals:
        return
    g = ApiGlobals.load()
    rows = dict_to_rows(scopes.globals, g.variables)
    if rows != g.variables:
        g.variables = rows
        g.save(update_fields=['variables'])


# ---------- one request with its scripts ----------

def _script_request(spec):
    return {'url': spec.get('url', ''), 'method': spec.get('method', 'GET'),
            'headers': [{'key': h.get('key', ''), 'value': h.get('value', ''), 'disabled': not h.get('enabled', True)}
                        for h in spec.get('headers') or []],
            'body': {'mode': spec.get('body_mode', 'none'), 'raw': spec.get('body', '')}}


def _apply_request(spec, req):
    """Changes a pre-request script made through pm.request."""
    if not req:
        return spec
    spec = dict(spec)
    spec['url'] = req.get('url', spec.get('url'))
    method = str(req.get('method') or spec.get('method') or 'GET').upper()
    spec['method'] = method if method in runner_methods() else spec.get('method', 'GET')
    spec['headers'] = [{'key': h.get('key', ''), 'value': h.get('value', ''), 'enabled': not h.get('disabled')}
                       for h in req.get('headers') or []]
    if spec.get('body_mode') == 'raw' and isinstance(req.get('body'), dict):
        spec['body'] = req['body'].get('raw', spec.get('body', ''))
    return spec


def runner_methods():
    return ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')


def _scripts(pairs):
    return [{'name': name, 'code': code} for name, code in pairs if (code or '').strip()]


def _event(engine, event, scripts, spec, response, scopes, info, report):
    if not scripts:
        return spec
    try:
        out = engine.run({'event': event, 'scripts': scripts, 'request': _script_request(spec), 'response': response,
                          'vars': scopes.for_script(), 'info': info})
    except ScriptError as exc:
        report['errors'].append({'script': event, 'message': str(exc)})
        return spec
    scopes.update(out.get('vars'))
    report['tests'] += [dict(t, event=event) for t in out.get('tests', [])]
    report['console'] += out.get('console', [])
    report['errors'] += out.get('errors', [])
    return _apply_request(spec, out.get('request')) if event == 'prerequest' else spec


def execute(spec, env, scopes, engine, collection=None, send=None, files=None, info=None):
    """Pre-request scripts -> {{variables}} -> send -> post-response scripts.

    Returns {'result': response dict, 'tests': [...], 'console': [...], 'errors': [...], 'request': final spec}.
    """
    send = send or runner.send
    info = info or {}
    report = {'tests': [], 'console': [], 'errors': [], 'lov': scopes.prepare_lovs(spec)}
    pre = _scripts([('Collection pre-request', collection.pre_request_script if collection else ''),
                    ('Pre-request', spec.get('pre_request_script', ''))])
    post = _scripts([('Collection tests', collection.test_script if collection else ''),
                     ('Post-response', spec.get('test_script', ''))])
    spec = _event(engine, 'prerequest', pre, spec, None, scopes, dict(info, eventName='prerequest'), report)
    final = {k: (scopes.substitute(v) if k not in ('pre_request_script', 'test_script') else v) for k, v in spec.items()}
    result = send(final, env, files) if files is not None else send(final, env)
    if post:
        response = None
        if result.get('ok'):
            response = {'code': result['status'], 'status': result.get('reason', ''), 'headers': result.get('headers', []),
                        'body': result.get('body', ''), 'responseTime': result.get('ms', 0),
                        'responseSize': result.get('size', 0)}
        if response:
            _event(engine, 'test', post, final, response, scopes, dict(info, eventName='test'), report)
        else:
            report['errors'].append({'script': 'Post-response',
                                     'message': 'Not run: the request got no response.'})
    return dict(report, result=result, request=final)
