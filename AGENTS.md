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

## Quy ước code

- Python ≥3.10, line-length 110, ngoặc kép đôi (ruff format, black-compatible).
- Seed/setup: **idempotent** (chỉ填补 chỗ trống) + **bench-safe** (`frappe.log_error`, không raise làm abort `install-app`/`migrate`).
- Commit nhỏ, conventional commit (`feat:`, `fix:`, `chore:`, `seed:`).
- API whitelisted (`gege_hr.gege_hr.api.*`): luôn gate `frappe.only_for(HR_ADMIN_ROLES)` hoặc check role phù hợp + ghi `VN Audit Event` với hành động quản trị.
