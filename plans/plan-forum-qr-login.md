# Đăng nhập Gege Forum bằng tài khoản HR ngay trên trang forum (QR + email / mật khẩu)

Ngày: 2026-10-10 · Yêu cầu của owner: "forum (.111) cũng có đăng nhập bằng quét mã QR từ Gege HR" (như console).
**Trạng thái:** QR đã deploy 10/10 (HR + theme), owner tự bật. Cùng ngày thêm **email + mật khẩu HR ở form bên trái của
forum** (mục riêng bên dưới). Mỗi cách một công tắc, mặc định **TẮT**.

## Bối cảnh

- Gege Forum (Discourse, https://forum.gegeteam.xyz, repo `1x4mel/Gege-discourse`) đăng nhập bằng **OIDC của HR**:
  nút "Đăng nhập bằng ERP Gege" → HR xác thực → quay về forum. Tức là *đăng nhập forum = trình duyệt có phiên HR*.
- Console đã có đăng nhập bằng QR (plan-tai-khoan-hr-cho-console.md), nhưng console tự cấp phiên của nó. Forum thì
  không tự cấp phiên được nếu không viết plugin Discourse → đi đường **cho trình duyệt một phiên HR** rồi để OIDC
  sẵn có làm phần còn lại. Không đụng tới OAuth của Frappe, không cần plugin hay rebuild forum.

## Luồng

```
trang đăng nhập forum (theme JS)                HR (.116)                          điện thoại (app HR)
 1. POST web_qr.start  app=forum  ───────────►  cấp mã (150 s) + khoá hỏi kết quả
    ◄── ma trận QR, poll, ttl                    (Redis; ghi IP + trình duyệt đang xin)
 2. vẽ QR  ·····································································►  quét → /hr/seat-login?c=<mã>
                                                 seat_qr_info  ◄──────────────────  hiện "Gege Forum" + IP + trình duyệt
                                                 seat_qr_approve ◄────────────────  bấm "Đăng nhập Gege Forum"
 3. POST web_qr.status poll=… (2 s/lần) ─────►  đã xác nhận → vé hoàn tất (1 lần, 60 s)
 4. mở web_qr.finish?t=<vé>&app=forum ───────►  login_as(người đã xác nhận) → 302 forum/login?gege_qr=1
 5. theme bấm tiếp nút ERP (OIDC) ───────────►  đã có phiên → cấp code ngay (client "Skip Authorization")
    → forum đăng nhập xong
```

- `start` / `status` gọi **khác origin, không gửi cookie** (`credentials: omit`), dạng form thường nên không cần preflight.
  HR tự gắn `Access-Control-Allow-Origin: <forum>` cho đúng hai hàm này — **không** bật `allow_cors` toàn site (forum và
  HR cùng site `gegeteam.xyz`: bật CORS kèm cookie thì một lỗi XSS trên forum đọc được API HR của người dùng).
- Mã QR là ma trận do HR tạo bằng `pyqrcode` (có sẵn trong Frappe); theme chỉ vẽ SVG, không cần thư viện QR.
- Màn quét dùng lại `/hr/seat-login` và hai hàm `seat_bridge.seat_qr_info` / `seat_qr_approve`: mã do HR cấp thì xử lý tại
  chỗ (`api/web_qr.peek` / `approve`), không phải thì hỏi console như cũ.

## Bảo mật

| Điểm | Xử lý |
|---|---|
| Kết quả của một lần quét | trình duyệt đang hiện mã có **phiên HR đầy đủ** của người quét — giống hệt khi họ gõ mật khẩu HR ở bước OIDC hôm nay |
| Kẻ gian đưa mã của HỌ cho nhân viên quét (QRLjacking) | màn xác nhận hiện **tên trang + IP + trình duyệt** đang xin đăng nhập và dòng cảnh báo; mã sống 150 s, dùng một lần; chỉ nhân viên đang làm xác nhận được; ghi Activity Log "Đăng nhập Gege Forum bằng QR (trình duyệt <ip>)" |
| Đích chuyển hướng | chỉ lấy từ cấu hình (`forum_url`) — không nhận URL từ người gọi; `app` lạ → báo lỗi |
| Khoá hỏi kết quả / vé hoàn tất | chỉ trình duyệt xin mã giữ khoá; vé trả đúng một lần (Redis INCR), đổi trong 60 s, đi trong URL nên dùng xong là vô giá trị |
| IP hiển thị | qua Cloudflare Tunnel nginx chỉ thấy loopback → lấy `CF-Connecting-IP`; vào thẳng trong LAN thì lấy IP nginx thấy |
| Gọi dồn dập | `start` giới hạn 60 lần/phút theo IP (cả văn phòng chung một IP nên để rộng) |
| Không chặn được | "đăng nhập hộ": kẻ gian tự xác nhận tài khoản CỦA HỌ rồi dụ người khác mở link `finish` → nạn nhân vào forum bằng tài khoản kẻ gian. Tác hại thấp; mọi luồng kiểu "quét để đăng nhập máy khác" đều có điểm này |
| 2FA của Frappe | `login_as` không hỏi OTP — nếu sau này bật 2FA cho HR thì coi việc xác nhận từ điện thoại đã đăng nhập là yếu tố thứ hai, hoặc tắt tính năng này |

Tính năng mặc định tắt để owner tự quyết sau khi đọc bảng trên.

## Email + mật khẩu HR ở form bên trái của forum (10/10, yêu cầu thêm của owner)

Form "Email / Mật khẩu" của Discourse chỉ kiểm mật khẩu riêng của forum; nhân viên hay gõ nhầm tài khoản HR vào đó.
Nay theme bắt lệnh gửi form: **email thuộc tên miền công ty** (`hr_login_domains`, mặc định `gegeteam.net`) thì gửi thẳng
tới HR; tên đăng nhập forum / email khác (tài khoản dự phòng `1x4mel`) vẫn đi đường cũ của Discourse.

```
form forum ── POST web_qr.password_start (usr, pwd, app=forum; khác origin, KHÔNG cookie) ──► HR kiểm mật khẩu
           ◄── { ok, t } hoặc { ok:false, msg }  (sai → báo ngay trên trang, không tải lại)
           ── mở web_qr.finish?t=…  ──► HR tạo phiên ──► forum/login?gege_qr=1 ──► OIDC như QR
```

- **Không dùng form POST thẳng tới HR** (kiểu `console_login`): forum và HR cùng site `gegeteam.xyz` nên trình duyệt gửi
  kèm cookie phiên HR, mà Frappe (`HTTPRequest.validate_csrf_token`) từ chối MỌI POST có phiên nhưng thiếu CSRF token →
  ai đang đăng nhập HR ở cùng trình duyệt sẽ gặp "Invalid Request". `fetch` với `credentials: omit` thì là khách, không dính.
  **`console_login` của trang web console dính đúng lỗi này** → thêm `seat_bridge.console_assertion` (cùng cách: fetch
  không cookie, CORS cho origin console, trả `go`), console chuyển sang dùng nó.
- Kiểm mật khẩu dùng chung một hàm với giấy xác nhận của console / gege-seat (`web_qr.check_hr_password`): chỉ nhân viên
  đang làm, sai 5 lần / 10 phút theo email thì khoá tạm, không lộ tài khoản có tồn tại hay không. Thêm giới hạn 30 lần /
  phút theo IP.
- Mật khẩu đi thẳng trình duyệt → HR qua HTTPS, **không tới máy chủ forum**. Nhưng nó được gõ trên trang của forum: mã
  chạy trên trang đó (theme, plugin, ai có quyền admin forum) về nguyên tắc đọc được. Và nếu một bản nâng cấp Discourse
  đổi `#login-form` / `#login-account-name` làm theme không bắt được nữa, mật khẩu HR sẽ bị gửi cho máy chủ forum như một
  lần đăng nhập sai → sau mỗi lần nâng cấp forum phải thử lại (docs/08 của repo forum).
- Công tắc: HR `enable_forum_password_login` (mặc định tắt) + theme `hr_password_login_enabled`. Theme hỏi
  `web_qr.features` để biết HR có bật không; chưa bật thì form chạy y như cũ.
- Nhật ký: "Đăng nhập Gege Forum bằng mật khẩu HR nhập trên trang đó (trình duyệt <ip>)".

## Code

| Repo | Nội dung |
|---|---|
| gege_hr | `api/web_qr.py` (`start`, `status`, `finish`, `peek`, `approve`, `features`, `password_start`, `check_hr_password`), `seat_bridge.console_assertion`, `utils/web_qr.py` (helper thuần + test `tests/test_web_qr.py`), `seat_bridge` nhận mã của HR, `VN HR Portal Setting`: `enable_forum_qr_login` + `enable_forum_password_login` (Check, 0), `forum_url` (Data — để trống = `https://forum.gegeteam.xyz`), `auth.me().features.seat_qr` = bật một trong hai loại QR |
| trader-ui | `SeatLoginView` / `seatQr.js`: mã `kind = "web"` có `title` → hiện tên trang, IP · trình duyệt, cảnh báo; công tắc ở Dữ liệu nền |
| Gege-discourse | theme: `javascripts/discourse/lib/gege-qr-login.js` (JS thuần), `api-initializers/gege-qr-login.gjs`, CSS, setting `qr_login_enabled`; tài liệu `docs/08-dang-nhap-qr.md` |

## Bật (owner)

1. Deploy gege_hr (có `bench migrate` — 2 trường mới) + trader-ui như thường lệ.
2. Deploy theme forum: `scripts/deploy-theme.sh` trong repo Gege-discourse (cần mật khẩu root .111). Chưa bật ở HR thì
   trang đăng nhập forum **không đổi gì** (thẻ QR chỉ hiện khi HR cấp được mã).
3. HR Admin → Dữ liệu nền → Cấu hình chung → Check-in & Xác thực → **"Cho phép đăng nhập Gege Forum bằng QR"**.
4. Thử: mở forum ở cửa sổ ẩn danh trên máy tính → quét mã bằng app HR (menu tài khoản → Quét QR) → xác nhận.

## Đã thử / chưa thử

- Helper thuần: `tests/test_web_qr.py`. Luồng đầy đủ phía HR với Frappe giả (cache dict): cấp mã, origin lạ, xác nhận một
  lần, vé một lần, người đã nghỉ việc, Frappe từ chối tạo phiên, tắt tính năng.
- Theme: chạy `gege-qr-login.js` trên **trang đăng nhập thật** của forum bằng Chrome headless với HR giả (hiện mã, hỏi kết
  quả không kèm cookie, chuyển sang `finish`, mất liên lạc → nút "Lấy mã mới"); file `.gjs` qua `content-tag` + `@glimmer/syntax`.
- **Chưa thử trên hệ thống thật**: cần owner bật công tắc và quét bằng tài khoản nhân viên (không có tài khoản thử trên HR
  production).
