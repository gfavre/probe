# Small-server deployment

## Recommended size

Use **Debian 13 stable (Trixie), x86-64**, with **1 shared vCPU, 2 GiB RAM and
25 GB SSD** as the practical starting size for a handful of probes and occasional
administration. This is a sizing recommendation, not a measured capacity guarantee.
Avoid a 1 GiB machine: PostgreSQL, Django, Celery and image builds leave too little
headroom when running together.

A small [Hetzner shared cloud instance](https://www.hetzner.com/cloud/) in Germany
is a reasonable low-cost choice. The [CX23 reference size](https://www.hetzner.com/cloud/cost-optimized/)
is 2 shared vCPUs, 4 GB RAM and 40 GB storage. Choose it if available and economical;
that RAM is headroom, not an application requirement. The public listing showed
limited/unavailable stock when checked on 13 November 2026, so verify regional
availability and the total including IPv4, VAT and backups in the console.
A 5–10 EUR monthly VM budget is a planning estimate, not a provider quote. A
cheaper existing 2 GiB VM on a separate host is equally suitable.

[Debian lists version 13 as stable](https://www.debian.org/releases/), and
[Docker supports Debian 13](https://docs.docker.com/engine/install/debian/).
No managed database, load balancer or cluster is required. Keep this machine
separate from monitored services so their host failure does not also stop alerts.
The following walkthrough uses generic domains and repository URLs deliberately.

## Resource budget

Compose uses one application image for web, worker, Beat and migrations:

| Service | Configuration | Container memory ceiling |
| --- | --- | --- |
| Web | One Gunicorn worker, two threads | 192 MiB |
| Worker | One Celery prefork child | 320 MiB |
| Beat | One scheduler | 128 MiB |
| PostgreSQL | 64 MiB shared buffers, 2 MiB work memory, 30 connections | 384 MiB |
| Redis | 64 MiB dataset limit, no eviction | 128 MiB |

These ceilings total 1'152 MiB; they are limits, not reserved or measured usage.
Allow additional memory for Debian, Docker, Nginx, filesystem cache and temporary
build/migration processes. Start with 2 GiB and check actual consumption before
reducing further. Defaults avoid extra worker processes and a frontend build.

Use three- or five-minute probe intervals where one-minute detection is not
needed. A single worker runs checks sequentially. Many slow/time-out checks,
TLS diagnostics or connector delays can delay other probes; queue tasks expire
after 30 seconds. Monitor stale probe states and worker delays before increasing the probe count.
Increase concurrency to two only if the schedule falls behind and RAM permits it.
Low dashboard traffic alone does not determine probe workload.

Storage grows with retained observations: ten probes at three-minute intervals
produce about 4'800 checks per day. Nightly compaction bounds raw history. Check database,
Docker and backup sizes rather than assuming the initial disk will last forever.

## 1. Prepare the VM

Create a fresh Debian 13 VM with SSH key authentication. Use a provider firewall:
allow SSH only from your administration addresses and allow public TCP 80/443.
Do not expose 8000, 5432 or 6379. Keep outbound DNS, HTTPS and monitored endpoints
reachable. Configure IPv6 rules too if publishing an AAAA record.

Commands below run as root on this dedicated VM (use `sudo -i`). Do not run them
on an unrelated machine with existing Docker/Nginx services without adapting paths.
Docker group membership grants root-equivalent access; it is unnecessary here.

```sh
apt-get update
apt-get upgrade -y
apt-get install -y ca-certificates curl git openssl nginx certbot python3-certbot-nginx unattended-upgrades
systemctl enable --now nginx
```

Enable unattended security updates using the distribution’s configuration:

```sh
dpkg-reconfigure -plow unattended-upgrades
```

Reboot after kernel updates when needed. Keep SSH key access working before
changing SSH policies. Use the provider firewall as the external boundary:
Docker-published ports can bypass ordinary UFW rules.

## 2. Install Docker Engine and Compose

On a fresh Debian installation, add Docker’s official signed package repository:

```sh
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version
```

If Docker is already installed from Debian’s packages, resolve conflicts using
[Docker’s Debian installation instructions](https://docs.docker.com/engine/install/debian/)
instead of replacing packages blindly. No Docker Desktop is needed.

## 3. Install the application and configure secrets

Use your repository’s actual clone URL, with a read-only deploy key if private.
Do not place an access token in a clone URL or in this document.

```sh
install -d -m 0750 /opt/http-monitor
git clone <YOUR_REPOSITORY_URL> /opt/http-monitor
cd /opt/http-monitor
umask 077
cp .env.example .env
chmod 600 .env
```

Generate independent values for `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`:

```sh
openssl rand -hex 32
openssl rand -hex 32
```

Generate a URL-safe Fernet key for `PLATFORM_ENCRYPTION_KEY`:

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

Enter these generated values in `.env` using an editor. Keep the encryption key
stable and back it up separately. Do not commit `.env` or paste it into support
logs. Set the deployment-specific values:

```dotenv
ALLOWED_HOSTS=status.example.com,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://status.example.com
PUBLIC_URL=https://status.example.com
HTTPS=true
TRUST_PROXY_HEADERS=true
SECURE_HSTS_SECONDS=3600
CACHE_URL=redis://redis:6379/1
```

Replace `status.example.com` everywhere with your own hostname. Keep the default
Compose database/broker hostnames and database/user names. Leave `OWNER_EMAIL`
empty if bootstrapping with a local superuser. Notification and Google credentials
can be configured later through Platform. Postmark needs both its server API
token and a sender address verified by Postmark.

The proxy setup below is required before enabling forwarded-header trust.
See [SECURITY.md](SECURITY.md) for the assumptions. Restrict `.env` and private
repository access to the administrator.

## 4. Build and start

```sh
cd /opt/http-monitor
docker compose config --quiet
docker compose build --pull migrate
docker compose up -d --no-build
docker compose ps -a
docker compose logs --tail=50 migrate web worker beat
```

One image is built and reused. Dependencies install from wheels without compiler
or frontend tooling. PostgreSQL and Redis must become healthy; the migration
service must exit successfully before the application services start. Run exactly
one Beat instance. Containers restart automatically with Docker after a reboot.

Create the initial administrator interactively:

```sh
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py check --deploy
```

With valid production settings, the deliberate HSTS subdomain/preload warnings
are described in SECURITY.md. Do not suppress them just to clear the report.
A direct HTTP request to port 8000 redirects to HTTPS; this is expected.

## 5. DNS, reverse proxy and TLS

Point the hostname’s A record to the VM. Add AAAA only if IPv6 is configured and
reachable. The application stays on `127.0.0.1:8000`; Nginx runs on the host.

Create `/etc/nginx/conf.d/monitor-logging.conf`:

```nginx
log_format monitor '$remote_addr $request_method $status $request_time';
```

This access format omits paths and query strings to avoid logging invitation
links and OAuth callback codes. Protect and rotate proxy error logs as they may
still contain request details.

Create `/etc/nginx/sites-available/http-monitor`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name status.example.com;
    server_tokens off;
    access_log /var/log/nginx/monitor-access.log monitor;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
    }
}
```

Enable the site and obtain the certificate:

```sh
ln -s /etc/nginx/sites-available/http-monitor /etc/nginx/sites-enabled/http-monitor
nginx -t
systemctl reload nginx
certbot --nginx --redirect -d status.example.com
systemctl enable --now certbot.timer
certbot renew --dry-run
```

Answer Certbot’s prompts using your administration email. Port 80 must be public
for HTTP certificate validation and renewal. Certbot adds HTTPS and HTTP redirects;
verify the resulting server block retains the proxy headers and access-log format.
Reject unknown hostnames in a default virtual host if this server hosts other sites.
If an existing trusted proxy already handles TLS, reuse it rather than installing
a second proxy; implement the same header and logging requirements.

## 6. Acceptance checks

### Managed startup and local backups

The repository includes systemd units for explicit Compose startup on boot and
daily backups. After installing the application in `/opt/http-monitor`, install them:

```sh
install -m 0644 deploy/http-monitor.service /etc/systemd/system/http-monitor.service
install -m 0644 deploy/http-monitor-backup.service /etc/systemd/system/http-monitor-backup.service
install -m 0644 deploy/http-monitor-backup.timer /etc/systemd/system/http-monitor-backup.timer
install -m 0750 deploy/backup.sh /usr/local/sbin/http-monitor-backup
systemctl daemon-reload
systemctl enable --now http-monitor.service http-monitor-backup.timer
systemctl start http-monitor-backup.service
```

The application unit starts Compose after Docker and networking, waits for
database health/migrations, and stops containers cleanly during shutdown.
Docker restart policies handle runtime process failures. The backup timer runs
daily around 03:15 local server time and catches missed runs after reboot. Dumps
and environment copies are root-readable only, with about seven days of retention;
the script verifies each dump’s archive listing before publishing it. This does
not replace a full restoration test or an off-host encrypted backup.

With the service installed, use `systemctl restart http-monitor` after updates
and verify `systemctl status http-monitor`. Keep the Docker and Nginx services,
Certbot timer and backup timer enabled. Validate a planned VM reboot before relying
on unattended recovery.

### Public response checks

```sh
curl -I http://status.example.com/login/
curl -I https://status.example.com/login/
docker compose ps -a
docker stats --no-stream
free -h
df -h
```

Check HTTP redirects to HTTPS and HTTPS responds successfully with CSP,
HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: same-origin`. Test local sign-in, then configure Google if wanted.
Its callback is `https://status.example.com/accounts/google/login/callback/`.

Configure a channel on Platform, add one probe and explicitly send a notification
test. Verify receipt and recent probe results after several scheduled checks.
Test an outage/recovery against a disposable endpoint, not by stopping a real
service. The monitor cannot detect its own host outage; add an independent
external check.

## 7. Backups and restoration

PostgreSQL holds all durable application data. Back up nightly and before upgrades.
A simple restricted custom-format dump is:

```sh
cd /opt/http-monitor
install -d -m 0700 /var/backups/http-monitor
umask 077
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "/var/backups/http-monitor/database-$(date +%F-%H%M%S).dump"
```

Check the command’s exit status before considering a backup successful. Schedule
it using a root-owned cron job or systemd timer with failure reporting. Keep, for
example, seven daily copies plus an off-host encrypted copy. A local dump or cloud
snapshot alone does not survive loss of the VM. Keep `.env` and encryption keys in
a separate protected backup; a database dump alone cannot decrypt connector secrets.

Test restoration on a separate empty deployment using the same PostgreSQL major
version and required keys. Once its empty database is healthy:

```sh
docker compose stop web worker beat
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' < database.dump
docker compose up -d --no-build
```

Keep external notifications disabled or network-isolated on the restoration test
host so restored probes cannot send real alerts. Do not run this restore command
against an existing production database. Never run `docker compose down -v` on a
deployment whose data must be retained.

## 8. Updates and maintenance

If the repository is not published, deploy a source archive rather than giving
the server a personal Git credential. On the development machine:

```sh
git ls-files --cached --others --exclude-standard -z > /tmp/http-monitor-files.list
COPYFILE_DISABLE=1 tar --no-xattrs -czf /tmp/http-monitor-source.tgz --null -T /tmp/http-monitor-files.list
scp /tmp/http-monitor-source.tgz <SSH_ALIAS>:/tmp/
```

This includes current source changes while respecting Git ignore rules. Review
the file list first; never include `.env`, local databases or generated files.
The `--no-xattrs` flag prevents macOS metadata files in the Linux deployment.
On the server, extract into `/opt/http-monitor` without replacing its protected
`.env`, then build the image and restart the managed application service.
For subsequent archive deployments, repeat that upload instead of `git pull`.
Record the source archive checksum and revision for traceability. Rebuild for the
server’s architecture rather than copying an image built for a different CPU.

Back up first and record the current commit ID. This simple update procedure
accepts a short monitoring interruption and preserves the database volume:

```sh
cd /opt/http-monitor
git rev-parse HEAD
git pull --ff-only
docker compose build --pull migrate
docker compose down
docker compose up -d --no-build
docker compose ps -a
docker compose logs --tail=50 migrate web worker beat
```

Building before stopping shortens downtime. Migrations rerun before web/worker
startup. Missing observations remain unknown; downtime is not reported as healthy.
Database migrations may prevent a simple code rollback: use a tested restore
procedure if necessary. Do not automatically upgrade PostgreSQL to another major
version by editing the image tag; plan and test its data migration separately.

Review `docker stats`, database growth, disk space, backup success and certificate
renewal. Images and build cache accumulate. Prune unused build cache/images only
after retaining the rollback image you need; never prune volumes indiscriminately.
Avoid running the full PostgreSQL test suite on the smallest production VM while
it monitors services; use development or staging for tests.

This walkthrough was reviewed against the repository’s configuration and official
Debian/Docker documentation. The image built successfully, all 65 tests passed
inside Python 3.13, and an isolated Gunicorn container returned the expected HTTPS
redirects and security headers. A fresh Debian 13 VM with 2 GiB RAM was also
deployed using the source-archive route. Public HTTPS, real certificate issuance,
simulated certificate renewal, a Celery check and a database archive were verified.
A real VM reboot restored Docker, Nginx, the application, worker, scheduler and
timers. Immediately after reboot, the whole VM reported approximately 650 MiB of
used RAM with an empty production database. This is an idle observation, not a
capacity or load test. Provider firewall, DNS and resource use must still be checked
for each new installation.

## External health monitoring

Configure an independent monitor to request `https://<your-domain>/health/`
every minute and require HTTP 200. The public GET/HEAD endpoint returns only
`status` and `version`; it does not run checks, send notifications or write data.
It returns HTTP 503 if PostgreSQL or the shared cache fails, if the dispatch task
has not completed within 180 seconds, or if an enabled probe is more than
180 seconds overdue. Monitored service outages do not themselves make it fail.

Beat sends the dispatch task every 15 seconds through the broker to the worker.
After successful dispatch, the worker refreshes a shared-cache heartbeat, even
with no enabled probes. Production must use the configured shared Redis cache;
the development process-local cache cannot share worker heartbeats. On initial
startup or after a cache reset, health stays unavailable until dispatch runs.
Allow the three-minute grace period plus the external monitor's polling and
confirmation delay before expecting an alert. Connector delivery and TLS renewal
are not tested by this endpoint. An external monitor is required to detect a
complete host or network outage.

## Nightly history maintenance

Celery Beat schedules `monitor.tasks.maintain_history` at **02:00 Europe/Zurich**.
Beat and the worker must both be running; there is no host cron or extra container.
A 03:00 guard catches up only on the spring night when local 02:00 does not exist.
Repeated autumn runs are harmless because compaction and deletion are idempotent.
If the host misses the schedule, the next nightly run catches up from persisted
per-probe watermarks. Run the command manually when immediate catch-up is needed.

Default retention is 7 days of all checks, 30 days of individual failures,
30 days of hourly statistics and 365 days of daily statistics. UTC bucket
boundaries and the nightly schedule can leave up to about one extra day of data.
The latest check and the observation preceding the raw boundary are protected.
Incident diagnostics and service identity are copied into each incident before
source deletion; incidents have no automatic expiry.

```sh
# Read-only policy/count preview.
docker compose exec -T web python manage.py maintain_history --dry-run
# Build and verify aggregates and incident snapshots without deleting sources.
docker compose exec -T web python manage.py maintain_history --no-purge
# Run the complete nightly maintenance now; optionally add --target <id>.
docker compose exec -T web python manage.py maintain_history
```

The command drains bounded units synchronously. Scheduled maintenance instead
queues one probe-day or one deletion batch at a time, allowing probe tasks to run
between units. It neither sends notifications nor performs HTTP checks. Advisory
locks prevent overlapping maintenance; short transactions share the probe lock
with the observation writer and notification sender. Do not use a transaction
pooler for these session advisory locks without adapting the locking strategy.

For the first rollout, back up PostgreSQL, apply migrations, run `--no-purge`,
compare raw and aggregated statistics, then run normal maintenance. Each hourly
batch is checked against its raw source. Missing aggregates refuse the related
purge, and a failed batch does not advance its watermark. Worker failures are
recorded as static integrity codes or exception class names without secret diagnostics. The platform page
shows retention, row counts, table/index sizes, failures and delays over 48 hours;
Django admin exposes read-only per-probe progress. Check the worker logs for
unexpected failures.

Environment settings are `CHECK_RETENTION_DAYS=7`, `FAILURE_RETENTION_DAYS=30`,
`HOURLY_RETENTION_DAYS=30`, `DAILY_RETENTION_DAYS=365`. They must satisfy
`1 <= checks <= failures <= hourly <= daily`. Raising retention later does not
restore deleted observations or their original resolution. Restore a backup to
recover deleted raw measurements. Retention changes do not affect existing
backup rotation. Keep autovacuum enabled: ordinary deletion makes PostgreSQL
space reusable without necessarily shrinking files on disk. Do not schedule
`VACUUM FULL` as routine maintenance.

## Sentry

Set `SENTRY_DSN` to the project's Client Key DSN in the protected server `.env`.
Do not commit a real DSN. Optionally set `SENTRY_ENVIRONMENT=production` and
`SENTRY_RELEASE=<commit>`. Recreate web, worker and Beat after environment edits;
restarting an existing container does not reload Compose environment values.
Leaving the DSN empty disables external error reporting.

Reporting covers uncaught Django/Celery exceptions, with strict payload filtering
and no tracing or profiling. See [SECURITY.md](SECURITY.md#error-reporting-and-error-pages)
for retained fields. A branded 500 page shows its reference; the branded 404 is
an ordinary missing-page response. Expected Google cancellation is not a server
error. Validate SDK transport with one intentional, caught diagnostic exception;
do not add a public endpoint that deliberately crashes the application.

## Published images

Use `ghcr.io/gfavre/probe:1.0.5` for a versioned image supporting x86-64 and ARM64.
The registry Compose file has the same services, resource limits, protected ports
and persistent PostgreSQL volume as the source-build configuration, without any
`build` entry. `PROBE_IMAGE` selects a release or immutable `@sha256:` digest.

For a fresh deployment, follow VM/Docker preparation in sections 1–2, then use the
[README image installation](README.md#install-a-published-image) instead of cloning
and building. Generate secrets and configure the domain as in section 3; continue
with reverse proxy/TLS and acceptance checks in sections 5–6. The downloaded
registry file is saved as `compose.yaml`, so the existing backup and systemd
commands continue to work without a source checkout. Preserve the deployment
directory/project name: changing it can select a different PostgreSQL volume.

Images are public only once the package's visibility is set to Public in GitHub
Packages. GitHub initially creates new packages as private, even for a public
repository; this one-time visibility setting is separate from workflow permissions.
For intentionally private copies, the operator needs package access and
`docker login ghcr.io` using a classic token with `read:packages`. Never put that
token in the application `.env` or the Compose file.

## Published-image upgrades

For a standalone installation using the downloaded `compose.yaml`:

1. Read the changelog and retain a verified database/environment backup.
2. Set `PROBE_IMAGE` in `.env` to the desired version or digest.
3. Pull the complete release before stopping application services.
4. Stop the scheduler, worker and web, apply migrations and start the release.

```sh
docker compose pull
docker compose stop --timeout 60 beat worker web
docker compose run --rm --no-deps migrate
docker compose up -d --no-build
docker compose ps -a
```

In a source checkout, add `-f compose.registry.yaml` to every command. Alternatively
set `COMPOSE_FILE=compose.registry.yaml` in the protected deployment `.env`; all
existing Compose, backup and systemd commands then use that file. Keep the same
Compose project name and volume; do not run `down -v`. When environment settings
change, `up` may also recreate supporting services; their persistent volumes stay
attached. Do not run multiple Beat instances against the same database.

Leave `SENTRY_RELEASE` empty to use the published image's `APP_VERSION` automatically,
or override it explicitly. Registry availability is needed for pulls, not for
normal operation or restarting already downloaded images. Keep a previous image
and backup; an image rollback alone does not reverse database migrations.

## Image publication

`.github/workflows/publish-image.yml` tests pull requests, main and version tags
using PostgreSQL inside the Python 3.13 application image. A successful `v<version>`
tag matching `VERSION` publishes `ghcr.io/gfavre/probe:<version>` and a `sha-<commit>`
alias, with OCI source/version/revision labels, provenance and an SBOM. GitHub
Actions builds both architectures; the small production VM does not compile them.

Publication uses the repository-scoped `GITHUB_TOKEN` with `packages: write` only
in the publishing job. No personal registry token or application secrets are
required. Actions are pinned to full commit SHAs. Manual workflow runs on a version
tag can rebuild that release; runs on main only test. Treat published version tags
as immutable: release a new patch version when the contents change.
