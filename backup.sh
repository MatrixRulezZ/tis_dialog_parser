#!/usr/bin/env bash
# Резервная копия БД кабинетов.
# Использует sqlite3 .backup — копия консистентна даже при активной записи.
#
# Запуск вручную:  ./backup.sh
# По расписанию (crontab -e), каждый день в 04:17:
#   17 4 * * * cd /opt/tis_dialog_parser && ./backup.sh >> data/backup.log 2>&1

set -euo pipefail
cd "$(dirname "$0")"

DB_FILE="${DB_FILE:-./data/tis_users.db}"
BACKUP_DIR="${BACKUP_DIR:-./data/backups}"
KEEP="${KEEP:-14}"   # сколько последних копий хранить

if [ ! -f "$DB_FILE" ]; then
    echo "БД не найдена: $DB_FILE" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
backup="$BACKUP_DIR/tis_users-$stamp.db"
sqlite3 "$DB_FILE" ".backup '$backup'"

# храним только последние KEEP копий
ls -1t "$BACKUP_DIR"/tis_users-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date '+%Y-%m-%d %H:%M:%S') бэкап создан: $backup"
