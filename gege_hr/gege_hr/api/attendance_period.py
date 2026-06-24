"""
Monthly Attendance Period API — plan v5 §10.7 / doctype-design §16-18.

Closing endpoints fronting the VN Monthly Attendance Period, VN Monthly
Attendance Line and VN Attendance Lock Log DocTypes. They map 1:1 to the
frontend ``gege_hr.gege_hr.api.attendance_period.<fn>`` calls in
``hr-ui/src/api/index.js``:

  * ``periods``              — list of VN Monthly Attendance Period rows
  * ``create_period``        — create a Draft period (no-dup company+month+year)
  * ``period_detail``        — period + its attendance lines
  * ``generate_lines``       — aggregate Work Sessions → upsert lines + totals
  * ``confirm_line``         — Draft/Adjusted → Confirmed (one line)
  * ``confirm_all_lines``    — bulk confirm every line of a period
  * ``lock_period``          — Generated → Locked (all lines must be Confirmed)
  * ``unlock_period``        — Locked → Unlocked (HR Manager only) + Lock Log
  * ``my_attendance_summary``— the employee's own line for a period

Aggregation uses the pure helpers in
:mod:`gege_hr.gege_hr.utils.attendance_period` so the maths is unit-testable
outside a bench. Closing operations require HR Manager roles; the employee
summary is employee-readable.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import attendance_period as ap
from gege_hr.gege_hr.utils import employee as emp_utils

PERIOD_DOCTYPE = "VN Monthly Attendance Period"
LINE_DOCTYPE = "VN Monthly Attendance Line"
LOCK_LOG_DOCTYPE = "VN Attendance Lock Log"

# Row shape returned to the SPA — stable/flat so the list renders directly.
_PERIOD_FIELDS = [
    "name",
    "period_name",
    "company",
    "from_date",
    "to_date",
    "payroll_month",
    "payroll_year",
    "attendance_policy",
    "status",
    "total_employees",
    "total_present_days",
    "total_absent_days",
    "total_overtime_hours",
    "total_late_minutes",
    "total_need_review",
    "generated_by",
    "generated_at",
    "locked_by",
    "locked_at",
]

_LINE_FIELDS = [
    "name",
    "attendance_period",
    "employee",
    "employee_name",
    "department",
    "branch",
    "company",
    "status",
    "working_days",
    "present_days",
    "absent_days",
    "paid_leave_days",
    "unpaid_leave_days",
    "holiday_days",
    "regular_hours",
    "regular_night_hours",
    "overtime_hours",
    "overtime_night_hours",
    "overtime_holiday_hours",
    "late_count",
    "late_minutes",
    "early_leave_count",
    "early_leave_minutes",
    "payable_hours",
    "payable_days",
    "need_review_count",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _assert_closer() -> None:
    """Only HR Manager / System Manager may run closing ops."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_MANAGER_ROLES):
        frappe.throw(
            _("Bạn không có quyền chốt kỳ công."),
            frappe.PermissionError,
        )


def _get_period(name: str):
    try:
        return frappe.get_doc(PERIOD_DOCTYPE, name)
    except Exception:
        frappe.throw(_("Kỳ công {0} không tồn tại.").format(name))


def _period_row(name: str) -> dict:
    return frappe.db.get_value(PERIOD_DOCTYPE, name, _PERIOD_FIELDS, as_dict=True) or {}


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def periods(
    company: str | None = None,
    status: str | None = None,
    year: int | str | None = None,
) -> list[dict]:
    """List attendance periods, newest first (HR users)."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_USER_ROLES):
        return []
    filters = {}
    if company:
        filters["company"] = company
    if status:
        filters["status"] = status
    if year:
        filters["payroll_year"] = int(year)
    return frappe.db.get_all(
        PERIOD_DOCTYPE,
        filters=filters,
        fields=_PERIOD_FIELDS,
        order_by="payroll_year desc, payroll_month desc, modified desc",
    )


@frappe.whitelist()
def period_detail(name: str) -> dict:
    """One period + its attendance lines → ``{ period, lines }`` (FE contract)."""
    _assert_closer()
    period = _period_row(name)
    if not period:
        frappe.throw(_("Kỳ công {0} không tồn tại.").format(name))
    lines = frappe.db.get_all(
        LINE_DOCTYPE,
        filters={"attendance_period": name, "docstatus": ["<", 2]},
        fields=_LINE_FIELDS,
        order_by="employee_name",
    )
    return {"period": period, "lines": lines}


@frappe.whitelist()
def my_attendance_summary(period: str) -> dict:
    """The caller's own attendance line for a period (employee-readable)."""
    emp = emp_utils.get_employee_for_user()
    if not emp:
        return {}
    line = frappe.db.get_value(
        LINE_DOCTYPE,
        {"attendance_period": period, "employee": emp, "docstatus": ["<", 2]},
        _LINE_FIELDS,
        as_dict=True,
    )
    return line or {}


