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
# 3) Unit test liên quan module vừa sửa (toàn bộ: pytest -q)
pytest tests/ -k <module> -q
```

- **KHÔNG commit** khi `ruff check` hoặc `ruff format --check` còn đỏ trên **file mình sửa**.
- **KHÔNG** chạy `ruff format .` toàn repo trong PR tính năng — chỉ format file trong scope (tránh diff hàng chục file làm loãng review).
- Sửa `setup.py` / seed / hooks → chạy thêm smoke: `bench --site <site> execute gege_hr.hooks.create_seed_data` (phải idempotent, không raise).

## Nợ kỹ thuật (baseline 2026-08-24)

Khi áp gate, toàn repo còn **~78 lỗi check + 74 file lệch format** (lịch sử trước gate). Nguyên tắc dọn dần:

- Mỗi PR `chore/ruff-<nhóm-file>` xử lý một nhóm file nhỏ, giữ nguyên semantics.
- Ưu tiên `--fix` an toàn trước (F401 unused imports, I001 isort, UP032 f-string), phần còn lại sửa tay + kèm test.
- Xem hiện trạng: `ruff check . | tail -1 && ruff format --check . | tail -1`.
- Không thêm lỗi mới: gate ở trên đã chặn ở cấp file-sửa.

## Quy ước code

- Python ≥3.10, line-length 110, ngoặc kép đôi (ruff format, black-compatible).
- Seed/setup: **idempotent** (chỉ填补 chỗ trống) + **bench-safe** (`frappe.log_error`, không raise làm abort `install-app`/`migrate`).
- Commit nhỏ, conventional commit (`feat:`, `fix:`, `chore:`, `seed:`).
- API whitelisted (`gege_hr.gege_hr.api.*`): luôn gate `frappe.only_for(HR_ADMIN_ROLES)` hoặc check role phù hợp + ghi `VN Audit Event` với hành động quản trị.
