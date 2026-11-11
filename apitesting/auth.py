"""Cookie jar (like Postman's) and automatic Keycloak / OpenID Connect login for API Testing.

Many apps (e.g. ACS) keep the Keycloak tokens on their server and give the browser a session cookie. BaigNova logs
in the same way a browser does: open the app's login URL, fill Keycloak's login form with the login's test user,
post the result back to the app's callback, and keep the session cookie. Requests sent with that login then carry
the cookie, and a 401 triggers one fresh login and a retry.

An environment can have several logins (AuthProfile): "Application client", "Backoffice client", "Integration
client"... Each keeps its own cookies, so two clients of the same app never overwrite each other's session. A request
picks its login by name (auth.profile); without a pick it uses the environment's default login.
"""
import json
import re
import threading
import time
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from django.utils import timezone

from . import runner
from .models import ApiGlobals, AuthProfile

LOGIN_TIMEOUT = 30
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
_jar_lock = threading.Lock()


class LoginError(Exception):
    pass


# ---------- cookie jar <-> database ----------

def _cookie_to_dict(c):
    return {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path, 'secure': c.secure,
            'expires': c.expires, 'httponly': c.has_nonstandard_attr('HttpOnly'),
            'host_only': not c.domain_specified}


def _dict_to_cookie(d):
    domain = d.get('domain', '')
    return Cookie(version=0, name=d['name'], value=d.get('value', ''), port=None, port_specified=False,
                  domain=domain, domain_specified=not d.get('host_only', False),
                  domain_initial_dot=domain.startswith('.'), path=d.get('path') or '/', path_specified=True,
                  secure=bool(d.get('secure')), expires=d.get('expires'), discard=d.get('expires') is None,
                  comment=None, comment_url=None, rest={'HttpOnly': None} if d.get('httponly') else {}, rfc2109=False)


def _fill_jar(jar, rows):
    for d in rows or []:
        if d.get('name') and d.get('domain'):
            jar.set_cookie(_dict_to_cookie(d))


def _jar_rows(jar):
    return sorted((_cookie_to_dict(c) for c in jar), key=lambda d: (d['domain'], d['path'], d['name']))


def load_jar(profile=None):
    """The shared jar (cookies added by hand, and of requests sent without a login) plus the login's own cookies."""
    jar = CookieJar()
    _fill_jar(jar, ApiGlobals.load().cookies)
    if profile is not None:
        _fill_jar(jar, profile.cookies)        # the login's cookies win over shared ones with the same name
    jar.clear_expired_cookies()
    return jar


def save_jar(jar, profile=None):
    """Keep the jar: in the login's own cookies, or (without a login) in the shared jar."""
    jar.clear_expired_cookies()
    with _jar_lock:
        rows = _jar_rows(jar)
        if profile is None:
            g = ApiGlobals.load()
            if rows != g.cookies:
                g.cookies = rows
                g.save(update_fields=['cookies'])
            return
        shared = ApiGlobals.load().cookies or []
        rows = [r for r in rows if r not in shared]   # shared cookies stay in the shared jar only
        if rows != profile.cookies:
            profile.cookies = rows
            profile.save(update_fields=['cookies'])


def add_cookie(domain, name, value, path='/', secure=False):
    jar = load_jar()
    jar.set_cookie(_dict_to_cookie({'name': name, 'value': value, 'domain': domain.strip().lstrip('.').lower(),
                                    'path': path or '/', 'secure': secure, 'host_only': True}))
    save_jar(jar)


def forget_host(jar, url):
    """Drop the cookies a host set (e.g. Keycloak's SSO session)."""
    host = (urlsplit(url).hostname or '').lower()
    for c in list(jar):
        domain = c.domain.lstrip('.').lower()
        if host == domain or host.endswith('.' + domain) or (domain.endswith('.local') and host == domain[:-6]):
            jar.clear(c.domain, c.path, c.name)


def remove_cookies(domain, name=None, profile=None):
    """Drop a cookie (or a domain's cookies) from the shared jar, or from one login's cookies."""
    jar = CookieJar()
    _fill_jar(jar, profile.cookies if profile is not None else ApiGlobals.load().cookies)
    for c in list(jar):
        if c.domain.lstrip('.') == domain.lstrip('.') and (name is None or c.name == name):
            jar.clear(c.domain, c.path, c.name)
    rows = _jar_rows(jar)
    with _jar_lock:
        target = profile if profile is not None else ApiGlobals.load()
        target.cookies = rows
        target.save(update_fields=['cookies'])


