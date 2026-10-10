# Agent Rules — gege_hr (Frappe backend VN HR)

App Frappe v15 (Python ≥3.10) — backend HR cho SPA `/hr` (trader-ui).
Chuẩn hóa quy trình code bằng AI: **mọi thay đổi PHẢI vượt gate kiểm tra dưới đây trước khi commit.**

## Gate bắt buộc TRƯỚC MỖI COMMIT

Linter/formatter chính thức = **ruff** (thay black + flake8 + isort; cấu hình trong `pyproject.toml`).

```bash
pip install --user ruff            # cài một lần
cd frappe-bench/apps/gege_hr

# 1) Lint các file đã sửa — PHẢI "All checks passed!"
ruff check <file1> <file2> ...
# 2) Format các file đã sửa — PHẢI "already formatted"
ruff format --check <file1> <file2> ...
#    (sửa tự động khi cần: ruff check --fix <files> && ruff format <files>)
# 3) Unit test liên quan module vừa sửa (toàn bộ: bỏ -k)
./env/bin/python -m pytest tests/ -k <module> -q
```

- **KHÔNG commit** khi `ruff check` hoặc `ruff format --check` còn đỏ trên **file mình sửa**.
- **KHÔNG** chạy `ruff format .` toàn repo trong PR tính năng — chỉ format file trong scope (tránh diff hàng chục file làm loãng review).
- Sửa `setup.py` / seed / hooks → chạy thêm smoke: `bench --site <site> execute gege_hr.hooks.create_seed_data` (phải idempotent, không raise).

## Branch model & PR (bắt buộc)

`feat/*|fix/*|chore/* → PR vào develop → CI XANH → merge`. Hotfix prod: `hotfix/*` cắt từ `main`, PR vào `main` rồi back-merge develop.

- **KHÔNG push thẳng vào develop/main** — mọi thay đổi (kể cả của AI agent) phải qua nhánh riêng + PR. (Bài học 2026-08-24: 4 commit push thẳng develop dù AGENTS.md đã có quy tắc.)
- CI (`.github/workflows/ci.yml`) chạy đủ 3 gate: `ruff check .` → `ruff format --check .` → `pytest -q`. **Chỉ merge khi CI xanh.**
- Không có `gh` CLI trên máy: push nhánh rồi mở link compare để tạo PR:
  `https://github.com/1x4mel/gege_hr/compare/develop...<tên-nhánh>?expand=1`
- 1 PR = 1 cụm task liên quan; commit nhỏ, conventional.

## Trạng thái baseline (2026-08-24: ĐÃ DỌN XONG)

`ruff check .` = **0 lỗi** • `ruff format --check .` = **227/227 file đạt chuẩn** • `pytest` = **992 passed**. Gate giờ áp dụng được cho TOÀN repo (không chỉ file sửa): chạy `ruff check . && ruff format --check .` trước mọi push. Đợt dọn đã sửa 2 bug thật: `SLIP_DOCTYPE` NameError trong `api/payroll.py` và filter `"time"` bị ghi đè (mất cận dưới khoảng truy vấn) trong `utils/calc.py`.

## Runbook hạ tầng (infra)

- Gán domain Cloudflare / expose máy nội bộ ra Internet: làm theo `docs/deploy-cloudflare-tunnel.md` (runbook từ lần deploy thành công 2026-08-24 — cloudflared 2026.8.2, tunnel 9637e533, verify từng bước). KHÔNG ghi token tunnel (`eyJ...`) vào repo/log.

- **Console gege (.90) ↔ HR (10/10/2026)**: HR là nguồn tài khoản của console, nhân viên đăng nhập console / vào ca gege-seat
  bằng tài khoản HR (QR hoặc mật khẩu qua *giấy xác nhận* dùng một lần), chấm công trên máy công ty bằng vé gege-seat. Code:
  `api/seat_bridge.py`, `utils/seat_checkin.py`; tài liệu: `plans/plan-tai-khoan-hr-cho-console.md`,
  `plans/plan-cham-cong-pc-gege-seat.md`; phía console: repo `Gege-Cyber-System-Localdisk` `docs/tai-khoan-hr.md`.
  Khoá ghép nối (`seat_console_key`) KHÔNG ghi vào repo/log. Endpoint `allow_guest` của `seat_bridge` phải giữ
  `_require_console` (đúng IP console + khoá) hoặc khoá tạm theo email.
- **Gege Forum đăng nhập bằng tài khoản HR ngay trên trang forum (10/10/2026)**: `api/web_qr.py` + `utils/web_qr.py`. Hai
  cách, mỗi cách một công tắc ở `VN HR Portal Setting` (owner đã bật cả hai): QR (`start` / `status`, app HR quét rồi
  `seat_qr_approve`) và email + mật khẩu HR gõ ở form của forum (`password_start`). Cả hai kết thúc ở `finish`: HR tạo phiên
  cho trình duyệt (`login_as`), forum đi tiếp OIDC. Quy tắc phải giữ:
  - Trang khác gọi sang HR **chỉ bằng `fetch` không cookie**; từng hàm tự gắn `Access-Control-Allow-Origin` cho đúng origin
    — KHÔNG bật `allow_cors` toàn site, KHÔNG dùng form POST cả trang (Frappe chặn mọi POST có phiên mà thiếu CSRF token
    → "Invalid Request" với người đang đăng nhập HR ở cùng trình duyệt; `seat_bridge.console_assertion` ra đời vì lỗi này).
  - Chỉ `finish?fmt=json` trả `Access-Control-Allow-Credentials` (phải nhận cookie phiên); nó chỉ trả `{"ok": bool}`.
  - Đích chuyển hướng luôn lấy từ cấu hình (`forum_url`, `seat_console_web_url`), không nhận từ người gọi.
  - Kiểm mật khẩu đi qua một hàm duy nhất `web_qr.check_hr_password` (khoá tạm 5 lần / 10 phút theo email).
- **Trang đăng nhập Gege HR có đăng nhập bằng QR (10/10/2026)**: cùng `api/web_qr.py`, `app="hr"`, công tắc
  `enable_hr_qr_login` (mặc định tắt) — `plans/plan-hr-login-qr.md`. `finish` cho `app="hr"` CHỈ nhận `fetch` cùng origin
  (`utils/web_qr.finish_request_ok`: `fmt=json` + `Sec-Fetch-Site: same-origin`), không nhận mở link — đừng nới.
  Tài liệu: `plans/plan-forum-qr-login.md`; phía forum: repo `Gege-discourse` `docs/08-dang-nhap-qr.md`.
- Cách tính công theo ca (ngưỡng co theo độ dài ca / theo phút, ân hạn trễ, Hourly đọc phiên chấm công):
  `plans/plan-cach-tinh-cong-theo-ca.md`.

## Quy ước code

- Python ≥3.10, line-length 110, ngoặc kép đôi (ruff format, black-compatible).
- Seed/setup: **idempotent** (chỉ填补 chỗ trống) + **bench-safe** (`frappe.log_error`, không raise làm abort `install-app`/`migrate`).
- Commit nhỏ, conventional commit (`feat:`, `fix:`, `chore:`, `seed:`).
- API whitelisted (`gege_hr.gege_hr.api.*`): luôn gate `frappe.only_for(HR_ADMIN_ROLES)` hoặc check role phù hợp + ghi `VN Audit Event` với hành động quản trị.
