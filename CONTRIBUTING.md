# Contributing

Read [AGENTS.md](AGENTS.md) for product scope and invariants, then
[STYLEGUIDE.md](STYLEGUIDE.md) for interface rules. [README.md](README.md) covers
installation, operation and backups.

## Language

Gregory’s general rule across projects: write code, identifiers, comments,
docstrings and code documentation in English. Keep technical contribution
instructions in English as well. User-facing copy follows the product’s supported
languages (French and English here), rather than the language of the code.
French interface copy must use Swiss Romand typography: no space before `: ; ? !`.

## Environment and structure

The reference image uses Python 3.13. Tests use PostgreSQL, just like the
application. Dependencies live in `requirements.txt`. Do not copy another
project’s package manager or dependencies without a concrete need. In PyCharm,
identify the configured interpreter before invoking the Python toolchain.

```text
config/                         Django settings, URLs, WSGI and Celery
monitor/models.py               persistent data and invariants
monitor/forms.py                input validation
monitor/views.py                HTTP views, access checks and messages
monitor/permissions.py          shared role checks
monitor/platform.py             platform settings and secret encryption
monitor/tasks.py                probe scheduling and orchestration
monitor/probes.py               HTTP requests and TLS diagnostics
monitor/network.py              per-session IP family selection
monitor/notifications.py         Postmark, Slack and Telegram delivery
monitor/invitations.py          account invitations and activation
monitor/metrics.py              uptime, observation coverage and durations
monitor/auth.py                 account admission and Google integration
monitor/middleware.py           profile and browser language selection
monitor/templates/              pages and reusable components
monitor/static/monitor/         styles, images, icons and local fonts
monitor/migrations/             versioned database migrations
monitor/tests.py                engine, interface and access tests
locale/en/LC_MESSAGES/           English interface translations
```

Keep the structure simple. Split modules when responsibilities require it,
not to introduce an abstract architecture.

## Python and Django

- Follow PEP 8 and keep new Python lines within 120 characters. Group imports.
  Preserve the surrounding conventions without reformatting unrelated code.
- Use descriptive names. Do not shadow built-ins such as `id`, `type`, `list`,
  `filter`, `input` or `format` with variables or parameters.
- Name reusable business limits and categories. Use `TextChoices` for new model
  enumerations instead of scattering string values throughout the code.
- Separate pure computation, network access and persistence. Business models
  must not return CSS classes, colours or icons.
- Prefer the ORM, `get_object_or_404`, named URLs and Django forms. Page routes
  end in `/`; resources such as `favicon.ico` are a natural exception.
- Protect concurrent invariants with PostgreSQL constraints and transactions.
  Form validation does not replace a database constraint.
- Use `related_name` for relations, `blank=True` for optional form fields and
  `null=True` for genuinely absent database values. Avoid representing absent
  text with both empty strings and `NULL` without a reason.
- Use timezone-aware dates, calculate with `timezone.now()` and display in the
  project’s timezone.
- Version migrations and check model/schema consistency. Never change only a
  local database schema.
- Add signals only when necessary and documented. Prefer explicit calls when
  they make an action easier to follow.

## Forms, views and access

- Use `ModelForm` for model editing and plain Django forms for other actions.
- Render labels, fields, help and errors explicitly. Existing components cover
  this need; do not add crispy-forms.
- Redirect after a successful POST. Preserve bound input and errors on failure.
- Mutations require POST and CSRF. Apply `operator_required` to shared read-only
  pages, `probe_editor_required` to probe changes and manual tests, and
  `platform_admin_required` to connectors, invitations and account management.
- Preserve the security controls documented in [SECURITY.md](SECURITY.md).
  Inline scripts require `nonce="{{ request.csp_nonce }}"`; prefer external scripts.
  Do not broaden CSP or trust forwarded headers to work around configuration errors.
- Roles are cumulative: viewer, probe editor, platform administrator. There is
  no per-probe ownership. Hiding a button does not enforce permissions.
