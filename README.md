# HTTP monitoring

A small HTTP monitoring application for a private shared workspace.
Server-rendered Django, PostgreSQL, Celery/Beat and Redis share a small deployment.
Application services use one Python slim image, without Node or dependency compilation.

## Design documentation

- [AGENTS.md](AGENTS.md): product context, architecture and business invariants.
- [CONTRIBUTING.md](CONTRIBUTING.md): development conventions and verification.
- [STYLEGUIDE.md](STYLEGUIDE.md): visual identity, navigation and components.
- [SECURITY.md](SECURITY.md): security controls, proxy requirements and verification.
- [DEPLOYMENT.md](DEPLOYMENT.md): small-server sizing and a complete Docker deployment walkthrough.

The visual reference is available at `/styleguide/`, without a navigation link.
All project documentation, code, comments and docstrings must be in English.
The interface supports French and English.

## Features

- Illustrated landing page with local or Google sign-in.
- Email invitations: pre-created accounts, local or Google activation, seven-day links.
- Three access levels: viewer, editor for all probes, platform administrator.
- Postmark, Slack, Telegram and Google settings on the Platform page.
- Personal profile and FR/EN interface with automatically saved language preference.
- Create, edit, pause and delete probes through the interface.
- Probe details: response chart, average and latest response, TLS expiration,
  90-day history and statistics over 24 hours, 7, 30 and 90 days.
- Paginated failure history filtered by HTTP status or network error.
- Incidents with start, confirmation, end, duration and notification tracking.
- One notification channel per probe: **Postmark email, Slack or Telegram**.
- Configurable HTTP method, Basic/Bearer authentication, raw/JSON/form body,
  headers, IPv4/IPv6, TLS validation, redirects and cache bypass.
- Expected status codes or ranges, text with optional case-insensitive matching,
  and response headers.
- Configuration preview and manual tests of saved settings.
- Manual outage simulation through each probe’s saved notification channel,
  without changing probe state, checks, incidents or uptime statistics.

The application is a simple shared workspace. Multiple monitoring locations,
subscribers, billing, an integration catalogue, screenshots, ICMP, TCP, DNS, RUM
and cron monitoring are outside this version’s scope.

## Installation

Run the monitor on a different machine from the services being monitored.

```sh
cp .env.example .env
```

Configure `.env`:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Long random secret unique to this installation |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `OWNER_EMAIL` | First Google account address, optional bootstrap |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `POSTMARK_SERVER_TOKEN` | Postmark server token for email notifications |
| `EMAIL_FROM` | Sender address verified in Postmark |
| `EMAIL_TO` | Default email recipient |
| `SLACK_WEBHOOK_URL` | Incoming webhook associated with the Slack channel |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Default Telegram conversation |
| `PUBLIC_URL` | Public URL used in invitation links |
| `PLATFORM_ENCRYPTION_KEY` | Optional stable Fernet key for stored credentials |

Configure only the channels you use. Individual probes can override the email
recipient or Telegram conversation. Slack uses the channel associated with the
global webhook. Administrators can configure connectors on **Platform**
(`/platform/`). Environment variables provide initial values; database settings
take precedence once saved, without a restart.

Secrets are encrypted and never displayed again. Leaving a secret field blank
preserves it; its clear checkbox explicitly removes it. Never commit `.env`.
Encryption uses `PLATFORM_ENCRYPTION_KEY` or, if absent, a key derived from
`DJANGO_SECRET_KEY`. Keep that key separately alongside backups: replacing it
makes existing encrypted secrets unreadable.

### Google

Create a **Web application** OAuth client in Google Cloud with this exact
allowed redirect URI:

```text
https://status.example.com/accounts/google/login/callback/
```

