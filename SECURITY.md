# Security configuration

## Application controls

- Django CSRF validation, escaped templates and server-side role checks remain
  mandatory. Account admission is invitation-only, with optional first-account
  bootstrap. Google uses verified addresses, OAuth state and PKCE.
- `HTTPS=true` enables secure session/CSRF cookies, HTTP-to-HTTPS redirects and
  HSTS. Session cookies are HttpOnly; CSRF cookies are also HttpOnly because forms
  use hidden CSRF inputs. Both cookies use SameSite=Lax to allow OAuth redirects.
  Sessions expire after 12 hours. Disabling accounts revokes their sessions.
- HSTS starts at 3'600 seconds. Increase `SECURE_HSTS_SECONDS` only after verifying
  HTTPS end to end. Subdomain inclusion and preload are deliberately disabled:
  this application must not make unverified commitments about other hosts.
- Responses reject framing, disable MIME sniffing, send referrers only within
  the same origin and isolate
  opener browsing contexts. Permissions Policy disables camera, microphone,
  geolocation, payment and USB access.
- `Referrer-Policy: same-origin` preserves the origin/referrer information Django
  needs for HTTPS form CSRF verification while suppressing referrers to external
  sites. Do not switch the entire application to `no-referrer`: browsers can
  submit forms with a null Origin or no Referer, causing valid tokens to fail.
- Content Security Policy limits resources to this origin. Scripts require a
  same-origin URL or a fresh per-response nonce. No `unsafe-inline` or `unsafe-eval`
  is allowed for scripts. Inline styles remain allowed for chart values, existing
  component specimens and Django admin. Images may use data URLs. Google is an
  allowed form destination; connector API calls run server-side.
- Application pages use `Cache-Control: private, no-store`, including login,
  profiles, invitations and platform settings. Static resources remain cacheable.
- Request form bodies are limited to 1 MiB and 200 fields. Only platform settings
  accept a single uploaded logo, restricted to platform administrators. A bounded
  in-memory upload handler rejects multipart bodies over 1 MiB; validated and
  normalized logo payloads must be at most 256 KiB, 4,096 pixels per side and
  two million pixels in total. All other upload routes reject files.
- PNG/JPEG/WebP inputs are decoded and re-encoded as static PNG, without metadata.
  SVG accepts UTF-8 XML without document types/entities, scripts, event handlers,
  animation, embedded images or external references. SVG markup is never inserted
  into application HTML. Public content-addressed image responses have a separate
  sandbox CSP with no scripts, network resources or framing, plus MIME sniffing
  protection. Existing page CSP and private/no-store behaviour remain unchanged.
  Logos are public brand assets, not private file storage. Older versions remain
  addressable so historical email images are not broken by a later replacement.
- Credential forms mark POST data sensitive for Django error reports. The
  production Gunicorn access log omits URLs, query strings and request bodies so
  OAuth codes and invitation tokens are not written into access logs. Configure
  the reverse proxy’s access/error logs with the same care.

## Authentication limits

Local login, Django admin login, Google login initiation, invitation activation
and password changes share a maximum of 20 POST attempts per client network per
five-minute fixed window. Local/admin login also share a limit of five attempts
per username and client network in that window. These count attempts, including
successful submissions, rather than exposing whether an account exists.

IPv4 addresses are counted individually; IPv6 addresses share a /64 bucket.
Limits return HTTP 429 with `Retry-After`. They expire automatically without
permanent account lockout. Fixed windows can allow two consecutive budgets around
a boundary. This is application-level throttling, not DDoS protection.

Production uses Django’s Redis cache backend with atomic counters and hashed
keys, shared across web processes. `CACHE_URL` defaults to `CELERY_BROKER_URL`;
the example uses Redis database 1 and a dedicated key prefix. Authentication
fails closed with HTTP 503 if the limiter is unavailable. Existing authenticated
read-only requests do not depend on it. HTTP development uses a process-local
cache; do not use that mode for a multi-process public deployment.

## Reverse proxy trust

