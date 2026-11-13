"""PERMISSION_MATRIX: dòng chỉ có ``report`` mà thiếu ``read`` làm role mất quyền đọc.

Custom DocPerm thay thế DocPerm chuẩn (không gộp). 13/11/2026: VN Checkout Miss
chỉ có {"report": 1} cho HR Manager → HR Manager không đọc được phiếu, Workflow
chặn doc.save(ignore_permissions=True) → không tự miễn được phiếu "Quên chấm ra"
khi admin sửa giờ ra. Đọc ma trận bằng ast, không cần frappe.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "gege_hr" / "gege_hr" / "api" / "setup_permissions.py"
NAMES = {"HR_MANAGER": "HR Manager", "HR_USER": "HR User", "EMPLOYEE": "Employee"}


def _matrix() -> dict:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == "PERMISSION_MATRIX":
            return eval(compile(ast.Expression(node.value), str(SRC), "eval"), {}, dict(NAMES))  # noqa: S307
    raise AssertionError("PERMISSION_MATRIX not found")


def test_every_grant_includes_read():
    bad = [
        f"{dt} / {role}"
        for dt, roles in _matrix().items()
        for role, ptypes in roles.items()
        if any(ptypes.values()) and not ptypes.get("read")
    ]
    assert not bad, f"grant thiếu read (Custom DocPerm đè quyền chuẩn): {bad}"


def test_checkout_miss_hr_manager_can_read_and_write():
    g = _matrix()["VN Checkout Miss"]["HR Manager"]
    assert g.get("read") and g.get("write") and g.get("report")


def _literal(path: Path, var: str) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == var:
            return eval(compile(ast.Expression(node.value), str(path), "eval"), {}, dict(NAMES))  # noqa: S307
    raise AssertionError(f"{var} not found")


def test_report_ref_doctypes_keep_report_for_hr_manager():
    """Báo cáo trong thư viện chỉ chạy khi HR Manager có ``report`` trên ref_doctype.
    Doctype đã có dòng ma trận thì dòng đó phải tự mang ``report``."""
    reports = _literal(SRC.parent / "reports.py", "REPORT_ALLOWLIST")
    matrix = _matrix()
    bad = [
        f"{rep} → {dt}"
        for rep, dt in reports.items()
        if dt in matrix and "HR Manager" in matrix[dt] and not matrix[dt]["HR Manager"].get("report")
    ]
    assert not bad, f"thiếu report: {bad}"


def test_field_level_grants_cover_approval_fields():
    fl = _literal(SRC, "FIELD_LEVEL_MATRIX")
    for dt in ("Expense Claim", "Leave Application"):
        assert fl[dt]["HR Manager"][1] == {"read": 1, "write": 1}


def test_employee_can_file_salary_advance():
    """Không có dòng này Workflow chặn send_for_approval → đơn kẹt Draft."""
    g = _matrix()["VN Salary Advance Request"]["Employee"]
    assert g.get("read") and g.get("write") and g.get("create")
    assert not g.get("submit") and not g.get("delete")


def test_employee_never_edits_or_deletes_checkins():
    g = _matrix()["Employee Checkin"]["Employee"]
    assert not g.get("write") and not g.get("delete")
