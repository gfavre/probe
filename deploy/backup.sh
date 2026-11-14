#!/bin/sh
# Keep daily database and environment copies restricted to the administrator.
set -eu
umask 077
cd /opt/http-monitor
backup_dir=/var/backups/http-monitor
install -d -m 0700 "$backup_dir"
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_tmp=$(mktemp "$backup_dir/.database.XXXXXX")
trap 'rm -f "$backup_tmp"' EXIT HUP INT TERM
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$backup_tmp"
docker compose exec -T db pg_restore --list < "$backup_tmp" > /dev/null
install -m 0600 .env "$backup_dir/environment-$backup_stamp.env"
mv "$backup_tmp" "$backup_dir/database-$backup_stamp.dump"
find "$backup_dir" -maxdepth 1 -type f -name 'database-*.dump' -mtime +7 -delete
find "$backup_dir" -maxdepth 1 -type f -name 'environment-*.env' -mtime +7 -delete
printf '%s\n' 'Database backup completed and archive listing verified.'