`TRUST_PROXY_HEADERS` defaults to false. Enable it only when the proxy is under
your control, overwrites `X-Forwarded-Proto`, appends or overwrites
`X-Forwarded-For`, and cannot be bypassed through a directly reachable application
port. The application trusts only the last forwarded client address, never an
unverified leftmost address. Keep host validation at both proxy and Django levels.

For a single Nginx proxy terminating TLS, the relevant location directives are:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_pass http://127.0.0.1:8000;
```

The proxy must also redirect public HTTP to HTTPS. For Traefik or a proxy chain,
verify the equivalent header and trusted-hop configuration. Compose binds the web
port to loopback and does not publish PostgreSQL or Redis. Do not publish them
while enabling forwarded-header trust.

If Cloudflare proxies the hostname, configure Nginx real-IP handling using only
Cloudflare’s official IPv4/IPv6 ranges and `CF-Connecting-IP`. Nginx must then
overwrite `X-Forwarded-For` with its verified visitor address. Never trust a
client-supplied Cloudflare header from arbitrary source addresses. Review trusted
edge ranges when the provider changes them, and use Full (strict) origin TLS.

The local eFBL Admin reference informed authentication throttling and use of the
last proxy-appended client address. The available municipal infrastructure
reference uses Traefik. Its deployment settings are not copied blindly into this
application, and no external deployment has been validated here.

## Credentials and network boundaries

Platform connector credentials are encrypted in PostgreSQL and are never rendered
back into forms. Blank fields preserve saved credentials; explicit clearing
disables them. Keep `PLATFORM_ENCRYPTION_KEY`, `DJANGO_SECRET_KEY`, database
passwords and `.env` out of Git. Preserve encryption keys separately from backups.
Request-specific HTTP credentials, custom headers and bodies may contain secrets
in PostgreSQL; database backups require protection too.

Authorized probe editors can access internal HTTP services by design. This is not
a public URL-fetching service or an SSRF sandbox. Restrict editor access and use
network-level egress rules to exclude metadata endpoints and unrelated internal
services where required. Do not remove internal monitoring support silently.

Compose already runs application services as an unprivileged user with a read-only
filesystem, dropped capabilities, no privilege escalation, bounded memory and
rotated logs. Dependencies are installed from wheels without a compiler toolchain.
Maintain supported package versions and protect the host, proxy and backups.

## Verification

Run against PostgreSQL:

```sh
docker compose run --rm web python manage.py test monitor
docker compose run --rm web python manage.py check --deploy
```

Use real production environment settings for deployment checks. With a strong
secret and HTTPS enabled, Django still reports `security.W005` and `security.W021`
because subdomain HSTS and preload are intentionally disabled. Do not silence
these warnings or enable their settings merely to obtain an empty report.

Tests cover nonce policies, HTTPS/proxy redirects, HSTS, CSRF, login throttling,
IPv6 aggregation, forwarded-address spoofing and limiter outage behaviour,
alongside business and access tests. Also inspect actual public response headers,
Google sign-in and proxy logging after deployment. Passing tests is not a complete
security audit or evidence that production infrastructure is configured correctly.

## Error reporting and error pages

`SENTRY_DSN` optionally enables error-only Sentry reporting for unhandled Django
and Celery exceptions. Empty disables the SDK. `SENTRY_ENVIRONMENT` defaults to
`production`; `SENTRY_RELEASE` can identify the deployed commit. Tracing,
profiling, session tracking and SDK log export are disabled.

Before sending an event, an allowlist retains exception classes and code locations
(filename/module/function/line), plus event identity, level, environment and
release. It drops exception values, source excerpts, frame locals, HTTP URLs,
OAuth parameters, headers, cookies, request bodies, user identity, breadcrumbs,
SQL and arbitrary extra/context data. This deliberately trades detailed runtime
values for credential safety. Diagnose private runtime details on the protected
server when necessary. SDK transport metadata is handled by Sentry itself.

The 500 page uses no application context processors and falls back to built-in
branding if database access fails. It displays a technical reference recorded in
server logs. It mentions automatic reporting only if the SDK created an event;
this does not guarantee remote receipt. A 404 neither claims to report an incident
nor leaks the requested path. Google cancellation returns to login; invalid OAuth
state remains rejected. Do not weaken state checks to handle cancellation.
