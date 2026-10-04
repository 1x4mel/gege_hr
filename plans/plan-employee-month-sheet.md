# Plan — Bảng công tháng của 1 nhân viên (`/hr/team/attendance/:employee`)

> Mở từ tên nhân viên ở `/hr/team/attendance`. Mục tiêu: HR rà / sửa / thống kê
> công 1 nhân viên trong 1 tháng, **đủ chi tiết để tính lương thủ công**.
> Chưa đụng tới tính lương tự động (payroll đang xây).

## 1. Quyết định nghiệp vụ (owner chốt 2026-10-03)

| # | Quyết định |
|---|---|
| D1 | Chỉ **HR Manager / System Manager** được sửa. **HR User** và **Line Manager** chỉ xem (LM: chỉ member `reports_to` mình). Áp dụng **mọi nơi** (siết luôn gate các endpoint sửa chấm công). |
| D2 | Kinh doanh 24/7: **mọi ngày là ngày công**, kể cả CN / lễ / không có ca. Ngày nghỉ = **chỉ ngày có đơn nghỉ đã duyệt**. Ngày đã qua không chấm + không đơn → **vắng không phép**. |
| D3 | Đi trễ / về sớm chia nhóm phút: ≤15 / 16–30 / >30. |
| D4 | Quên chấm: chỉ hiện **số lần** (không hiện tiền phạt). |

## 2. Phân loại ngày (1 trạng thái chính + cờ phụ)

Ưu tiên:
1. `before_join` / `after_relieve` — ngoài thời gian làm việc.
2. `future` — sau hôm nay.
3. `leave_paid` / `leave_unpaid` — đơn nghỉ **Approved** (docstatus 1); `Leave Type.is_lwp` quyết định
   có/không lương; `half_day` (+ `half_day_date`) → 0.5.
4. `worked` — có ít nhất 1 lượt chấm (Work Session có `actual_checkin` hoặc `actual_checkout`).
5. `absent` — còn lại (ngày đã qua). **Không dùng cờ `absent` của WS** (engine reset về 0).

Nửa ngày phép + có chấm → trạng thái `leave_*` với `leave_fraction=0.5` và vẫn tính `worked` 0.5.

Cờ phụ: `late_minutes`, `early_minutes`, `ot_approved_hours`, `ot_pending_hours`, `checkout_miss`
(ticket VN Checkout Miss hoặc `vn_auto_checkout`), `checkin_miss` (`missing_checkin` khi có OUT),
`pending` (đơn chờ), `edited` (VN Audit Event), `locked`, `need_review`.

Ticket **Waived** không tính `checkout_miss` (owner 2026-10-04). Ticket thành Waived khi HR bấm
"Miễn", khi CR được duyệt, hoặc khi **HR Manager / Admin chấm/sửa giờ RA thủ công**
(`admin.admin_custom_checkin` tự miễn ticket Pending/Explained/Penalised của ca đó, trước recalc).
Closed/Penalised/Pending/Explained vẫn tính.

