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

# консистентная копия: через sqlite3, а если его нет — через модуль sqlite3 в python3
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_FILE" ".backup '$backup'"
elif command -v python3 >/dev/null 2>&1; then
    python3 - "$DB_FILE" "$backup" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
src_conn = sqlite3.connect(src)
dst_conn = sqlite3.connect(dst)
with dst_conn:
    src_conn.backup(dst_conn)
src_conn.close()
dst_conn.close()
PYEOF
else
    echo "Нужен sqlite3 (apt install sqlite3) или python3" >&2
    exit 1
fi

# храним только последние KEEP копий
ls -1t "$BACKUP_DIR"/tis_users-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date '+%Y-%m-%d %H:%M:%S') бэкап создан: $backup"
