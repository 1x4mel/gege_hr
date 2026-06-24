"""
Per-employee attendance / OT / leave report helpers — pure functions
(bench-free, testable).

Mirrors the pattern of :mod:`gege_hr.gege_hr.utils.attendance_period` /
:mod:`gege_hr.gege_hr.utils.payroll`: the pure core folds a flat list of VN
Attendance Work Session rows + Leave Application rows into a per-employee
period summary that powers the HR "Báo cáo" view
(:mod:`gege_hr.gege_hr.api.dashboard.get_attendance_report`). Bench loaders
are frappe-guarded so the pure core runs under pytest without a bench.

Report row shape (plan v5 §13 / §10.9 — HR "Báo cáo theo nhân viên")::

    {
        employee,
        employee_name,
        department,
        branch,
        company,
        present_days,
        absent_days,
        paid_leave_days,
        unpaid_leave_days,
        holiday_days,
        leave_days,
        regular_hours,
        overtime_hours,
        overtime_night_hours,
        late_count,
        late_minutes,
        early_leave_minutes,
        payable_days,
        need_review_count,
    }
"""

from __future__ import annotations

import calendar

try:  # pragma: no cover — bench import
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

from gege_hr.gege_hr.utils import attendance_period as ap


# --------------------------------------------------------------------------- #
# Numeric helpers (mirror attendance_period.py so behaviour is identical)
# --------------------------------------------------------------------------- #
def _num(v, default: float = 0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def round2(v) -> float:
    return round(_num(v), 2)


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Period window
# --------------------------------------------------------------------------- #
def month_window(period_month, period_year):
    """Return ``(from_date, to_date)`` date objects for a calendar month.

    ``period_month`` / ``period_year`` accept ints or zero-padded strings.
    Returns ``None`` for any invalid month/year so the caller can short-circuit
    (an empty report rather than a crash on a brand-new site).
    """
    month = _to_int(period_month, 0)
    year = _to_int(period_year, 0)
    if month < 1 or month > 12:
        return None
    if year < 1900 or year > 2999:
        return None
    last_day = calendar.monthrange(year, month)[1]
    from_date = _date(year, month, 1)
    to_date = _date(year, month, last_day)
    return (from_date, to_date)


def _date(year: int, month: int, day: int):
    """Build a ``date`` without importing ``datetime.date`` at module top —
    keeps the helper robust even if a test fiddles with ``datetime``."""
    from datetime import date

    return date(year, month, day)


# --------------------------------------------------------------------------- #
# Leave-row normalisation (Leave Application rows → leave-days ledger)
# --------------------------------------------------------------------------- #
def _leave_days_for(row: dict) -> float:
    """Leave days attributable to a Leave Application row.

    Uses ``total_leave_days`` when present (Frappe HR populates it), falling
    back to computing from ``from_date``/``to_date`` inclusive.
    """
    raw = row.get("total_leave_days")
    if raw not in (None, "", 0, 0.0):
        tld = _num(raw)
        if tld > 0:
            return tld
    f = row.get("from_date")
    t = row.get("to_date")
    if not f or not t:
        return 0.0
    try:
        # inclusive day count (mirror utils/leave.inclusive_day_count)
        return (_date_from_str(t) - _date_from_str(f)).days + 1
    except Exception:
        return 0.0


def _date_from_str(value):
    from datetime import date, datetime

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        # Accept ``YYYY-MM-DD`` (Frappe storage) and ``YYYY-MM-DD HH:MM:SS``.
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    raise ValueError(f"unparseable date: {value!r}")


def _is_lwp(row: dict) -> bool:
    """A Leave Application counts as *unpaid* when its Leave Type is LWP."""
    return bool(int(_num(row.get("is_lwp"), 0)))


# --------------------------------------------------------------------------- #
# Pure aggregation — Work Session + Leave rows → per-employee report rows
# --------------------------------------------------------------------------- #
def build_employee_report(
    work_sessions: list[dict],
    leave_applications: list[dict] | None = None,
) -> list[dict]:
    """Fold Work Session + Leave Application rows into per-employee report rows.

    The attendance / OT / late figures come from the Work Sessions (via
    :func:`attendance_period.aggregate_period`); leave days are augmented from
    the Leave Application ledger so a period where an employee was fully on
    leave (no Work Sessions at all) still appears in the report with their
    leave balance.

    Returns a list of report rows sorted by ``employee_name`` then
    ``employee`` (stable for the HR table + Excel export).
    """
    lines = ap.aggregate_period(work_sessions or [])
    ap.recalc_payable_days(lines)

    # Merge leave days from the Leave Application ledger. Each approved leave
    # row contributes its ``total_leave_days`` (or inclusive day span) toward
    # ``leave_days`` and the paid/unpaid split.
    for row in leave_applications or []:
        emp = row.get("employee")
        if not emp:
            continue
        line = lines.get(emp) or _blank_report_line(emp, row)
        lines[emp] = line
        line.setdefault("leave_days", 0.0)
        days = _leave_days_for(row)
        line["leave_days"] = round2(line["leave_days"] + days)
        if _is_lwp(row):
            line.setdefault("unpaid_leave_days", 0.0)
            line["unpaid_leave_days"] = round2(line["unpaid_leave_days"] + days)
        else:
            line.setdefault("paid_leave_days", 0.0)
            line["paid_leave_days"] = round2(line["paid_leave_days"] + days)

    rows = []
    for line in lines.values():
        rows.append(_report_row(line))

    rows.sort(key=lambda r: (str(r.get("employee_name") or ""), str(r.get("employee") or "")))
    return rows


def _blank_report_line(employee: str, row: dict) -> dict:
    return {
        "employee": employee,
        "employee_name": row.get("employee_name") or "",
        "department": row.get("department"),
        "branch": row.get("branch"),
        "company": row.get("company"),
        "working_days": 0,
        "present_days": 0.0,
        "absent_days": 0.0,
        "paid_leave_days": 0.0,
        "unpaid_leave_days": 0.0,
        "holiday_days": 0.0,
        "regular_hours": 0.0,
        "regular_night_hours": 0.0,
        "overtime_hours": 0.0,
        "overtime_night_hours": 0.0,
        "overtime_holiday_hours": 0.0,
        "late_count": 0,
        "late_minutes": 0,
        "early_leave_count": 0,
        "early_leave_minutes": 0,
        "payable_hours": 0.0,
        "payable_days": 0.0,
        "need_review_count": 0,
        "leave_days": 0.0,
    }


def _report_row(line: dict) -> dict:
    """Project an aggregate_period line into the report row shape (adds the
    ``leave_days`` + ``early_leave_minutes`` fields and drops internal keys)."""
    return {
        "employee": line.get("employee"),
        "employee_name": line.get("employee_name") or "",
        "department": line.get("department") or "",
        "branch": line.get("branch") or "",
        "company": line.get("company") or "",
        "present_days": round2(line.get("present_days")),
        "absent_days": round2(line.get("absent_days")),
        "paid_leave_days": round2(line.get("paid_leave_days")),
        "unpaid_leave_days": round2(line.get("unpaid_leave_days")),
        "holiday_days": round2(line.get("holiday_days")),
        "leave_days": round2(line.get("leave_days")),
        "regular_hours": round2(line.get("regular_hours")),
        "overtime_hours": round2(line.get("overtime_hours")),
        "overtime_night_hours": round2(line.get("overtime_night_hours")),
        "late_count": _to_int(line.get("late_count")),
        "late_minutes": _to_int(line.get("late_minutes")),
        "early_leave_minutes": _to_int(line.get("early_leave_minutes")),
        "payable_days": round2(line.get("payable_days")),
        "need_review_count": _to_int(line.get("need_review_count")),
    }


# --------------------------------------------------------------------------- #
# Pure rollup — report rows → period totals
# --------------------------------------------------------------------------- #
def report_totals(rows: list[dict]) -> dict:
    """Sum the per-employee report rows into period totals."""
    totals = {
        "total_employees": len(rows or []),
        "total_present_days": 0.0,
        "total_absent_days": 0.0,
        "total_leave_days": 0.0,
        "total_paid_leave_days": 0.0,
        "total_unpaid_leave_days": 0.0,
        "total_overtime_hours": 0.0,
        "total_late_minutes": 0,
        "total_need_review": 0,
        "total_payable_days": 0.0,
    }
    for row in rows or []:
        totals["total_present_days"] += _num(row.get("present_days"))
        totals["total_absent_days"] += _num(row.get("absent_days"))
        totals["total_leave_days"] += _num(row.get("leave_days"))
        totals["total_paid_leave_days"] += _num(row.get("paid_leave_days"))
        totals["total_unpaid_leave_days"] += _num(row.get("unpaid_leave_days"))
        totals["total_overtime_hours"] += _num(row.get("overtime_hours"))
        totals["total_late_minutes"] += _to_int(row.get("late_minutes"))
        totals["total_need_review"] += _to_int(row.get("need_review_count"))
        totals["total_payable_days"] += _num(row.get("payable_days"))
    totals["total_present_days"] = round2(totals["total_present_days"])
    totals["total_absent_days"] = round2(totals["total_absent_days"])
    totals["total_leave_days"] = round2(totals["total_leave_days"])
    totals["total_paid_leave_days"] = round2(totals["total_paid_leave_days"])
    totals["total_unpaid_leave_days"] = round2(totals["total_unpaid_leave_days"])
    totals["total_overtime_hours"] = round2(totals["total_overtime_hours"])
    totals["total_payable_days"] = round2(totals["total_payable_days"])
    return totals


# --------------------------------------------------------------------------- #
# Bench loaders — frappe-guarded, safe to import without a bench.
# --------------------------------------------------------------------------- #
def _existing_fields(doctype: str, candidates) -> list[str]:
    """Return the subset of ``candidates`` that exist on ``doctype``.

    The target DocType may have been created without every field we'd like to
    read (e.g. ``branch`` is not a stock ``Leave Application`` column). Querying
    a non-existent column raises ``OperationalError: Unknown column`` on the
    bench, so we ask the meta which fields are present and only SELECT those.

    Frappe-guarded: outside a bench (``frappe`` is None) or when the meta
    cannot be loaded, the full candidate list is returned unchanged so the
    bench-free tests keep exercising every field.
    """
    cand = list(candidates)
    if not frappe:
        return cand
    try:
        meta = frappe.get_meta(doctype)
    except Exception:  # pragma: no cover — defensive
        return cand
    if meta is None:
        return cand
    return [f for f in cand if f == "name" or meta.has_field(f)]


def load_work_sessions(company: str | None, from_date: str, to_date: str) -> list[dict]:
    """VN Attendance Work Session rows in the window, company-scoped."""
    if not frappe or not from_date or not to_date:
        return []
    filters = {
        "work_date": ["between", [from_date, to_date]],
        "docstatus": ["<", 2],
    }
    if company:
        filters["company"] = company
    return frappe.db.get_all(
        "VN Attendance Work Session",
        filters=filters,
        fields=_existing_fields("VN Attendance Work Session", list(ap._WS_FIELDS) + ["name"]),
        order_by="employee, work_date",
    )


def load_leave_applications(company: str | None, from_date: str, to_date: str) -> list[dict]:
    """Approved Leave Application rows overlapping the window.

    Leave rows are matched when ``from_date <= window.to_date`` and
    ``to_date >= window.from_date`` (a leave spanning into / out of the month
    still contributes the days that fall inside the window — the per-day
    detail already lives in the Work Sessions; here we only need the leave-type
    ledger for the leave-days tile). The Leave Type's ``is_lwp`` flag is
    attached so the paid/unpaid split is correct.
    """
    if not frappe or not from_date or not to_date:
        return []
    filters = [
        ["docstatus", "=", 1],
        ["status", "=", "Approved"],
        ["from_date", "<=", to_date],
        ["to_date", ">=", from_date],
    ]
    if company:
        filters.append(["company", "=", company])
    rows = frappe.db.get_all(
        "Leave Application",
        filters=filters,
        fields=_existing_fields(
            "Leave Application",
            [
                "name",
                "employee",
                "employee_name",
                "department",
                "branch",
                "company",
                "from_date",
                "to_date",
                "total_leave_days",
                "leave_type",
            ],
        ),
    )
    # Attach is_lwp from the Leave Type (best-effort; unknown type → paid).
    if not rows:
        return []
    try:
        lwp_map = {
            lt.name: bool(int(lt.is_lwp or 0))
            for lt in frappe.get_all("Leave Type", fields=["name", "is_lwp"], limit_page_length=0)
        }
    except Exception:  # pragma: no cover — defensive
        lwp_map = {}
    for row in rows:
        row["is_lwp"] = 1 if lwp_map.get(row.get("leave_type")) else 0
    return rows


__all__ = [
    "month_window",
    "build_employee_report",
    "report_totals",
    "load_work_sessions",
    "load_leave_applications",
]
