"""
Dashboard KPI API — plan v5 §10.9 / §13.

Fronts the company-wide HR overview used by ``HrReportsView`` (and the manager
dashboard tiles). The ``hr-ui`` ``fetchHrDashboard`` client calls one endpoint:

* ``get_employee_dashboard`` → KPI aggregates scoped to the caller's role /
  company. Returns (plan §13.3):

      {
        total_employees, present_today, absent_today, late_today,
        on_leave_today, company_attendance_rate, total_exceptions,
        locked_periods,
      }

Data sources:

* present / absent / late today  → ``VN Attendance Work Session`` (work_date ==
  today, ``absent`` flag, ``late_minutes``).
* on_leave_today                  → ``Leave Application`` (Approved covering
  today).
* total_exceptions                → ``VN Attendance Exception`` (status open).
* locked_periods                  → ``VN Monthly Attendance Period`` (Locked).

The pure aggregation helpers at the top of this module avoid importing
``frappe`` so they can be unit-tested outside a bench (same pattern as
``api/device.py`` / ``utils/calc.py``). Each ``@frappe.whitelist()`` endpoint
imports ``frappe`` lazily.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import report as report_utils
from gege_hr.gege_hr.utils import tz as tz_utils


# --------------------------------------------------------------------------- #
# Lazy ``frappe`` shims — defined first so endpoint decorators resolve at
# module-import time WITHOUT importing frappe (keeps the pure helpers unit-
# testable outside a bench, matching ``api/device.py``'s pattern).
# --------------------------------------------------------------------------- #
def frappe_whitelist():
    """``@frappe_whitelist()`` → real ``frappe.whitelist()`` in a bench, else a
    no-op marker decorator so the module still imports outside a bench."""
    try:
        import frappe

        return frappe.whitelist()
    except Exception:  # pragma: no cover - outside bench

        def _decorator(func):
            func.whitelisted = True
            return func

        return _decorator


def _(msg: str, *args, **kwargs) -> str:
    """Lazy translation marker — resolves to ``frappe._`` inside a bench."""
    try:
        import frappe

        return frappe._(msg, *args, **kwargs)
    except Exception:  # pragma: no cover
        if args or kwargs:
            try:
                return msg.format(*args, **kwargs)
            except Exception:
                return msg
        return msg


# Exception statuses still considered "open" / un-actioned (§exception status).
OPEN_EXCEPTION_STATUSES = ("Open", "In Progress", "Escalated")


# --------------------------------------------------------------------------- #
# Pure helpers (bench-free — unit tested in tests/test_dashboard.py)
# --------------------------------------------------------------------------- #
def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def compute_rate(present: int, absent: int) -> float:
    """Company attendance rate = present / (present + absent) * 100.

    Returns ``0.0`` when the denominator is zero (no attendance recorded yet)
    rather than raising — a brand-new site must not crash the dashboard.
    """
    denom = (present or 0) + (absent or 0)
    if denom <= 0:
        return 0.0
    return round(((present or 0) / denom) * 100.0, 2)


def aggregate_today(ws_rows: Iterable[dict]) -> dict:
    """Fold today's VN Attendance Work Session rows into present/absent/late
    counts.

    A row counts as **present** when ``absent`` is falsy and there is at least
    a check-in (``missing_checkin`` falsy). A row is **absent** when the
    ``absent`` flag is set. **Late** = ``late_minutes`` > 0.

    Returns ``{present_today, absent_today, late_today}``.
    """
    present = absent = late = 0
    for row in ws_rows or []:
        is_absent = bool(int(row.get("absent") or 0))
        if is_absent:
            absent += 1
            continue
        # Present requires an actual check-in (not a missing-IN placeholder).
        if not int(row.get("missing_checkin") or 0):
            present += 1
        else:
            # No check-in recorded and not flagged absent → still counts as
            # absent for the rate (shift exists but employee hasn't shown up).
            absent += 1
        if _to_int(row.get("late_minutes")) > 0:
            late += 1
    return {"present_today": present, "absent_today": absent, "late_today": late}


def aggregate_dashboard(
    ws_rows: Iterable[dict],
    total_employees: int,
    on_leave_today: int,
    total_exceptions: int,
    locked_periods: int,
) -> dict:
    """Compose the full dashboard payload from the raw counts.

    ``ws_rows`` are today's Work Session rows; ``total_employees`` is the
    company head-count. The attendance rate uses present/absent from the work
    sessions (on-leave employees are excluded from the denominator so a holiday
    doesn't crater the rate).
    """
    today = aggregate_today(ws_rows)
    rate = compute_rate(today["present_today"], today["absent_today"])
    return {
        "total_employees": _to_int(total_employees),
        "present_today": today["present_today"],
        "absent_today": today["absent_today"],
        "late_today": today["late_today"],
        "on_leave_today": _to_int(on_leave_today),
        "company_attendance_rate": rate,
        "total_exceptions": _to_int(total_exceptions),
        "locked_periods": _to_int(locked_periods),
    }


# --------------------------------------------------------------------------- #
# Bench loaders
# --------------------------------------------------------------------------- #
def _company_filter(company: str | None) -> list[list]:
    """Build a Frappe filter fragment for the optional company scope."""
    if company:
        return [["company", "=", company]]
    return []


def _today_portal() -> str:
    """Today's date in the portal timezone as ``YYYY-MM-DD`` (matches
    ``VN Attendance Work Session.work_date``)."""
    return tz_utils.now_in_portal().date().isoformat()


def _count_employees(company: str | None) -> int:
    import frappe  # noqa: WPS433 - lazy

    filters = {"status": "Active"}
    if company:
        filters["company"] = company
    return frappe.db.count("Employee", filters=filters)


def _today_work_sessions(company: str | None, today: str) -> list[dict]:
    import frappe  # noqa: WPS433 - lazy

    return (
        frappe.db.get_all(
            "VN Attendance Work Session",
            filters=[["work_date", "=", today], *_company_filter(company)],
            fields=["absent", "missing_checkin", "late_minutes"],
        )
        or []
    )


def _count_on_leave(company: str | None, today: str) -> int:
    import frappe  # noqa: WPS433 - lazy

    filters = [
        ["status", "=", "Approved"],
        ["from_date", "<=", today],
        ["to_date", ">=", today],
        ["docstatus", "=", 1],
    ]
    if company:
        filters.append(["company", "=", company])
    return frappe.db.count("Leave Application", filters=filters)


def _count_open_exceptions(company: str | None) -> int:
    import frappe  # noqa: WPS433 - lazy

    filters = [["status", "in", list(OPEN_EXCEPTION_STATUSES)]]
    if company:
        filters.append(["company", "=", company])
    return frappe.db.count("VN Attendance Exception", filters=filters)


def _count_locked_periods(company: str | None) -> int:
    import frappe  # noqa: WPS433 - lazy

    filters = [["status", "=", "Locked"]]
    if company:
        filters.append(["company", "=", company])
    return frappe.db.count("VN Monthly Attendance Period", filters=filters)


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@frappe_whitelist()
def get_employee_dashboard(
    company: str | None = None,
    period_month: str | None = None,
    period_year: str | None = None,
) -> dict:
    """Plan §10.9 / §13 — company-wide KPI aggregates.

    ``company`` may be empty (then the whole tenant is aggregated).
    ``period_month`` / ``period_year`` are accepted for forward-compat with the
    FE contract but the headline tiles always reflect *today* (the monthly
    figures already live on the Monthly Attendance view).
    """
    _require_hr_user()

    company = (company or "").strip() or None
    today = _today_portal()

    ws_rows = _today_work_sessions(company, today)
    total_employees = _count_employees(company)
    on_leave = _count_on_leave(company, today)
    exceptions = _count_open_exceptions(company)
    locked = _count_locked_periods(company)

    payload = aggregate_dashboard(
        ws_rows=ws_rows,
        total_employees=total_employees,
        on_leave_today=on_leave,
        total_exceptions=exceptions,
        locked_periods=locked,
    )
    payload["as_of_date"] = today
    payload["company"] = company or ""
    return payload


# --------------------------------------------------------------------------- #
# Per-employee attendance / OT / leave report (plan §10.9 / §13)
# --------------------------------------------------------------------------- #
def _require_hr_user() -> None:
    """Only HR User / HR Manager / System Manager may read the company report."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_USER_ROLES):
        import frappe  # noqa: WPS433 - lazy

        frappe.throw(
            _("Bạn không có quyền xem báo cáo chấm công."),
            frappe.PermissionError,
        )