def cookie_list():
    """Cookies grouped by domain for the Cookies dialog: the shared jar first, then each login's own cookies."""
    def groups(rows, profile=None):
        by_domain = {}
        for d in rows or []:
            by_domain.setdefault(d['domain'].lstrip('.'), []).append(d)
        return [{'domain': k, 'cookies': v, 'profile': profile.pk if profile else None,
                 'login': f'{profile.environment.project.name} · {profile.environment.name} · {profile.name}' if profile else ''}
                for k, v in sorted(by_domain.items())]
    out = groups(ApiGlobals.load().cookies)
    for p in AuthProfile.objects.select_related('environment__project'):
        out += groups(p.cookies, p)
    return out


# ---------- HTML forms (Keycloak pages) ----------

class _Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self._form, self.text = [], None, []
        self._capture = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self._form = {'attrs': a, 'inputs': []}
            self.forms.append(self._form)
        elif tag in ('input', 'button') and self._form is not None and a.get('name'):
            self._form['inputs'].append(a)
        if tag in ('span', 'div', 'p') and re.search(r'input-error|kc-feedback|alert-error|error', a.get('class', '') + ' ' + a.get('id', '')):
            self._capture += 1

    def handle_endtag(self, tag):
        if tag == 'form':
            self._form = None
        if tag in ('span', 'div', 'p') and self._capture:
            self._capture -= 1

    def handle_data(self, data):
        if self._capture and data.strip():
            self.text.append(data.strip())


def _parse(html):
    p = _Forms()
    try:
        p.feed(html)
    except Exception:
        pass
    return p


def _find_form(forms, *ids):
    for f in forms:
        if f['attrs'].get('id') in ids:
            return f
    return None


def _form_fields(form):
    return {i['name']: i.get('value', '') for i in form['inputs']
            if i.get('type', 'text').lower() not in ('submit', 'button', 'image', 'checkbox', 'radio')}


# ---------- login ----------

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _Browser:
    def __init__(self, jar, verify_ssl, opener=None):
        handlers = [HTTPSHandler(context=runner.ssl_context(verify_ssl)), HTTPCookieProcessor(jar)]
        self.opener = opener or build_opener(*handlers)
        # the app's callback sets the session cookie and then redirects to its web frontend; that page is not
        # needed (and may be slow or unreachable from the BaigNova server), so the callback is not followed
        self.no_follow = opener or build_opener(*handlers, _NoRedirect())

    def go(self, url, data=None, follow=True):
        body = urlencode(data).encode() if data is not None else None
        req = Request(url, data=body, method='POST' if data is not None else 'GET',
                      headers={'User-Agent': 'BaigNova-TestManager', 'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8',
                               **({'Content-Type': 'application/x-www-form-urlencoded'} if data is not None else {})})
        try:
            resp = (self.opener if follow else self.no_follow).open(req, timeout=LOGIN_TIMEOUT)
        except HTTPError as exc:
            resp = exc
        except (URLError, OSError, ValueError) as exc:
            raise LoginError(f'Could not reach {urlsplit(url).netloc}: {getattr(exc, "reason", exc)}')
        with resp:
            text = resp.read(2 * 1024 * 1024).decode('utf-8', errors='replace')
            return resp.status, resp.geturl(), resp.headers.get('Content-Type', ''), text


def _safe_for_password(url):
    parts = urlsplit(url)
    return parts.scheme == 'https' or parts.hostname in LOCAL_HOSTS


