# Project context and rules

## Product

This is a small HTTP monitoring application. It tracks service status, outages and uptime and sends
useful alerts. It serves a trusted shared workspace with few users and probes.

Users add URLs,
configure requests and expected responses, select email, Slack or Telegram, and
review checks and incidents. Django renders pages. Django admin remains available
for platform administrators and recovery operations.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before technical changes and
[STYLEGUIDE.md](STYLEGUIDE.md) before interface changes. [README.md](README.md)
covers installation and operation. Verify current facts in code; documentation
is not evidence of deployment.
Read [SECURITY.md](SECURITY.md) before changing security controls and
[DEPLOYMENT.md](DEPLOYMENT.md) for resource sizing and deployment procedures.

## Language

Write all documentation, code, identifiers, comments and docstrings in English.
This is Gregory’s general preference across projects, also recorded in
CONTRIBUTING.md. User-facing strings follow the product’s French/English language
support. French copy uses Swiss Romand typography: no space before `: ; ? !` and
apostrophes as thousands separators.

## Architecture to preserve

- Python 3.13 in the image, Django 5.2, PostgreSQL, Celery, django-celery-beat and Redis.
- PostgreSQL stores probes, checks, incidents, invitations, profiles, platform
  settings and sessions.
- Redis is the broker; there is no Celery result backend.
- One Python slim image serves web, worker, Beat and migrations in Compose.
- Install wheels only. Do not add C compilation, Node or a frontend framework for
  simple interactions.
- `config/` owns configuration; `monitor/` owns business logic, views, forms,
  tasks, templates and static assets.
- Reuse existing tools. Do not reorganize directories or add dependencies merely
  to imitate another repository.

## Monitoring contract

1. Each probe checks an HTTP(S) URL every 1–5 minutes according to its settings.
2. Beat dispatches due checks every 15 seconds. Actual start times may vary;
   never promise a check at exactly every minute.
3. **No HTTP or Celery automatic retries.** A failed observation schedules a
   confirmation after 60 seconds through durable `next_check_at`, without sleeping
   in the worker. Keep confirming at that interval until the failure threshold
   (at least two) is reached; then return to the normal probe interval.
4. Prevent concurrent checks of one probe with PostgreSQL locking. Expire queued
   tasks rather than catching up in a burst after an interruption.
5. Record observations before attempting notification delivery. A connector
   outage must not discard a check result.
6. Open at most one active incident per probe after the failure threshold.
   Date it from the series’ first failure and close it at the first healthy check.
   An isolated failure still reduces uptime, but sends neither outage nor recovery.
7. Send one accepted outage alert without reminders, followed by recovery.
   A later outage may open a new incident.
8. Rejected delivery stays pending and may be attempted during the next scheduled
   check, without extra HTTP checks or Celery retries. If the outage was never
   reported before it ended, do not send a misleading recovery message.
9. Retain the incident’s channel and destination until closure. Do not promise
   exactly-once delivery: a crash between acceptance and persistence can duplicate
   a message.
10. Notifications support only Postmark, Slack and Telegram. Platform
    administrators configure encrypted database credentials, with environment
    fallback before configuration. Manual request tests create neither uptime
    observations nor incidents; notification tests remain explicit actions.
    The outage simulation button sends a clearly labelled simulation through the
    saved channel without recording monitoring history or changing probe state.
    Real incident emails include HTML and plain text, error type, diagnostic,
    local timestamps, observed duration and a link to the probe.

Core models are `Target` (probe configuration/state), `Check` (observation),
`Incident` (confirmed outage and notifications), `Invitation` (email admission),
`UserProfile` (role/language) and `PlatformSettings` (connectors). Checks and
incidents are read-only in admin. Probe deletion removes its history after
confirmation.

## Uptime and diagnostics

- Distinguish current status, confirmed incidents and missing observations.
- Uptime describes observed time. A check covers at most its configured interval;
  unobserved periods remain unknown.
- Show coverage alongside uptime. Never report a paused probe, future period or
  absence of data as 100% available.
