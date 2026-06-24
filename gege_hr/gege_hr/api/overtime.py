"""
Overtime Request API — plan v5 §10.4 / doctype-design §13.

Milestone-2 read + submit endpoints fronting the VN Overtime Request DocType.
These map 1:1 to the frontend ``gege_hr.gege_hr.api.overtime.<fn>`` calls in
``hr-ui/src/api/index.js``:

  * ``my_overtime_requests`` — the caller's own OT requests (filtered by window)
  * ``submit_overtime_request`` — create a Draft OT request (validation + naming
    handled by the DocType); the workflow transitions move it toward Approved.

The lifecycle is workflow-driven (Draft → Pending Manager → Pending HR →
Approved → Confirmed/Rejected). Only Approved/Confirmed rows are picked up by
the calculation engine (``calc.get_approved_ot_requests``).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

DOCTYPE = "VN Overtime Request"

# Row shape returned to the SPA — kept stable/flat so the list renders without
# a second lookup (matches ``hr-ui/src/api/index.js`` OT request row docstring).
_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_instance",
    "work_session",
    "company",
    "overtime_type",
    "from_datetime",
    "to_datetime",
    "requested_hours",
    "actual_hours",
    "approved_hours",
    "workflow_state",
    "docstatus",
    "reason",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve(employee: str | None) -> str:
    """Resolve the employee for the caller (param > session user)."""
    if employee:
        return emp_utils.emp_name(employee)
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(
            _("Tài khoản này chưa được liên kết với nhân viên."),
            frappe.PermissionError,
        )
    return emp


def _is_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _assert_own(employee: str) -> None:
    """HR/Manager may read anyone; a plain Employee may only read their own row."""
    if _is_manager():
        return
    own = emp_utils.get_employee_for_user()
    if own != emp_utils.emp_name(employee):
        frappe.throw(
            _("Bạn không có quyền truy cập dữ liệu của nhân viên khác."),
            frappe.PermissionError,
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_overtime_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Plan §10.4 — the caller's OT requests, optionally narrowed by work_date.

    Managers (HR Manager/System Manager) may pass any ``employee``; a plain
    Employee is scoped to their own record.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="work_date desc, creation desc",
    )
    return rows


@frappe.whitelist()
def submit_overtime_request(**kwargs) -> dict:
    """Plan §10.4 — create a Draft OT request.

    Accepts the flat payload the SPA sends: ``employee``, ``work_date``,
    ``overtime_type``, ``from_datetime``, ``to_datetime``, ``requested_hours``,
    ``reason`` (optional ``shift_instance``/``work_session``/``attachment``).

    Returns ``{ name, status, message }``. Validation (window, requested-hours
    cap, no-duplicate) runs in ``VNOvertimeRequest.validate``; naming in its
    ``before_insert`` hook (``OR-YYMMDD-XXXXXX``).
    """
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu yêu cầu tăng ca."))

    employee = (kwargs.get("employee") or "").strip()
    if employee:
        _assert_own(employee)
    emp = _resolve(employee)

    doc = frappe.new_doc(DOCTYPE)
    doc.update(
        {
            "employee": emp,
            "work_date": getdate(kwargs.get("work_date")) if kwargs.get("work_date") else None,
            "overtime_type": kwargs.get("overtime_type") or "Post-shift",
            "from_datetime": kwargs.get("from_datetime"),
            "to_datetime": kwargs.get("to_datetime"),
            "requested_hours": kwargs.get("requested_hours"),
            "reason": kwargs.get("reason") or "",
            "workflow_state": "Draft",
            "docstatus": 0,
        }
    )
    # Optional link fields.
    if kwargs.get("shift_instance"):
        doc.shift_instance = kwargs["shift_instance"]
    if kwargs.get("work_session"):
        doc.work_session = kwargs["work_session"]
    if kwargs.get("attachment"):
        doc.attachment = kwargs["attachment"]

    doc.insert()
    # Move into the approval pipeline (Draft → Pending Manager). Best-effort:
    # stays Draft if the workflow isn't seeded yet.
    send_for_approval(doc)
    audit_api.log(
        "OT Submit",
        doc=doc.as_dict(),
        work_date=doc.work_date,
        description=f"Draft → {doc.workflow_state}",
    )
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "message": _("Đã tạo yêu cầu tăng ca {0}.").format(doc.name),
    }


@frappe.whitelist()
def cancel_overtime_request(name: str | None = None) -> dict:
    """Cancel a Draft/Pending OT request (set Rejected, keep the audit trail).

    A dedicated endpoint is preferable to the SPA ``setValue`` fallback so the
    transition is consistent and permission-checked server-side.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu tăng ca."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state in ("Approved", "Confirmed"):
        frappe.throw(
            _("Yêu cầu đã duyệt không thể hủy từ phía nhân viên."),
            frappe.PermissionError,
        )
    doc.workflow_state = "Rejected"
    if doc.docstatus == 1:
        doc.cancel()
    else:
        doc.save()
    return {
        "name": name,
        "status": doc.workflow_state,
        "message": _("Đã hủy yêu cầu tăng ca {0}.").format(name),
    }
