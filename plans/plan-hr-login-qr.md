# Trang đăng nhập Gege HR: giao diện kiểu console + đăng nhập bằng QR

Ngày: 2026-10-10 · Yêu cầu của owner: "thay đổi giao diện https://hr.gegeteam.xyz/login giống
https://console.gegeteam.xyz và cũng có thể đăng nhập bằng mã QR".

**Trạng thái (10/10 17:20):** ĐÃ deploy .116 — gege_hr #61 (c48d47b, đã `bench migrate`) + trader-ui #42 (07ef217, đã
build). Trang `/login` thật đã là giao diện mới; đã kiểm từ ngoài bằng Chrome headless (đăng nhập sai → báo lỗi trên
trang, không lỗi JS) và gọi API thật (`start` app=hr → `{"ok": false}` vì công tắc tắt; QR của forum vẫn cấp mã).
Công tắc **`enable_hr_qr_login` đang TẮT** (mặc định) — chờ owner bật ở Dữ liệu nền rồi quét thử bằng điện thoại
(chưa ai thử trọn vòng trên máy thật). Trước khi deploy đã thử trọn luồng trên máy làm việc (mục cuối file).

## Phạm vi

1. **Giao diện** (`trader-ui/src/views/LoginView.vue`): dựng lại như trang đăng nhập console — luôn nền tối, đồng hồ +
   ngày, thẻ đăng nhập ("● Chưa đăng nhập", "Đăng nhập Gege HR", hai ô nhập, nút), thẻ "Đăng nhập bằng QR", chân trang.
   Thông số chép từ các lớp `.sl-*` của console (`Gege-Cyber-System-Localdisk/console/static/index.html`).
   Đăng nhập bằng mật khẩu giữ nguyên (`/api/method/login`).
2. **QR**: máy tính chưa đăng nhập hiện mã → nhân viên quét bằng app Gege HR trên điện thoại (đang đăng nhập sẵn) →
   xác nhận → trình duyệt trên máy tính được đăng nhập HR.

## Dùng lại cơ chế của forum (`api/web_qr.py`)

Thêm một "trang web" nữa: `app="hr"` (`utils/web_qr.py` `APP_HR`). Khác forum ở chỗ trang xin mã **cùng origin** với HR:

```
trang /login của HR (máy tính)                HR                                   điện thoại (app HR)
 1. POST web_qr.start  app=hr  ─────────────►  cấp mã (150 s) + khoá hỏi kết quả
 2. vẽ QR  ··································································►  quét → /hr/seat-login?c=<mã>
                                               seat_qr_info / seat_qr_approve ◄──  hiện "Gege HR" + IP · trình duyệt → xác nhận
 3. POST web_qr.status (2 s/lần)  ──────────►  đã xác nhận → vé hoàn tất (1 lần, 60 s)
 4. fetch web_qr.finish?t=…&app=hr&fmt=json ►  login_as(người đã xác nhận) → {"ok": true} + cookie phiên
 5. SPA: initCsrfToken() → fetchUser → vào /hr/attendance (hoặc `redirect-to` cùng host)
```

- Cấu hình trang lấy từ `web_qr.app_config(app, forum_url, hr_base)`; `hr_base` = `public_base(request.host)` — không
  nhận địa chỉ từ người gọi. Mỗi trang có công tắc riêng (`flag_qr` / `flag_password`): forum giữ
  `enable_forum_qr_login` / `enable_forum_password_login`, HR dùng **`enable_hr_qr_login`** (mật khẩu: không có đường vé
  — trang HR gọi thẳng `/api/method/login`).
- `start` / `status`: như forum — phải đúng `Origin` (trình duyệt tự gắn cho POST cùng origin), gọi **không cookie**
  (trình duyệt còn phiên HR cũ mà POST kèm cookie sẽ bị Frappe chặn vì thiếu CSRF token).
- `peek` / `approve` (app HR quét): kiểm công tắc theo `app` của chính mã đó.
- Mục "Quét QR" trong app (`auth.me().features.seat_qr`) hiện khi bật một trong ba công tắc.

## Bảo mật — khác biệt so với forum