def _auth_url_from_json(text):
    """ACS style: GET /login answers {"authorizationUrl": "https://keycloak/...auth?..."}."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    for key in ('authorizationUrl', 'authorizeUrl', 'loginUrl', 'redirectUrl', 'redirectUri', 'url'):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str) and value.startswith('http'):
            return value
    found = re.search(r'https?://[^"\s]+/protocol/openid-connect/auth[^"\s]*', text)
    return found.group(0).replace('\\u0026', '&') if found else None


def _json_error(text):
    """The message of a JSON error answer ({"error": ...} / {"error_description": ...} / {"message": ...})."""
    try:
        data = json.loads(text)
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    parts = [str(data[k]) for k in ('error_description', 'error', 'message', 'detail') if isinstance(data.get(k), (str, int))]
    return parts[0] if parts else ''


def keycloak_login(profile, jar, opener=None, scopes=None):
    """Log in with the profile's user; cookies end up in `jar`. Returns a message, raises LoginError."""
    env = profile.environment
    if not (profile.username and profile.password):
        raise LoginError('Enter the username and password of the test user first.')
    resolve = (lambda t: scopes.substitute(t)) if scopes else (lambda t: env.resolve(t))
    login_url = resolve(profile.login_url or '{{baseUrl}}/login')
    browser = _Browser(jar, profile.verify_ssl, opener)

    status, url, ctype, text = browser.go(login_url)
    page = _parse(text)
    form = _find_form(page.forms, 'kc-form-login')
    if form is None:
        auth_url = _auth_url_from_json(text) if 'json' in ctype or text.lstrip().startswith('{') else None
        if not auth_url and urlsplit(url).hostname != urlsplit(login_url).hostname:
            auth_url = url             # redirected to Keycloak, which answered from an older session
        if not auth_url:
            raise LoginError(f'The login URL did not lead to a Keycloak login page (got HTTP {status} from {url}). '
                             'Check the Login URL.')
        # forget an older Keycloak session, so the login always uses this profile's user
        forget_host(jar, auth_url)
        status, url, ctype, text = browser.go(auth_url)
        page = _parse(text)
        form = _find_form(page.forms, 'kc-form-login')
    if form is None:
        raise LoginError(f'No Keycloak login form found at {urlsplit(url).netloc} (HTTP {status}).')

    action = urljoin(url, unescape(form['attrs'].get('action', '')))
    if not _safe_for_password(action):
        raise LoginError(f'Refusing to send the password to {action}: it is not HTTPS.')
    fields = _form_fields(form)
    fields.update({'username': profile.username, 'password': profile.password})
    fields.setdefault('credentialId', '')
    status, url, ctype, text = browser.go(action, fields)

    # Keycloak's answer: back to the app (redirect / form_post), or another Keycloak page
    for _ in range(3):
        refusal = _json_error(text)
        if refusal:                    # e.g. ACS: "This account doesn't have access to the Application portal..."
            raise LoginError(f'Keycloak refused the login (HTTP {status}): {refusal}')
        page = _parse(text)
        if _find_form(page.forms, 'kc-form-login'):
            reason = ' '.join(page.text) or 'Invalid username or password.'
            raise LoginError(f'Keycloak did not accept the login: {reason}')
        if _find_form(page.forms, 'kc-otp-login-form') or 'otp' in ' '.join(f['attrs'].get('id', '') for f in page.forms):
            raise LoginError('This user must enter a one-time code (OTP / MFA). Use a test user without MFA.')
        if _find_form(page.forms, 'kc-passwd-update-form', 'kc-update-profile-form', 'kc-update-email-form',
                      'kc-terms-form'):
            raise LoginError('Keycloak asks this user to update the password or profile first. '
                             'Log in once in the browser to finish it, then try again.')
        post_back = next((f for f in page.forms if f['attrs'].get('method', '').lower() == 'post'
                          and 'code' in _form_fields(f)), None)
        if post_back is None:
            break
        # response_mode=form_post: Keycloak's auto-submitting form carries the code to the app's callback
        status, url, ctype, text = browser.go(urljoin(url, unescape(post_back['attrs'].get('action', ''))),
                                              _form_fields(post_back), follow=False)
    if status >= 400:
        where = 'Keycloak' if urlsplit(url).hostname == urlsplit(action).hostname else 'The app'
        detail = _json_error(text) or ' '.join(_parse(text).text)
        raise LoginError(f'{where} answered HTTP {status} at {urlsplit(url).path}' + (f': {detail}' if detail else '.'))

    if profile.check_url:
        status, url, ctype, text = browser.go(resolve(profile.check_url))
        if not 200 <= status < 300:
            raise LoginError(f'Logged in to Keycloak, but the check URL answered HTTP {status}.')
    app_host = urlsplit(login_url).hostname or ''
    if not any(app_host.endswith(c.domain.lstrip('.')) for c in jar):
        raise LoginError('Keycloak accepted the login but the app set no session cookie.')
    return f'Logged in as {profile.username}.'


