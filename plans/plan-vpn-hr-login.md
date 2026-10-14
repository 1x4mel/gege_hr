# Đăng nhập app Gege VPN bằng tài khoản HR (`app="vpn"`)

Ngày: 2026-10-14 · Yêu cầu của owner: đăng nhập Gege VPN bằng tài khoản Gege HR, giống console và forum.
Phía VPN: repo `1x4mel/Gege-VPN`, `docs/DANG-NHAP-HR.md`.

**Trạng thái:** code + test xong (nhánh `feat/vpn-hr-login`), chưa deploy. Công tắc mặc định TẮT.

## Khác forum ở đâu

Gege VPN là **chương trình trên máy người dùng** (Python), không phải trang web, và không cần phiên HR:

| | forum / hr | vpn |
|---|---|---|
| ai gọi `start` / `status` / `password_start` | JS trang web (có `Origin`) | app Python (**không** `Origin`) |
| kiểm nguồn | `Origin == cfg.origin` | `native`: yêu cầu KHÔNG được có `Origin` (`utils/web_qr.origin_ok`) |
| vé hoàn tất đổi ở | `finish` → `login_as` (phiên trình duyệt) | `redeem` do **máy chủ Hub** gọi kèm khoá → trả email; `finish` từ chối |
| công tắc | `enable_forum_*`, `enable_hr_qr_login` | `enable_vpn_qr_login`, `enable_vpn_password_login` |

Trình duyệt luôn gắn `Origin` cho `fetch` POST → trang web lạ không mượn được đường của app để dò mật khẩu.
Kiểm mật khẩu vẫn qua `check_hr_password` (khoá 5 lần / 10 phút theo email, đếm chung).

## API mới (`api/web_qr.py`)

- `redeem(t)` — allow_guest, POST, header `X-Gege-Vpn-Key` = `vpn_hub_key` (Password, ≥ 32 ký tự; sai khoá → giới hạn
  10 lần / 10 phút theo IP). Vé dùng một lần (`_once("f:"…)` chung với `finish`), phải là vé `app="vpn"` ≤ 60 s, công
  tắc của cách xác thực đó phải bật, người đó phải là nhân viên đang làm (`_employee_user`). Ghi Activity Log
  "Đăng nhập Gege VPN bằng …". → `{"ok", "user", "full_name", "via"}`.
- `vpn_active(users)` — cùng khoá; → email nào trong danh sách (≤ 500) còn là nhân viên đang làm. Hub dùng để khoá VPN
  của người nghỉ việc.

## Trường mới ở `VN HR Portal Setting`

`enable_vpn_qr_login`, `enable_vpn_password_login` (Check, mặc định 0, có trong `PORTAL_SETTING_FIELDS`),
`vpn_hub_key` (Password — đặt ở Desk, không qua trang Dữ liệu nền). `auth._features().seat_qr` bật khi
`enable_vpn_qr_login` bật (hiện mục "Quét QR" trong app HR).

## Màn xác nhận QR

Dùng chung `/hr/seat-login` (kind `web`, title "Gege VPN", device "App Gege VPN trên Linux"). trader-ui: thêm 2 công tắc
ở Dữ liệu nền + câu xác nhận nói "app trên máy" thay vì "trình duyệt" cho `app="vpn"`.

## Deploy

PR → develop → CI xanh → merge → `.116`: `git pull` + `bench --site erp-hr.local migrate` + restart web. Sinh khoá
`openssl rand -hex 32` → Desk (VN HR Portal Setting → Gege VPN Hub Key) và `hub.ini [hr] key` trên mel-sing1.
