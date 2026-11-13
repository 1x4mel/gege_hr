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
