# Tài khoản HR cho console gege (đồng bộ tài khoản · QR vào ca · mật khẩu HR)

Ngày: 2026-10-10 · Owner chốt: 2026-10-10

## Mục tiêu

Console gege (.90) đang có tài khoản riêng. Owner muốn dùng tài khoản HR:

| Hạng mục | Chốt | Trạng thái |
|---|---|---|
| HR là nguồn quyết định **ai có tài khoản** console (tạo / khoá); vai trò + quyền máy chia ở console | có | **đợt 1** |
| Màn hình vào ca (gege-seat): **quét QR bằng app HR** | có | **đợt 1** |
| Quên điện thoại: nhập **email + mật khẩu HR** ở màn hình vào ca | có | **đợt 2** (code xong, bật theo máy `hr_auth`) |
| Trang web console: đăng nhập bằng email + mật khẩu HR | có | **đợt 2** (code xong, `auth` 0/2 thử/1 chính thức) |
| HTTPS cho console | có | **xong 10/10** — `https://console.gegeteam.xyz` (chỉ LAN) |
| Mật khẩu console cũ của nhân viên | **tắt hẳn** khi chuyển (giữ tài khoản nội bộ không phải email) | đợt 2 |

Đợt 1 không gửi mật khẩu nào qua mạng nên không phụ thuộc HTTPS.

## Kênh giữa hai máy chủ

Một khoá bí mật dùng chung (`VN HR Portal Setting.seat_console_key` ↔ console `seat_settings` scope `hr`):

- **HR → console** (`utils/seat_checkin.console_post`, header `X-Seat-Key`; console chỉ nhận IP máy chủ HR):
  `/api/seat/hr/verify` (vé chấm công PC), `/api/seat/hr/qr-info`, `/api/seat/hr/qr-approve`.
- **console → HR** (`api/seat_bridge.py`, `allow_guest` nhưng `_require_console`: đúng IP trong `seat_console_url`
  + đúng khoá — gọi thẳng `http://192.168.2.116` trong LAN, qua Cloudflare Tunnel thì IP là `::1` → bị từ chối):
  `list_accounts`.

## Đồng bộ tài khoản

`seat_bridge.list_accounts` → mỗi System User (trừ Administrator/Guest): `{email, full_name, user_enabled,
employee_status, department, eligible}`; `eligible` = User bật **và** hồ sơ nhân viên `Active`
(`seat_checkin.build_accounts`). Console (`_hr_sync_plan`): tạo tài khoản cho người `eligible` chưa có (không vai trò,
không quyền máy, không mật khẩu console); khoá tài khoản mà HR báo rõ không còn hợp lệ (đóng luôn ca đang mở).
**Không bao giờ tự khoá**: tài khoản không phải email, tài khoản HR không biết, người không có hồ sơ nhân viên mà User
vẫn bật, tài khoản quyền `*`. Không tự bật lại tài khoản đã khoá.

## QR vào ca

1. Màn hình vào ca (overlay gege-seat) hỏi dịch vụ gege-seat `GET /qr` → console `POST /api/seat/m/qr` (khoá theo IP
   máy) → mã dùng một lần (đổi mỗi 60 s, sống 150 s) + ma trận QR của `https://hr.gegeteam.xyz/hr/seat-login?c=<mã>`.
2. App HR (`/hr/seat-login`, nút "Quét QR vào ca trên máy" ở trang chấm công khi `today_status.seat_qr.enabled`):
   quét → `seat_qr_info(code)` hiện tên máy + trạng thái → nhân viên bấm xác nhận → `seat_qr_approve(code)`.
3. HR gọi console `qr-approve {code, username = email đang đăng nhập}`; console kiểm tài khoản + quyền máy rồi mở ca
   (máy trống) / mở tạm khoá (đúng chủ ca) / từ chối (ca của người khác). Màn hình tự mở trong vài giây.

Cài đặt: `enable_seat_qr_login` (mặc định 0). Chỉ nhân viên `Active` đã đăng nhập mới xác nhận được; giới hạn 6 lần/phút.

## Giới hạn

- Ai thấy màn hình qua Moonlight cũng quét được mã (tương đương vé Moonlight tự mở ca hiện có).
- Quét bằng camera của máy khi chưa đăng nhập HR: phải đăng nhập rồi quét lại (mã sống 150 s).

## Đợt 2 — mật khẩu HR không đi qua console (giấy xác nhận dùng một lần)

Thay vì console nhận mật khẩu rồi hỏi HR, người dùng gửi mật khẩu **thẳng tới HR** qua HTTPS:

- `seat_bridge.console_login(usr, pwd)` — form đăng nhập của trang web console POST tới đây; HR chuyển hướng về
  `<seat_console_web_url>/api/sso?a=<giấy>` (lỗi → `/?login_err=…`). Đích chỉ lấy từ cấu hình (`console_redirect`).
- `seat_bridge.login_assertion(usr, pwd, aud="seat:<máy>")` — dịch vụ gege-seat của máy gọi (JSON).
- `seat_bridge.verify_assertion(assertion, aud)` — chỉ console gọi được (`_require_console`); giấy dùng một lần.

`_issue_assertion`: User bật + mật khẩu đúng (`check_password`, không tạo phiên HR) + nhân viên Active → giấy
`token_urlsafe(24)` lưu Redis 60 s kèm `aud`. Sai 5 lần / email / 10 phút → khoá tạm. Cấu hình: `seat_console_web_url`.
