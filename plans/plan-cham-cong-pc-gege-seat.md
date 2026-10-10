# Chấm công trên máy công ty qua gege-seat

Ngày: 2026-10-10 · Owner chốt: 2026-10-10 (thử trên **b6** trước, ổn mới cài cả dàn)

## Bài toán

Chấm công hiện chỉ bằng điện thoại + GPS. Nhân viên quên điện thoại / GPS yếu không chấm được.
Yêu cầu: cho chấm trên PC nhưng **bắt buộc là máy công ty**; máy bị điều khiển qua Moonlight **từ ngoài mạng công ty**
**không** được chấm (Moonlight từ máy trong mạng công ty thì được — owner chốt thêm 10/10, xem mục Moonlight).

Kiểm IP không dùng được: `hr.gegeteam.xyz` đi qua Cloudflare Tunnel (nginx .116 chỉ thấy `::1`), và IP
không phân biệt ngồi tại chỗ với điều khiển từ xa. → dùng **gege-seat** (console .90,
`Gege-Cyber-System-Localdisk/docs/ca-lam-viec-khoa-may.md` mục 11) làm bằng chứng.

## Luồng — vé dùng một lần cấp từ chính máy

1. `today_status.pc_checkin.enabled` (cài đặt `enable_pc_checkin`, mặc định **tắt**) → SPA dò dịch vụ
   gege-seat của máy: `GET http://127.0.0.1:47150/hr-ticket?probe=1` (chỉ trình duyệt chạy TRÊN máy gọi được;
   dịch vụ chỉ nhận Origin của trang HR).
2. Bấm chấm công → SPA `GET /hr-ticket` → dịch vụ xin console `POST /api/seat/m/ticket` (console chỉ nhận
   đúng IP đã đăng ký của máy; ca phải đang mở, không tạm khoá) → vé sống 90 giây, kèm cờ `moonlight`
   (máy đang có phiên Moonlight điều khiển).
3. SPA gọi `mobile_checkin(seat_ticket=…)` (không GPS) → máy chủ HR `POST <console>/api/seat/hr/verify`
   (console chỉ nhận IP máy chủ HR + header `X-Seat-Key`) → `{host, username, remote, moonlight}`.
4. `utils/seat_checkin.evaluate`: cho chấm khi vé hợp lệ **và** `username` = `Employee.user_id` của người
   đang đăng nhập HR **và** `remote = 0` **và** `moonlight = 0`. Lượt chấm ghi `device_id = seat:<máy>`,
   `vn_source_type = PC`. Mọi luật khác của `mobile_checkin` giữ nguyên (cửa sổ giờ + lý do, chống bấm trùng,
   khoá kỳ, tự đóng phiên quên chấm ra).

## Moonlight trong / ngoài mạng công ty

Nhân viên thường ngồi một máy trong dàn rồi Moonlight sang máy khác (729/729 phiên console đã ghi đều từ
`192.168.2.x`). Dịch vụ gege-seat gửi IP các phiên Moonlight điều khiển đang mở (`ml_ips`); **console** phân loại:
trong mạng = khớp tiền tố `lan` (mặc định `192.168.2.`) và không thuộc `lan_exclude` (mặc định `192.168.2.1` — router).
Console trả `moonlight = 1` khi có phiên từ ngoài, `remote = 1` khi ca mở bằng vé Moonlight mà không còn phiên nội bộ
nào → `evaluate` (không đổi) từ chối đúng hai cờ đó. Đổi dải: console `POST /api/seat/hrcfg {lan, lan_exclude}`.

## Cấu hình (VN HR Portal Setting)

| Field | Ý nghĩa |
|---|---|
| `enable_pc_checkin` | bật tính năng (mặc định 0) |
| `seat_console_url` | địa chỉ console trong LAN (mặc định `http://192.168.2.90:8080`) |
| `seat_console_key` | khoá bí mật do console cấp (`POST /api/seat/hrcfg {gen_key: true}`) — Password, không echo/audit |

Console: `seat_settings` scope `hr` (`enabled`, `ip` = IP máy chủ HR, `key`) qua `/api/seat/hrcfg` (admin).

## Giới hạn

- Chỉ dùng được trên máy đã cài + bật gege-seat (hiện: b6).
- Console sập / mở máy bằng mật khẩu khẩn cấp → không chấm trên PC được (còn điện thoại).
- Ai biết mật khẩu console của người khác vẫn chấm hộ được (như mọi cách dùng mật khẩu) — giảm bằng gán máy riêng.
- Phiên Moonlight lấy từ log Sunshine (`GEGE-SESSION` start/end theo `id`, mode play). Người ở ngoài vào được một máy
  trong mạng bằng công cụ khác Moonlight, hoặc VPN cấp IP `192.168.2.x` chưa khai trong `lan_exclude`, sẽ được coi là
  trong mạng công ty.
