import json
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit
import requests
from .network import FamilyAdapter, connect
from .validators import parse_status_codes


@dataclass
class ProbeResult:
    healthy: bool
    detail: str
    duration_ms: int
    status_code: int | None = None


def caused_by_dns_error(error):
    """Inspect exception types without retaining potentially secret URL text."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        pending.extend(value for value in (
            current.__cause__, current.__context__, getattr(current, 'reason', None), *current.args,
        ) if isinstance(value, BaseException))
    return False


def probe(target):
    started = time.monotonic()
    status = None
    healthy, detail = False, 'Erreur réseau, DNS ou TLS'
    headers = {'User-Agent': 'uptime-probe/2.0', **target.request_headers}
    if target.auth_type == 'bearer':
        headers['Authorization'] = f'Bearer {target.auth_token}'
    auth = (target.auth_username, target.auth_password) if target.auth_type == 'basic' else None
    data = json.loads(target.body) if target.body and target.body_type == 'json' else target.body or None
    if target.body_type == 'form' and target.body:
        data = parse_qsl(target.body, keep_blank_values=True)
    kwargs = {'json' if target.body_type == 'json' else 'data': data}
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.max_redirects = 5
            if target.ip_version != 'any':
                adapter = FamilyAdapter(socket.AF_INET if target.ip_version == '4' else socket.AF_INET6)
                session.mount('http://', adapter)
                session.mount('https://', adapter)
            with session.request(target.method, target.url, headers=headers, auth=auth,
                                 timeout=target.timeout_seconds, verify=target.verify_tls,
                                 allow_redirects=target.follow_redirects, stream=True,
                                 params={'_uptime': time.time_ns()} if target.cache_buster else None, **kwargs) as response:
                status = response.status_code
                healthy = status in parse_status_codes(target.expected_status_codes)
                detail = f'HTTP {status}'
                if healthy and any(response.headers.get(k) != v for k, v in target.response_headers.items()):
                    healthy, detail = False, f'HTTP {status} · En-tête attendu absent ou différent'
                if healthy and target.keyword:
                    # Bounded body and total reading time, even for streaming responses.
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        content.extend(chunk[:1048576 - len(content)])
                        if len(content) >= 1048576 or time.monotonic() - started >= target.timeout_seconds:
                            break
                    text = content.decode(response.encoding or 'utf-8', errors='replace')
                    keyword = target.keyword
                    if target.keyword_ignore_case:
                        text, keyword = text.casefold(), keyword.casefold()
                    if keyword not in text:
                        healthy, detail = False, f'HTTP {status} · Texte attendu absent (premier Mio)'
    except requests.Timeout:
        detail = 'Délai de réponse dépassé'
        healthy = False
    except requests.exceptions.SSLError:
        healthy, detail = False, 'Erreur TLS'
    except requests.exceptions.ConnectionError as exc:
        healthy, detail = False, 'Erreur DNS' if caused_by_dns_error(exc) else 'Connexion impossible'
    except requests.exceptions.TooManyRedirects:
        healthy, detail = False, 'Trop de redirections'
    except (requests.RequestException, OSError):
        healthy = False
        detail = 'Erreur réseau, DNS ou TLS'
    return ProbeResult(healthy, detail, round((time.monotonic() - started) * 1000), status)


def certificate_expiry(target):
    parsed = urlsplit(target.url)
    if parsed.scheme != 'https':
        return None
    family = {'any': socket.AF_UNSPEC, '4': socket.AF_INET, '6': socket.AF_INET6}[target.ip_version]
    try:
        with connect(parsed.hostname, parsed.port or 443, target.timeout_seconds, family) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname) as tls:
                return datetime.fromtimestamp(ssl.cert_time_to_seconds(tls.getpeercert()['notAfter']), timezone.utc)
    except (OSError, ValueError, KeyError):
        return None
