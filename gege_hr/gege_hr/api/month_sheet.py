"""Bảng công tháng của 1 nhân viên — ``/hr/team/attendance/:employee``
(plans/plan-employee-month-sheet.md).

Read endpoint only: loads every source for one employee × one month in a few
batched queries, then hands plain dicts to the pure helpers in
``utils/month_sheet.py`` (day classification + monthly totals). Edits keep
going through the existing endpoints (TeamDayDrawer / CheckinEditModal), which
carry their own role + lock gates.
"""

from __future__ import annotations

import calendar
from datetime import date

import frappe
from frappe import _

from gege_hr.gege_hr.api import attendance as att_api
from gege_hr.gege_hr.utils import employee as emp_utils, month_sheet as ms, tz as tz_utils

WS_DOCTYPE = "VN Attendance Work Session"
EDIT_ROLES = {"HR Manager", "System Manager"}  # owner D1: HR User / Line Manager = read-only
PENDING_WORKFLOW_STATES = ["Pending Manager", "Pending HR"]
EXPLANATION_KIND = {"Late Check-in": "late", "Early Check-out": "early"}

_WS_FIELDS = [
    "name",
    "work_date",
    "shift_type",
    "planned_start",
    "planned_end",
    "actual_checkin",
    "actual_checkout",
    "late_minutes",
    "early_leave_minutes",
    "regular_hours",
    "total_actual_hours",
    "raw_overtime_hours",
    "approved_overtime_hours",
    "overtime_night_hours",
    "missing_checkout",
    "vn_auto_checkout",
    "need_review",
    "calculation_status",
]


@frappe.whitelist()
def employee_month_sheet(
    employee: str | None = None, year: int | None = None, month: int | None = None
) -> dict:
    """Monthly attendance sheet of ONE employee (days + totals).

    Gate: HR Manager / HR User / System Manager read anyone; a Line Manager only
    their ``reports_to`` members. ``can_edit`` is true for HR Manager / System
    Manager only (owner D1) — the FE hides every edit affordance otherwise.
    """
    manager_emp, is_hr = att_api._team_viewer()
    if not employee:
        frappe.throw(_("Thiếu nhân viên cần tra cứu."), frappe.ValidationError)
    emp = emp_utils.emp_name(employee)
    if not att_api._can_view_member(manager_emp, is_hr, emp):
        frappe.throw(_("Bạn chỉ được xem nhân viên trong team của mình."), frappe.PermissionError)

    today = tz_utils.now_in_portal().date()
    y, m = _month_of(year, month, today)
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])

    info = frappe.db.get_value(
        "Employee",
        emp,
        [
            "name",
            "employee_name",
            "department",
            "designation",
            "company",
            "status",
            "date_of_joining",
            "relieving_date",
        ],
        as_dict=True,
    )
    if not info:
        frappe.throw(_("Không tìm thấy nhân viên {0}.").format(emp), frappe.DoesNotExistError)

    sessions = _group(_get_all(WS_DOCTYPE, _range(emp, "work_date", start, end, docstatus=True), _WS_FIELDS))
    leaves = ms.leaves_by_date(_approved_leaves(emp, start, end), _lwp_types(), start, end)
    misses = {
        str(r.get("work_date"))[:10]: r
        for r in _get_all(
            "VN Checkout Miss", _range(emp, "work_date", start, end), ["name", "work_date", "status"]
        )
    }
    pending = _pending_by_date(emp, start, end)
    explanations = _explanations_by_date(emp, start, end)
    edited = _edited_by_date(emp, start, end)
    locked_dates, period = att_api._locked_days_between(start, end)

    days = []
    for d in ms.iter_days(start, end):
        iso = d.isoformat()
        days.append(
            ms.build_day(
                d,
                today=today,
                date_of_joining=ms.as_date(info.get("date_of_joining")),
                relieving_date=ms.as_date(info.get("relieving_date")),
                sessions=sessions.get(iso),
                leave=leaves.get(iso),
                checkout_miss=misses.get(iso),
                pending=pending.get(iso),
                explanations=explanations.get(iso),
                edited=edited.get(iso, 0),
                locked=iso in locked_dates,
            )
        )

    roles = set(emp_utils.get_user_roles() or [])
    return {
        "employee": {
            "name": info.get("name"),
            "employee_name": info.get("employee_name"),
            "department": info.get("department"),
            "designation": info.get("designation"),
            "status": info.get("status"),
            "date_of_joining": _iso(info.get("date_of_joining")),
            "relieving_date": _iso(info.get("relieving_date")),
        },
        "year": y,
        "month": m,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "today": today.isoformat(),
        "locked_dates": sorted(locked_dates),
        "period": period,
        "can_edit": bool(roles & EDIT_ROLES),
        "days": days,
        "totals": ms.aggregate_month(days),
    }


