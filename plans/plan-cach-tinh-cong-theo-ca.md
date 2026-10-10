# Cách tính công theo ca (ca 8h + ca 12h)

Ngày: 2026-10-08 · Owner chốt: 2026-10-08 · **Trạng thái: ĐÃ TRIỂN KHAI ĐỦ (08/10/2026)** — xem mục cuối

## Bối cảnh

- Ngưỡng ngày công là giờ tuyệt đối của chính sách (`min_working_hours_full_day` = 8h,
  `half_day` = 4h). Ca 12h dư 4h, nhưng **ca 8h (Ca 9h-17h) không dư phút nào**: trễ 35 giây
  vẫn rơi xuống 0.5 công (Kiều Nguyễn Tuấn Kiệt 29/09).
- Ân hạn trễ (5') chỉ bỏ nhãn "đi trễ", phút trễ vẫn bị trừ khỏi giờ.
- Lương có 2 chế độ (`Employee.vn_payroll_mode`), cả 23 NV đang **Hourly**:
  - **Monthly**: lương cơ bản × ngày công / 26. Ngày công trước đây = Σ giờ/8 (trường
    `Shift Type.vn_hours_per_day` không tồn tại → luôn chia 8) → ca 12h = 1.5 công/ngày;
    ngưỡng 0/0.5/1 của phiên chấm công không được dùng.
  - **Hourly**: tự ghép cặp IN/OUT từ lượt chấm thô (`api/payroll._employee_bracket_hours`),
    không đọc phiên chấm công → trả cả giờ đến sớm trước ca, không áp ân hạn.

## Quyết định của owner

| Câu hỏi | Chốt |
|---|---|
| Ca 9h-17h nghỉ trưa? | Không — đủ 8h |
| Quá ân hạn | Trừ theo phút, **chỉ trừ phần vượt ân hạn** (trễ 12', ân hạn 5' → trừ 7') |
| Phút trễ trong ân hạn | **Không trừ** (cả công lẫn lương) |
| Ân hạn về sớm | Không (giữ 0) |
| Dung sai đủ công | 30 phút |
| Sắp có thêm NV ca 8h | Có → cấu hình theo **ca**, không gán chính sách từng người |

## Thiết kế — phiên chấm công là nguồn số liệu duy nhất

Bộ tính công (`utils/calc.calculate_work_session`) ghi cho mỗi ngày:

1. **Giờ tính lương** `payable_regular_hours` = giờ làm trong ca + phút trễ được miễn
   (`min(phút trễ, ân hạn)`, chỉ khi có đủ cặp IN/OUT hợp lệ), tối đa = độ dài ca.
   Giờ đến trước ca không tính (trừ OT được duyệt — đi riêng qua `approved_overtime_hours`).
2. **Ngày công** `payable_day` theo `Shift Type.vn_payable_day_method`:
   - **Theo ngưỡng giờ** (mặc định): đủ công nếu giờ tính lương ≥
     `min(min_working_hours_full_day, độ dài ca − full_day_shortage_tolerance_minutes)`;
     nửa công nếu ≥ `min(min_working_hours_half_day, độ dài ca / 2)`
     (`calc.payable_thresholds`). Ca 12h → 8h/4h (không đổi), ca 8h → 7h30/4h, ca 6h → 5h30/3h.
   - **Theo phút**: `min(1, giờ tính lương / độ dài ca)`, 2 số lẻ.

Lương đọc từ đó:

- **Monthly**: Σ `payable_day` các ngày có làm (+ ngày phép như cũ) — `utils/payroll.summarize_work_sessions`.
- **Hourly** (PR2): Σ giờ tính lương chia theo khung giờ + OT đã duyệt, thay cho tự ghép cặp lượt chấm thô.

## PR

**PR1 — gege_hr (engine + Monthly + cấu hình)**

- Custom field `Shift Type.vn_payable_day_method` (Select: Theo ngưỡng giờ / Theo phút), thêm vào
  `admin.SHIFT_TYPE_VN_FIELDS`.
- `VN Attendance Policy.full_day_shortage_tolerance_minutes` (Int, mặc định 30) + EDITABLE + validate ≥ 0.
- `calc`: ân hạn vào `payable_regular_hours`; `calculate_payable_day` dùng giờ tính lương + cách tính của ca.
- `payroll.summarize_work_sessions`: dùng `payable_day` của phiên (dòng không có khóa → luật cũ giờ/8).

**PR1-UI — trader-ui**: ô "Cách tính công" ở form ca; ô "Được thiếu tối đa (phút)" ở form chính sách.

**PR2 — gege_hr (Hourly đọc phiên chấm công)** — thay đổi lương thực tế của cả 23 NV Hourly:
không còn trả giờ đến sớm trước ca (trừ OT duyệt), trễ ≤ ân hạn được trả đủ. Khung giờ (hệ số)
chia trên cửa sổ [max(IN, giờ vào ca) − phút ân hạn được miễn, min(OUT, giờ hết ca)] + đoạn OT duyệt.
Cần đối chiếu bảng lương tháng 10 trước/sau khi bật.

## Triển khai PR1

1. Merge → `git pull` app + `bench --site erp-hr.local migrate` (cài custom field + cột chính sách)
   → restart web + workers.
2. Đặt `Ca 9h-17h.vn_payable_day_method = "Theo phút"`.
3. Tính lại phiên chấm công của Kiệt (HR-EMP-00067) từ 26/09. Các ca 12h không đổi ngưỡng
   (min(8, 11.5) = 8) — chỉ tính lại khi có lượt chấm mới/“Tính lại kỳ”.

## Trạng thái triển khai (08/10/2026)

| Hạng mục | PR | Ghi chú |
|---|---|---|
| Cách tính công theo ca + ân hạn trễ + Monthly đọc `payable_day` | gege_hr #48 | có `bench migrate` |
| Phiên chấm công nhận công lẻ 0–1 | gege_hr #49 | sót của #48: validate cũ chỉ nhận 0 / 0.5 / 1 → ca "Theo phút" không lưu được |
| Ô "Cách tính công" (form ca) + "Được thiếu tối đa" (chính sách) | trader-ui #31 | |
| Hourly đọc phiên chấm công (PR2) | gege_hr #50 | owner chốt: **OT chỉ trả khi có đơn OT được duyệt** |

- `Ca 9h-17h` (tạo 07/10, 09:00–17:00, 8h) đặt **Theo phút**; các ca 12h giữ **Theo ngưỡng giờ** (8h / 4h không đổi).
- Kiều Nguyễn Tuấn Kiệt (HR-EMP-00067) chuyển toàn bộ lịch sử sang `Ca 9h-17h` từ 26/09 và tính lại: 28/09 0.5 → **0.88**,
  29/09 (trễ 35 giây) 0.5 → **1.0**, 02/10 → 0.13.
- Tính lại toàn bộ 176 phiên 01–08/10 của cả công ty (0 lỗi) để các phiên trễ trong ân hạn được cộng lại phút ân hạn.
- **So sánh Hourly 01–07/10 (23 NV, đơn giá mặc định 20.000đ/h): 1802,5h → 1467,2h.** Chênh gồm ~266h ca ngoài khoảng tính
  (lỗi của cách ghép cặp cũ: nới truy vấn ±1 ngày mà không lọc lại — ngày giáp ranh trả 2 lần giữa 2 kỳ), 54h làm quá giờ hết
  ca chưa duyệt OT (lúc đó hệ thống có 0 đơn OT), 17,6h ngày cần xem lại, 5h đến sớm trước ca, +7,1h ân hạn trễ.
- Việc của vận hành: thông báo nhân viên làm quá giờ phải gửi đơn tăng ca (ma trận duyệt `Default Overtime Approval
  Matrix` + workflow `VN Overtime Request Workflow` đã bật; duyệt đơn → phiên tự tính lại).
- Lưu ý kỹ thuật: `Shift Type.vn_hours_per_day` mà `payroll._employee_hours_per_day` đọc **không tồn tại** (luôn 8h) — chỉ
  còn ảnh hưởng dòng không có `payable_day`; tính lại một ngày ĐÃ QUA không có lượt chấm sẽ sinh bản nháp Attendance
  "Present" (hành vi sẵn có của `attendance_sync`, lương không đọc).
