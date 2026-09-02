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
if [[ -n "${DATABASE_URL:-}" ]]; then
    # Extract components from postgresql://user:pass@host:port/db
    DB_URL="${DATABASE_URL#postgresql://}"
    DB_URL="${DB_URL#postgresql+psycopg://}"
    PGUSER="$(echo "$DB_URL" | sed -n 's/^\([^:@]*\).*/\1/p')"
    PGPASS="$(echo "$DB_URL" | sed -n 's/^[^:]*:\([^@]*\)@.*/\1/p')"
    PGHOST="$(echo "$DB_URL" | sed -n 's/.*@\([^:]*\).*/\1/p')"
    PGPORT="$(echo "$DB_URL" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')"
    PGDATABASE="$(echo "$DB_URL" | sed -n 's/.*\/\([^?]*\).*/\1/p')"
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

# Run pg_dump in custom format.
if ! pg_dump --format=custom --no-owner --no-privileges --file="$BACKUP_FILE"; then
    echo "ERROR: pg_dump failed!" >&2
    # Remove any zero-byte or partial backup.
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
chmod 600 "$BACKUP_FILE"

BACKUP_SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
echo "Backup completed: ${BACKUP_FILE} (${BACKUP_SIZE})"

# Optional retention cleanup.
if [[ "$RETENTION_DAYS" -gt 0 ]]; then
    echo "Cleaning up backups older than ${RETENTION_DAYS} days..."
    find "$DEST_DIR" -name "geo_tracker_*.dump" -type f -mtime +"$RETENTION_DAYS" -delete
    echo "Retention cleanup complete."
fi

echo "Done."