# --------------------------------------------------------------------------- #
# loaders (batched — one query per source)
# --------------------------------------------------------------------------- #
def _approved_leaves(emp: str, start: date, end: date) -> list[dict]:
    return _get_all(
        "Leave Application",
        {
            "employee": emp,
            "docstatus": 1,
            "status": "Approved",
            "from_date": ["<=", end],
            "to_date": [">=", start],
        },
        ["name", "leave_type", "from_date", "to_date", "half_day", "half_day_date"],
    )


def _lwp_types() -> set[str]:
    return {r.get("name") for r in _get_all("Leave Type", {"is_lwp": 1}, ["name"])}


def _pending_by_date(emp: str, start: date, end: date) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}

    def add(iso: str, doctype: str, name: str, label: str) -> None:
        out.setdefault(iso, []).append({"doctype": doctype, "name": name, "label": label})

    for la in _get_all(
        "Leave Application",
        {
            "employee": emp,
            "docstatus": 0,
            "status": "Open",
            "from_date": ["<=", end],
            "to_date": [">=", start],
        },
        ["name", "from_date", "to_date", "leave_type"],
    ):
        f, t = ms.as_date(la.get("from_date")), ms.as_date(la.get("to_date"))
        if f and t:
            for d in ms.iter_days(max(f, start), min(t, end)):
                add(d.isoformat(), "Leave Application", la.get("name"), _("Đơn nghỉ chờ duyệt"))
    for doctype, label in (
        ("VN Overtime Request", _("OT chờ duyệt")),
        ("VN Attendance Correction Request", _("Sửa công chờ duyệt")),
    ):
        filters = _range(emp, "work_date", start, end)
        filters["workflow_state"] = ["in", PENDING_WORKFLOW_STATES]
        for r in _get_all(doctype, filters, ["name", "work_date"]):
            add(str(r.get("work_date"))[:10], doctype, r.get("name"), label)
    filters = _range(emp, "work_date", start, end)
    filters["status"] = "Open"
    for r in _get_all("VN Attendance Explanation", filters, ["name", "work_date"]):
        add(
            str(r.get("work_date"))[:10],
            "VN Attendance Explanation",
            r.get("name"),
            _("Giải trình chờ duyệt"),
        )
    return out


def _explanations_by_date(emp: str, start: date, end: date) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in _get_all(
        "VN Attendance Explanation",
        _range(emp, "work_date", start, end),
        ["work_date", "explanation_type", "status"],
    ):
        kind = EXPLANATION_KIND.get(r.get("explanation_type") or "")
        if not kind:
            continue
        slot = out.setdefault(str(r.get("work_date"))[:10], {})
        # Approved wins over Open / Rejected for the same day.
        if slot.get(kind) != "Approved":
            slot[kind] = r.get("status")
    return out


def _edited_by_date(emp: str, start: date, end: date) -> dict[str, int]:
    """Manual edits per day (VN Audit Event ``Manual Override`` rows)."""
    out: dict[str, int] = {}
    filters = _range(emp, "work_date", start, end)
    filters["audit_type"] = "Manual Override"
    for r in _get_all("VN Audit Event", filters, ["work_date"]):
        iso = str(r.get("work_date"))[:10]
        out[iso] = out.get(iso, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _month_of(year, month, today: date) -> tuple[int, int]:
    try:
        y = int(year) if year else today.year
        m = int(month) if month else today.month
    except (TypeError, ValueError):
        frappe.throw(_("Tháng / năm không hợp lệ."), frappe.ValidationError)
    if not 1 <= m <= 12 or not 2000 <= y <= 2100:
        frappe.throw(_("Tháng / năm không hợp lệ."), frappe.ValidationError)
    return y, m


def _range(emp: str, field: str, start: date, end: date, docstatus: bool = False) -> dict:
    filters = {"employee": emp, field: ["between", [start, end]]}
    if docstatus:
        filters["docstatus"] = ["!=", 2]
    return filters


def _get_all(doctype: str, filters: dict, fields: list[str]) -> list[dict]:
    """Best-effort read — a missing doctype / column must not blank the sheet."""
    try:
        return frappe.db.get_all(doctype, filters=filters, fields=fields, limit_page_length=0)
    except Exception:
        frappe.log_error(title=f"month_sheet: load {doctype} failed")
        return []


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: str(r.get("planned_start") or "")):
        out.setdefault(str(r.get("work_date"))[:10], []).append(r)
    return out


def _iso(v) -> str | None:
    d = ms.as_date(v)
    return d.isoformat() if d else None