# --------------------------------------------------------------------------- #
# Create / generate
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_period(
    company: str,
    payroll_month: str,
    payroll_year: int,
    from_date: str,
    to_date: str,
    period_name: str | None = None,
    attendance_policy: str | None = None,
) -> dict:
    """Create a Draft attendance period (no-dup company+month+year)."""
    _assert_closer()
    exists = frappe.db.exists(
        PERIOD_DOCTYPE,
        {
            "company": company,
            "payroll_month": payroll_month,
            "payroll_year": payroll_year,
            "docstatus": ["<", 2],
        },
    )
    if exists:
        frappe.throw(_("Đã tồn tại kỳ công {0}/{1} cho công ty này.").format(payroll_month, payroll_year))
    if not period_name:
        period_name = _("Công tháng {0}/{1}").format(payroll_month, payroll_year)
    doc = frappe.get_doc(
        {
            "doctype": PERIOD_DOCTYPE,
            "period_name": period_name,
            "company": company,
            "from_date": from_date,
            "to_date": to_date,
            "payroll_month": payroll_month,
            "payroll_year": payroll_year,
            "attendance_policy": attendance_policy,
        }
    )
    doc.insert()
    return {"name": doc.name, "status": doc.status, "message": _("Đã tạo kỳ công.")}


@frappe.whitelist()
def generate_monthly_period(
    company: str,
    payroll_month: str,
    payroll_year: int,
    from_date: str,
    to_date: str,
    attendance_policy: str | None = None,
) -> dict:
    """One-shot create + generate (FE contract: ``generate_monthly_period``).

    Creates the period if it does not exist, then aggregates Work Sessions into
    per-employee lines and stamps the period ``Generated``.
    """
    _assert_closer()
    existing = frappe.db.exists(
        PERIOD_DOCTYPE,
        {
            "company": company,
            "payroll_month": payroll_month,
            "payroll_year": payroll_year,
            "docstatus": ["<", 2],
        },
    )
    name = (
        existing
        or create_period(
            company=company,
            payroll_month=payroll_month,
            payroll_year=payroll_year,
            from_date=from_date,
            to_date=to_date,
            attendance_policy=attendance_policy,
        )["name"]
    )
    return generate_lines(name)


@frappe.whitelist()
def generate_lines(name: str) -> dict:
    """Aggregate Work Sessions → upsert per-employee lines + period totals.

    Sets the period to ``Generated`` and stamps audit fields. Idempotent: an
    existing line for an employee is updated in place (status preserved unless
    Draft, in which case it stays Draft for confirmation).
    """
    _assert_closer()
    period = _get_period(name)
    if period.status == "Locked":
        frappe.throw(_("Kỳ công đã khoá, không thể tạo lại."))

    rows = ap.load_period_work_sessions(name)
    summaries = ap.aggregate_period(rows)

    # Build a running payable_day total per employee directly from rows so the
    # line's payable_days is the engine's leave-aware value, not present_days.
    payable_by_emp: dict[str, float] = {}
    for r in rows:
        emp = r.get("employee")
        if emp:
            payable_by_emp[emp] = payable_by_emp.get(emp, 0.0) + ap._num(r.get("payable_day"))

    for emp, summary in summaries.items():
        summary["payable_days"] = ap.round2(payable_by_emp.get(emp, 0.0))
        _upsert_line(period, emp, summary)

    _apply_rollup(period)
    period.status = "Generated"
    period.generated_by = frappe.session.user
    period.generated_at = now_datetime()
    period.save()
    return {
        "name": name,
        "status": period.status,
        "total_employees": period.total_employees,
        "message": _("Đã tạo {0} dòng công.").format(len(summaries)),
    }


def _upsert_line(period, employee: str, summary: dict) -> None:
    existing = frappe.db.get_value(
        LINE_DOCTYPE,
        {"attendance_period": period.name, "employee": employee, "docstatus": ["<", 2]},
        ["name", "status"],
        as_dict=True,
    )
    payload = {
        "doctype": LINE_DOCTYPE,
        "attendance_period": period.name,
        "employee": employee,
        "employee_name": summary.get("employee_name"),
        "department": summary.get("department"),
        "branch": summary.get("branch"),
        "company": summary.get("company") or period.company,
        "working_days": summary.get("working_days", 0),
        "present_days": summary.get("present_days", 0),
        "absent_days": summary.get("absent_days", 0),
        "paid_leave_days": summary.get("paid_leave_days", 0),
        "unpaid_leave_days": summary.get("unpaid_leave_days", 0),
        "holiday_days": summary.get("holiday_days", 0),
        "regular_hours": summary.get("regular_hours", 0),
        "regular_night_hours": summary.get("regular_night_hours", 0),
        "overtime_hours": summary.get("overtime_hours", 0),
        "overtime_night_hours": summary.get("overtime_night_hours", 0),
        "overtime_holiday_hours": summary.get("overtime_holiday_hours", 0),
        "late_count": summary.get("late_count", 0),
        "late_minutes": summary.get("late_minutes", 0),
        "early_leave_count": summary.get("early_leave_count", 0),
        "early_leave_minutes": summary.get("early_leave_minutes", 0),
        "payable_hours": summary.get("payable_hours", 0),
        "payable_days": summary.get("payable_days", 0),
        "need_review_count": summary.get("need_review_count", 0),
    }
    if existing:
        # Preserve a Confirmed/Adjusted status so a re-generate doesn't wipe a
        # manually confirmed line.
        payload["name"] = existing.name
        line = frappe.get_doc(LINE_DOCTYPE, existing.name)
        if existing.status not in ("Confirmed", "Adjusted"):
            payload["status"] = "Draft"
        else:
            payload["status"] = existing.status
        line.update(payload)
        line.save()
    else:
        payload["status"] = "Draft"
        frappe.get_doc(payload).insert()


