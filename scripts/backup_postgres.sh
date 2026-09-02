#!/usr/bin/env bash
# =============================================================================
# GEO Tracker — PostgreSQL backup script
# =============================================================================
# Creates a timestamped pg_dump custom-format backup.
#
# Requirements:
#   - pg_dump available in PATH
#   - DATABASE_URL or PG environment variables set
#
# Usage:
#   ./scripts/backup_postgres.sh [destination_dir] [retention_days]
#
# Environment:
#   DATABASE_URL  — or PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
#
# Exit codes:
#   0 = success
#   1 = backup failed
#   2 = destination not writable
# =============================================================================

set -euo pipefail

DEST_DIR="${1:-./backups}"
RETENTION_DAYS="${2:-30}"

# Parse DATABASE_URL if provided, otherwise rely on PG* env vars.
# Uses Python for robust URL parsing (avoids fragile sed/regex).
if [[ -n "${DATABASE_URL:-}" ]]; then
    # Strip the SQLAlchemy driver prefix if present.
    DB_URL="${DATABASE_URL#postgresql://}"
    DB_URL="${DB_URL#postgresql+psycopg://}"

    # Use Python's urllib.parse for reliable URL component extraction.
    # The password is passed via a pipe (never echoed to terminal/logs).
    read -r PGUSER PGPASSWORD PGHOST PGPORT PGDATABASE <<< "$(
        echo "$DB_URL" | python3 -c '
import sys, urllib.parse
url = sys.stdin.read().strip()
# Prepend a scheme if stripped, for urllib to parse.
if not url.startswith("postgresql://"):
    url = "postgresql://" + url
p = urllib.parse.urlparse(url)
user = p.username or ""
password = p.password or ""
host = p.hostname or ""
port = p.port or 5432
# Database is the path without leading slash.
db = p.path.lstrip("/") or ""
print(user, password, host, port, db)
'
    )"
    export PGUSER PGPASSWORD PGHOST PGPORT PGDATABASE
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${DEST_DIR}/geo_tracker_${TIMESTAMP}.dump"

# Verify destination is writable.
if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
    echo "ERROR: Cannot create destination directory: $DEST_DIR" >&2
    exit 2
fi
if ! touch "${DEST_DIR}/.write_test" 2>/dev/null; then
    echo "ERROR: Destination directory not writable: $DEST_DIR" >&2
    exit 2
fi
rm -f "${DEST_DIR}/.write_test"

echo "Starting PostgreSQL backup..."
echo "  Database: ${PGDATABASE:-unknown}"
echo "  Host: ${PGHOST:-unknown}"
echo "  Destination: ${BACKUP_FILE}"
# NOTE: Password is never echoed.

# Run pg_dump in custom format.
# On failure, remove any partial backup file.
if ! pg_dump --format=custom --no-owner --no-privileges --file="$BACKUP_FILE"; then
    echo "ERROR: pg_dump failed!" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Verify backup file exists and is non-empty.
if [[ ! -s "$BACKUP_FILE" ]]; then
    echo "ERROR: Backup file is empty or does not exist: $BACKUP_FILE" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Set safe file permissions (owner read/write only).
chmod 600 "$BACKUP_FILE" 2>/dev/null || true

BACKUP_SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
echo "Backup completed: ${BACKUP_FILE} (${BACKUP_SIZE})"

# Optional retention cleanup.
if [[ "$RETENTION_DAYS" -gt 0 ]]; then
    echo "Cleaning up backups older than ${RETENTION_DAYS} days..."
    find "$DEST_DIR" -name "geo_tracker_*.dump" -type f -mtime +"$RETENTION_DAYS" -delete
    echo "Retention cleanup complete."
fi

echo "Done."
