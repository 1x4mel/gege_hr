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
from gege_hr.gege_hr.utils import pagination
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
def _filter_rows(rows: list[dict], search: str | None, fields: tuple[str, ...]) -> list[dict]:
    """Server-side free-text filter across the given row fields (DNA §6.6 D).

    Applied after the rows are fetched so it never risks an ``or_filters``
    "column does not exist" error on a per-DocType list.
    """
    q = (search or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows if any(q in str(r.get(k) or "").lower() for k in fields)]


def _to_float(value) -> float:
    """Best-effort numeric coercion (whitelist args + DB rows may be ``None``)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ot_summary(rows: list[dict], ws_approved: float | None = None) -> dict:
    """Aggregate the OT list over the *full* filtered set (DNA §6.6 A).

    Computed once on the filtered (pre-pagination) list so the SPA summary tiles
    stay correct on every page. Mirrors the employee OT tile semantics:

      * ``requested`` — Σ ``requested_hours``
      * ``approved``  — OT thực sự đã được tính (Work Session total when
        available; falls back to Σ ``approved_hours`` on the request rows).
        The per-row ``approved_hours`` is only stamped *after* a Work Session
        recalc, so it can lag — the Work Session figure is the source of truth
        (BUG-2 fix, plan T2).
      * ``pending``   — count of still-cancellable requests (``Draft`` /
        ``Pending Manager`` / ``Pending HR`` / ``Open`` — matches
        ``hrStatus.otIsCancellable``).
    """
    requested = 0.0
    approved_rowsum = 0.0
    pending = 0
    for r in rows or []:
        requested += _to_float(r.get("requested_hours"))
        approved_rowsum += _to_float(r.get("approved_hours"))
        state = str(r.get("workflow_state") or "").lower()
        if state in ("draft", "open") or "pending" in state:
            pending += 1
    approved = ws_approved if ws_approved is not None else approved_rowsum
    return {"requested": requested, "approved": approved, "pending": pending}


def _ws_approved_hours(employee: str, from_date, to_date) -> float:
    """Σ ``approved_overtime_hours`` across the employee's Work Sessions in the
    window — the authoritative "OT đã được tính" figure for the summary tile
    (plan T2 / BUG-2). Bench-safe: returns ``0`` when the doctype/table is absent
    or the read fails.
    """
    try:
        if not frappe.db.table_exists("VN Attendance Work Session"):  # type: ignore[attr-defined]
            return 0.0
    except Exception:
        return 0.0
    filters = {"employee": employee}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    try:
        rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters=filters,
            fields=["approved_overtime_hours"],
        )
    except Exception:
        return 0.0
    return _to_float(sum(_to_float(r.get("approved_overtime_hours")) for r in rows))


@frappe.whitelist()
def my_overtime_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    status: str | None = None,
    overtime_type: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.4 — the caller's OT requests, optionally narrowed by filters.

    Server-side filters (DNA §6.6 — gear popover Law #2):

      * ``from_date`` / ``to_date`` — ``work_date`` window
      * ``status``          — exact ``workflow_state`` (Draft / Pending Manager /
        Pending HR / Approved / Confirmed / Rejected)
      * ``overtime_type``   — exact ``overtime_type`` (Pre-shift / Post-shift /
        Holiday / Custom)

    ``search`` OR-matches a free-text query across the row's text **and numeric**
    fields (name / overtime_type / reason / work_date / employee /
    employee_name / requested_hours / approved_hours), applied server-side
    (DNA §6.6 A/D — broad search covers numeric columns too, HR-BL-08). Managers
    (HR Manager/System Manager) may pass any ``employee``; a plain Employee is
    scoped to own.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive
    ``{"data": [...], "total": int, "summary": {requested, approved, pending}}``
    — ``total`` is the filtered list length and ``summary`` aggregates the full
    filtered set (not just the page); without ``page_size`` the legacy bare-list
    return is preserved.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    if status:
        filters["workflow_state"] = status
    if overtime_type:
        filters["overtime_type"] = overtime_type

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="work_date desc, creation desc",
    )
    filtered = _filter_rows(
        rows,
        search,
        (
            "name",
            "overtime_type",
            "reason",
            "work_date",
            "employee",
            "employee_name",
            "requested_hours",
            "approved_hours",
        ),
    )
    summary = _ot_summary(
        filtered, ws_approved=_ws_approved_hours(emp, from_date, to_date)
    )
    return pagination.paginate_filtered(
        filtered, page=page, page_size=page_size, summary=summary
    )


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
    from gege_hr.gege_hr.api import approval as approval_api

    prev_state = doc.workflow_state
    doc.workflow_state = "Rejected"
    if doc.docstatus == 1:
        doc.cancel()
    else:
        doc.save()
    # Keep the Work Session in sync if this request was previously engine-active
    # (Approved/Confirmed). For Draft/Pending cancels this is a harmless no-op
    # (BUG-1 fix, plan T1).
    approval_api._after_ot_state_change(doc, from_state=prev_state, to_state="Rejected")
    return {
        "name": name,
        "status": doc.workflow_state,
        "message": _("Đã hủy yêu cầu tăng ca {0}.").format(name),
    }
