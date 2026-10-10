# Runbook — Prod Readiness (WP7/WP8)

Vận hành hằng ngày/ tháng cho các cải tiến của `plans/prod-readiness-plan.md`.

## Backup (WP8 / BK1)

- Script: [`scripts/backup.sh`](backup.sh) — mysqldump `erp.local` (single-transaction, routines, triggers) → gzip.
- Cron đề xuất (crontab -e):

```
0 1 * * *  /home/frappe/frappe-bench/apps/gege_hr/scripts/backup.sh >> /home/frappe/logs/backup.log 2>&1
```

- Retention: `daily/` giữ 7 ngày; ngày 1 hàng tháng copy sang `monthly/` giữ 6 tháng (183 ngày).
- Kiểm tra: log mỗi sáng có dòng `dump OK`; thiếu dòng → kiểm tra `MYSQL_PASS`/disk.

## Restore drill (WP8 / BK2)

- Script: [`scripts/restore-drill.sh`](restore-drill.sh) — restore dump mới nhất vào DB `erp_drill`, truncate Scheduled Job Type (chặn enqueue), chạy smoke pytest.
- Cron đề xuất (Chủ nhật đầu tiên hàng tháng 05:00):

```
0 5 1-7 * 7 [ "$(date +\%u)" = 7 ] && /home/frappe/frappe-bench/apps/gege_hr/scripts/restore-drill.sh >> /home/frappe/logs/restore-drill.log 2>&1
```

- Đạt: `BK2 PASS: smoke xanh`. Fail → xem `/tmp/drill_smoke.log`, KHÔNG đụng prod cho tới khi查明 nguyên nhân.

## Error Log digest (WP8 / BK3)

- Job: `gege_hr.gege_hr.utils.health.daily_error_digest` (cron `0 7 * * *`).
- Gửi Notification cho HR Manager/System Manager: top-10 tiêu đề lỗi 24h.
- Không mail server? Notification trong app đủ cho giai đoạn đầu (plan WP4 checklist).

## Load test giờ cao điểm (WP7 / LT1-3)

- Script: `Custom-UI-GeGe-Currency/tests-e2e/load-test-08h.mjs` (Playwright).
- Chạy staging trước:

```bash
cd Custom-UI-GeGe-Currency && node tests-e2e/load-test-08h.mjs
# nhanh hơn: LT_JITTER_MS=3000 LT_TOTAL=50 node tests-e2e/load-test-08h.mjs
```

- Prod: chạy 1 LẦN sau giờ hành chính, thông báo trước.
- Ngưỡng đạt: 0 lỗi 5xx, p95 < 3s, đủ punch. Fail → tune gunicorn workers / rate-limit / tách queue (plan WP7).

## Health tab (WP4)

- UI: `/hr/settings` → tab "Sức khoẻ hệ thống" (🟢/🟡/🔴 từng job + Error Log 24h + "Chạy thử ngay").
- Alert tự động: cron `*/10` `alert_if_unhealthy` → Notification + WARN Error Log khi job 🔴.
- Lock run_hourly: TTL 10 phút + owner token — worker chết giữa chừng chỉ kẹt tối đa 10' (HC5).

## Auto close payroll (WP6)

- Cron `30 7 * * *` `payroll.auto_close_payroll`: ngày 1–5 thử tính kỳ tháng trước; sau ngày 5 vẫn vướng → Notification đỏ hằng ngày.
- Kỳ do job tạo có badge "⚙️ Tự động" (field `vn_auto_created`).
- Tuyệt đối KHÔNG auto-approve — job dừng ở Calculated.

## Restore khẩn cấp (thủ công)

```bash
# 1) dừng web workers
bench --site erp.local stop  || supervisorctl stop all
# 2) restore
gunzip -c /home/frappe/backups/daily/erp.local_<stamp>.sql.gz | mysql -u root erp.local
# 3) migrate + khởi động
bench --site erp.local migrate && bench start   # hoặc supervisorctl start all
```

Sau restore: mở `/hr/settings` → Sức khoẻ hệ thống → "Chạy thử ngay" từng job để heartbeat xanh lại.

## Tính năng phụ thuộc HRMS: handover / leave-extra / services (G8 — plan-test-complete-hr-extra)

Ba màn `/hr/handover`, `/hr/leave-extra`, `/hr/services` tái dùng DocType của app **HRMS**, kèm custom fields portal `vn_status` / `vn_note` (+ `vn_from_date` / `vn_to_date` / `vn_purpose` / `vn_total_cost` trên Travel Request) do `bench migrate` tự tạo. Lưu ý vận hành:

**Đổi phép (Leave Encashment)** — cần master-data trước khi nhân viên dùng được:

1. Leave Type bật "cho phép đổi phép": UI sẵn sàng — `/hr/settings` → Danh mục → "Loại nghỉ phép" → sửa loại phép, bật checkbox **"Cho phép đổi phép"** (+ optionally "Số ngày đổi tối đa / năm"). Field gốc `allow_encashment` (bản HRMS cũ là `is_encash`; API tự dò field đúng, catalog tự drop field không tồn tại theo meta).
2. **Leave Period** active phủ ngày hiện tại: UI sẵn sàng — `/hr/leave-policy` → "Kỳ nghỉ phép" → tạo/sửa kỳ với checkbox **"Đang hoạt động"** bật (map `is_active=1`). API tự gán `leave_period` khi submit; thiếu thì HRMS báo lỗi validate.
3. Leave Allocation cho nhân viên + loại phép đó (API chặn submit vượt số dư — thông báo "vượt số dư phép").
4. Muốn approve được (submit tạo Additional Salary thật), NV cần đủ bộ:
   - **Salary Structure Assignment** phủ ngày đổi phép (HRMS throw "No Salary Structure assigned" nếu thiếu);
   - **mức tiền/ngày**: field `leave_encashment_amount_per_day` trên SSA (hoặc Salary Structure) — thiếu thì amount=0 và HRMS từ chối "valid encashment amount";
   - **Leave Type.earning_component** (vd "Leave Encashment") để HRMS tạo Additional Salary khi submit;
   - HR Manager cần quyền `submit` trên **Additional Salary** (đã cấp qua `grant_hr_permissions`).
   Endpoint approve chạy submit + side-effects (Additional Salary / Leave Allocation) as **Administrator** rồi trả session — để vượt per-employee User-Permission check của Frappe (HR duyệt hàng loạt NV khác).
5. Từ chối sau khi đã duyệt = cancel document (HRMS hoàn lại số dư); trạng thái portal lưu ở `vn_status="Rejected"` + lý do ở `vn_note`, NV nhận notification.

**Nghỉ bù (Comp-off)** — quy tắc HRMS bắt buộc: ngày làm bù phải là **ngày lễ** theo Holiday List của công ty VÀ nhân viên có **Attendance "Present"** ngày đó, và ngày ≥ ngày vào việc, không tương lai. Approve thành công HRMS tự tạo Leave Allocation "Compensatory Off". Lỗi điển hình: "is not a holiday" / "not present all day(s)" / "Future dates not allowed" → bổ sung Attendance hoặc chọn đúng ngày lễ.

**Khiếu nại + Công tác (services)** — API tự điền các field bắt buộc HRMS: grievance gán `grievance_against_party=Employee` (chính NV), tự seed Grievance Type "Chung" và Purpose of Travel "Công tác chung" lần đầu; travel mặc định `travel_type=Domestic`, ngày tháng lưu ở `vn_from_date`/`vn_to_date`. HR mới có nút **Từ chối** công tác (kèm lý do). Filter trạng thái dropdown = `vn_status` (Draft/Approved/Rejected).

**Khác biệt trạng thái**: cột `status` trên portal là `vn_status || docstatus` (1→Approved, 2→Cancelled). Bản ghi tạo trực tiếp trong desk HRMS (không qua portal) sẽ không khớp filter `vn_status` cho đến khi được duyệt/từ chối qua portal.

**Deploy code mới lên web workers:** gunicorn chạy `--preload` — code Python mới KHÔNG tự load. Sau khi pull/migrate phải restart web worker (`sudo supervisorctl restart frappe-bench:frappe-web` hoặc kill master để supervisor tự kéo lại), nếu không endpoint vẫn chạy bản cũ âm thầm.

**HRMS chặn 2 comp-off chồng lấn ngày** ("A Compensatory Leave Request exists between …") — mỗi NV 1 comp-off cho mỗi khoảng ngày; muốn làm lại phải hủy bản cũ trong desk.

**Test:**
- Unit: `pytest tests/test_leave_extra.py tests/test_employee_services.py tests/test_idor.py -q` (bench-free) — 85 test.
- Browser E2E (Playwright + Chromium thật qua vite:5173 → bench:8000): `npx playwright test tests/hr-handover.spec.js tests/hr-leave-extra.spec.js tests/hr-services.spec.js` — 14 test, 2 nhân vật (NV `hr.employee@gege.test` / HRM `hr.manager2@gege.test`), cover submit/duyệt/từ chối/G2/G3/G5 + toast lỗi tiếng Việt. Chạy lại được nhiều lần (idempotent), cần bench web + `npx vite` đang chạy.
