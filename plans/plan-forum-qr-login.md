# Đăng nhập Gege Forum bằng mã QR quét từ app HR

Ngày: 2026-10-10 · Yêu cầu của owner: "forum (.111) cũng có đăng nhập bằng quét mã QR từ Gege HR" (như console).
**Trạng thái:** code xong (gege_hr + trader-ui + theme forum); tính năng mặc định **TẮT** — xem "Bật" ở cuối.

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

## Code

| Repo | Nội dung |
|---|---|
| gege_hr | `api/web_qr.py` (`start`, `status`, `finish`, `peek`, `approve`), `utils/web_qr.py` (helper thuần + test `tests/test_web_qr.py`), `seat_bridge` nhận mã của HR, `VN HR Portal Setting`: `enable_forum_qr_login` (Check, 0), `forum_url` (Data — để trống = `https://forum.gegeteam.xyz`), `auth.me().features.seat_qr` = bật một trong hai loại QR |
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
