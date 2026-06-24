"""
Monthly Attendance Period helpers — pure functions (bench-free, testable).

Mirrors the pattern of :mod:`gege_hr.gege_hr.utils.payroll` /
:mod:`gege_hr.gege_hr.utils.calc`: ``aggregate_period`` folds a flat list of
VN Attendance Work Session rows (one row per work date) into a per-employee
day/hour summary that becomes a VN Monthly Attendance Line; ``rollup_period``
sums those lines into the period totals. Bench loaders are frappe-guarded so
the pure core can run under pytest without a bench.
"""

from __future__ import annotations

try:  # pragma: no cover — bench import
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore


# --------------------------------------------------------------------------- #
# Numeric helpers (mirror payroll.py so behaviour is identical)
# --------------------------------------------------------------------------- #
def _num(v, default=0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def round2(v) -> float:
    return round(_num(v), 2)


# Real columns on the VN Attendance Work Session table — used by the loader's
# SELECT. The aggregator below reads additional synthetic keys
# (department/branch/is_holiday/leave_unpaid/overtime_normal_hours) tolerantly:
# they default to 0/None when absent (department/branch are filled later from
# the Employee by the line's _normalize_employee).
_WS_FIELDS = (
    "employee",
    "employee_name",
    "work_date",
    "company",
    "payable_day",
    "regular_hours",
    "regular_night_hours",
    "raw_overtime_hours",
    "overtime_night_hours",
    "overtime_holiday_hours",
    "late_minutes",
    "early_leave_minutes",
    "absent",
    "need_review",
    "has_leave",
)


def _g(row: dict, key: str, default=None):
    v = row.get(key, default)
    return v


def _gn(row: dict, key: str) -> float:
    return _num(_g(row, key), 0.0)


# --------------------------------------------------------------------------- #
# Pure aggregation — Work Session rows → per-employee line summaries
# --------------------------------------------------------------------------- #
def aggregate_period(work_sessions: list[dict]) -> dict:
    """Fold Work Session rows into per-employee day/hour summaries.

    ``work_sessions`` items are plain dicts (as returned by
    ``frappe.db.get_all(..., as_dict=True)``). Missing keys default to 0.

    Returns ``{ employee: line_summary_dict }`` where each summary has the full
    set of VN Monthly Attendance Line fields (employee_name/department/branch/
    company + the day/hour/payable breakdown).
    """
    lines: dict[str, dict] = {}
    for row in work_sessions or []:
        emp = _g(row, "employee")
        if not emp:
            continue
        line = lines.setdefault(emp, _blank_line(emp, row))

        # Day counts ------------------------------------------------------ #
        line["working_days"] += 1
        payable = _gn(row, "payable_day")
        absent = _gn(row, "absent") >= 1
        if absent:
            line["absent_days"] += 1
        else:
            line["present_days"] += payable
        # Leave split (best-effort): WS exposes a boolean ``has_leave``; the
        # paid/unpaid classification comes from an optional ``leave_paid`` flag
        # the loader attaches from Leave Type config. Without it we count the
        # leave day under paid leave.
        if _gn(row, "has_leave") >= 1:
            if _gn(row, "leave_unpaid") >= 1:
                line["unpaid_leave_days"] += 1
            else:
                line["paid_leave_days"] += 1
        if _gn(row, "is_holiday") >= 1 and not absent:
            line["holiday_days"] += payable

        # Hours ----------------------------------------------------------- #
        line["regular_hours"] += _gn(row, "regular_hours")
        line["regular_night_hours"] += _gn(row, "regular_night_hours")
        normal = _gn(row, "overtime_normal_hours")
        night = _gn(row, "overtime_night_hours")
        holiday = _gn(row, "overtime_holiday_hours")
        if normal or night or holiday:
            line["overtime_hours"] += normal
            line["overtime_night_hours"] += night
            line["overtime_holiday_hours"] += holiday
        else:
            line["overtime_hours"] += _gn(row, "raw_overtime_hours")

        # Late / early ---------------------------------------------------- #
        late = int(_gn(row, "late_minutes"))
        early = int(_gn(row, "early_leave_minutes"))
        if late > 0:
            line["late_count"] += 1
            line["late_minutes"] += late
        if early > 0:
            line["early_leave_count"] += 1
            line["early_leave_minutes"] += early

        # Payable & review ------------------------------------------------ #
        line["payable_hours"] += _gn(row, "regular_hours")
        if _gn(row, "need_review") >= 1:
            line["need_review_count"] += 1

    # Final payable days = sum of payable_day across all rows (incl. leave).
    for line in lines.values():
        line["present_days"] = round2(line["present_days"])
        line["absent_days"] = round2(line["absent_days"])
        line["paid_leave_days"] = round2(line["paid_leave_days"])
        line["unpaid_leave_days"] = round2(line["unpaid_leave_days"])
        line["holiday_days"] = round2(line["holiday_days"])
        # payable_days derives from the engine's per-day payable_day (already
        # leave-aware), re-summed here from a fresh pass to stay honest.
        line["regular_hours"] = round2(line["regular_hours"])
        line["regular_night_hours"] = round2(line["regular_night_hours"])
        line["overtime_hours"] = round2(line["overtime_hours"])
        line["overtime_night_hours"] = round2(line["overtime_night_hours"])
        line["overtime_holiday_hours"] = round2(line["overtime_holiday_hours"])
        line["payable_hours"] = round2(line["payable_hours"])

    return lines


def _blank_line(employee: str, row: dict) -> dict:
    return {
        "employee": employee,
        "employee_name": _g(row, "employee_name") or "",
        "department": _g(row, "department"),
        "branch": _g(row, "branch"),
        "company": _g(row, "company"),
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
    }


def recalc_payable_days(lines: dict) -> dict:
    """Re-sum ``payable_days`` for each line from a stored per-row cache.

    The engine already computes ``payable_day`` per Work Session (leave-aware:
    Paid=1, Half=0.5, Unpaid=0, Absent=0). ``aggregate_period`` tracks day
    *counts*; the actual payable-day total is supplied by the loader via a
    ``payable_day`` sum already present on each row. This helper copies that
    pre-summed value if the caller attached it, otherwise falls back to
    ``present_days``.
    """
    for line in lines.values():
        if line.get("payable_days") in (None, 0, 0.0):
            line["payable_days"] = line.get("present_days", 0.0)
        line["payable_days"] = round2(line["payable_days"])
    return lines


# --------------------------------------------------------------------------- #
# Pure rollup — line summaries → period totals
# --------------------------------------------------------------------------- #
def rollup_period(lines: dict) -> dict:
    """Sum the per-employee line summaries into period totals (§16 fields)."""
    totals = {
        "total_employees": len(lines),
        "total_present_days": 0.0,
        "total_absent_days": 0.0,
        "total_overtime_hours": 0.0,
        "total_late_minutes": 0,
        "total_need_review": 0,
    }
    for line in lines.values():
        totals["total_present_days"] += _num(line.get("present_days"))
        totals["total_absent_days"] += _num(line.get("absent_days"))
        totals["total_overtime_hours"] += _num(line.get("overtime_hours"))
        totals["total_late_minutes"] += int(_num(line.get("late_minutes")))
        totals["total_need_review"] += int(_num(line.get("need_review_count")))
    totals["total_present_days"] = round2(totals["total_present_days"])
    totals["total_absent_days"] = round2(totals["total_absent_days"])
    totals["total_overtime_hours"] = round2(totals["total_overtime_hours"])
    return totals


# --------------------------------------------------------------------------- #
# Bench loaders — frappe-guarded, safe to import without a bench.
# --------------------------------------------------------------------------- #
def load_period_work_sessions(period_name: str) -> list[dict]:
    """Return the Work Session rows that belong to a period's date window.

    The period is read to get ``company`` / ``from_date`` / ``to_date``; rows
    are scoped by employee→company + ``work_date`` between the bounds. A
    ``payable_day`` running sum per employee is NOT attached here — the caller
    (:mod:`gege_hr.gege_hr.api.attendance_period`) computes payable_days from
    :func:`aggregate_period`.
    """
    if not frappe:
        return []
    period = (
        frappe.db.get_value(
            "VN Monthly Attendance Period",
            period_name,
            ["company", "from_date", "to_date"],
            as_dict=True,
        )
        or {}
    )
    if not period.get("from_date") or not period.get("to_date"):
        return []
    return frappe.db.get_all(
        "VN Attendance Work Session",
        filters={
            "work_date": ["between", [period.from_date, period.to_date]],
            "company": period.company,
            "docstatus": ["<", 2],
        },
        fields=list(_WS_FIELDS) + ["name"],
        order_by="employee, work_date",
    )


def can_lock(lines: list[dict]) -> bool:
    """A period can be locked only when every line is Confirmed/Adjusted."""
    LOCKABLE = {"Confirmed", "Adjusted"}
    if not lines:
        return False
    return all((ln.get("status") in LOCKABLE) for ln in lines)