Configure the consent screen to allow the `OWNER_EMAIL` account to use the app.
Enter client credentials in `.env` or on Platform.
The integration uses [django-allauth’s Google provider](https://docs.allauth.org/en/latest/socialaccount/providers/google.html),
OAuth CSRF/state protection and PKCE. A verified Google address must match an
active account or a valid pending invitation. `OWNER_EMAIL` can bootstrap only
the very first Google account while no accounts exist. There is no public signup.

Google is optional. Local sign-in at `/login/local/` supports all roles;
`/admin/` is restricted to platform administrators. An active local account can
also connect through Google using the same verified email, while retaining its
local password.

### Notifications

For email, configure the Postmark **server** token and a verified sender address
or domain. The sender address is required in addition to the API key. Set a
default recipient or override it for individual probes.
The [Postmark API](https://postmarkapp.com/developer/api/email-api) acknowledges
submission; that does not guarantee final delivery. The default message stream
is `outbound`.

For Slack, create an [incoming webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)
associated with the desired channel. In [Your Apps](https://api.slack.com/apps),
create or select an app, open **Incoming Webhooks**, enable the feature and
choose **Add New Webhook to Workspace**. Copy the resulting URL into Platform.

For Telegram, create a bot with BotFather, start a conversation with it or add it
to a group, then configure the bot token and conversation ID. Notifications use
[`sendMessage`](https://core.telegram.org/bots/api#sendmessage).

### Startup

```sh
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

The `migrate` service applies migrations before web, worker and Beat start.
Run only one Beat instance.

The application listens on `127.0.0.1:8000`. Point DNS at the server and configure
its existing reverse proxy to terminate TLS and forward
`https://status.example.com` to `http://127.0.0.1:8000`.
This repository does not provision DNS, certificates or the proxy.
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are configured for this domain.
`HTTPS=true` enables secure cookies.
It also enables HTTPS redirects, HSTS and shared Redis authentication limits.
Set `TRUST_PROXY_HEADERS=true` only when your reverse proxy overwrites forwarded
headers and clients cannot bypass it. See [SECURITY.md](SECURITY.md) before deployment.

For local HTTP testing, use `HTTPS=false` and register a local OAuth callback in
Google Cloud if needed. There is no authentication bypass mode.

Sign in with the initial local account or Google, add a probe, select its
channel, then use **Send a test** on the Alerts tab and verify receipt. This sends
a real message. **Test request** runs a manual HTTP check of the saved settings
without changing statistics or sending alerts.

On a saved probe’s settings page, **Simulate an outage** sends an explicitly
labelled simulation through its saved channel and destination. It creates no
checks or incidents, changes no probe state or statistics, and starts no automatic
recovery notification. Rejected delivery is reported immediately rather than
queued for a later monitoring check.

### Invitations and accounts

The **Invitations** page creates an account from its email and sends a Postmark
activation link. Accounts remain inactive until acceptance. Links are signed,
valid for seven days and single-use. Resending replaces the previous link;
revoking blocks activation. If delivery fails, administrators can copy the link
for manual sharing.

Invitees choose a local password of at least 12 characters or use a verified
Google account matching the invited address. The invitation specifies the role:
viewer by default, editor for all probes, or platform administrator. Only
platform administrators manage invitations, permissions and connectors. There
is no per-probe ownership. Local accounts remain usable when Google is unavailable.

Platform lists active and inactive accounts. Role changes save immediately when
a new value is selected. Deactivation revokes sessions; reactivation requires a
fresh sign-in. Deletion requires confirmation and preserves probes and history.
Administrators cannot disable or delete their own account through this interface;
the last active platform administrator is protected. Pending invitations must be
accepted before activation. The migration preserves existing accounts’ access.

Click your identity in the navigation footer to edit your profile. FR/EN buttons
save the language preference immediately; without a saved preference, the browser
language applies. The profile does not allow changing the email tied to Google
and invitations.

Internal pages and admin use the grid background; the landing page uses the
map-and-servers illustration. Both cover the viewport and are served locally
from `monitor/static/monitor/`.

## Checks and incidents

Each probe has a whole-minute interval **from 1 to 5 minutes** (default 3), a
1–15 second timeout (default 10), a consecutive-failure threshold (minimum/default 2)
and a minimum notification delay (default 0).

Beat dispatches due checks every 15 seconds. PostgreSQL locking prevents
simultaneous checks of a probe. Queued tasks expire after 30 seconds. There are
no automatic HTTP or Celery retries. A failure schedules a confirmation check
after 60 seconds using durable database state, without sleeping in the worker.
Until the configured threshold is reached, further failures keep that shorter
confirmation interval. With the default threshold, one failed check followed by
a healthy confirmation affects uptime but sends no messages. After confirmation
or recovery, the normal interval resumes. Worker load and dispatching can add a
small delay: confirmation typically starts after 60–75 seconds, not at an exact
deadline. Results become stale after their recorded coverage interval plus a
15-second dispatch margin.

- A confirmed failure opens an incident dated from the first failure in the series.
- Send one accepted outage notification, without reminders during the incident.
- The first healthy check closes the incident and sends recovery only if the
  outage was reported.
- Rejected delivery remains pending in the database and is retried during the
  next scheduled check, without a Celery retry or extra HTTP probe.
- An outage that ends before its notification is accepted produces no recovery
  message. Each incident retains its notification channel and destination.

The minimum notification delay is evaluated during scheduled checks, not by a
retry task. State survives restarts. A crash between message acceptance and the
database update can produce a duplicate. Deduplication is per probe: two failing
URLs on the same server open two incidents.

Incident emails include both HTML and plain text: service and URL, failure type,
diagnostic, first failure, confirmation, recovery where applicable, observed
duration, latest response and a direct probe link. Times use the configured local
timezone and durations use readable units. DNS, TLS, connection failures and
timeouts are distinguished for newly recorded checks without storing raw exceptions.

Monitor an application health endpoint such as
`https://service.example.com/health/`. A static page returning 200 does not
prove that Django, its dependencies or its workers are functioning.

URLs containing credentials, query strings or fragments are rejected to avoid
exposing secrets in diagnostics. Configure HTTP credentials separately.
Custom request bodies and headers are stored in PostgreSQL and may contain
secrets; protect backups. Remote response bodies are not stored. Text matching
reads at most the first MiB. Redirects are limited to five; the HTTP client strips
Authorization credentials on a host change. Internal URLs are allowed.

TLS certificate expiration for the configured host is checked at most daily with
normal certificate validation. Errors produce no expiration date. This diagnostic
is separate from scheduled HTTP requests and does not send certificate-expiry
reminders.

## Statistics

Each check records time, result, optional HTTP status, a short diagnostic and
response time. Incidents retain their timestamps and accepted notifications.
Failure history supports pagination and filtering.

A result covers time until the next check, capped at the interval configured
when that result was recorded. Uptime is healthy time divided by observed time;
coverage is observed time divided by the reporting period. Time before probe
creation is excluded. Unobserved periods remain unknown, never assumed healthy.
Every failed check counts toward uptime, including failures below the alert
threshold. Incident duration reflects confirmed incidents and can differ from
failed-check duration.

The chart displays at most 240 points; statistics use all relevant
observations or aggregates. Measurements are periodic, not continuous. Pausing a probe retains
its data; deleting one removes its checks and incidents after confirmation.
History maintenance runs nightly at 02:00 Europe/Zurich through Celery Beat.
All individual observations are retained for 7 days; failed observations for
30 days. Hourly statistics remain for 30 days, daily statistics for 365 days.
Incidents retain their service identity and diagnostic snapshots without expiry.
The latest observation and one boundary anchor are preserved even for paused
probes. Cleanup is conservative around UTC bucket boundaries and only follows
validated aggregation. It never sends alerts or creates observations.

Availability and coverage combine disjoint raw, hourly and daily ranges using
persisted compaction boundaries. Durations and response-time sums/counts are
added before computing ratios or means. Unknown time remains unknown; isolated
failures still count. Archived period starts are rounded down to the available
UTC hour/day and shown on the probe page. Daily history uses UTC calendar days.
The 30-day chart combines hourly means/maxima with recent individual checks;
there are no invented historical percentiles.

Platform administrators can inspect policy, progress, sizes and maintenance
warnings on the platform page and read-only per-probe progress in Django admin.
See [DEPLOYMENT.md](DEPLOYMENT.md#nightly-history-maintenance) for commands,
settings, rollout and recovery. For a read-only preview:

```sh
docker compose exec -T web python manage.py maintain_history --dry-run
```

## Operations and tests

One Python slim image uses wheels only, without C compilation or a frontend
build. Redis is the broker without a Celery result backend; PostgreSQL is durable
storage. One probe child process and one web worker with two threads serve a
small URL list. PostgreSQL uses a 64 MiB shared buffer pool and 30 connections;
database and Redis containers have explicit memory limits. See
[DEPLOYMENT.md](DEPLOYMENT.md) for sizing assumptions and capacity checks.

```sh
docker compose logs -f worker beat
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > uptime.sql
docker compose run --rm web python manage.py test monitor
docker compose run --rm web python manage.py makemigrations --check --dry-run
```

Tests use PostgreSQL, a local HTTP server and mocked connectors. They send no
real messages and do not perform a real Google login. Back up PostgreSQL and
test restoration. A stopped monitor cannot report its own failure; use an
external check.

This version replaces the Go/Signal probe. Legacy `DOMAINS`, `SIGNAL_*`,
`REMINDER_INTERVAL` and `state.json` are not imported.

The interface and admin use a dark navy, cyan and violet theme, with favicons
derived from the supplied illustration. Assets are served locally without a CDN.

## Styleguide

The authenticated `/styleguide/` page collects the palette, calculated contrasts,
typography, an Inter/Michroma menu comparison, components and simulated states
without modifying probes. Panels remain nearly opaque over the full-page backgrounds.

- Michroma Regular 400 for headings and menus (14 px), without synthetic bold.
- Inter variable 400–700 elsewhere: body 400, captions 500, actions and metrics 600.
- Tabular numerals, cyan focus indicators and stronger input borders.
- Local Latin and Latin Extended WOFF2 fonts with OFL licences in
  `monitor/static/monitor/fonts/`; no browser requests to Google Fonts.

Documented contrast ratios cover listed colour pairs, not a complete accessibility
audit. Shared `monitor/static/monitor/buttons.css` provides bevelled violet primary
buttons, dark glass secondary buttons and pink destructive buttons. Highlights,
short shadows, pressed states and cyan focus apply across the application,
login and admin. Reduced-motion preferences disable animations.

The supplied SVG logo appears on the landing page, navigation and admin. Lettering
is outlined to preserve Michroma rendering. Both original and production versions
are kept in `monitor/static/monitor/brand/`.
