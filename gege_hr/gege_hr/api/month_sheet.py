"""Bảng công tháng — 1 nhân viên (``/hr/team/attendance/:employee``) và cả team
(``/hr/team/attendance/review``) (plans/plan-employee-month-sheet.md).

Read endpoints only: load every source for N employees × one month in one
batched query per source, then hand plain dicts to the pure helpers in
``utils/month_sheet.py`` (day classification + monthly totals + legacy review
sections). Edits keep going through the existing endpoints (TeamDayDrawer /
CheckinEditModal), which carry their own role + lock gates.
"""

from __future__ import annotations

import base64
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
EMPLOYEE_FIELDS = [
    "name",
    "employee_name",
    "department",
    "designation",
    "company",
    "status",
    "date_of_joining",
    "relieving_date",
    "reports_to",
]

_WS_FIELDS = [
    "name",
    "employee",
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
    start, end = _month_range(y, m)

    info = frappe.db.get_value("Employee", emp, EMPLOYEE_FIELDS, as_dict=True)
    if not info:
        frappe.throw(_("Không tìm thấy nhân viên {0}.").format(emp), frappe.DoesNotExistError)

    src = _load_sources([emp], start, end)
    locked_dates, period = att_api._locked_days_between(start, end)
    days = _build_days(info, src, start, end, today, locked_dates)

    roles = set(emp_utils.get_user_roles() or [])
    return {
        "employee": _employee_view(info),
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


@frappe.whitelist()
def team_month_review(
    year: int | None = None, month: int | None = None, department: str | None = None
) -> dict:
    """Monthly review of the whole roster (HR) / own team (Line Manager):
    one row per employee with totals, a compact day strip and the legacy sheet
    sections (``items``) + reasons — for viewing and manual payroll."""
    review = _team_review(year, month, department)
    review.pop("_infos", None)
    return review


@frappe.whitelist()
def team_month_review_xlsx(
    year: int | None = None, month: int | None = None, department: str | None = None
) -> dict:
    """The legacy-format workbook (employees as column pairs) as base64."""
    from gege_hr.gege_hr.utils import month_review_xlsx as mrx

    review = _team_review(year, month, department)
    layout = mrx.legacy_layout(review["employees"])
    tag = f"{review['month']:02d}-{review['year']}"
    content = mrx.render_xlsx(layout, sheet_title=f"Thang {tag}")
    return {
        "filename": f"danh-gia-cong-thang-{tag}.xlsx",
        "content": base64.b64encode(content).decode(),
    }


def _team_review(year, month, department) -> dict:
    manager_emp, is_hr = att_api._team_viewer()
    today = tz_utils.now_in_portal().date()
    y, m = _month_of(year, month, today)
    start, end = _month_range(y, m)

    infos = _roster(manager_emp, is_hr, start, end, department)
    names = [i["name"] for i in infos]
    src = _load_sources(names, start, end)
    locked_dates, period = att_api._locked_days_between(start, end)

    rows = []
    for info in infos:
        days = _build_days(info, src, start, end, today, locked_dates)
        totals = ms.aggregate_month(days)
        rows.append(
            {
                **_employee_view(info),
                "shift_type": _main_shift(src["sessions"].get(info["name"], {})),
                "totals": totals,
                "items": ms.review_items(days),
                "days": [_compact_day(d) for d in days],
            }
        )
    rows.sort(key=lambda r: (str(r.get("department") or "~"), str(r.get("employee_name") or "")))
    return {
        "year": y,
        "month": m,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "today": today.isoformat(),
        "locked_dates": sorted(locked_dates),
        "period": period,
        "scope": "company" if is_hr else "team",
        "employees": rows,
        "summary": _team_summary(rows),
    }


# --------------------------------------------------------------------------- #
# roster + loaders (batched — one query per source for all employees)
# --------------------------------------------------------------------------- #
def _roster(manager_emp, is_hr: bool, start: date, end: date, department) -> list[dict]:
    """Employees to review: HR → everyone, Line Manager → ``reports_to`` me.
    Only people employed in the month AND on the attendance roster (a Shift
    Assignment overlapping the month or a Work Session in it) — so staff who
    never punch (directors, admins) do not show 31 absent days."""
    filters: dict = {}
    if not is_hr:
        if not manager_emp:
            return []
        filters["reports_to"] = manager_emp
    if department:
        filters["department"] = department
    infos = []
    for e in _get_all("Employee", filters, EMPLOYEE_FIELDS):
        doj, rel = ms.as_date(e.get("date_of_joining")), ms.as_date(e.get("relieving_date"))
        if doj and doj > end:
            continue
        if e.get("status") != "Active" and not (rel and rel >= start):
            continue
        infos.append(e)
    if not infos:
        return []
    names = [e["name"] for e in infos]
    on_roster = {
        r.get("employee")
        for r in _get_all(
            "Shift Assignment",
            {"employee": ["in", names], "docstatus": 1, "start_date": ["<=", end]},
            ["employee", "end_date"],
        )
        if not r.get("end_date") or ms.as_date(r.get("end_date")) >= start
    }
    on_roster |= {
        r.get("employee")
        for r in _get_all(
            WS_DOCTYPE,
            {"employee": ["in", names], "work_date": ["between", [start, end]], "docstatus": ["!=", 2]},
            ["employee"],
        )
    }
    return [e for e in infos if e["name"] in on_roster]


def _load_sources(emps: list[str], start: date, end: date) -> dict:
    """Every per-day source for ``emps`` → ``{source: {emp: {iso: value}}}``."""
    if not emps:
        return {k: {} for k in ("sessions", "leaves", "misses", "pending", "explanations", "edited")}
    sessions: dict = {}
    for r in sorted(
        _get_all(WS_DOCTYPE, _range(emps, "work_date", start, end, docstatus=True), _WS_FIELDS),
        key=lambda r: str(r.get("planned_start") or ""),
    ):
        sessions.setdefault(r.get("employee"), {}).setdefault(_d(r.get("work_date")), []).append(r)

    lwp = _lwp_types()
    by_emp: dict = {}
    for la in _get_all(
        "Leave Application",
        {
            "employee": ["in", emps],
            "docstatus": 1,
            "status": "Approved",
            "from_date": ["<=", end],
            "to_date": [">=", start],
        },
        [
            "name",
            "employee",
            "leave_type",
            "from_date",
            "to_date",
            "half_day",
            "half_day_date",
            "description",
        ],
    ):
        by_emp.setdefault(la.get("employee"), []).append(la)
    leaves = {e: ms.leaves_by_date(rows, lwp, start, end) for e, rows in by_emp.items()}

    misses: dict = {}
    for r in _get_all(
        "VN Checkout Miss",
        _range(emps, "work_date", start, end),
        ["name", "employee", "work_date", "status", "explanation"],
    ):
        misses.setdefault(r.get("employee"), {})[_d(r.get("work_date"))] = r

    return {
        "sessions": sessions,
        "leaves": leaves,
        "misses": misses,
        "pending": _pending_by_date(emps, start, end),
        "explanations": _explanations_by_date(emps, start, end),
        "edited": _edited_by_date(emps, start, end),
    }


def _build_days(info: dict, src: dict, start: date, end: date, today: date, locked_dates) -> list[dict]:
    emp = info["name"]
    sessions = src["sessions"].get(emp, {})
    leaves = src["leaves"].get(emp, {})
    misses = src["misses"].get(emp, {})
    pending = src["pending"].get(emp, {})
    expl = src["explanations"].get(emp, {})
    edited = src["edited"].get(emp, {})
    days = []
    for d in ms.iter_days(start, end):
        iso = d.isoformat()
        ex = expl.get(iso) or {}
        miss = misses.get(iso)
        days.append(
            ms.build_day(
                d,
                today=today,
                date_of_joining=ms.as_date(info.get("date_of_joining")),
                relieving_date=ms.as_date(info.get("relieving_date")),
                sessions=sessions.get(iso),
                leave=leaves.get(iso),
                checkout_miss=miss,
                pending=pending.get(iso),
                explanations={k: v for k, v in ex.items() if k in ("late", "early")},
                reasons={
                    "late": ex.get("late_reason", ""),
                    "early": ex.get("early_reason", ""),
                    "checkout_miss": (miss or {}).get("explanation") or "",
                },
                edited=edited.get(iso, 0),
                locked=iso in locked_dates,
            )
        )
    return days


def _lwp_types() -> set[str]:
    return {r.get("name") for r in _get_all("Leave Type", {"is_lwp": 1}, ["name"])}


def _pending_by_date(emps: list[str], start: date, end: date) -> dict:
    out: dict = {}

    def add(emp: str, iso: str, doctype: str, name: str, label: str) -> None:
        out.setdefault(emp, {}).setdefault(iso, []).append({"doctype": doctype, "name": name, "label": label})

    for la in _get_all(
        "Leave Application",
        {
            "employee": ["in", emps],
            "docstatus": 0,
            "status": "Open",
            "from_date": ["<=", end],
            "to_date": [">=", start],
        },
        ["name", "employee", "from_date", "to_date"],
    ):
        f, t = ms.as_date(la.get("from_date")), ms.as_date(la.get("to_date"))
        if f and t:
            for d in ms.iter_days(max(f, start), min(t, end)):
                add(
                    la.get("employee"),
                    d.isoformat(),
                    "Leave Application",
                    la.get("name"),
                    _("Đơn nghỉ chờ duyệt"),
                )
    for doctype, label in (
        ("VN Overtime Request", _("OT chờ duyệt")),
        ("VN Attendance Correction Request", _("Sửa công chờ duyệt")),
    ):
        filters = _range(emps, "work_date", start, end)
        filters["workflow_state"] = ["in", PENDING_WORKFLOW_STATES]
        for r in _get_all(doctype, filters, ["name", "employee", "work_date"]):
            add(r.get("employee"), _d(r.get("work_date")), doctype, r.get("name"), label)
    filters = _range(emps, "work_date", start, end)
    filters["status"] = "Open"
    for r in _get_all("VN Attendance Explanation", filters, ["name", "employee", "work_date"]):
        add(
            r.get("employee"),
            _d(r.get("work_date")),
            "VN Attendance Explanation",
            r.get("name"),
            _("Giải trình chờ duyệt"),
        )
    return out


def _explanations_by_date(emps: list[str], start: date, end: date) -> dict:
    """``{emp: {iso: {"late": status, "late_reason": text, "early": …}}}``."""
    out: dict = {}
    for r in _get_all(
        "VN Attendance Explanation",
        _range(emps, "work_date", start, end),
        ["employee", "work_date", "explanation_type", "status", "reason"],
    ):
        kind = EXPLANATION_KIND.get(r.get("explanation_type") or "")
        if not kind:
            continue
        slot = out.setdefault(r.get("employee"), {}).setdefault(_d(r.get("work_date")), {})
        if r.get("status") == "Rejected" and slot.get(kind):
            continue
        # Approved wins over Open / Rejected for the same day.
        if slot.get(kind) != "Approved":
            slot[kind] = r.get("status")
            slot[f"{kind}_reason"] = (r.get("reason") or "").strip()
    return out


def _edited_by_date(emps: list[str], start: date, end: date) -> dict:
    """Manual edits per employee/day (VN Audit Event ``Manual Override`` rows)."""
    out: dict = {}
    filters = _range(emps, "work_date", start, end)
    filters["audit_type"] = "Manual Override"
    for r in _get_all("VN Audit Event", filters, ["employee", "work_date"]):
        slot = out.setdefault(r.get("employee"), {})
        iso = _d(r.get("work_date"))
        slot[iso] = slot.get(iso, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_COMPACT_KEYS = (
    "date",
    "weekday",
    "status",
    "counted",
    "leave",
    "worked",
    "absent",
    "checkin",
    "checkout",
    "late_minutes",
    "early_minutes",
    "late_bucket",
    "early_bucket",
    "ot_approved_hours",
    "ot_pending_hours",
    "checkout_miss",
    "checkin_miss",
    "edited",
    "reasons",
)


def _compact_day(d: dict) -> dict:
    out = {k: d.get(k) for k in _COMPACT_KEYS}
    out["pending"] = len(d.get("pending") or [])
    return out


def _team_summary(rows: list[dict]) -> dict:
    s = {
        "employees": len(rows),
        "with_issues": 0,
        "late_days": 0,
        "late_minutes": 0,
        "early_days": 0,
        "early_minutes": 0,
        "checkout_miss": 0,
        "checkin_miss": 0,
        "absent_days": 0.0,
        "leave_unpaid_days": 0.0,
        "pending": 0,
    }
    for r in rows:
        t = r["totals"]
        s["late_days"] += t["late"]["days"]
        s["late_minutes"] += t["late"]["minutes"]
        s["early_days"] += t["early"]["days"]
        s["early_minutes"] += t["early"]["minutes"]
        s["checkout_miss"] += t["checkout_miss_count"]
        s["checkin_miss"] += t["checkin_miss_count"]
        s["absent_days"] += t["absent_days"]
        s["leave_unpaid_days"] += t["leave_unpaid_days"]
        s["pending"] += t["pending_count"]
        if (
            t["late"]["days"]
            or t["early"]["days"]
            or t["checkout_miss_count"]
            or t["checkin_miss_count"]
            or t["absent_days"]
        ):
            s["with_issues"] += 1
    return s


def _main_shift(sessions_by_day: dict) -> str:
    counts: dict = {}
    for rows in sessions_by_day.values():
        for r in rows:
            st = r.get("shift_type") or ""
            if st:
                counts[st] = counts.get(st, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _employee_view(info: dict) -> dict:
    return {
        "name": info.get("name"),
        "employee_name": info.get("employee_name"),
        "department": info.get("department"),
        "designation": info.get("designation"),
        "status": info.get("status"),
        "date_of_joining": _iso(info.get("date_of_joining")),
        "relieving_date": _iso(info.get("relieving_date")),
    }


def _month_of(year, month, today: date) -> tuple[int, int]:
    try:
        y = int(year) if year else today.year
        m = int(month) if month else today.month
    except (TypeError, ValueError):
        frappe.throw(_("Tháng / năm không hợp lệ."), frappe.ValidationError)
    if not 1 <= m <= 12 or not 2000 <= y <= 2100:
        frappe.throw(_("Tháng / năm không hợp lệ."), frappe.ValidationError)
    return y, m


def _month_range(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _range(emps: list[str], field: str, start: date, end: date, docstatus: bool = False) -> dict:
    filters = {"employee": ["in", list(emps)], field: ["between", [start, end]]}
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


def _d(v) -> str:
    return str(v)[:10]


def _iso(v) -> str | None:
    d = ms.as_date(v)
    return d.isoformat() if d else None