def login(profile, jar=None, opener=None, scopes=None, save=True):
    """Run the login, record the outcome on the profile, keep the cookies. Returns (ok, message)."""
    jar = jar if jar is not None else load_jar(profile)
    started = time.monotonic()
    try:
        message = keycloak_login(profile, jar, opener, scopes)
        ok = True
    except LoginError as exc:
        ok, message = False, str(exc)
    if ok:
        message += f' ({int((time.monotonic() - started) * 1000)} ms)'
    profile.last_status, profile.last_message, profile.last_login_at = ('Passed' if ok else 'Failed'), message, timezone.now()
    profile.save(update_fields=['last_status', 'last_message', 'last_login_at'])
    if save:
        save_jar(jar, profile)
    return ok, message


def test_login(profile, opener=None):
    """Try the login with a throw-away cookie jar; nothing is saved. Returns (ok, message)."""
    started = time.monotonic()
    try:
        message = keycloak_login(profile, CookieJar(), opener)
    except LoginError as exc:
        return False, str(exc)
    return True, f'{message} ({int((time.monotonic() - started) * 1000)} ms)'


def profile_for(env, choice=None):
    """The login a request is sent with, or None for no login.

    `choice` is the request's auth.profile: empty = the environment's default login, 'none' = no login, else the
    name of one of the environment's logins (a name the environment does not have falls back to the default).
    """
    if env is None or choice == 'none':
        return None
    profiles = list(AuthProfile.objects.filter(environment=env).select_related('environment'))
    if choice:
        named = next((p for p in profiles if p.name == choice), None)
        if named is not None:
            return named if named.is_active else None
    active = [p for p in profiles if p.is_active]
    return next((p for p in active if p.is_default), None) or (active[0] if active else None)


def choice_of(spec):
    """The login a request spec picked (its auth.profile)."""
    auth = spec.get('auth') if isinstance(spec, dict) else None
    return str((auth or {}).get('profile') or '')


# ---------- sending with cookies and automatic re-login ----------

def sender(opener=None, login_opener=None, persist=True, jars=None):
    """A send() with one cookie jar per login, that logs in again once when a login's session has expired.

    The request's login (auth.profile, else the environment's default) decides which jar is used, so requests of
    different clients never share a session. `jars` ({profile id or None: CookieJar}) can be shared between calls.
    """
    jars = jars if jars is not None else {}

    def jar_for(profile):
        key = profile.pk if profile else None
        if key not in jars:
            jars[key] = load_jar(profile)
        return jars[key]

    def send(spec, env=None, files=None):
        choice = choice_of(spec)
        profile = profile_for(env, choice)
        jar = jar_for(profile)
        result = runner.send(spec, env, files, opener=opener, jar=jar)
        if result.get('ok') and result['status'] == 401 and profile and profile.auto_relogin:
            ok, message = login(profile, jar, login_opener, save=False)
            if ok:
                for f in (files or {}).values():
                    if hasattr(f, 'seek'):
                        f.seek(0)
                result = runner.send(spec, env, files, opener=opener, jar=jar)
            result['auth'] = ('Session had expired: ' if ok else 'Automatic login failed: ') + message
        elif result.get('ok') and result['status'] == 401 and env is not None and not profile:
            if choice == 'none':
                result['auth_hint'] = ('This request is sent without a login. Pick one in the Login menu above '
                                       'the URL so BaigNova sends that login\'s session cookie.')
            elif choice:
                result['auth_hint'] = (f'The login "{choice}" is not set up in {env.project.name} · {env.name}. '
                                       'Set it up in Auth & Cookies, or pick another login in the Login menu.')
            else:
                result['auth_hint'] = (f'{env.project.name} · {env.name} has no login set up. Set up the Keycloak '
                                       'login in Auth & Cookies so BaigNova logs in and sends the session cookie.')
        if profile:
            result['login'] = profile.name
        if persist:
            save_jar(jar, profile)
        return result
    return send
