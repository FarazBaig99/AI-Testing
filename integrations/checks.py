"""Connection tests: a URL endpoint gets a real HTTP request, host:port only a TCP reachability check.

`urlopen` and `connect` are injectable so tests never touch the network.
"""
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from delivery.connectors.base import basic_auth


def expected_codes(text):
    """'200, 401' -> {200, 401}; '200-299' ranges allowed; empty -> None (any code below 500)."""
    codes = set()
    for part in (text or '').replace(' ', '').split(','):
        if '-' in part:
            low, _, high = part.partition('-')
            if low.isdigit() and high.isdigit():
                codes.update(range(int(low), int(high) + 1))
        elif part.isdigit():
            codes.add(int(part))
    return codes or None


def _headers(integration):
    if integration.auth_type == 'basic':
        return {'Authorization': basic_auth(integration.auth_user, integration.auth_secret)}
    if integration.auth_type == 'bearer':
        return {'Authorization': f'Bearer {integration.auth_secret}'}
    if integration.auth_type == 'header' and integration.auth_user:
        return {integration.auth_user: integration.auth_secret}
    return {}


def _reason(error):
    reason = getattr(error, 'reason', error)
    if isinstance(reason, socket.timeout) or isinstance(error, TimeoutError) or 'timed out' in str(reason):
        return 'Timed out: no answer from the partner system.'
    if isinstance(reason, socket.gaierror):
        return 'Host name could not be resolved (DNS). Check the endpoint.'
    if isinstance(reason, ssl.SSLError):
        return f'SSL / certificate problem: {reason}'
    if isinstance(reason, ConnectionRefusedError):
        return 'Connection refused: nothing is listening on that address.'
    return f'Could not connect: {reason}'


def check_http(integration, urlopen=None):
    urlopen = urlopen or urllib.request.urlopen
    request = urllib.request.Request(integration.endpoint, method=integration.check_method,
                                     data=b'{}' if integration.check_method == 'POST' else None,
                                     headers={'User-Agent': 'BaigNova-TestManager/1.0', 'Accept': '*/*',
                                              **({'Content-Type': 'application/json'}
                                                 if integration.check_method == 'POST' else {}),
                                              **_headers(integration)})
    start = time.monotonic()
    try:
        with urlopen(request, timeout=integration.timeout) as response:
            code = response.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'passed': False, 'code': None, 'ms': None, 'message': _reason(e)}
    ms = round((time.monotonic() - start) * 1000)
    wanted = expected_codes(integration.expected_status)
    passed = code in wanted if wanted else code < 500
    if passed:
        message = f'HTTP {code} in {ms} ms'
    elif code in (401, 403):
        message = f'HTTP {code}: reached the system but access was denied. Check the credentials.'
    else:
        message = f'HTTP {code}: ' + ('server error at the partner.' if code >= 500
                                      else f'expected {integration.expected_status}.')
    return {'passed': passed, 'code': code, 'ms': ms, 'message': message}


def host_port(endpoint):
    """'kafka.bank.com:9092', 'sftp://files.psw.gov.pk:22', 'tcp://10.0.0.5:1414' -> (host, port) or None."""
    text = endpoint.strip()
    if '://' not in text:
        text = 'tcp://' + text
    try:
        parts = urlsplit(text)
        if parts.hostname and parts.port:
            return parts.hostname, parts.port
    except ValueError:
        pass
    return None


def check_tcp(integration, connect=None):
    connect = connect or socket.create_connection
    target = host_port(integration.endpoint)
    if not target:
        return {'passed': False, 'code': None, 'ms': None,
                'message': 'Endpoint must be a URL (https://...) or host:port (e.g. kafka.bank.com:9092).'}
    start = time.monotonic()
    try:
        connect(target, timeout=integration.timeout).close()
    except OSError as e:
        return {'passed': False, 'code': None, 'ms': None, 'message': _reason(e)}
    ms = round((time.monotonic() - start) * 1000)
    return {'passed': True, 'code': None, 'ms': ms, 'message': f'Port {target[1]} on {target[0]} reachable in {ms} ms'}


def check(integration, urlopen=None, connect=None):
    return check_http(integration, urlopen) if integration.is_url else check_tcp(integration, connect)


def read_version(endpoint, urlopen=None, timeout=10):
    """Reads an environment's version URL -> {'ok', 'value', 'message'}; `version_key` may be dotted (build.commit)."""
    urlopen = urlopen or urllib.request.urlopen
    request = urllib.request.Request(endpoint.url, headers={'User-Agent': 'BaigNova-TestManager/1.0',
                                                            'Accept': 'application/json, text/plain'})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(20000).decode(errors='replace').strip()
    except urllib.error.HTTPError as e:
        return {'ok': False, 'value': '', 'message': f'HTTP {e.code} from the version URL.'}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'ok': False, 'value': '', 'message': _reason(e)}
    if not endpoint.version_key:
        return {'ok': True, 'value': raw[:200], 'message': 'Read the response text.'}
    try:
        value = json.loads(raw)
        for key in endpoint.version_key.split('.'):
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (ValueError, KeyError, IndexError, TypeError):
        return {'ok': False, 'value': '', 'message': f'No "{endpoint.version_key}" in the JSON response.'}
    return {'ok': True, 'value': str(value)[:200], 'message': f'Read "{endpoint.version_key}".'}
