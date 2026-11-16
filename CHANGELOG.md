# Changelog

This file records notable changes for users and operators.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.5] - 2026-11-16

### Added

- Public, read-only `/health/` endpoint for independent monitoring. Return HTTP
  503 when PostgreSQL or the shared cache is unavailable, dispatch has not run
  within three minutes, or an active probe is over three minutes overdue.

### Fixed

- Include the application version on fallback error pages as well.

## [1.0.4] - 2026-11-16

### Added

- Display the running application version in the shared page footer, including
  sign-in pages and Django admin.

## [1.0.3] - 2026-11-16

### Changed

- Check failing probes every 60 seconds throughout an outage, including after
  confirmation; restore the configured interval after the first healthy check.
  Alert confirmation, deduplication and uptime accounting remain unchanged.

## [1.0.2] - 2026-11-15

### Added

- Versioned x86-64 and ARM64 container images published to GitHub Container
  Registry after PostgreSQL tests pass on release tags.
- A standalone registry Compose configuration and installation/upgrade
  instructions that require no source checkout or local image build.
- OCI version/revision labels, build provenance and a software bill of materials;
  published image versions provide the default Sentry release identifier.

### Security

- Exclude environment variants, local credentials and database dumps from image
  build contexts; keep registry publication permissions scoped to its CI job.

## [1.0.1] - 2026-11-15

### Fixed

- Nightly history maintenance no longer requires aggregate buckets from before
  a probe was created. Existing young probes recover without altering statistics
  or disabling missing-aggregate safeguards.
- Preserve static maintenance-integrity diagnostics in Sentry while continuing
  to exclude arbitrary exception messages and private runtime values.

### Removed

- Secondary observation-coverage percentages from the dashboard, probe cards,
  detail statistics and history tooltips. Availability calculations still
  distinguish failed observations from missing data.

## [1.0.0] - 2026-11-14

### Added

- HTTP(S) probes with configurable 1–5 minute intervals, request methods,
  authentication, headers, bodies, IP version, TLS validation, redirects and
  expected responses.
- Durable failure confirmation after 60 seconds, confirmed incidents and one
  outage alert followed by recovery through Postmark, Slack or Telegram.
- HTML and plain-text incident emails with failure diagnostics, readable dates
  and durations, service identity and links to probe details.
- Time-weighted uptime, observation coverage, response charts, TLS expiration,
  filtered failure history and incident statistics. Isolated failures count
  toward downtime without triggering notifications.
- Nightly history compaction at 02:00 Europe/Zurich: seven days of detailed checks,
  30 days of failed checks and hourly statistics, and 365 days of daily statistics.
  Incident diagnostics remain available after source observations are removed.
- Invitation-only local and Google authentication, viewer/editor/platform-admin
  roles, account management, profiles and saved French/English preferences.
- Configurable product name and logo, shared dark visual identity, mobile
  navigation drawer, probe cards and stacked histories on small screens.
- Explicit request tests and outage notification simulations without changing
  monitoring state or statistics.
- Optional Sentry reporting for Django and Celery, branded 404/500 pages and
  technical references for server errors.
- Docker Compose deployment with PostgreSQL, Redis and Celery Beat, bounded
  resources, restart support, backup tooling and operational documentation.

### Fixed

- Google consent cancellation returns to login instead of causing a server error;
  invalid OAuth state remains rejected.
- HTTPS form submissions preserve same-origin referrers for CSRF validation.
- Content-hashed assets prevent stale styles after deployment.

### Security

- Enforce server-side roles, CSRF protection, authentication throttling, secure
  production cookies and browser security headers.
- Encrypt platform credentials, preserve secrets during settings edits and omit
  sensitive URLs and request bodies from production access logs.
- Validate bounded logo uploads, remove raster metadata and isolate public SVG
  responses with a sandbox policy.
- Filter Sentry events to omit request data, OAuth parameters, user identity,
  exception values, source excerpts and local variables.

[Unreleased]: https://github.com/gfavre/probe/compare/v1.0.5...HEAD
[1.0.5]: https://github.com/gfavre/probe/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/gfavre/probe/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/gfavre/probe/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/gfavre/probe/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/gfavre/probe/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/gfavre/probe/tree/v1.0.0
