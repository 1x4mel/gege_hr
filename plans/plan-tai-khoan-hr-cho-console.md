# Tài khoản HR cho console gege (đồng bộ tài khoản · QR vào ca · mật khẩu HR)

Ngày: 2026-10-10 · Owner chốt: 2026-10-10 · **Trạng thái: ĐÃ CHUYỂN CHÍNH THỨC 10/10/2026 14:10** — xem mục cuối

## Mục tiêu

Console gege (.90) đang có tài khoản riêng. Owner muốn dùng tài khoản HR:

| Hạng mục | Chốt | Trạng thái |
|---|---|---|
| HR là nguồn quyết định **ai có tài khoản** console (tạo / khoá); vai trò + quyền máy chia ở console | có | **đợt 1** |
| Màn hình vào ca (gege-seat): **quét QR bằng app HR** | có | **đợt 1** |
| Quên điện thoại: nhập **email + mật khẩu HR** ở màn hình vào ca | có | **xong** — bật theo máy (`hr_auth`), hiện: b6 |
| Trang web console: đăng nhập bằng email + mật khẩu HR | có | **xong** — `auth = 1` (chính thức) |
| HTTPS cho console | có | **xong 10/10** — `https://console.gegeteam.xyz` (chỉ LAN) |
| Mật khẩu console cũ của nhân viên | **tắt hẳn** khi chuyển (giữ tài khoản nội bộ không phải email) | **xong** — đã xoá băm của 23 tài khoản email ("mức 2") |
| Trang web console: đăng nhập bằng **QR** quét bằng app HR | có (thêm 10/10) | **xong** |
| Mục "Quét QR" ở menu tài khoản của app HR (cùng chỗ Đổi mật khẩu / Đăng xuất) | có (thêm 10/10) | **xong** |

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

## Đăng nhập trang web console bằng QR + mục "Quét QR" trong app

- Trang `/hr/seat-login` dùng chung cho hai loại mã: mã của **màn hình vào ca** và mã của **trang đăng nhập console**
  (`seat_qr_info` trả `kind = "web"` + `ip` của trình duyệt đang xin đăng nhập → app hiện "Trang web console · máy <ip>" để
  nhân viên đối chiếu, nút "Đăng nhập console").
- Lối vào trong app: **menu tài khoản** (bấm tên ở cuối sidebar) → **📷 Quét QR** — hiện khi `auth.me().features.seat_qr`
  (`enable_seat_qr_login`); nút ở trang Chấm công vẫn còn.
- Phía console: `/api/login-qr/new` (mã 150 s + *poll token* chỉ trình duyệt xin mã giữ) → `qr-approve` ghi nhận người xác
  nhận → `/api/login-qr/poll` trả phiên cho đúng trình duyệt đó.

## Trạng thái triển khai (10/10/2026)

| Hạng mục | PR / commit |
|---|---|
| `seat_bridge`: `list_accounts`, `seat_qr_info`, `seat_qr_approve`, `enable_seat_qr_login` | gege_hr #53 (migrate) |
| Giấy xác nhận: `console_login`, `login_assertion`, `verify_assertion`, `seat_console_web_url` | gege_hr #54 (migrate) |
| `auth.me().features`, `kind` / `ip` cho mã đăng nhập console | gege_hr #55 |
| App HR: `/hr/seat-login`, nút ở trang Chấm công, công tắc Dữ liệu nền | trader-ui #37 |
| App HR: mục "Quét QR" ở menu tài khoản, đăng nhập console bằng QR | trader-ui #38 |
| App HR: mục "Đổi mật khẩu" ở menu tài khoản (tính năng đổi mật khẩu vốn đã có ở Hồ sơ → Bảo mật) | trader-ui #36 |
| Console, dịch vụ gege-seat, HTTPS, màn đăng nhập mới | repo `Gege-Cyber-System-Localdisk`, `docs/tai-khoan-hr.md` |

- **Chuyển chính thức 14:10**: console `auth = 1`, xoá mật khẩu console của 23 tài khoản email; tài khoản nội bộ không có
  `@` (`admin`, `claude-test`, `gege`) vẫn mật khẩu console. Trước khi chuyển đã có người thật dùng đủ 4 đường (web bằng mật
  khẩu HR, web bằng QR, vào ca b6 bằng QR, vào ca b6 bằng mật khẩu HR); 23/23 tài khoản email đều `eligible`.
- Đồng bộ tài khoản đang bật (10 phút/lần): lượt đầu tạo 2 tài khoản (nhân viên HR chưa có trên console), không khoá ai.
- Cài đặt đang đặt: `enable_pc_checkin = 1`, `enable_seat_qr_login = 1`, `seat_console_url = http://192.168.2.90:8080`,
  `seat_console_web_url = https://console.gegeteam.xyz`, `seat_console_key` = khoá ghép nối (Password).
- **Bẫy:** (1) trường mới của Single DocType không tự nhận `default` — `seat_console_web_url` rỗng làm `console_login` trả
  417 cho tới khi ghi giá trị; (2) module API có `@frappe.whitelist` không import được trong bộ test (conftest ép `import
  frappe` lỗi) → helper thuần đặt ở `utils/seat_checkin.py`; (3) các endpoint `allow_guest` của `seat_bridge` là mặt tiếp xúc
  internet (qua Cloudflare Tunnel) — `list_accounts` / `verify_assertion` bắt buộc `_require_console` (IP + khoá),
  `login_assertion` / `console_login` có khoá tạm theo email; đừng bỏ các lớp này.
- **Sửa 10/10 chiều — `console_assertion`:** form đăng nhập của trang web console POST cả trang tới `console_login` bị
  Frappe từ chối ("Invalid Request") khi trình duyệt đang có phiên HR: console và HR cùng site nên cookie phiên đi kèm, mà
  `HTTPRequest.validate_csrf_token` chặn mọi POST có phiên thiếu CSRF token. Nay console gọi `seat_bridge.console_assertion`
  bằng `fetch` **không cookie** (CORS cho đúng origin `seat_console_web_url`), nhận `go` rồi tự chuyển; `console_login` giữ
  làm đường lùi (gege_hr #58; console `loginHr()`).
- **Còn lại:** rollout gege-seat cả dàn (nút Gege Seat ở trang Công cụ của console; `hr_auth` nay tự mặc định theo chế độ
  HR chính thức); giao diện console cho đổi chế độ / đồng bộ tay.
