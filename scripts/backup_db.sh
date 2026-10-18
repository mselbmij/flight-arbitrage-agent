#!/bin/sh
# Online backup of the SQLite database (ARCHITECTURE.md section 7). Safe while a poll is running:
# uses SQLite's backup API (sqlite3 CLI `.backup`, else Python's Connection.backup), never `cp`.
#
#   scripts/backup_db.sh              # data/fares.db -> data/backups/fares-YYYYmmdd-HHMM.db
#
# Environment: FARES_DB_PATH (default: .env's value, else data/fares.db), BACKUP_DIR
# (default data/backups), BACKUP_KEEP (default 14). Exits non-zero on any failure.
set -eu

cd "$(dirname "$0")/.."

if [ -z "${FARES_DB_PATH:-}" ] && [ -f .env ]; then
    FARES_DB_PATH=$(sed -n 's/^FARES_DB_PATH=//p' .env | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/^["'"'"']//; s/["'"'"']$//')
fi
DB=${FARES_DB_PATH:-data/fares.db}
BACKUP_DIR=${BACKUP_DIR:-data/backups}
KEEP=${BACKUP_KEEP:-14}

# on any failure, ping the phone too (notify.sh is optional: absent in a bare copy of this script)
trap 'rc=$?; rm -f "${PART:-}"; if [ "$rc" -ne 0 ] && [ -x scripts/notify.sh ]; then
    scripts/notify.sh "fare tracker: backup failed" "backup_db.sh exited $rc for $DB
Fix: run scripts/backup_db.sh by hand and read ops/backup.log (disk full? DB path?)" || true
fi' EXIT

case "$DB$BACKUP_DIR" in
    *"'"*) echo "backup_db: quote characters in paths are not supported" >&2; exit 1 ;;
esac
if [ ! -f "$DB" ]; then
    echo "backup_db: no database at $DB" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
DEST="$BACKUP_DIR/fares-$(date +%Y%m%d-%H%M).db"
PART="$DEST.part"
rm -f "$PART"

if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$PART'"
else
    python3 - "$DB" "$PART" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY
fi

if [ ! -s "$PART" ]; then
    echo "backup_db: backup of $DB is empty" >&2
    rm -f "$PART"
    exit 1
fi
mv "$PART" "$DEST"

# keep the newest $KEEP backups (names sort chronologically)
# shellcheck disable=SC2012
ls -1 "$BACKUP_DIR"/fares-*.db | sort -r | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
    rm -f "$old"
done

echo "backup_db: wrote $DEST"
