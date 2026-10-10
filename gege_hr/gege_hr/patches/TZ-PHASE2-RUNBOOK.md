# WP9 — TZ Phase 2 runbook (wall → UTC)

Tham chiếu: [`plans/tz-frame-unification-plan.md`](../../../../plans/tz-frame-unification-plan.md) §4, [`plans/prod-readiness-plan.md`](../../../../plans/prod-readiness-plan.md) WP9.

## 0. Điều kiện mở (§4.1 — BẮT BUỘC)

1. Kỳ công chứa "hôm nay" đã **lock + export** xong → `lock_date` = ngày lock.
2. Backup DB đầy đủ: `mysqldump erp.local | gzip > /home/frappe/backups/pre-utc-migration_$(date +%F).sql.gz`
3. Tag code `pre-utc-migration`: `git tag pre-utc-migration && git push --tags`
4. P0/P1 (WP1–WP6) đã chạy ổn ≥2 tuần trên prod.

## 1. Dry-run (không đổi dữ liệu)

```bash
bench --site erp.local execute gege_hr.patches.tz_phase2.run --args "['<lock_date>', True]"
```

Kiểm tra output: mọi bảng có `rows_in_window`, KHÔNG có lỗi "row(s) sau lock_date".
Log JSON: `logs/tz_phase2_<stamp>.json`.

## 2. Migrate thật (cửa sổ bảo trì, ngoài giờ)

```bash
# stop workers trước khi chạy (sudo supervisorctl stop frappe-bench-workers:)
bench --site erp.local execute gege_hr.patches.tz_phase2.run --args "['<lock_date>']"
```

Chỉ rows `<= lock_date` bị dịch `−D` giờ (D đọc từ `VN HR Portal Setting.timezone`,
mặc định +7). Có row nào sau lock_date ⇒ toàn bộ run ABORT (transaction rollback).

## 3. Flip code (cùng 1 deploy — §4.3)

Writers quay về `tz.utc_now_str()`; readers về bounds UTC (revert Pha-1);
`tz.portal_now_str` giữ làm alias deprecated; FE `formatTime` giữ +7.
Sau flip: `bench restart && bench --site erp.local migrate`.

## 4. Đối soát Δ=0 (§4.4 — điều kiện hoàn tất)

Per employee/per ngày, 2 kỳ gần nhất, so khớp trước/sau:
`SUM(total_actual_hours)`, `SUM(regular_hours)`, late/early/OT, `payable_day`,
số ticket + `penalty_amount`. Δ ≠ 0 bất kỳ dòng nào ⇒ DỪNG, điều tra.

Rồi chạy E2E full (42 case) trên frame UTC + `recalculate_period` 1 kỳ sanity.

## 5. Rollback (§4.5)

```bash
sudo supervisorctl stop frappe-bench-workers:
# cách A: đảo chiều dữ liệu bằng patch (giữ nguyên code mới)
bench --site erp.local execute gege_hr.patches.tz_phase2.rollback --args "['<lock_date>']"
# cách B: restore dump + checkout tag pre-utc-migration (an toàn tuyệt đối)
gunzip -c /home/frappe/backups/pre-utc-migration_<date>.sql.gz | mysql -u root erp.local
git checkout pre-utc-migration && bench restart
```

## Unit tests

`tests/test_tz_phase2.py` — 6 case TZ theo plan: offset động từ tz name; plan cắt
theo meta; SQL dịch đúng chiều; guard refuse rows sau lock; rollback đảo dấu;
round-trip ±0 giây.