- Role selectors save immediately on change through the existing POST endpoint.
  Keep account deactivation and deletion separate from editing permissions.
- New accounts default to viewer; invitations carry the chosen role. Serialize
  account and role changes to preserve an active platform administrator.
- Disabling an account removes its sessions. Reactivation requires a fresh
  sign-in. Pending invitations must still be accepted before activation.
- Account deletion requires a confirmation page. Preserve probes and history.
  Administrators cannot disable or delete their own account from this interface.
- Never trust hidden fields or browser validation.
- Probe URL forms accept a bare hostname and assume HTTPS, including hostnames
  with ports. Preserve explicit HTTP(S) schemes and keep model validation after
  normalization. Native browser URL validation must not block bare hostnames.
- Preserve existing secrets when an edit field is blank. Never render saved
  secrets in plaintext. `platform.py` resolves settings and encrypts credentials.
- Manual HTTP tests must not create observations or incidents. Notification
  tests send real messages and must remain explicit actions.
- Paginate histories and bound rendered data. Chart limits must not truncate
  the observations used to calculate statistics.

## Network and tasks

- No automatic HTTP or Celery retries. Failed observations schedule durable
  confirmation checks after 60 seconds until the threshold (minimum two) is met.
  Use `next_check_at`, not a worker sleep or in-memory delayed-task state.
  Record the initial failure for uptime even if confirmation succeeds silently.
  Keep the normal interval once an incident is confirmed or a check succeeds.
  Respect the schedule and per-probe lock.
- Expire obsolete tasks rather than building a backlog of old checks.
- Configure timeouts, redirects and TLS explicitly. IPv4/IPv6 selection is local
  to the request session; never modify process-wide DNS behaviour.
- Bound response reads. Keep remote bodies and secrets out of logs, diagnostics
  and error messages.
- Validate connector responses, not merely the absence of an exception.
- Distinguish recorded observations, accepted notifications and final delivery.
- Persist rejected notifications and retry delivery during normal scheduled
  checks. Do not introduce a separate Celery retry chain.

## Frontend, translations and admin

Follow [STYLEGUIDE.md](STYLEGUIDE.md). Reuse shared templates and CSS files grouped
by responsibility. Do not duplicate component rules or embed `<style>` blocks in
page templates. Calculated styleguide specimens may use dedicated CSS variables
or style attributes.

Docker enables `STATIC_MANIFEST=true`: `collectstatic` produces content-hashed,
compressed assets through WhiteNoise. Always use Django's `static` tag rather
than hardcoding asset URLs. Rebuild the image after asset changes so browsers
and CDN caches receive the new filenames. Local runserver uses source filenames.

Prefer native HTML and focused JavaScript. Bootstrap, HTMX, Alpine, Tom Select
and a frontend build pipeline are not part of this application; a minor
interaction does not justify adding them.

Mark user-facing strings for translation and maintain the English PO/MO catalog.
The profile’s explicit FR/EN choice overrides browser preferences. With no saved
choice, use the browser language; there is no Auto button.

Keep checks and incidents read-only in Django admin. Organize long forms into
sections when changing them, and make related objects searchable when needed.

## Verification

With the development PostgreSQL stack:

```sh
docker compose run --rm web python manage.py test monitor
docker compose run --rm web python manage.py makemigrations --check --dry-run
```

Cover meaningful behaviour: failure and recovery, thresholds, delays,
deduplication, rejected notifications, absence of retries, missing observations,
HTTP validation, invitation activation, access control and concurrency. Mock
connectors and use local HTTP servers. Automated verification must not send real
notifications.

For visual changes, inspect rendering, wrapping, keyboard access and a narrow
viewport. A simple CSS change does not need tests that restate its declarations.
Report what was verified and any remaining limitations.

Demo data and local test credentials must never be introduced in migrations or
become production accounts.
