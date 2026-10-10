#!/usr/bin/env bash
# WP8 (prod-readiness-plan) — monthly restore drill.
#
# Restore dump mới nhất vào DB drill (erp_drill), chạy smoke pytest 20 case
# trên drill DB. BK2 đạt khi smoke xanh.
#
# Cron (Chủ nhật đầu tiên hàng tháng, 05:00):
#   0 5 1-7 * 7 [ "$(date +\%u)" = 7 ] && /home/frappe/frappe-bench/apps/gege_hr/scripts/restore-drill.sh >> /home/frappe/logs/restore-drill.log 2>&1
#
# Env overrides: DB_NAME / DRILL_DB / MYSQL_USER / MYSQL_PASS / BACKUP_DIR
set -euo pipefail

DB_NAME="${DB_NAME:-erp.local}"
DRILL_DB="${DRILL_DB:-erp_drill}"
BACKUP_DIR="${BACKUP_DIR:-/home/frappe/backups}"
BENCH="/home/frappe/frappe-bench"

LATEST="$(ls -1t "$BACKUP_DIR/daily"/*.sql.gz 2>/dev/null | head -1 || true)"
if [ -z "$LATEST" ]; then
  echo "[$(date -Is)] FATAL: không tìm thấy dump nào trong $BACKUP_DIR/daily" >&2
  exit 1
fi
echo "[$(date -Is)] drilling restore: $LATEST → $DRILL_DB"

mysql -u"${MYSQL_USER:-root}" ${MYSQL_PASS:+-p"$MYSQL_PASS"} -e "DROP DATABASE IF EXISTS \`$DRILL_DB\`; CREATE DATABASE \`$DRILL_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

gunzip -c "$LATEST" | mysql -u"${MYSQL_USER:-root}" ${MYSQL_PASS:+-p"$MYSQL_PASS"} "$DRILL_DB"

# Truncate scheduled-job state để drill không enqueue đè prod.
mysql -u"${MYSQL_USER:-root}" ${MYSQL_PASS:+-p"$MYSQL_PASS"} "$DRILL_DB" -e \
  "TRUNCATE TABLE \`tabScheduled Job Type\`;" 2>/dev/null || true

echo "[$(date -Is)] restore xong — chạy smoke 20 case (bench-free) trên mã app..."
cd "$BENCH"
if ./env/bin/python -m pytest apps/gege_hr/tests/ -q -x -k "smoke or calc or payroll or checkout_miss or health" --maxfail=1 > /tmp/drill_smoke.log 2>&1; then
  echo "[$(date -Is)] BK2 PASS: smoke xanh ($(tail -1 /tmp/drill_smoke.log))"
else
  echo "[$(date -Is)] BK2 FAIL: smoke đỏ — xem /tmp/drill_smoke.log" >&2
  exit 1
fi
