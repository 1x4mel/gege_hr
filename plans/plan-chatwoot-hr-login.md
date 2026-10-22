# Đăng nhập Gege Chatwoot bằng tài khoản HR (QR + email / mật khẩu ngay trên trang Chatwoot)

Ngày: 2026-10-22 · Yêu cầu của owner: Chatwoot (hộp thư G2G / Eldorado, https://chat.gegeteam.xyz, máy .120) đăng nhập
bằng tài khoản HR **giống forum và console**: tick cho phép ở HR, quét QR từ app HR, gõ email + mật khẩu HR ngay ở form.

## Nguyên tắc: đi y hệt Gege Forum (plans/plan-forum-qr-login.md)

Chatwoot cộng đồng không có OIDC → phía Chatwoot có trang SSO nhỏ (`/sso/hr/login`, repo `1x4mel/gege-chatwoot-g2g-eldo`
`bridge/sso/hr_sso.py`) đăng nhập OIDC + PKCE với HR rồi cấp link đăng nhập một lần của Chatwoot. Vì vậy, như forum,
*đăng nhập Chatwoot = trình duyệt có phiên HR*: QR / mật khẩu ở `web_qr` cho trình duyệt một phiên HR (`finish`), rồi trang
Chatwoot mở `/sso/hr/login` → OIDC đi thẳng (client "Skip Authorization") → vào Chatwoot.

```
trang đăng nhập Chatwoot (button.js)       HR (.116)                      điện thoại (app HR)
 web_qr.features app=chatwoot ─────────►  {qr, password} theo 2 công tắc
 web_qr.start    app=chatwoot ─────────►  mã QR (150 s) ····················►  /hr/seat-login → "Gege Chatwoot" + IP
 web_qr.status   (2 s/lần)    ─────────►  vé hoàn tất (1 lần, 60 s)         ◄─  xác nhận
 hoặc web_qr.password_start (email + mật khẩu HR, không cookie) → vé
 web_qr.finish?fmt=json (fetch KÈM cookie) ►  login_as → {"ok": true} (cookie phiên HR, cùng site gegeteam.xyz)
 mở /sso/hr/login ──► OIDC với HR (đã có phiên) ──► kiểm role Chat Agent / Chat Admin ──► vào Chatwoot
```

## Thay đổi ở gege_hr

| Chỗ | Nội dung |
|---|---|
| `utils/web_qr.py` | `APP_CHATWOOT = "chatwoot"`, `DEFAULT_CHATWOOT_URL`; `app_config(..., chatwoot_url)`: origin = địa chỉ Chatwoot, `done` = `<chat>/sso/hr/login`, `fail` = `<chat>/app/login?gege_qr=err`, công tắc `enable_chatwoot_qr_login` / `enable_chatwoot_password_login` |
| `api/web_qr.py` | `_cfg` đọc thêm `chatwoot_url`. Mọi hàm (`start`, `status`, `finish`, `features`, `password_start`, `peek`, `approve`) dùng chung, không đổi luật |
| `VN HR Portal Setting` | 2 Check (mặc định 0) + `chatwoot_url` (Data, để trống = https://chat.gegeteam.xyz) — cần `bench migrate` |
| `api/admin.py` | cho trang Dữ liệu nền sửa 3 trường mới |
| `api/auth.py` | `features.seat_qr` bật khi `enable_chatwoot_qr_login` bật (hiện mục "Quét QR" trong app) |
| `tests/test_web_qr.py` | cấu hình, công tắc, luật origin / `finish` của chatwoot |

Quyền vào Chatwoot **không** ở đây: phía Chatwoot chỉ cho vào người có role HR **Chat Agent** (agent) / **Chat Admin**
(administrator), kiểm sau bước OIDC. Người không có role vẫn có thể có phiên HR (như mọi lần đăng nhập HR) nhưng bị Chatwoot
từ chối với thông báo rõ ràng.

## Bảo mật

Giống hệt forum (cùng bảng rủi ro): mã 150 s một lần, màn xác nhận hiện tên trang + IP + trình duyệt, đích chuyển hướng chỉ
lấy từ cấu hình, chỉ `finish?fmt=json` trả `Access-Control-Allow-Credentials` cho đúng origin Chatwoot, mật khẩu kiểm qua
`check_hr_password` (khoá 5 lần / 10 phút). Chatwoot chỉ trong LAN (192.168.2.120) nhưng HR thì công khai — trình duyệt
nhân viên gọi HR qua https như forum.

Mật khẩu HR gõ trên trang Chatwoot: `button.js` (nginx chèn vào trang) bắt lệnh gửi form; email thuộc tên miền công ty
(`gegeteam.net`) thì gửi thẳng tới HR, không tới máy chủ Chatwoot. Sau mỗi lần nâng cấp Chatwoot phải thử lại (selector form
có thể đổi → mật khẩu HR sẽ bị gửi cho Chatwoot như một lần đăng nhập sai).

## Bật / tắt

Deploy gege_hr (có `bench migrate`) + trader-ui (công tắc ở Dữ liệu nền → Check-in & Xác thực). Hai công tắc mặc định TẮT —
owner tự bật. Tắt cái nào thì trang đăng nhập Chatwoot tự bỏ cách đó ở lần tải sau.
