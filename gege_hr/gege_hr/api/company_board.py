"""Employee-visible company schedule board + shift-swap requests.

Dashboard redesign (2026-09-11): every employee opens /hr/dashboard and sees a
read-only, company-wide window of *who works which shift* and *who is on
leave* — so they can plan their own days off, avoid thin staffing and propose
shift swaps with colleagues.

Surfaces
    board(from_date, to_date)      → the window (schedule + leaves + holidays)
    create_swap_request(...)       → employee proposes a two-day swap
    my_swap_requests()             → the viewer's swap requests
    pending_swap_requests()        → HR inbox
    decide_swap_request(...)       → HR approve (auto-swaps) / reject

Transparency is a product decision: schedule + leave *dates* are visible to
all employees; salary, reason-for-leave and other private fields are NOT
included in any projection below.
"""

from __future__ import annotations

import datetime as _dt

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api.admin import _audit_admin, _default_company, _require_hr_admin

_HR_ROLES = {"HR Manager", "HR User", "System Manager"}
BOARD_MAX_DAYS = 62
_SWAP_REASON_MIN = 5

_SWAP_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "from_date",
    "from_shift_type",
    "target_employee",
    "target_employee_name",
    "target_date",
    "target_shift_type",
    "reason",
    "status",
    "decided_by",
    "decided_on",
    "decision_note",
    "creation",
]


# --------------------------------------------------------------------------- #
# Viewer helpers
# --------------------------------------------------------------------------- #
def _viewer() -> dict:
    """Session user → {employee, is_hr}; HR may browse without an Employee."""
    user = frappe.session.user
    if user in ("Guest",):
        frappe.throw(_("Vui lòng đăng nhập."), frappe.PermissionError)
    is_hr = bool(set(frappe.get_roles(user) or []) & _HR_ROLES)
    emp = frappe.db.get_value(
        "Employee", {"user_id": user, "status": "Active"}, ["name", "employee_name"], as_dict=True
    )
    if not emp and not is_hr:
        frappe.throw(
            _("Tài khoản không liên kết nhân viên — không xem được bảng lịch."),
            frappe.PermissionError,
        )
    return {
        "employee": emp.name if emp else None,
        "employee_name": emp.employee_name if emp else "",
        "is_hr": is_hr,
    }