- Every failure affects uptime, even before the alert threshold.
- Statistics use all relevant observations; charts may reduce displayed points.
- Periodic checks cannot measure exact outage duration between observations.
- Compact history nightly at 02:00 Europe/Zurich (03:00 catch-up on the spring
  clock-change night). Retain raw checks for 7 days, failures and hourly statistics
  for 30 days, daily statistics for 365 days, and incidents without automatic expiry.
- Validate UTC rollups before deleting source observations. Preserve unknown time,
  weighted sums/counts, the latest check and the raw boundary anchor. Use persisted
  cutoffs to read disjoint resolutions. Snapshot incident identity and diagnostics
  before purge. Maintenance must not send notifications or perform HTTP requests.
- TLS expiration is a separate diagnostic checked at most daily. It is not an
  independent certificate-expiry alert system.

## Accounts and access

- Minimal public landing page, without open signup.
- Local sign-in at `/login/local/` complements Google. `/admin/` requires platform
  administration rights.
- Invitations create inactive accounts from email addresses. Activation uses a
  local password or the same verified Google email. Signed links last seven days,
  are single-use and revocable; resending invalidates the previous link.
- Google admits active accounts and valid invitations. `OWNER_EMAIL` bootstraps
  the first account only while no accounts exist.
- Three cumulative levels: viewer, editor for all probes, platform administrator
  (connectors, invitations, accounts and Django admin).
- New accounts default to viewer; invitations specify the role. Enforce rights
  server-side on each mutation, not merely in navigation.
- No per-probe ownership. Preserve at least one active platform administrator
  during role and account changes. Role selection saves immediately.
- Administrators can disable, reactivate and delete other accounts. Deactivation
  revokes sessions. Pending invitations still require acceptance. Deletion needs
  confirmation and preserves probes/history. No self-deactivation or self-deletion.
- Profiles support names, username, password and FR/EN language. Without a saved
  language, follow the browser; there is no Auto button.
- Require POST and CSRF for mutations. GET must never send notifications or delete
  data. Do not log secrets or complete connector responses.

## Design principles

General principles were adapted from `eFBL-admin/STYLEGUIDE.md`,
`CONTRIBUTING.md` and `CLAUDE.md`. Conflicting frontend conventions in the source
were resolved into one rule per topic here. FIATA terminology, palette,
dependencies and access rules do not apply.

- Shared shell, reusable components and centralized styles.
- Separate business logic, presentation and network orchestration. Models hold
  data and invariants; CSS classes, icons and colours belong to presentation.
- Name business categories, states and limits with constants or `TextChoices`
  when introducing or modifying them. Avoid scattered sentinel strings.
- Prefer Django functions and the ORM. Use transactions and database constraints
  for invariants and concurrent actions.
- Validate server-side in forms/services. Show errors beside fields without
  discarding entered values.
- Use consistent sections for probe display and editing, shared buttons/states,
  and paginated histories.
- One primary action per group. Describe deletion effects and confirm on the
  dedicated page.
- Links navigate, buttons act. Give icon actions accessible labels and preserve
  keyboard navigation.
- Test meaningful behaviour, errors, access and concurrency. Visually inspect
  layout changes; never invent verification results.

## Approved visual choices

- Navy grid background with cyan/violet accents on internal pages and admin;
  map-and-servers illustration on the landing page. Full-viewport backgrounds and
  nearly opaque panels.
- Michroma Regular 400 for headings and menus; 14 px menus, no synthetic bold.
- Inter elsewhere: body 400, captions 500, actions and metrics 600.
- Bevelled violet primary buttons, dark glass secondary buttons, pink destructive
  buttons; highlights, short shadows, pressed feedback, cyan focus and reduced motion.
- Sidebar width: 264 px on large screens, 248 px at intermediate widths.
  Keep logout on one line with its exit icon.
- No redundant top header, no monitoring label above the menu, no Styleguide
  navigation link. `/styleguide/` remains directly accessible.
- Concise labels and no unnecessary promotional or technical prose in normal flows.

## Scope

Do not spontaneously add multiple organizations or monitoring locations, RUM,
browser screenshots, ICMP/TCP/DNS checks, public subscribers, billing, AI or a
connector catalogue. DNS/TLS/proxy deployment, Google credentials and real
notification accounts are external configuration; do not claim validation
without evidence.