`finish` tạo phiên HR bằng một yêu cầu GET kèm vé. Với forum, GET này có thể mở cả trang (HR chuyển hướng về forum).
Với `app="hr"` thì **không nhận mở link**: nếu nhận, một người có thể tự lấy vé cho tài khoản của mình rồi gửi link cho
người khác — trình duyệt nạn nhân sẽ bị đăng nhập HR bằng tài khoản kẻ gửi (chấm công, gửi đơn… dưới tên người khác).
`utils/web_qr.finish_request_ok`: với `app="hr"` chỉ nhận `fmt=json` có `Sec-Fetch-Site: same-origin` và
`Sec-Fetch-Mode` khác `navigate` — hai header trình duyệt tự gắn, trang khác không giả được. Bị từ chối thì vé không bị
tiêu. Trình duyệt quá cũ không gửi `Sec-Fetch-Site` → không dùng được QR (dùng mật khẩu).

Còn lại giống forum: mã 150 giây, dùng một lần (`_once`), vé 60 giây dùng một lần, chỉ nhân viên đang làm
(`_employee_user`), màn xác nhận hiện IP + trình duyệt đang xin, ghi Authentication Log
("Đăng nhập Gege HR bằng QR (trình duyệt <IP>)"), giới hạn 60 lần xin mã / phút / IP.

Điểm đã biết của luồng forum (không đổi ở đây): `finish` mở cả trang cho `app="forum"` vẫn có tính chất "đăng nhập
bằng vé của người khác" nói trên — tác động là trình duyệt nạn nhân có phiên HR + forum của kẻ gửi link.

## File

| Repo | File | Thay đổi |
|---|---|---|
| gege_hr | `utils/web_qr.py` | `APP_HR`, `app_config(..., hr_base)`, `flag_for`, `finish_request_ok` |
| gege_hr | `api/web_qr.py` | công tắc theo từng trang (`_cfg` / `_on` / `_app`), `finish` cho `app="hr"` |
| gege_hr | `doctype/vn_hr_portal_setting` | trường `enable_hr_qr_login` (Check, mặc định 0) → **cần `bench migrate`** |
| gege_hr | `api/admin.py`, `api/auth.py` | cho sửa công tắc ở Dữ liệu nền; `features.seat_qr` |
| gege_hr | `tests/test_web_qr.py` | cấu hình `hr`, công tắc theo trang, `finish_request_ok` |
| trader-ui | `src/views/LoginView.vue` | giao diện mới + thẻ QR |
| trader-ui | `src/composables/useLoginQr.js`, `src/utils/loginPage.js` (+ test) | vòng xin mã / hỏi kết quả; đồng hồ, vẽ QR, `safeRedirectTarget` |
| trader-ui | `src/hr/utils/settingsView.js`, `src/hr/views/SeatLoginView.vue` | công tắc ở Dữ liệu nền; câu hướng dẫn ở trang quét |

## Triển khai (.116)

1. gege_hr: merge PR vào `develop` → `git pull` → `bench --site erp-hr.local migrate` → restart web.
2. trader-ui: merge PR vào `develop` → `rsync dist/ dist.prev/` → `git pull upstream develop` → `npm run build`.
3. Trang đăng nhập mới có ngay sau bước 2 (QR chưa hiện). Bật QR: HR Admin → Dữ liệu nền → Cấu hình chung →
   "Cho phép đăng nhập Gege HR trên máy tính bằng QR".
4. Thử: cửa sổ ẩn danh trên máy tính mở `/login` → quét bằng điện thoại (app HR → bấm tên → Quét QR) → xác nhận.

Quay lại: tắt công tắc (thẻ QR biến mất ở lần tải trang sau); giao diện cũ: `rsync dist.prev/ dist/`.

## Cách đã thử khi không có bench

Máy chủ thử (http.server) nạp `gege_hr.gege_hr.api.web_qr` thật với một module `frappe` giả (cache có hạn dùng, `db`,
`login_manager.login_as` → đặt cookie `sid`), phục vụ luôn `trader-ui/dist` và các hàm `/api/method/login`,
`get_logged_user`, `get_session_csrf_token`, `get_current_user_roles`; thêm `/__test/approve` đóng vai điện thoại.
Chrome headless (CDP) mở `/login` ở 1920×1080, 1366×768, 390×800: thẻ QR hiện, xác nhận → vào `/hr/attendance`; sai mật
khẩu → báo lỗi; `redirect-to` cùng host được theo; điện thoại và khi tắt công tắc không xin mã. Gọi tay bằng curl: thiếu
`Origin` → từ chối; mở link `finish` / gọi từ trang khác → `{"ok": false}`, vé còn nguyên; dùng lại vé → từ chối.