def _viewer_employee() -> str:
    v = _viewer()
    if not v["employee"]:
        frappe.throw(_("Tài khoản không liên kết nhân viên."), frappe.PermissionError)
    return v["employee"]


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def board(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Company-wide read-only schedule window for every employee.

    Returns ``{from_date, to_date, employees, holidays, leave_counts,
    working_counts, viewer}`` where each employee row carries ``days:
    {iso_date: {shift, shift_name, instance} | {leave, leave_status} | None}``.
    """
    _viewer()
    today = getdate()
    start = getdate(from_date) if from_date else today
    end = getdate(to_date) if to_date else today + _dt.timedelta(days=6)
    if end < start:
        start, end = end, start
    if (end - start).days > BOARD_MAX_DAYS:
        frappe.throw(_("Khoảng thời gian tối đa {0} ngày.").format(BOARD_MAX_DAYS))
    start_iso, end_iso = start.isoformat(), end.isoformat()

    emps = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        fields=["name", "employee_name", "department", "holiday_list"],
        order_by="employee_name",
        limit_page_length=0,
    )
    instances = frappe.get_all(
        "VN Employee Shift Instance",
        filters={
            "work_date": ["between", [start_iso, end_iso]],
            "status": ["not in", ["Cancelled", "Skipped"]],
        },
        fields=["name", "employee", "work_date", "shift_type", "shift_name"],
        limit_page_length=0,
    )
    leaves = frappe.get_all(
        "Leave Application",
        filters={
            "from_date": ["<=", end_iso],
            "to_date": [">=", start_iso],
            "status": ["in", ["Open", "Approved"]],
        },
        fields=["employee", "from_date", "to_date", "leave_type", "status"],
        limit_page_length=0,
    )

    # Holidays: union of the distinct holiday lists assigned to employees.
    hlists = {e.holiday_list for e in emps if e.holiday_list}
    holidays: dict[str, str] = {}
    if hlists:
        for h in frappe.get_all(
            "Holiday",
            filters={"parent": ["in", list(hlists)], "holiday_date": ["between", [start_iso, end_iso]]},
            fields=["holiday_date", "description"],
            order_by="idx",
            limit_page_length=0,
        ):
            holidays.setdefault(str(h.holiday_date), h.description or "Nghỉ lễ")

    days = {e.name: {} for e in emps}
    for inst in instances:
        if inst.employee in days:
            days[inst.employee][str(inst.work_date)] = {
                "shift": inst.shift_type,
                "shift_name": inst.shift_name or inst.shift_type,
                "instance": inst.name,
            }
    leave_counts: dict[str, list[str]] = {}
    for la in leaves:
        if la.employee not in days:
            continue
        ld = getdate(la.from_date)
        while ld <= end:
            iso = ld.isoformat()
            if iso >= start_iso:
                cell = days[la.employee].get(iso)
                # A leave day wins the cell unless the employee is scheduled
                # that day (rare overlap — keep the shift visible).
                if not cell:
                    days[la.employee][iso] = {
                        "leave": la.leave_type,
                        "leave_status": la.status,
                    }
                    leave_counts.setdefault(iso, [])
                    if la.employee not in leave_counts[iso]:
                        leave_counts[iso].append(la.employee)
            ld += _dt.timedelta(days=1)

    working_counts = {}
    d = start
    while d <= end:
        iso = d.isoformat()
        working_counts[iso] = sum(
            1 for emp in days.values() if isinstance(emp.get(iso), dict) and emp[iso].get("shift")
        )
        d += _dt.timedelta(days=1)

    return {
        "from_date": start_iso,
        "to_date": end_iso,
        "employees": [
            {
                "employee": e.name,
                "employee_name": e.employee_name,
                "department": e.department,
                "days": days[e.name],
            }
            for e in emps
        ],
        "holidays": holidays,
        "leave_counts": {k: len(v) for k, v in leave_counts.items()},
        "working_counts": working_counts,
        "viewer": _viewer(),
    }


# --------------------------------------------------------------------------- #
# Shift swap
# --------------------------------------------------------------------------- #
def _instance_brief(name: str) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    row = frappe.db.get_value(
        "VN Employee Shift Instance",
        name,
        ["employee", "work_date", "shift_type"],
        as_dict=True,
    )
    if not row or not getattr(row, "employee", None):
        return None
    return {
        "employee": row.employee,
        "work_date": str(row.work_date)[:10],
        "shift_type": row.shift_type,
    }


@frappe.whitelist()
def create_swap_request(
    from_instance: str | None = None, target_instance: str | None = None, reason: str | None = None
) -> dict:
    """Employee proposes swapping their shift day with a colleague's.

    ``from_instance`` must belong to the caller; both days must be in the
    future. The colleague is NOT notified v1 — HR sees the request in the
    pending inbox (and the requester can coordinate in person/Zalo).
    """
    emp = _viewer_employee()
    reason = (reason or "").strip()
    if len(reason) < _SWAP_REASON_MIN:
        frappe.throw(_("Lý do đổi ca cần ít nhất {0} ký tự.").format(_SWAP_REASON_MIN))

    a = _instance_brief(from_instance)
    b = _instance_brief(target_instance)
    if not a or not b:
        frappe.throw(_("Không tìm thấy phiên ca cần hoán đổi."))
    if a["employee"] != emp:
        frappe.throw(_("Chỉ được đề xuất đổi ca của chính mình."), frappe.PermissionError)
    if a["employee"] == b["employee"]:
        frappe.throw(_("Không thể đổi ca với chính mình."))
    today = getdate()
    if getdate(a["work_date"]) < today or getdate(b["work_date"]) < today:
        frappe.throw(_("Không thể đổi ca của ngày đã qua."))

    doc = frappe.get_doc(
        {
            "doctype": "VN Shift Swap Request",
            "employee": a["employee"],
            "employee_name": frappe.db.get_value("Employee", a["employee"], "employee_name"),
            "company": _default_company(),
            "from_date": a["work_date"],
            "from_shift_type": a["shift_type"],
            "from_instance": from_instance,
            "target_employee": b["employee"],
            "target_employee_name": frappe.db.get_value("Employee", b["employee"], "employee_name"),
            "target_date": b["work_date"],
            "target_shift_type": b["shift_type"],
            "target_instance": target_instance,
            "reason": reason,
        }
    )
    doc.insert(ignore_permissions=True)
    _audit_admin(
        _("Đề xuất đổi ca {0} ↔ {1}").format(a["work_date"], b["work_date"]),
        reference_doctype="VN Shift Swap Request",
        reference_name=doc.name,
        company=_default_company(),
        employee=a["employee"],
    )
    frappe.db.commit()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def my_swap_requests() -> list[dict]:
    """The viewer's swap requests (newest first)."""
    emp = _viewer_employee()
    return frappe.get_all(
        "VN Shift Swap Request",
        filters={"employee": emp},
        fields=_SWAP_LIST_FIELDS,
        order_by="creation desc",
        limit_page_length=50,
    )


@frappe.whitelist()
def pending_swap_requests() -> list[dict]:
    """HR inbox — all Open swap requests."""
    _require_hr_admin()
    return frappe.get_all(
        "VN Shift Swap Request",
        filters={"status": "Open"},
        fields=_SWAP_LIST_FIELDS,
        order_by="creation asc",
        limit_page_length=100,
    )


@frappe.whitelist()
def decide_swap_request(
    name: str | None = None, decision: str | None = None, note: str | None = None
) -> dict:
    """HR approves (auto-swaps the two days) or rejects a swap request."""
    _require_hr_admin()
    name = (name or "").strip()
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        frappe.throw(_("Hành động phải là approve hoặc reject."))
    doc = frappe.get_doc("VN Shift Swap Request", name) if name else None
    if not doc:
        frappe.throw(_("Yêu cầu đổi ca không tồn tại."))
    if doc.status != "Open":
        frappe.throw(_("Yêu cầu đã được xử lý trước đó."))

    if decision == "approve":
        # Re-validate the days are still in the future (request may have aged).
        if getdate(doc.from_date) < getdate() or getdate(doc.target_date) < getdate():
            frappe.throw(_("Không thể duyệt đổi ca của ngày đã qua."))
        from gege_hr.gege_hr.api.admin import swap_shift_days

        swap_shift_days(doc.from_instance, doc.target_instance)
        doc.status = "Approved"
    else:
        doc.status = "Rejected"
    doc.decided_by = frappe.session.user
    doc.decided_on = frappe.utils.now_datetime()
    doc.decision_note = (note or "").strip()
    doc.save(ignore_permissions=True)
    _audit_admin(
        _("Duyệt đổi ca {0} → {1}").format(name, doc.status),
        reference_doctype="VN Shift Swap Request",
        reference_name=name,
        company=_default_company(),
        employee=doc.employee,
    )
    frappe.db.commit()
    return {"name": doc.name, "status": doc.status}
