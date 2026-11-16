"""Root-cause analysis for a sent request: what went wrong, why, and how to fix it.

Rule-based and offline. It reads the response (status, body, headers, or the transport error) together
with the request's own setup (environment, login, auth, body) and returns a plain-language diagnosis the
Diagnosis tab shows. The structure (`level`, `title`, `summary`, `causes`, `fixes`, `server_message`,
`trace_id`, `source`) is stable, so an AI explanation can be layered on later without changing callers.
"""
import json


def _json_message(body):
    """The server's own error message and trace id from a JSON body, if any."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    msg = None
    for k in ('message', 'error_description', 'error', 'detail', 'title', 'Message', 'errorMessage'):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            msg = v.strip()
            break
        if isinstance(v, dict) and isinstance(v.get('message'), str):
            msg = v['message'].strip()
            break
    trace = data.get('traceId') or data.get('trace_id') or data.get('traceID') or data.get('correlationId')
    return msg, (str(trace) if trace else None)


def _result(level, title, summary, causes, fixes, server_message=None, trace_id=None):
    return {'level': level, 'title': title, 'summary': summary, 'causes': causes, 'fixes': fixes,
            'server_message': server_message, 'trace_id': trace_id, 'source': 'rule'}


def _transport(error):
    """Diagnose a request that never got an HTTP response (network / TLS / URL)."""
    text = (error or '').lower()
    if 'no environment is attached' in text or '{{baseurl}}' in text:
        return _result('error', 'No environment for {{baseUrl}}',
                       'The URL uses {{baseUrl}} but no environment is attached, so BaigNova cannot build the real address.',
                       ['The request has {{baseUrl}} (or another environment variable) but no environment is selected.'],
                       ['Select the project and environment in the top bar.',
                        'Or type the full URL instead of {{baseUrl}}.'])
    if 'certificate' in text or 'ssl' in text or 'cert_' in text:
        return _result('error', 'SSL certificate not trusted',
                       'The server’s HTTPS certificate could not be verified (common on QA / self-signed setups).',
                       ['The environment uses a self-signed or internal certificate.'],
                       ['Open the Settings tab of the request and turn off “Verify SSL certificates”, then Send again.',
                        'Make sure the host is correct and reachable over HTTPS.'])
    if 'timed out' in text or 'timeout' in text:
        return _result('error', 'Request timed out',
                       'The server did not answer in time. It may be down, slow, or blocked by the network.',
                       ['The service is slow or not running.', 'A VPN / firewall is blocking the host.'],
                       ['Check that you can open the host in the browser (are you on the right VPN?).',
                        'Confirm the environment base URL points to a running service.'])
    if any(s in text for s in ('getaddrinfo', 'name or service not known', 'nodename', 'name resolution', 'no address')):
        return _result('error', 'Host not found',
                       'The address in the URL could not be resolved. The host name is wrong or not reachable from here.',
                       ['A typo in the URL / base URL.', 'The host is internal and needs a VPN.'],
                       ['Check the environment base URL and the request path.',
                        'If it is an internal host, connect to the VPN and try again.'])
    if 'refused' in text:
        return _result('error', 'Connection refused',
                       'The host is reachable but nothing accepted the connection on that port.',
                       ['The service is stopped.', 'Wrong port or scheme (http vs https) in the URL.'],
                       ['Confirm the service is running and the base URL (host and port) is right.'])
    return _result('error', 'Could not send the request',
                   error or 'The request failed before any response.',
                   ['A network or URL problem stopped the request.'],
                   ['Check the URL and that the host is reachable, then Send again.'])


# how BaigNova UI is referred to in fixes
_AUTH = 'the Auth & Cookies dialog (top bar)'
_LOGIN_MENU = 'the Login menu next to the request name'


def _unauthorized(has_login, picked, server_message, trace_id):
    if picked == 'none':
        causes = ['This request is set to “No login”, so no session cookie / token is sent.']
        fixes = [f'Pick a login in {_LOGIN_MENU} so BaigNova sends that login’s session.']
    elif not has_login:
        causes = ['No login is set up for this environment, so the request goes out unauthenticated.']
        fixes = [f'Open {_AUTH} and set up the Keycloak login (login URL, test user) for this environment.',
                 'Run “Test connection” there until it passes, then Send again.']
    else:
        causes = ['The login’s session has expired, or this user does not have access to this API.',
                  'The token audience / client may not match this endpoint.']
        fixes = [f'Open {_AUTH} and press “Test connection” / “Login now” to refresh the session.',
                 'Confirm the test user is allowed to call this API.']
    return _result('error', '401 Unauthorized — not logged in',
                   'The server rejected the request because it is not authenticated.',
                   causes, fixes, server_message, trace_id)


def _forbidden(server_message, trace_id):
    return _result('error', '403 Forbidden — no permission',
                   'The request is authenticated, but this user / client is not allowed to do this.',
                   ['The test user’s role lacks permission for this endpoint.',
                    'The token was issued for a different client / audience than this API expects.'],
                   ['Use a test user with the right role, or ask for access to this API.',
                    f'Check in {_AUTH} that the login belongs to the same client as this endpoint.'],
                   server_message, trace_id)


def _status(status, spec, body, server_message, trace_id, has_login, picked):
    if status == 401:
        return _unauthorized(has_login, picked, server_message, trace_id)
    if status == 403:
        return _forbidden(server_message, trace_id)
    if status == 404:
        return _result('error', '404 Not Found — wrong URL or path',
                       'The server has no resource at this address.',
                       ['A typo in the path, or {{baseUrl}} resolving to the wrong host.',
                        'A path variable (like an id) that does not exist.'],
                       ['Check the URL and that {{baseUrl}} points to the right service for this API.',
                        'Verify any id / path value exists.'], server_message, trace_id)
    if status == 400:
        return _result('error', '400 Bad Request — the server rejected the input',
                       'The request reached the server, but its body or parameters are not valid.',
                       ['A required field is missing or has the wrong type.',
                        'The body is not valid JSON, or the Content-Type is wrong.'],
                       ['Open the Body tab and check required fields and valid JSON.',
                        'Check the Params and the Content-Type header.'], server_message, trace_id)
    if status == 405:
        return _result('error', '405 Method Not Allowed',
                       f'This endpoint does not accept {spec.get("method", "this")} requests.',
                       ['The HTTP method is wrong for this URL.'],
                       ['Change the method (GET / POST / PUT / ...) to the one this endpoint expects.'],
                       server_message, trace_id)
    if status == 409:
        return _result('error', '409 Conflict',
                       'The request clashes with the current state (often a duplicate).',
                       ['A record with the same key already exists.'],
                       ['Use a unique value (an LOV “Unique value” category helps), then Send again.'],
                       server_message, trace_id)
    if status in (415,):
        return _result('error', '415 Unsupported Media Type',
                       'The server does not accept the body’s content type.',
                       ['The Content-Type header does not match the body (e.g. JSON body sent as text).'],
                       ['In the Body tab pick the right type (raw → JSON), or fix the Content-Type header.'],
                       server_message, trace_id)
    if status == 422:
        return _result('error', '422 Unprocessable — validation failed',
                       'The input is well-formed but broke a business rule.',
                       ['A field value is out of range or fails a validation rule.'],
                       ['Read the server message below and fix the offending field in the Body.'],
                       server_message, trace_id)
    if status == 429:
        return _result('warn', '429 Too Many Requests — rate limited',
                       'The server is throttling because too many requests arrived too quickly.',
                       ['A performance run or a tight loop is sending too fast.'],
                       ['Wait a moment and retry.', 'Add a delay between requests in the collection runner.'],
                       server_message, trace_id)
    if status in (502, 503, 504):
        return _result('error', f'{status} — service unavailable',
                       'A gateway or upstream service is down or timing out. This is on the server side.',
                       ['The API or a service it depends on is restarting or overloaded.'],
                       ['Retry shortly.', 'If it persists, tell the backend team (share the trace id below).'],
                       server_message, trace_id)
    if status >= 500:
        return _result('error', f'{status} Server Error — the API itself failed',
                       'The request was accepted but the server threw an error while handling it. This is usually a backend bug, not your request.',
                       ['An unhandled error in the API.',
                        'A specific payload value the backend did not expect.'],
                       ['Share the trace id below with the backend team.',
                        'Try a smaller / known-good payload to see if a value triggers it.'],
                       server_message, trace_id)
    if status >= 400:
        return _result('error', f'{status} — request rejected',
                       'The server rejected the request.',
                       ['See the server message below.'],
                       ['Adjust the request based on the server message, then Send again.'],
                       server_message, trace_id)
    return None


def diagnose(spec, result, env=None, has_login=False, picked='', tests=None, errors=None):
    """Return a diagnosis dict for a sent request, or None when there is nothing to flag."""
    tests = tests or []
    errors = errors or []
    spec = spec or {}

    if errors:
        e = errors[0]
        return _result('error', 'A script could not run',
                       f'{e.get("script", "A script")} stopped: {e.get("message", "")}',
                       ['A pre-request or post-response script threw an error or timed out.'],
                       ['Open the Pre-request / Post-response tab and fix the script shown in the Console.'])

    if not result.get('ok'):
        return _transport(result.get('error'))

    body = result.get('body') or ''
    server_message, trace_id = _json_message(body)
    status = result.get('status')
    diag = _status(status, spec, body, server_message, trace_id, has_login, picked)
    if diag:
        return diag

    # 2xx
    failed = [t for t in tests if not t.get('passed') and not t.get('skipped')]
    if failed:
        return _result('warn', f'{status} OK, but {len(failed)} test{"s" if len(failed) > 1 else ""} failed',
                       'The server answered successfully, but an assertion in your Post-response script did not hold.',
                       [f'Assertion failed: {failed[0].get("name", "")}' + (f' — {failed[0].get("error", "")}' if failed[0].get('error') else '')],
                       ['Open the Test Results tab to see each assertion.',
                        'Fix the expectation or the request so the response matches.'])
    return _result('ok', f'{status} {result.get("reason", "")}'.strip() + ' — looks good',
                   'The request succeeded and all checks passed.', [], [])