**Một bộ số cho mọi màn (owner 2026-10-04).** `month_sheet.month_totals(emp, y, m)` là nguồn DUY NHẤT
cho tổng tháng: bảng công tháng, ô thống kê `/hr/attendance` (`attendance.my_monthly_summary`) và
`/hr/attendance/monthly` (`my_month_meta.ws_summary`; riêng "Công" vẫn là `payable_day` của engine).
Lưới team nhận cờ ô `checkout_miss` = `ms.is_checkout_miss` (cùng luật). "Làm thêm" = OT **đã duyệt**;
OT chưa duyệt trả riêng `overtime_pending_hours`. Người **không** có Shift Assignment / Work Session
trong tháng: ngày không chấm = `off_roster` (không tính vắng) — cùng luật roster của review team.
**Một mốc "quá hạn chấm ra"** (04/10): ca có IN, chưa OUT, quá `planned_end + vn_cm_buffer_minutes`
(360') = "Quên chấm ra" ở cả 3 màn (`ms.is_checkout_overdue`, `attendance._checkout_cutoff`) — đúng mốc
auto-close tạo ticket; FE lưới dùng cờ BE thay cửa sổ 5h riêng.

## 3. Thống kê (đếm theo NGÀY, cộng PHÚT/GIỜ)

- **Công**: ngày công chuẩn (= số ngày làm việc trong khoảng tính − 0), ngày đi làm, ngày nghỉ có lương,
  không lương, vắng; giờ trong ca, giờ thực tế.
- **Đi trễ / Về sớm**: số ngày, tổng phút, 3 nhóm D3, số ngày có giải trình được duyệt.
- **Tăng ca**: số ngày có OT đã duyệt, giờ đã duyệt, giờ chờ duyệt (raw − approved, không cộng tổng), giờ đêm.
- **Quên chấm**: số lần quên ra, quên vào.
- **Nghỉ**: theo từng loại phép (có ½ ngày), tổng có lương / không lương, vắng không phép.
- **Đơn chờ** (Leave Open, OT / Correction pending, Explanation Open) → cảnh báo “số liệu chưa chốt”.
- **Bất thường**: ngày nhiều phiên, `need_review`, `calculation_status=Error`, ngày sửa tay.

Mọi số phải tính bằng 1 hàm thuần `utils/month_sheet.py` (có test) — **không** tái dùng
`my_monthly_summary` (đếm theo phiên, OT raw-fallback, missing_checkout sai nghĩa).

## 4. API

`gege_hr.gege_hr.api.attendance.employee_month_sheet(employee, year, month)`
- Gate: `_team_viewer()` + `_can_view_member()` (HR*/SM toàn bộ; LM chỉ team).
- Trả `{employee, year, month, from_date, to_date, today, locked_dates, period, can_edit, days[], totals, pending[]}`.
- `can_edit` = HR Manager / System Manager (D1). Ô ngày không tự mang `can` — FE mở `TeamDayDrawer`
  (đã gate theo `team_member_day_detail.can`).

## 5. Sửa lỗi đi kèm

- #2 `attendance_admin_ops._audit` truyền `company=None` → `audit.log` bỏ qua → không có nhật ký sửa
  chấm công. Sửa: lấy company của nhân viên.
- #3 `team_attendance`: ngày nghỉ phép không có WS/Attendance hiện "Not marked" (check phép nằm sau
  `continue`). Sửa: xét phép trước.
- (P3) engine `_load_leave_info` luôn "Paid" và cắt `leave_days_equivalent` ≤ 1; khoá kỳ không đóng băng WS.

## 6. Giai đoạn

- **P1**: hàm thuần + endpoint + FE route/lịch/thống kê/lọc theo chỉ số/◀▶ đổi NV; sửa #2 #3; siết D1.
- **P2**: thao tác sửa trên màn (theo `can`), dấu ✎ + lịch sử sửa, Excel từng người + tổng hợp cả team.
- **P3**: sửa engine (phép LWP, khoá kỳ), `my_monthly_summary` dùng chung hàm mới.

## 7. Đánh giá công tháng — cả team (`/hr/team/attendance/review`)

Owner chốt 2026-10-03: thay bảng Excel HR vẫn gõ tay (nhân viên theo cột, 4 khối Quên điểm danh /
Đi trễ-Về sớm / Nghỉ việc riêng / Thiếu điểm danh cả ngày). **Chỉ xem + tính tay** — chưa chốt tháng,
không xếp loại, quên chấm chỉ đếm số lần. Cột "Nội dung" = **lý do chi tiết**: tự điền từ giải trình
trễ/sớm (VN Attendance Explanation.reason), giải trình quên chấm ra (VN Checkout Miss.explanation),
lý do đơn nghỉ (Leave Application.description).

- API `month_sheet.team_month_review(year, month, department)`: HR → mọi nhân viên trên roster chấm
  công (Shift Assignment giao tháng hoặc có WS trong tháng, còn làm trong tháng); Line Manager → team
  `reports_to`. Mỗi dòng: totals + `items` (4 khối mẫu cũ, `utils.month_sheet.review_items`) + dải ngày
  gọn. Dùng chung `build_day` / `aggregate_month` với bảng công 1 người → số luôn khớp.
- API `month_sheet.team_month_review_xlsx(...)`: file Excel **đúng mẫu cũ** (openpyxl, có màu / merge /
  viền đỏ, in ngang vừa 1 trang) — `utils/month_review_xlsx.legacy_layout` (thuần, có test) +
  `render_xlsx`. Thêm 1 dòng "Nghỉ phép có lương" (mẫu cũ không có).