# Broad-search fields for the per-employee report (DNA §6.6 A — Law #3):
# the free-text query OR-matches any of these identifying columns, INCLUDING
# numeric columns (gõ số → khớp giá trị cột đó, e.g. "5" match present_days=5/15).
_REPORT_SEARCH_KEYS = (
    "employee",
    "employee_name",
    "department",
    "branch",
    "company",
    # numeric columns also match the free-text query (DNA §6.6 A)
    "present_days",
    "absent_days",
    "leave_days",
    "overtime_hours",
    "late_minutes",
    "payable_days",
)


def _filter_report_employees(employees: list[dict], search: str | None) -> list[dict]:
    """Server-side free-text filter across a report row's text + numeric fields.

    Applied after the report is built so the returned ``employees`` + ``totals``
    always agree with the visible (filtered) rows.
    """
    q = (search or "").strip().lower()
    if not q:
        return employees
    out: list[dict] = []
    for row in employees or []:
        for key in _REPORT_SEARCH_KEYS:
            val = row.get(key)
            if val is not None and q in str(val).lower():
                out.append(row)
                break
    return out


def _parse_float(value):
    """Best-effort float parse; returns ``None`` for empty/invalid input."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Numeric columns that support a popover min/max range filter (DNA §6.6 B — Law #2).
_REPORT_RANGE_KEYS = (
    "present_days",
    "absent_days",
    "leave_days",
    "overtime_hours",
    "late_minutes",
    "payable_days",
)


def _filter_report_ranges(employees: list[dict], ranges: dict) -> list[dict]:
    """Apply server-side numeric min/max ranges to the built report rows.

    ``ranges`` maps a numeric column name to a ``(min, max)`` tuple where either
    bound may be ``None`` (open-ended). Rows whose value lies outside the range
    are dropped; totals recompute over the survivors upstream (DNA §6.6 B — two
    separate ``>=``/``<=`` bounds, never a single ``between``).
    """
    if not ranges or not employees:
        return employees
    out: list[dict] = []
    for row in employees:
        keep = True
        for key, bounds in ranges.items():
            lo, hi = bounds
            if lo is None and hi is None:
                continue
            val = row.get(key)
            if val is None:
                keep = False
                break
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if lo is not None and num < lo:
                keep = False
                break
            if hi is not None and num > hi:
                keep = False
                break
        if keep:
            out.append(row)
    return out


@frappe_whitelist()
def get_attendance_report(
    company: str | None = None,
    period_month: str | None = None,
    period_year: str | None = None,
    search: str | None = None,
    # Numeric range filters (DNA §6.6 B — Law #2). Each column takes an
    # open-ended min/max; either bound may be omitted. Param names mirror the
    # column field with a ``_min``/``_max`` suffix so the FE can map generically.
    present_days_min: float | None = None,
    present_days_max: float | None = None,
    absent_days_min: float | None = None,
    absent_days_max: float | None = None,
    leave_days_min: float | None = None,
    leave_days_max: float | None = None,
    overtime_hours_min: float | None = None,
    overtime_hours_max: float | None = None,
    late_minutes_min: float | None = None,
    late_minutes_max: float | None = None,
    payable_days_min: float | None = None,
    payable_days_max: float | None = None,
) -> dict:
    """Plan §10.9 / §13 — per-employee attendance / OT / leave report for a
    calendar month (the HR "Báo cáo theo nhân viên" table).

    ``period_month`` / ``period_year`` resolve to a calendar month window; the
    company scope is optional (empty = whole tenant). Returns::

        {
          company, period_month, period_year, from_date, to_date,
          employees: [ { employee, employee_name, department, branch, company,
                         present_days, absent_days, paid_leave_days,
                         unpaid_leave_days, holiday_days, leave_days,
                         regular_hours, overtime_hours, overtime_night_hours,
                         late_count, late_minutes, early_leave_minutes,
                         payable_days, need_review_count } ],
          totals: { total_employees, total_present_days, total_absent_days,
                    total_leave_days, total_paid_leave_days,
                    total_unpaid_leave_days, total_overtime_hours,
                    total_late_minutes, total_need_review, total_payable_days },
        }

    An invalid month/year (or a window with no data) yields an empty report
    rather than raising — a brand-new site must not crash the report view.
    """

    _require_hr_user()

    company = (company or "").strip() or None
    window = report_utils.month_window(period_month, period_year)
    if not window:
        return {
            "company": company or "",
            "period_month": str(period_month or ""),
            "period_year": str(period_year or ""),
            "from_date": "",
            "to_date": "",
            "employees": [],
            "totals": report_utils.report_totals([]),
        }

    from_date, to_date = window
    from_str = from_date.isoformat()
    to_str = to_date.isoformat()

    ws_rows = report_utils.load_work_sessions(company, from_str, to_str)
    leave_rows = report_utils.load_leave_applications(company, from_str, to_str)

    employees = report_utils.build_employee_report(ws_rows, leave_rows)
    employees = _filter_report_employees(employees, search)
    employees = _filter_report_ranges(
        employees,
        {
            key: (
                _parse_float(locals().get(f"{key}_min")),
                _parse_float(locals().get(f"{key}_max")),
            )
            for key in _REPORT_RANGE_KEYS
        },
    )
    totals = report_utils.report_totals(employees)
    return {
        "company": company or "",
        "period_month": str(period_month or ""),
        "period_year": str(period_year or ""),
        "from_date": from_str,
        "to_date": to_str,
        "employees": employees,
        "totals": totals,
    }


__all__ = [
    "compute_rate",
    "aggregate_today",
    "aggregate_dashboard",
    "get_employee_dashboard",
    "get_attendance_report",
    "OPEN_EXCEPTION_STATUSES",
]
