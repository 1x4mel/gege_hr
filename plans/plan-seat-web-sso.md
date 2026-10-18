# SSO gege-seat: vào ca là vào HR/Forum, hết ca là thoát hết (`seat_bridge.web_login` / `web_qr.seat_go`)

Ngày: 2026-10-18 · Yêu cầu của owner: đồng bộ tài khoản gege-seat trên máy dàn với HR / Console / Forum — bấm mở
app là vào thẳng, kết thúc ca thì thoát các phiên đã đăng nhập trên máy đó.

**Trạng thái (18/10 tối): ĐÃ DEPLOY .116** — e73a784 + fix 09e8696 (main = develop, đã `bench restart`);
console `.90` đã gọi thật (`Gege-Cyber-System-Localdisk` 804302b/408a99d). E2E trên b7: `/app/hr` → 302
`/hr/attendance` + cookie sid; `/app/forum` → 302 forum `/login?gege_qr=1` đi tiếp OIDC; `kill_sessions` thu hồi
3/3 phiên test. Công tắc dùng chung `enable_seat_qr_login` (đã bật).

## Luồng

```
bấm 🌐 trên notch → daemon máy (127.0.0.1:47150/app/<id>)
  → console /api/seat/m/sso (chan IP máy, kiểm ca đang mở)
  → HR seat_bridge.web_login {email, host, dest}   (console→HR: IP LAN + X-Seat-Key; trả go TƯƠNG ĐỐI)
  → console ghép go với hr.app (domain công khai) → trình duyệt mở
  → web_qr.seat_go?t=… : đổi vé (1 lần, 60 s) lấy phiên HR cho ĐÚNG người trong ca
      dest=hr    → redirect /hr/attendance
      dest=forum → redirect forum /login?gege_qr=1 → OIDC sẵn có
  → sid ghi theo tag gege_seat_sso:<host>:<email> (cache 12 h)
HẾT CA (mọi cách đóng ca) → console _seat_end → HR kill_sessions {email, host}
  → delete_session từng sid đã tag (frappe.sessions.delete_session) — CHỈ phiên tạo qua SSO từ máy đó,
    phiên trên điện thoại nhân viên không nằm trong tag nên không bị ảnh hưởng.
```

Console tự thu hồi phiên console web của mình + gọi admin API Discourse `log_out` (forum giữ phiên riêng sau
OIDC) — xem `Gege-discourse/docs/09-thoat-khi-het-ca.md`.

## Bẫy đã gặp (18/10)

- `login_email` / `public_base` nằm ở **utils** web_qr, không phải api — seat_bridge import `api.web_qr` là
  AttributeError 500.
- Console gọi HR PHẢI qua IP LAN (`_hr_call`, `hr.url`/`hr.ip`): qua domain công khai dính Cloudflare 403
  lỗi 1010 (chặn UA python-urllib).
- `go` phải là đường dẫn TƯƠNG ĐỐI cho console ghép `hr.app`: vé mở qua IP LAN thì cookie HR gắn nhầm host
  nội bộ, forum OIDC (chạy trên domain công khai) không nhận được phiên.
