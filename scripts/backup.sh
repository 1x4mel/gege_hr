#!/usr/bin/env bash
# WP8 (prod-readiness-plan) — daily mysqldump backup + retention.
#
# Cron (01:00 mỗi ngày, giữ 7 ngày; ngày 1 hàng tháng giữ 6 tháng):
#   0 1 * * *  /home/frappe/frappe-bench/apps/gege_hr/scripts/backup.sh >> /home/frappe/logs/backup.log 2>&1
#
# Env overrides:
#   DB_NAME     (default erp.local)   BACKUP_DIR (default /home/frappe/backups)
#   MYSQL_USER  (default root)       MYSQL_PASS (nếu cần)
set -euo pipefail

DB_NAME="${DB_NAME:-erp.local}"
BACKUP_DIR="${BACKUP_DIR:-/home/frappe/backups}"
DAILY_DIR="$BACKUP_DIR/daily"
MONTHLY_DIR="$BACKUP_DIR/monthly"
STAMP="$(date +%Y%m%d_%H%M%S)"

mkdir -p "$DAILY_DIR" "$MONTHLY_DIR"

DUMP_FILE="$DAILY_DIR/${DB_NAME}_${STAMP}.sql.gz"
echo "[$(date -Is)] dumping ${DB_NAME} → ${DUMP_FILE}"

if [ -n "${MYSQL_PASS:-}" ]; then
  mysqldump -u"${MYSQL_USER:-root}" -p"${MYSQL_PASS}" --single-transaction --routines --triggers "$DB_NAME" | gzip -6 > "$DUMP_FILE"
else
  mysqldump -u"${MYSQL_USER:-root}" --single-transaction --routines --triggers "$DB_NAME" | gzip -6 > "$DUMP_FILE"
fi

# BK1 assert: file tồn tại + không rỗng
if [ ! -s "$DUMP_FILE" ]; then
  echo "[$(date -Is)] FATAL: dump rỗng — kiểm tra mysqldump credentials" >&2
  exit 1
fi
echo "[$(date -Is)] dump OK: $(du -h "$DUMP_FILE" | cut -f1)"

# Ngày 1: giữ bản full tháng (6 tháng)
if [ "$(date +%d)" = "01" ]; then
  cp "$DUMP_FILE" "$MONTHLY_DIR/${DB_NAME}_monthly_${STAMP}.sql.gz"
  echo "[$(date -Is)] monthly snapshot saved"
  find "$MONTHLY_DIR" -name '*.sql.gz' -mtime +183 -delete
fi

# Retention: daily giữ 7 ngày
find "$DAILY_DIR" -name '*.sql.gz' -mtime +7 -delete

echo "[$(date -Is)] backup done. daily=$(ls "$DAILY_DIR" | wc -l) monthly=$(ls "$MONTHLY_DIR" | wc -l)"