def _apply_rollup(period) -> None:
    lines = frappe.db.get_all(
        LINE_DOCTYPE,
        filters={"attendance_period": period.name, "docstatus": ["<", 2]},
        fields=[
            "present_days",
            "absent_days",
            "overtime_hours",
            "late_minutes",
            "need_review_count",
        ],
    )
    totals = ap.rollup_period({f"row-{i}": ln for i, ln in enumerate(lines)})
    period.total_employees = totals["total_employees"]
    period.total_present_days = totals["total_present_days"]
    period.total_absent_days = totals["total_absent_days"]
    period.total_overtime_hours = totals["total_overtime_hours"]
    period.total_late_minutes = totals["total_late_minutes"]
    period.total_need_review = totals["total_need_review"]


# --------------------------------------------------------------------------- #
# Confirm / lock / unlock
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def confirm_line(name: str) -> dict:
    """Mark one attendance line Confirmed (Draft/Adjusted → Confirmed)."""
    _assert_closer()
    line = frappe.get_doc(LINE_DOCTYPE, name)
    if line.status == "Locked":
        frappe.throw(_("Dòng công đã khoá."))
    line.status = "Confirmed"
    line.save()
    return {"name": name, "status": line.status, "message": _("Đã xác nhận dòng công.")}


@frappe.whitelist()
def confirm_all_lines(period: str) -> dict:
    """Bulk-confirm every Draft line of a period."""
    _assert_closer()
    names = frappe.db.get_all(
        LINE_DOCTYPE,
        filters={"attendance_period": period, "status": "Draft", "docstatus": ["<", 2]},
        pluck="name",
    )
    for n in names:
        line = frappe.get_doc(LINE_DOCTYPE, n)
        line.status = "Confirmed"
        line.save()
    return {
        "period": period,
        "confirmed": len(names),
        "message": _("Đã xác nhận {0} dòng công.").format(len(names)),
    }


@frappe.whitelist()
def lock_period(name: str, reason: str | None = None) -> dict:
    """Lock a period — every line must be Confirmed/Adjusted (§16)."""
    _assert_closer()
    period = _get_period(name)
    if period.status == "Locked":
        return {"name": name, "status": "Locked", "message": _("Kỳ công đã khoá.")}
    if period.status not in ("Generated", "Unlocked"):
        frappe.throw(_("Chỉ kỳ đã sinh công mới có thể khoá."))

    lines = frappe.db.get_all(
        LINE_DOCTYPE,
        filters={"attendance_period": name, "docstatus": ["<", 2]},
        fields=["name", "status"],
    )
    if not ap.can_lock(lines):
        frappe.throw(_("Phải xác nhận tất cả dòng công trước khi khoá."))

    old_status = period.status
    for ln in lines:
        doc = frappe.get_doc(LINE_DOCTYPE, ln.name)
        doc.status = "Locked"
        doc.save()
    period.status = "Locked"
    period.locked_by = frappe.session.user
    period.locked_at = now_datetime()
    period.save()
    _write_lock_log(name, "Lock", old_status, "Locked", reason)
    audit_api.log(
        "Monthly Lock",
        doc=period.as_dict(),
        description=f"{old_status} → Locked",
        old_value=old_status,
        new_value="Locked",
    )
    return {"name": name, "status": "Locked", "message": _("Đã khoá kỳ công.")}


@frappe.whitelist()
def unlock_period(name: str, reason: str | None = None) -> dict:
    """Unlock a locked period — HR Manager only (§16). Creates a Lock Log."""
    _assert_closer()
    period = _get_period(name)
    if period.status != "Locked":
        frappe.throw(_("Chỉ kỳ đang khoá mới có thể mở khoá."))
    old_status = period.status
    period.status = "Unlocked"
    period.save()
    _write_lock_log(name, "Unlock", old_status, "Unlocked", reason)
    audit_api.log(
        "Monthly Unlock",
        doc=period.as_dict(),
        description=f"{old_status} → Unlocked",
        old_value=old_status,
        new_value="Unlocked",
    )
    return {"name": name, "status": "Unlocked", "message": _("Đã mở khoá kỳ công.")}


def _write_lock_log(
    period_name: str, action: str, old_status: str, new_status: str, reason: str | None
) -> None:
    try:
        frappe.get_doc(
            {
                "doctype": LOCK_LOG_DOCTYPE,
                "attendance_period": period_name,
                "action": action,
                "reason": reason,
                "actor": frappe.session.user,
                "old_status": old_status,
                "new_status": new_status,
            }
        ).insert()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "attendance_period lock log")
