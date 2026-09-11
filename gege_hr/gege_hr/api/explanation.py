"""Phiếu giải trình chấm công (VN Attendance Explanation) — list + HR duyệt.

Sinh tự động khi ``mobile_checkin`` nhận một lượt chấm ngoài cửa sổ của ca
kèm lý do bắt buộc (api/attendance._window_violation). Trang nhân viên xem
phiếu của mình tại /hr/attendance; HR duyệt/từ chối tại /hr/exceptions.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "log_type",
    "log_time",
    "shift_type",
    "explanation_type",
    "minutes_deviation",
    "reason",
    "status",
    "decided_by",
    "decided_on",
    "decision_note",
    "creation",
]

_HR_ROLES = ("HR Manager", "System Manager")


def _viewer_employee() -> str:
    emp = frappe.db.get_value(
        "Employee", {"user_id": frappe.session.user, "status": "Active"}, "name"
    )
    if not emp:
        frappe.throw(_("Tài khoản không liên kết nhân viên."), frappe.PermissionError)
    return emp


@frappe.whitelist()
def my_explanations() -> list[dict]:
    """Phiếu giải trình của người dùng hiện tại (mới nhất trước)."""
    return frappe.get_all(
        "VN Attendance Explanation",
        filters={"employee": _viewer_employee()},
        fields=_LIST_FIELDS,
        order_by="creation desc",
        limit_page_length=50,
    )


@frappe.whitelist()
def pending_explanations() -> list[dict]:
    """Hộp chờ duyệt của HR."""
    frappe.only_for(list(_HR_ROLES))
    return frappe.get_all(
        "VN Attendance Explanation",
        filters={"status": "Open"},
        fields=_LIST_FIELDS,
        order_by="creation asc",
        limit_page_length=100,
    )


@frappe.whitelist()
def decide_explanation(name: str | None = None, decision: str | None = None, note: str | None = None) -> dict:
    """HR duyệt / từ chối một phiếu giải trình."""
    frappe.only_for(list(_HR_ROLES))
    name = (name or "").strip()
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        frappe.throw(_("Hành động phải là approve hoặc reject."))
    if not name or not frappe.db.exists("VN Attendance Explanation", name):
        frappe.throw(_("Phiếu giải trình không tồn tại."))
    doc = frappe.get_doc("VN Attendance Explanation", name)
    if doc.status != "Open":
        frappe.throw(_("Phiếu đã được xử lý trước đó."))
    doc.status = "Approved" if decision == "approve" else "Rejected"
    doc.decided_by = frappe.session.user
    doc.decided_on = now_datetime()
    doc.decision_note = (note or "").strip()
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    return {"name": doc.name, "status": doc.status}
