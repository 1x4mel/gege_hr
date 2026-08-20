"""
Payroll Review API — plan v5 §10.8 / doctype-design §20-21.

Milestone-3 closing & payroll endpoints fronting the VN Payroll Review Period
and VN Payroll Review Line DocTypes. They map 1:1 to the frontend
``gege_hr.gege_hr.api.payroll.<fn>`` calls in ``hr-ui/src/api/index.js``:

  * ``periods``                 — list of VN Payroll Review Period rows
  * ``create_payroll_review``   — create a Draft period from a locked attendance period
  * ``calculate_payroll_review``— build per-employee review lines + period totals
  * ``review_detail``           — period + its review lines
  * ``approve_review``          — approve the period (all lines must be Confirmed+)
  * ``generate_salary_slips``   — create Additional Salary rows / Salary Slips
  * ``publish_payslips``        — mark payslips employee-visible + realtime event
  * ``my_payslips``             — the employee's published payslips
  * ``payslip_detail``          — one payslip with the VN breakdown

Computation uses the pure helpers in
:mod:`gege_hr.gege_hr.utils.payroll` so the maths is unit-testable outside a
bench. All endpoints require HR Manager / Payroll Manager roles (closing is an
admin operation); ``my_payslips`` / ``payslip_detail`` are employee-readable.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import _db, notify, pagination
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import payroll as calc

# Checkout-miss defaults shared with the engine (BUG-6 fix) — the settings UI
# must fall back to exactly what utils.checkout_miss falls back to.
from gege_hr.gege_hr.utils.checkout_miss import DEFAULTS as CM_DEFAULTS

PERIOD_DOCTYPE = "VN Payroll Review Period"
LINE_DOCTYPE = "VN Payroll Review Line"
WORK_SESSION_DOCTYPE = "VN Attendance Work Session"

# Row shape returned to the SPA — stable/flat so the list renders directly.
_PERIOD_FIELDS = [
    "name",
    "company",
    "payroll_month",
    "payroll_year",
    "from_date",
    "to_date",
    "attendance_period",
    "status",
    "total_employees",
    "total_gross_pay",
    "total_deductions",
    "total_net_pay",
    "payroll_entry",
    "generate_errors",
    "vn_auto_created",
]

_LINE_FIELDS = [
    "name",
    "payroll_review_period",
    "employee",
    "employee_name",
    "department",
    "branch",
    "company",
    "status",
    "base_salary",
    "hourly_rate",
    "worked_hours",
    "bracket_hours_1",
    "bracket_hours_1_2",
    "bracket_hours_1_5",
    "payable_days",
    "regular_hours",
    "overtime_hours",
    "leave_days",
    "absent_days",
    "need_review_days",
    "worked_days",
    "overtime_amount",
    "night_allowance_amount",
    "allowance_amount",
    "late_penalty_amount",
    "unpaid_leave_deduction",
    "salary_advance_deduction",
    "other_deduction",
    "gross_pay",
    "total_deduction",
    "net_pay",
    "formula_net",
    "manual_adjustment_total",
    "salary_slip",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _assert_closer() -> None:
    """Only HR Manager / Payroll Manager / System Manager may run closing ops."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_MANAGER_ROLES):
        frappe.throw(
            _("Bạn không có quyền thực hiện chốt kỳ lương."),
            frappe.PermissionError,
        )


def _get_period(name: str):
    try:
        return frappe.get_doc(PERIOD_DOCTYPE, name)
    except Exception:
        frappe.throw(_("Kỳ lương {0} không tồn tại.").format(name))


def _claim_period_for_calculation(period_name: str) -> None:
    """Atomic claim: only one request at a time may run the calculation loop.

    Without this, two concurrent "Tính lương" requests both read status=Draft,
    both loop employees, and both insert review lines → duplicate pay per
    employee (double Salary Slip). Guarded UPDATE: the loser sees 0 rows and
    aborts before touching any line.

    Crash-recovery (E2E 2026-08-20): a run that throws after claiming leaves
    the period stuck at 'Calculating'. A guarded ``SET status='Calculating'``
    on a row already 'Calculating' reports 0 changed rows (MySQL), which used
    to wedge the period FOREVER ("đang được tính") even though the WHERE
    clause explicitly allowed re-claiming it. So on a zero-row update we read
    the status: 'Calculating' (stale claim, owner gone) is re-entered; any
    terminal state (Approved/Slips Generated/Published/…) still throws.
    """
    claimed = _db.guarded_update(
        "UPDATE `tabVN Payroll Review Period` SET status = 'Calculating'"
        " WHERE name = %(name)s AND status IN ('Draft', 'Calculated')",
        {"name": period_name},
    )
    if not claimed:
        status = None
        try:
            status = frappe.db.get_value("VN Payroll Review Period", period_name, "status")
        except Exception:
            status = None
        claimed = status == "Calculating"  # stale claim from a crashed run
        if not claimed and status == "Published":
            # Adjustment loop (2026-08 ack/pay plan): a PUBLISHED period with
            # ≥1 Requested slip and NO confirmed lock may be REOPENED so HR
            # can fix the line, recalculate and republish a fresh slip (the
            # regenerated draft replaces the old one; the employee confirms
            # the corrected amounts). Without this the fix path is dead — the
            # terminal Published state blocked every recalculation.
            if (
                calc.period_has_adjustment_requests(period_name) > 0
                and calc.period_has_confirmed_slips(period_name) == 0
            ):
                claimed = _db.guarded_update(
                    "UPDATE `tabVN Payroll Review Period` SET status = 'Calculating'"
                    " WHERE name = %(name)s AND status = 'Published'",
                    {"name": period_name},
                )
    frappe.db.commit()
    if not claimed:
        frappe.throw(
            _("Kỳ lương đang được tính hoặc đã chốt — không thể tính lại lúc này."),
            frappe.ValidationError,
        )


def _assert_period_not_confirmed(period) -> None:
    """2026-08 ack/pay lock (user decision #2): once ANY slip of the period is
    employee-confirmed (Awaiting Payment / Paid) the WHOLE period is frozen —
    calculate / approve / generate / publish must all refuse so the amounts
    the employee acknowledged can never change. Adjustment-``Requested``
    slips do NOT lock (that is the fix-and-republish loop)."""
    locked = calc.period_has_confirmed_slips(period.name)
    if locked:
        frappe.throw(
            _(
                "Kỳ đã có {0} phiếu lương được nhân viên xác nhận — không thể tính lại hay chỉnh sửa."
            ).format(locked),
            frappe.ValidationError,
        )


def _resolve(employee: str | None) -> str:
    if employee:
        # The SPA may echo the whole Employee object; coerce to its name string
        # so it is safe for filters and for the self-access equality check.
        return emp_utils.emp_name(employee)
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(
            _("Tài khoản này chưa được liên kết với nhân viên."),
            frappe.PermissionError,
        )
    return emp


def _period_employees(company: str, from_date, to_date) -> list[dict]:
    """Distinct active employees who have work sessions in the window."""
    try:
        rows = frappe.db.sql(
            """
            SELECT DISTINCT e.name AS employee, e.employee_name,
                   e.department, e.branch, e.company
            FROM `tabEmployee` e
            INNER JOIN `tabVN Attendance Work Session` ws
                ON ws.employee = e.name
            WHERE e.company = %(company)s
              AND e.status = 'Active'
              AND ws.docstatus < 2
              AND ws.work_date BETWEEN %(from_date)s AND %(to_date)s
            """,
            {
                "company": company,
                "from_date": from_date,
                "to_date": to_date,
            },
            as_dict=True,
        )
    except Exception:
        # Fallback: list active employees of the company if WS table missing.
        try:
            rows = frappe.db.get_all(
                "Employee",
                filters={"company": company, "status": "Active"},
                fields=["name as employee", "employee_name", "department", "branch", "company"],
            )
        except Exception:
            rows = []
    return rows or []


def _employee_work_sessions(employee: str, from_date, to_date) -> list[dict]:
    """Work Session rows for an employee in a date window, shaped for
    :func:`utils.payroll.aggregate_work_sessions`.

    Two bugs previously made EVERY payroll ``gross_pay = 0`` (the surrounding
    ``except: return []`` silently swallowed them, so the aggregator always got
    an empty list):

    1. ``frappe.db.get_all(..., as_dict=True)`` — this Frappe version's
       ``DatabaseQuery.execute`` rejects ``as_dict`` → ``TypeError``. ``get_all``
       with an explicit ``fields=[...]`` already returns ``frappe._dict`` rows,
       so the kwarg is both invalid and unnecessary.
    2. The WS doctype has no ``overtime_normal_hours`` column (normal OT lives in
       ``approved_overtime_hours``). Query the real column and remap the key.
    """
    try:
        rows = (
            frappe.db.get_all(
                WORK_SESSION_DOCTYPE,
                filters={
                    "employee": employee,
                    "docstatus": ["<", 2],
                    "work_date": ["between", [from_date, to_date]],
                },
                fields=[
                    "payable_day",
                    "regular_hours",
                    "regular_night_hours",
                    "raw_overtime_hours",
                    "approved_overtime_hours",
                    "overtime_night_hours",
                    "overtime_holiday_hours",
                    "late_minutes",
                    "early_leave_minutes",
                    "absent",
                ],
            )
            or []
        )
    except Exception:
        return []
    for row in rows:
        # aggregator reads `overtime_normal_hours`; map from the real column.
        row["overtime_normal_hours"] = row.get("approved_overtime_hours") or 0.0
    return rows


# --------------------------------------------------------------------------- #
# Endpoints — listing & detail
# --------------------------------------------------------------------------- #
# Broad-search fields for a payroll review period (DNA §6.6 D — HR-BL-10):
# OR-combined free text across the period's identifying/text columns. Numeric
# totals are included so a free-text "500" also matches displayed amounts.
# (``from_date``/``to_date`` are DATE — not Datetime — so ``like`` is safe;
# DNA §6.6 A only forbids ``like`` on Datetime/timestamp fields.)
_PERIOD_SEARCH_FIELDS = (
    "name",
    "company",
    "payroll_month",
    "payroll_year",
    "status",
    "from_date",
    "to_date",
    "total_net_pay",
    "total_employees",
)


def _period_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text payroll-period search."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [[field, "like", like] for field in _PERIOD_SEARCH_FIELDS]


@frappe.whitelist()
def periods(
    company: str | None = None,
    year: str | None = None,
    search: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Plan §10.8 — list payroll review periods, optionally filtered.

    ``status`` is an exact match (Law #2). ``search`` is a broad free-text
    search (Law #3) OR-combined across the period's columns (DNA §6.6).
    """
    _assert_closer()
    filters = {}
    if company:
        filters["company"] = company
    if year:
        filters["payroll_year"] = year
    if status:
        filters["status"] = status
    try:
        return frappe.db.get_all(
            PERIOD_DOCTYPE,
            filters=filters,
            or_filters=_period_search_or_filters(search),
            fields=_PERIOD_FIELDS,
            order_by="payroll_year desc, payroll_month desc",
        )
    except Exception:
        return []


# Broad-search fields for a review period's lines (DNA §6.6 A/D — HR-BL-05):
# OR-combined free text across the line's identifying columns + the numeric
# columns actually shown in the table (gross_pay / total_deduction / net_pay /
# payable_days / overtime_hours) so a free-text "5000" also matches an amount
# or an OT-hour figure (DNA §6.6 A — `like` is safe on numeric columns; only
# Datetime/timestamp fields are forbidden).
_REVIEW_LINE_SEARCH_FIELDS = (
    "employee",
    "employee_name",
    "department",
    "branch",
    "name",
    "gross_pay",
    "total_deduction",
    "net_pay",
    "payable_days",
    "overtime_hours",
)


def _review_line_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text payroll-line search."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [[field, "like", like] for field in _REVIEW_LINE_SEARCH_FIELDS]


def _append_range(filters: list, field: str, low, high) -> None:
    """Append numeric range clauses ``[field, ">=", low]`` / ``[field, "<=", high]``.

    Empty / None bounds are skipped; the bound is coerced to ``float`` so a stray
    empty string from the SPA never reaches the query. Two separate clauses per
    field (NOT ``between``) so a min–max on the same column is honoured — a
    dict-form filter cannot express that (DNA §6.6 B).
    """
    if low not in (None, ""):
        try:
            filters.append([field, ">=", float(low)])
        except (TypeError, ValueError):
            pass
    if high not in (None, ""):
        try:
            filters.append([field, "<=", float(high)])
        except (TypeError, ValueError):
            pass


@frappe.whitelist()
def review_detail(
    name: str | None = None,
    search: str | None = None,
    status: str | None = None,
    department: str | None = None,
    branch: str | None = None,
    gross_min: float | None = None,
    gross_max: float | None = None,
    deduction_min: float | None = None,
    deduction_max: float | None = None,
    net_min: float | None = None,
    net_max: float | None = None,
) -> dict:
    """Plan §10.8 — a period plus its review lines (doctype-design §21).

    ``search`` OR-matches a free-text query across the line's text + numeric
    columns (DNA §6.6 A/D). The remaining kwargs narrow the lines server-side
    (status / department / branch exact; gross / deduction / net ranges). All
    applied server-side (DNA §6.6, HR-BL-05) so the SPA never filters an
    already-loaded line list client-side. Numeric ranges use two separate list
    filters per field, not ``between`` (DNA §6.6 B).
    """
    _assert_closer()
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã kỳ lương."))

    try:
        period = frappe.db.get_value(PERIOD_DOCTYPE, name, _PERIOD_FIELDS, as_dict=True)
    except Exception:
        period = None
    if not period:
        frappe.throw(_("Kỳ lương {0} không tồn tại.").format(name))

    # List-form filters (DNA §6.6 B) — a dict cannot hold two range conditions
    # on the same field, so ranges are appended as separate clauses.
    filters = [
        ["payroll_review_period", "=", name],
        ["docstatus", "<", 2],
    ]
    if status:
        filters.append(["status", "=", status])
    if department:
        filters.append(["department", "=", department])
    if branch:
        filters.append(["branch", "=", branch])
    _append_range(filters, "gross_pay", gross_min, gross_max)
    _append_range(filters, "total_deduction", deduction_min, deduction_max)
    _append_range(filters, "net_pay", net_min, net_max)

    or_filters = _review_line_search_or_filters(search)
    try:
        lines = frappe.db.get_all(
            LINE_DOCTYPE,
            filters=filters,
            or_filters=or_filters,
            fields=_LINE_FIELDS,
            order_by="employee_name asc",
        )
    except Exception:
        lines = []
    return {"period": period, "lines": lines}


@frappe.whitelist()
def review_line_filter_options(name: str | None = None) -> dict:
    """Distinct ``department`` / ``branch`` values of a period's review lines
    (DNA §6.3 / §6.4 step 2) so the gear-popover ``SearchableSelect``s are never
    empty. Status is a fixed enum (hard-coded in the SPA), so only the dimension
    columns that need distinct-value discovery are fetched here.
    """
    name = (name or "").strip()
    base: dict = {"departments": [], "branches": []}
    if not name:
        return base
    flt = {"payroll_review_period": name, "docstatus": ["<", 2]}
    for key, field in (("departments", "department"), ("branches", "branch")):
        try:
            rows = frappe.db.get_all(
                LINE_DOCTYPE,
                filters=flt,
                fields=[field],
                distinct=True,
            )
            base[key] = sorted(r[field] for r in rows if r.get(field))
        except Exception:
            base[key] = []
    return base


# --------------------------------------------------------------------------- #
# Endpoints — lifecycle (closing operations)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_payroll_review(**kwargs) -> dict:
    """Plan §10.8 — create a Draft VN Payroll Review Period.

    Accepts: ``company``, ``payroll_month``, ``payroll_year``, ``from_date``,
    ``to_date`` (optional ``attendance_period``). Returns ``{ name, status,
    message }``.
    """
    _assert_closer()
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu tạo kỳ lương."))

    company = (kwargs.get("company") or "").strip()
    month = (kwargs.get("payroll_month") or "").strip()
    year = kwargs.get("payroll_year")
    from_date = kwargs.get("from_date")
    to_date = kwargs.get("to_date")
    attendance_period = (kwargs.get("attendance_period") or "").strip()

    if not (company and month and year and from_date and to_date):
        frappe.throw(_("Thiếu thông tin kỳ lương (công ty/tháng/năm/ngày)."))

    # Prevent duplicate (company, month, year).
    try:
        dup = frappe.db.exists(
            PERIOD_DOCTYPE,
            {
                "company": company,
                "payroll_month": month,
                "payroll_year": year,
                "docstatus": ["<", 2],
            },
        )
    except Exception:
        dup = None
    if dup:
        frappe.throw(_("Đã tồn tại kỳ lương {0}-{1} cho công ty này.").format(month, year))

    doc = frappe.new_doc(PERIOD_DOCTYPE)
    doc.update(
        {
            "company": company,
            "payroll_month": month,
            "payroll_year": year,
            "from_date": from_date,
            "to_date": to_date,
            "attendance_period": attendance_period or None,
            "status": "Draft",
        }
    )
    doc.insert()
    return {
        "name": doc.name,
        "status": doc.status,
        "message": _("Đã tạo kỳ lương {0}.").format(doc.name),
    }


@frappe.whitelist()
def calculate_payroll_review(name: str | None = None) -> dict:
    """Plan §10.8 — compute review lines + period totals, set status Calculated.

    For each employee with work sessions in the window this:
      1. aggregates their VN Attendance Work Session rows,
      2. computes the salary breakdown via :mod:`utils.payroll`,
      3. upserts a VN Payroll Review Line,
      4. rolls the lines up into the period totals.

    Returns ``{ name, status, total_employees, total_gross_pay,
    total_deductions, total_net_pay, message }``.
    """
    _assert_closer()
    period = _get_period(name)
    _assert_period_not_confirmed(period)
    _claim_period_for_calculation(period.name)

    # WP2 (PR6): BLOCK calculation while checkout-miss tickets are still
    # Pending — their penalty/waive outcome isn't final, so any pay computed
    # now could be wrong. Wrong pay costs far more than a short wait.
    pending = calc.pending_checkout_miss_tickets(period.company, period.from_date, period.to_date)
    if pending:
        frappe.throw(
            _("Kỳ công còn {0} ticket quên checkout chờ xử lý (Pending) — xử lý xong mới tính lương được.").format(pending),
            frappe.ValidationError,
        )

    # Advance requests still Approved block the run (2026-08 rule): an
    # advance is deducted from the payroll period containing its posting
    # date (disbursed the next month) and the deduction only materialises
    # at Paid — calculating the period early would silently drop it, and the
    # NEXT period's window can never pick it up again.
    pending_adv = calc.pending_advance_requests(
        period.company, period.from_date, period.to_date
    )
    if pending_adv:
        frappe.throw(
            _(
                "Kỳ còn {0} yêu cầu ứng lương Approved chưa ghi nhận thanh toán — xử lý xong mới tính lương được."
            ).format(pending_adv),
            frappe.ValidationError,
        )

    # Load configurable settings ONCE (plan: payroll-hourly-rate-design).
    time_brackets = calc.load_time_brackets()
    deduction_rates = calc.load_deduction_rates()
    penalty_rules = calc.load_penalty_rules(period.company)

    employees = _period_employees(period.company, period.from_date, period.to_date)
    emp_names = [e["employee"] for e in employees]
    advance_ded = calc.employee_advance_deductions(
        period.company, emp_names, period.from_date, period.to_date
    )

    line_amounts: list[dict] = []
    for emp in employees:
        emp_id = emp["employee"]
        hourly_rate = calc.resolve_hourly_rate(emp_id, period.to_date)

        # Split IN/OUT checkin pairs by time brackets → {coeff: hours}.
        bracket_hours = _employee_bracket_hours(
            emp_id, period.from_date, period.to_date, time_brackets
        )

        # Late minutes (from Attendance late_entry rows) → penalty.
        late_minutes_list = _employee_late_minutes(
            emp_id, period.from_date, period.to_date
        )
        # daily_rate = 8 standard hours × hourly rate (basis for Percentage /
        # Half-Day / Full-Day penalty rules — M4).
        late_penalty = calc.compute_late_penalty(
            late_minutes_list, penalty_rules, daily_rate=hourly_rate * 8.0
        )

        # Checkout-miss penalty: Σ penalty_amount of Penalised (non-waived) tickets this period.
        checkout_miss_penalty = calc.load_checkout_miss_penalty(
            emp_id, period.from_date, period.to_date
        )

        # Load Salary Structure allowances + extra deductions (fixed amounts).
        allowances, extra_deductions = _employee_salary_components(emp_id)

        amounts = calc.compute_hourly_line(
            bracket_hours=bracket_hours,
            hourly_rate=hourly_rate,
            deduction_rates=deduction_rates,
            late_penalty=late_penalty,
            checkout_miss_penalty=checkout_miss_penalty,
            salary_advance_deduction=advance_ded.get(emp_id, 0.0),
            allowances=allowances,
            extra_deductions=extra_deductions,
        )

        # WP2 (F-LC13): REAL hours/days from the Work-Session engine — the
        # line's regular/OT hours and payable days must reflect actual work
        # sessions (approved OT, approved leave, absent), never a flat base.
        summary = calc.load_employee_period_summary(emp_id, period.from_date, period.to_date)
        amounts["regular_hours"] = summary["regular_hours"]
        amounts["overtime_hours"] = summary["overtime_hours"]
        amounts["payable_days"] = summary["payable_days"]
        amounts["leave_days"] = summary["leave_days"]
        amounts["absent_days"] = summary["absent_days"]
        amounts["need_review_days"] = summary["need_review_days"]
        amounts["worked_days"] = json.dumps(
            {
                "worked": summary["worked_days"],
                "leave": summary["leave_days"],
                "absent": summary["absent_days"],
                "need_review": summary["need_review_days"],
            }
        )

        _upsert_line(period.name, emp, amounts)
        line_amounts.append(amounts)

    totals = calc.rollup_lines(line_amounts)
    period.db_set(
        {
            "status": "Calculated",
            "total_employees": totals["total_employees"],
            "total_gross_pay": totals["total_gross_pay"],
            "total_deductions": totals["total_deductions"],
            "total_net_pay": totals["total_net_pay"],
        }
    )
    audit_api.log(
        "Payroll Calculate",
        doc=period.as_dict(),
        description="{} dòng — gross {} / net {}".format(
            totals["total_employees"], totals["total_gross_pay"], totals["total_net_pay"]
        ),
    )
    return {
        "name": period.name,
        "status": "Calculated",
        "total_employees": totals["total_employees"],
        "total_gross_pay": totals["total_gross_pay"],
        "total_deductions": totals["total_deductions"],
        "total_net_pay": totals["total_net_pay"],
        "message": _("Đã tính {0} dòng lương.").format(totals["total_employees"]),
    }


@frappe.whitelist()
def delete_payroll_review(name: str | None = None) -> dict:
    """Delete a review period (and its lines) so it can be recalculated.

    Allowed only while the period is ``Draft``/``Calculated`` — once Approved
    or with salary slips generated, the payroll is effective and must not be
    silently removed. Deleting does NOT touch the locked attendance period:
    re-locking the month (unlock → lock) recreates a fresh Draft review.
    """
    _assert_closer()
    period = _get_period(name)

    if period.status not in ("Draft", "Calculated"):
        frappe.throw(
            _("Chỉ kỳ lương Draft/Calculated mới được xoá để tính lại (kỳ hiện tại: {0}).").format(
                period.status
            )
        )

    lines = frappe.db.get_all(
        LINE_DOCTYPE,
        filters={"payroll_review_period": name, "docstatus": ["<", 2]},
        fields=["name", "salary_slip"],
    )
    with_slip = [ln for ln in lines if ln.salary_slip]
    if with_slip:
        frappe.throw(
            _("Kỳ lương đã sinh {0} payslip, không thể xoá.").format(len(with_slip))
        )

    for ln in lines:
        try:
            if frappe.db.get_value(LINE_DOCTYPE, ln.name, "docstatus") == 1:
                frappe.get_doc(LINE_DOCTYPE, ln.name).cancel()
            frappe.delete_doc(LINE_DOCTYPE, ln.name, ignore_permissions=True, force=True)
        except Exception:
            frappe.db.delete(LINE_DOCTYPE, {"name": ln.name})
    frappe.db.delete(LINE_DOCTYPE, {"payroll_review_period": name})  # orphan sweep

    if period.docstatus == 1:
        period.cancel()
    period_dict = period.as_dict()
    frappe.delete_doc(PERIOD_DOCTYPE, name, ignore_permissions=True, force=True)

    audit_api.log(
        "Manual Override",
        doc=period_dict,
        description="Xoá kỳ lương {} ({} dòng) để tính lại".format(name, len(lines)),
        old_value="Calculated" if period_dict.get("status") == "Calculated" else "Draft",
        new_value=None,
    )
    return {
        "name": name,
        "deleted_lines": len(lines),
        "message": _("Đã xoá kỳ lương {0} ({1} dòng). Khoá lại kỳ công để tạo kỳ lương mới.").format(
            name, len(lines)
        ),
    }


def _employee_bracket_hours(employee: str, from_date, to_date, brackets: list[dict]) -> dict:
    """Split the employee's IN/OUT checkin pairs into ``{coeff: hours}``.

    OT-approval model: regular hours (IN → planned_end) are always counted.
    Hours beyond planned_end are only counted when an approved VN Overtime
    Request exists for that work_date — otherwise the OUT is capped at
    planned_end so unpaid OT is excluded from gross.
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    VN = ZoneInfo("Asia/Ho_Chi_Minh")
    UTC = ZoneInfo("UTC")
    # H4: logs are stored UTC while the period bounds are portal (VN) dates.
    # A VN shift touching either boundary (e.g. 06:00 VN on from_date =
    # 23:00 UTC the day before) fell outside the old [00:00, 23:59] window
    # and its whole pair was dropped from payroll. Widen one day each side;
    # each pair is later stamped with its VN work_date so nothing leaks in.
    win_start = (_dt.strptime(str(from_date), "%Y-%m-%d") - _td(days=1)).strftime("%Y-%m-%d 00:00:00")
    win_end = (_dt.strptime(str(to_date), "%Y-%m-%d") + _td(days=1)).strftime("%Y-%m-%d 23:59:59")
    try:
        rows = frappe.db.get_all(
            "Employee Checkin",
            filters={"employee": employee, "time": ["between", [win_start, win_end]]},
            fields=["time", "log_type"],
            order_by="time asc",
        )
    except Exception:
        return {}

    # Parse + sort logs chronologically (UTC).
    logs: list[tuple] = []
    for r in rows:
        try:
            t = _dt.strptime(str(r["time"])[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo("UTC")
            )
        except Exception:
            continue
        logs.append((t, (r.get("log_type") or "").strip().upper()))
    logs.sort(key=lambda x: x[0])

    # --- Shift windows per date (H1) ---------------------------------------
    # The old lookup took ONE currently-Active assignment and applied its
    # window to every log of the period — anyone who changed shifts mid-period
    # had the wrong planned_end (wrong OT cap) on the other half of the month.
    # Resolve the assignment effective on each VN date instead.
    from frappe.utils import get_time

    _shift_cache: dict = {}

    def _shift_for(vn_date):
        if vn_date in _shift_cache:
            return _shift_cache[vn_date]
        st_name = None
        try:
            # Effective assignment: started on/before the date, not yet ended.
            st_name = frappe.db.get_value(
                "Shift Assignment",
                {
                    "employee": employee,
                    "docstatus": 1,
                    "status": "Active",
                    "start_date": ["<=", vn_date],
                    "or": [["end_date", "is", "not set"], ["end_date", ">=", vn_date]],
                },
                "shift_type",
            )
        except Exception:
            st_name = None
        window = (None, None)
        if st_name:
            try:
                s = get_time(frappe.db.get_value("Shift Type", st_name, "start_time"))
                e = get_time(frappe.db.get_value("Shift Type", st_name, "end_time"))
                window = (s, e)
            except Exception:
                window = (None, None)
        _shift_cache[vn_date] = window
        return window

    # --- Approved OT dates (VN Overtime Request submitted/docstatus=1) -----
    approved_ot: set[str] = set()
    try:
        for r in frappe.db.get_all(
            "VN Overtime Request",
            filters={
                "employee": employee,
                "docstatus": 1,
                # C4: submitted-but-Rejected requests were still paying OT —
                # match the WS engine's approved-set exactly.
                "workflow_state": ["in", ["Approved", "Confirmed"]],
            },
            fields=["work_date"],
        ):
            if r.get("work_date"):
                approved_ot.add(str(r["work_date"]))
    except Exception:
        pass

    def _planned_end_wall(vn_date):
        """PHASE-1 FRAME: naive PORTAL WALL datetime of the shift's planned_end
        effective on that VN date (logs are wall — compare naive↔naive)."""
        s_time, e_time = _shift_for(vn_date)
        if not e_time:
            return None
        end_vn = _dt.combine(vn_date, e_time)
        if s_time and e_time <= s_time:
            end_vn += _td(days=1)  # overnight shift (end < start)
        return end_vn

    # --- Pair IN→OUT, cap at planned_end, add OT only if approved ---------
    def _flush(cur_in, out_t):
        """Count one IN→OUT pair (regular capped at planned_end; OT only if
        approved). An absurd span (>16h — forgotten checkout over multiple
        days) is capped at planned_end instead of dropping the whole shift
        (M6), mirroring the checkout-miss policy of the WS engine."""
        span_h = (out_t - cur_in).total_seconds() / 3600.0
        if span_h <= 0:
            return
        try:
            vn_date = (cur_in.astimezone(VN) if cur_in.tzinfo else cur_in).date()
            pe = _planned_end_wall(vn_date)
        except Exception:
            pe = None
        if span_h > 16.0 and pe:
            out_t = pe  # cap runaway pair at the shift's planned end
        # Regular hours: IN → min(OUT, planned_end). Always counted.
        regular_out = min(out_t, pe) if pe else out_t
        split = calc.split_hours_by_bracket(cur_in, regular_out, brackets)
        for coeff, hours in split.items():
            bracket_hours[coeff] = bracket_hours.get(coeff, 0.0) + hours
        # OT hours: planned_end → actual OUT. Only if approved.
        if pe and out_t > pe and str(vn_date) in approved_ot:
            ot_split = calc.split_hours_by_bracket(pe, out_t, brackets)
            for coeff, hours in ot_split.items():
                bracket_hours[coeff] = bracket_hours.get(coeff, 0.0) + hours

    bracket_hours: dict[float, float] = {}
    cur_in = None
    for t, lt in logs:
        if lt == "IN":
            if cur_in is not None:
                # C3: previous shift never checked out — a second IN used to
                # silently DISCARD it (a full lost day). Close it at the
                # shift's planned end (checkout-miss policy A), no OT.
                try:
                    vn_date = (cur_in.astimezone(VN) if cur_in.tzinfo else cur_in).date()
                    pe = _planned_end_wall(vn_date)
                except Exception:
                    pe = None
                _flush(cur_in, pe or t)
            cur_in = t
        elif lt == "OUT" and cur_in is not None and t > cur_in:
            _flush(cur_in, t)
            cur_in = None
    # Trailing IN with no OUT at all: close at planned_end too.
    if cur_in is not None:
        try:
            vn_date2 = (cur_in.astimezone(VN) if cur_in.tzinfo else cur_in).date()
            pe2 = _planned_end_wall(vn_date2)
        except Exception:
            pe2 = None
        _flush(cur_in, pe2 or (cur_in + _td(hours=8)))
    return bracket_hours


def _employee_late_minutes(employee: str, from_date, to_date) -> list[float]:
    """Minutes-late per shift-day from the Work-Session engine (H2).

    The old source (``Attendance.late_entry``) is never set by any production
    flow in this app — only seed scripts wrote it — so late penalties were
    always computed from an empty list. The Work-Session engine already
    computes ``late_minutes`` WITH the policy grace window and overnight
    handling; read it straight from there."""
    try:
        rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters={
                "employee": employee,
                "work_date": ["between", [from_date, to_date]],
                "docstatus": ["<", 2],
            },
            fields=["late_minutes"],
        )
    except Exception:
        return []
    return [float(r.late_minutes or 0) for r in rows if float(r.late_minutes or 0) > 0]



def _employee_salary_components(employee: str) -> tuple[list[float], list[float]]:
    """Fixed-amount earnings (allowances) + deductions from Salary Structure.

    Reads the employee's active Salary Structure Assignment → the linked Salary
    Structure's ``earnings`` / ``deductions`` child tables. Only rows with a
    fixed ``amount`` (not formula-based) are returned — those are treated as
    allowances / extra deductions that supplement the hourly-rate gross.

    Returns ``(allowances, extra_deductions)`` — each a list of float amounts.
    """
    try:
        st_name = frappe.db.get_value(
            "Salary Structure Assignment",
            {"employee": employee, "docstatus": 1},
            "salary_structure",
            order_by="from_date desc",
        )
    except Exception:
        st_name = None
    if not st_name:
        return [], []
    try:
        st = frappe.get_doc("Salary Structure", st_name)
    except Exception:
        return [], []

    allowances: list[float] = []
    for row in (st.earnings or []):
        if not row.amount_based_on_formula and row.amount:
            allowances.append(float(row.amount))
    extra_deductions: list[float] = []
    for row in (st.deductions or []):
        if not row.amount_based_on_formula and row.amount:
            extra_deductions.append(float(row.amount))
    return allowances, extra_deductions


def _upsert_line(period_name: str, emp: dict, amounts: dict) -> None:
    """Insert or replace a review line for (period, employee)."""
    try:
        existing = frappe.db.get_value(
            LINE_DOCTYPE,
            {
                "payroll_review_period": period_name,
                "employee": emp["employee"],
                "docstatus": ["<", 2],
            },
            "name",
        )
    except Exception:
        existing = None

    payload = {
        "payroll_review_period": period_name,
        "employee": emp["employee"],
        "employee_name": emp.get("employee_name"),
        "department": emp.get("department"),
        "branch": emp.get("branch"),
        "company": emp.get("company"),
        "status": "Confirmed" if not existing else None,
    }
    payload.update(amounts)
    # The auto-computed net is the "formula net" — net BEFORE any manual
    # adjustments. On recalc we keep existing manual_adjustments (child rows)
    # and re-derive net = formula_net + Σ(adjustments).
    formula_net = float(payload.get("net_pay") or 0)

    if existing:
        doc = frappe.get_doc(LINE_DOCTYPE, existing)
        doc.update({k: v for k, v in payload.items() if k != "status"})
        doc.formula_net = formula_net
        _apply_manual_adjustments(doc)
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc(LINE_DOCTYPE)
        doc.update(payload)
        doc.formula_net = formula_net
        doc.manual_adjustment_total = 0
        doc.net_pay = formula_net
        doc.insert(ignore_permissions=True)


# --------------------------------------------------------------------------- #
# Manual adjustments (bonus / penalty / deduction) — line-detail popup + CRUD
# --------------------------------------------------------------------------- #
_ADJUSTMENT_FIELDS = [
    "name",
    "adjustment_type",
    "description",
    "amount",
    "signed_amount",
    "added_by",
    "added_on",
    "note",
]


def _apply_manual_adjustments(doc) -> None:
    """Recompute ``manual_adjustment_total`` and ``net_pay`` from the line's
    child ``manual_adjustments`` rows. ``formula_net`` (auto net) must be set
    first; ``net_pay = formula_net + Σ(signed_amount)``."""
    formula_net = float(doc.get("formula_net") or doc.get("net_pay") or 0)
    total = 0.0
    for row in (doc.manual_adjustments or []):
        amt = float(row.amount or 0)
        row.signed_amount = amt if row.adjustment_type == "Bonus" else -amt
        total += float(row.signed_amount or 0)
    doc.manual_adjustment_total = total
    doc.net_pay = formula_net + total


def _parse_worked_days(raw) -> dict:
    """WP2 — parse the line's worked-days JSON (blank/invalid → all zeros)."""
    if not raw:
        return {"worked": 0, "leave": 0, "absent": 0, "need_review": 0}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return {
            "worked": int(parsed.get("worked") or 0),
            "leave": float(parsed.get("leave") or 0),
            "absent": int(parsed.get("absent") or 0),
            "need_review": int(parsed.get("need_review") or 0),
        }
    except Exception:
        return {"worked": 0, "leave": 0, "absent": 0, "need_review": 0}


def _line_breakdown(doc) -> dict:
    """Build the formula-breakdown dict for the line-detail popup."""
    hourly_rate = float(doc.get("hourly_rate") or 0)
    brackets = [
        {"coeff": 1.0, "hours": float(doc.get("bracket_hours_1") or 0), "label": "Thường (1.0×)"},
        {"coeff": 1.2, "hours": float(doc.get("bracket_hours_1_2") or 0), "label": "Tăng ca (1.2×)"},
        {"coeff": 1.5, "hours": float(doc.get("bracket_hours_1_5") or 0), "label": "OT / đêm (1.5×)"},
    ]
    for b in brackets:
        b["rate"] = hourly_rate
        b["amount"] = round(b["hours"] * hourly_rate * b["coeff"], 2)
    base_gross = round(sum(b["amount"] for b in brackets), 2)
    gross = float(doc.get("gross_pay") or 0)
    rates = calc.load_deduction_rates() or {}
    deductions_pct = [
        {"label": k, "rate_pct": float(v or 0), "amount": round(gross * float(v or 0) / 100.0, 2)}
        for k, v in rates.items()
    ]
    adjustments = [{k: r.get(k) for k in _ADJUSTMENT_FIELDS} for r in (doc.manual_adjustments or [])]
    return {
        "line": doc.name,
        "employee": doc.employee,
        "employee_name": doc.employee_name,
        "department": doc.department,
        "status": doc.status,
        "hourly_rate": hourly_rate,
        "worked_hours": float(doc.get("worked_hours") or 0),
        "brackets": brackets,
        # WP2 (F-LC13): real-hours breakdown from the Work-Session engine.
        "regular_hours": float(doc.get("regular_hours") or 0),
        "overtime_hours": float(doc.get("overtime_hours") or 0),
        "leave_days": float(doc.get("leave_days") or 0),
        "absent_days": int(doc.get("absent_days") or 0),
        "need_review_days": int(doc.get("need_review_days") or 0),
        "worked_days": _parse_worked_days(doc.get("worked_days")),
        "base_gross": base_gross,
        "allowance_amount": float(doc.get("allowance_amount") or 0),
        "gross_pay": gross,
        "deductions_pct": deductions_pct,
        "late_penalty": float(doc.get("late_penalty") or doc.get("late_penalty_amount") or 0),
        "salary_advance_deduction": float(doc.get("salary_advance_deduction") or 0),
        "unpaid_leave_deduction": float(doc.get("unpaid_leave_deduction") or 0),
        "other_deduction": float(doc.get("other_deduction") or 0),
        "total_deduction": float(doc.get("total_deduction") or 0),
        "formula_net": float(doc.get("formula_net") or 0),
        "adjustments": adjustments,
        "manual_adjustment_total": float(doc.get("manual_adjustment_total") or 0),
        "net_pay": float(doc.get("net_pay") or 0),
        "period": doc.payroll_review_period,
    }


def _recompute_period_totals(period_name: str) -> None:
    """Roll up gross/deduction/net totals on the period from its lines."""
    if not period_name:
        return
    try:
        agg = frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": period_name, "docstatus": ["<", 2]},
            fields=[
                "count(*) as n",
                "sum(gross_pay) as gross",
                "sum(total_deduction) as ded",
                "sum(net_pay) as net",
            ],
        )[0]
    except Exception:
        return
    frappe.db.set_value(
        PERIOD_DOCTYPE,
        period_name,
        {
            "total_employees": int(agg.get("n") or 0),
            "total_gross_pay": float(agg.get("gross") or 0),
            "total_deductions": float(agg.get("ded") or 0),
            "total_net_pay": float(agg.get("net") or 0),
        },
    )


def _assert_line_editable(doc) -> None:
    """Block adjustment edits once the period is closed/submitted."""
    period_status = frappe.db.get_value(PERIOD_DOCTYPE, doc.payroll_review_period, "status")
    if period_status in ("Approved", "Submitted") or int(doc.docstatus or 0) >= 1:
        frappe.throw(_("Kỳ đã chốt — không thể chỉnh sửa điều chỉnh."))


@frappe.whitelist()
def payroll_line_detail(line: str) -> dict:
    """Full formula breakdown + manual adjustments for the line-detail popup."""
    _assert_closer()
    line = (line or "").strip()
    if not line or not frappe.db.exists(LINE_DOCTYPE, line):
        frappe.throw(_("Dòng tính lương không tồn tại."))
    doc = frappe.get_doc(LINE_DOCTYPE, line)
    if not doc.has_permission("read"):
        frappe.throw(_("Không có quyền xem."), frappe.PermissionError)
    return _line_breakdown(doc)


@frappe.whitelist()
def save_payroll_adjustment(
    line: str,
    adjustment_type: str,
    description: str,
    amount,
    name: str | None = None,
    note: str | None = None,
) -> dict:
    """Create/update a manual adjustment (bonus/penalty/deduction) and
    recompute the line's net + the period totals."""
    _assert_closer()
    line = (line or "").strip()
    if not line or not frappe.db.exists(LINE_DOCTYPE, line):
        frappe.throw(_("Dòng tính lương không tồn tại."))
    if adjustment_type not in ("Bonus", "Penalty", "Deduction", "Other"):
        frappe.throw(_("Loại điều chỉnh không hợp lệ."))
    description = (description or "").strip()
    if not description:
        frappe.throw(_("Nội dung điều chỉnh là bắt buộc."))
    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        frappe.throw(_("Số tiền không hợp lệ."))
    if amount < 0:
        frappe.throw(_("Số tiền không được âm."))

    doc = frappe.get_doc(LINE_DOCTYPE, line)
    _assert_line_editable(doc)
    row = None
    if name:
        row = next((r for r in (doc.manual_adjustments or []) if r.name == name), None)
    if row is None:
        row = doc.append("manual_adjustments", {})
    row.adjustment_type = adjustment_type
    row.description = description
    row.amount = amount
    row.note = note
    row.added_by = frappe.session.user
    row.added_on = frappe.utils.now()
    doc.save(ignore_permissions=True)  # child validate → signed_amount
    _apply_manual_adjustments(doc)
    doc.save(ignore_permissions=True)
    _recompute_period_totals(doc.payroll_review_period)
    return _line_breakdown(doc)


@frappe.whitelist()
def delete_payroll_adjustment(line: str, name: str) -> dict:
    """Remove a manual adjustment and recompute the line + period."""
    _assert_closer()
    line = (line or "").strip()
    name = (name or "").strip()
    if not line or not name:
        frappe.throw(_("Thiếu dòng / điều chỉnh."))
    doc = frappe.get_doc(LINE_DOCTYPE, line)
    _assert_line_editable(doc)
    row = next((r for r in (doc.manual_adjustments or []) if r.name == name), None)
    if row is None:
        frappe.throw(_("Điều chỉnh không tồn tại."))
    doc.remove(row)
    _apply_manual_adjustments(doc)
    doc.save(ignore_permissions=True)
    _recompute_period_totals(doc.payroll_review_period)
    return _line_breakdown(doc)


# --------------------------------------------------------------------------- #
# Adjustment presets — "Mẫu điều chỉnh" managed in /hr/salary-structure (tab 6),
# quick-picked in the review-line popup so closers don't re-type every entry.
# --------------------------------------------------------------------------- #
_ADJUSTMENT_TYPES = ("Bonus", "Penalty", "Deduction", "Other")


def _load_adjustment_presets() -> list[dict]:
    """Parse ``VN HR Portal Setting.vn_adjustment_presets`` (JSON array of
    ``{adjustment_type, description, amount}``). Malformed rows are silently
    dropped — presets are a convenience, never a blocker."""
    import json

    raw = frappe.db.get_single_value("VN HR Portal Setting", "vn_adjustment_presets") or "[]"
    try:
        items = json.loads(raw)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    presets = []
    for it in items:
        if not isinstance(it, dict):
            continue
        adj_type = it.get("adjustment_type") or "Bonus"
        if adj_type not in _ADJUSTMENT_TYPES:
            continue
        description = (it.get("description") or "").strip()
        if not description:
            continue
        try:
            amount = float(it.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amount < 0:
            continue
        presets.append({"adjustment_type": adj_type, "description": description, "amount": amount})
    return presets


@frappe.whitelist()
def adjustment_presets() -> list[dict]:
    """Read-only preset list for the review-line popup's 'Chọn mẫu' dropdown.
    Guarded like ``payroll_line_detail`` — ``get_payroll_settings`` is
    HR-Manager-only but every closer role needs the presets."""
    _assert_closer()
    return _load_adjustment_presets()


@frappe.whitelist()
def approve_review(name: str | None = None, comment: str | None = None) -> dict:
    """Plan §10.8 — approve the review (all lines must be Confirmed/Approved)."""
    _assert_closer()
    period = _get_period(name)
    _assert_period_not_confirmed(period)

    if period.status not in ("Calculated", "Approved"):
        frappe.throw(
            _("Chỉ chốt kỳ đã tính mới được phê duyệt."),
            frappe.ValidationError,
        )

    try:
        draft_lines = frappe.db.count(
            LINE_DOCTYPE,
            filters={
                "payroll_review_period": period.name,
                "status": "Draft",
                "docstatus": ["<", 2],
            },
        )
    except Exception:
        draft_lines = 0
    if draft_lines:
        frappe.throw(
            _("Còn {0} dòng lương ở trạng thái Draft — cần xác nhận trước.").format(draft_lines),
            frappe.ValidationError,
        )

    # Proper Frappe flow: set status then save() so validate + on_update run (the
    # period's own hooks/audit fire). No db_set bypass (that skipped on_update).
    period.status = "Approved"
    period.save()
    audit_api.log("Payroll Approve", doc=period.as_dict(), description="Kỳ lương được phê duyệt")
    return {
        "name": period.name,
        "status": "Approved",
        "message": _("Đã phê duyệt kỳ lương {0}.").format(period.name),
    }


@frappe.whitelist()
def generate_salary_slips(name: str | None = None, retry_failed: int = 0) -> dict:
    """Plan §10.8 / WP1 — create a real Salary Slip per review line.

    For each line of an Approved (or already partially generated) period this
    builds a Salary Slip via the standard HRMS flow. Failures are NO LONGER
    silent (F-LC14b): every line that cannot produce a slip lands in
    ``failed_lines`` with a user-facing reason, is persisted to the period's
    ``generate_errors`` JSON field, and the period still advances so HR can
    retry just the broken lines via ``retry_failed=1`` (idempotent engine:
    an existing draft slip for the same employee+window is replaced first).

    Returns ``{ generated, failed, failed_lines, message }``.
    """
    _assert_closer()
    period = _get_period(name)
    _assert_period_not_confirmed(period)
    if period.status not in ("Approved", "Slips Generated"):
        frappe.throw(
            _("Kỳ lương phải ở trạng thái Approved trước khi tạo phiếu lương."),
            frappe.ValidationError,
        )

    component_map = calc.load_component_map(period.company)
    generated = 0
    failed_lines: list[dict] = []
    lines = (
        frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": period.name, "docstatus": ["<", 2]},
            fields=[
                "name",
                "employee",
                "employee_name",
                "salary_slip",
                "payable_days",
                "regular_hours",
                "overtime_hours",
                "overtime_amount",
                "night_allowance_amount",
                "late_penalty_amount",
                "salary_advance_deduction",
                "other_deduction",
                "checkout_miss_penalty",
                "total_deduction",
                "gross_pay",
                "net_pay",
            ],
        )
        or []
    )

    for ln in lines:
        # Retry mode only re-processes lines that have no slip yet (SL5).
        if retry_failed and ln.get("salary_slip"):
            continue
        try:
            slip_name = _generate_for_line(period, ln, component_map)
        except SlipGenerationError as exc:
            failed_lines.append(
                {
                    "employee": ln.get("employee"),
                    "employee_name": ln.get("employee_name"),
                    "line": ln.get("name"),
                    "code": exc.code,
                    "reason": exc.reason,
                }
            )
            try:
                frappe.log_error(
                    title="payroll slip generate failed",
                    message=f"{ln.get('employee')} {period.name}: {exc.reason}",
                )
            except Exception:
                pass
            continue
        except Exception as exc:  # unknown error — record, never abort the batch
            reason = str(exc) or exc.__class__.__name__
            failed_lines.append(
                {
                    "employee": ln.get("employee"),
                    "employee_name": ln.get("employee_name"),
                    "line": ln.get("name"),
                    "code": SLIP_CODE_GENERIC,
                    "reason": reason,
                }
            )
            try:
                frappe.log_error(
                    title="payroll slip generate failed",
                    message=f"{ln.get('employee')} {period.name}\n{frappe.get_traceback()}",
                )
            except Exception:
                pass
            continue
        generated += 1
        try:
            frappe.db.set_value(LINE_DOCTYPE, ln["name"], "salary_slip", slip_name)
        except Exception:
            pass

    # Persist the failure snapshot so the UI banner survives reloads (SL2/SL4).
    errors_payload = (
        json.dumps(
            {
                "at": frappe.utils.now(),
                "generated": generated,
                "failed_lines": failed_lines,
            },
            ensure_ascii=False,
        )
        if failed_lines
        else None
    )
    # Proper Frappe flow (see approve_review): save() runs validate + on_update.
    period.status = "Slips Generated"
    if period.meta.has_field("generate_errors"):
        period.generate_errors = errors_payload
    period.save()
    message = _("Đã tạo {0} phiếu lương.").format(generated)
    if failed_lines:
        message = _("Đã tạo {0} phiếu lương — {1} dòng lỗi, xem chi tiết.").format(
            generated, len(failed_lines)
        )
    return {
        "generated": generated,
        "failed": len(failed_lines),
        "failed_lines": failed_lines,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# WP-FIX-SSA — stable failure codes shared by slip generation, the persisted
# generate_errors banner and the ssa_preflight readiness check.
# --------------------------------------------------------------------------- #
SLIP_CODE_GENERIC = "SLIP_ERROR"
SLIP_CODE_LINE_NO_EMPLOYEE = "LINE_NO_EMPLOYEE"
SLIP_CODE_SSA_MISSING = "SSA_MISSING"
SLIP_CODE_SSA_DRAFT = "SSA_DRAFT"
SLIP_CODE_SSA_MID_PERIOD = "SSA_MID_PERIOD"
SLIP_CODE_SSA_FUTURE = "SSA_FUTURE"
SLIP_CODE_SSA_STRUCTURE_INVALID = "SSA_STRUCTURE_INVALID"
SLIP_CODE_SSA_STRUCTURE_INACTIVE = "SSA_STRUCTURE_INACTIVE"
SLIP_CODE_SSA_COMPANY_MISMATCH = "SSA_COMPANY_MISMATCH"


@frappe.whitelist()
def ssa_preflight(name: str | None = None) -> dict:
    """WP-FIX-SSA — pre-Approve readiness check for every review line.

    Runs the exact ``_resolve_ssa`` used at slip-generation time so HR can fix
    data BEFORE approving the period (non-blocking by design — the generate
    flow still collects per-line failures and supports ``retry_failed``).

    Returns ``{ ok, total, issues, message }`` where each issue is
    ``{ employee, employee_name, line, code, reason }``.
    """
    _assert_closer()
    period = _get_period(name)
    lines = (
        frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": period.name, "docstatus": ["<", 2]},
            fields=["name", "employee", "employee_name", "salary_slip"],
        )
        or []
    )
    issues: list[dict] = []
    total = 0
    for ln in lines:
        if ln.get("salary_slip"):
            continue
        total += 1
        try:
            if not ln.get("employee"):
                raise SlipGenerationError(
                    _("Dòng lương thiếu mã nhân viên — không thể sinh phiếu."),
                    code=SLIP_CODE_LINE_NO_EMPLOYEE,
                )
            _resolve_ssa(ln["employee"], period)
        except SlipGenerationError as exc:
            issues.append(
                {
                    "employee": ln.get("employee"),
                    "employee_name": ln.get("employee_name"),
                    "line": ln.get("name"),
                    "code": exc.code,
                    "reason": exc.reason,
                }
            )
    ok = total - len(issues)
    message = (
        _("Tất cả {0} dòng đã sẵn sàng SSA.").format(ok)
        if not issues
        else _("{0}/{1} dòng chưa sẵn sàng SSA — xem chi tiết trước khi phê duyệt.").format(
            len(issues), total
        )
    )
    return {"ok": ok, "total": total, "issues": issues, "message": message}


class SlipGenerationError(Exception):
    """WP1 (F-LC14b) — a slip could not be generated.

    ``reason`` carries a user-facing message (shown in the failed-lines banner)
    so generate failures are NEVER silent again. The old flow fell back to an
    OT-only Additional Salary and returned ``None`` — employees without SSA or
    with validation issues simply got no payslip and nobody knew.

    ``code`` (WP-FIX-SSA) is a stable machine-readable tag (``SLIP_CODE_*``)
    consumed by the UI banner and ``ssa_preflight`` so each failure mode can
    be rendered with its own guidance.
    """

    def __init__(self, reason: str, code: str = SLIP_CODE_GENERIC):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _slip_failure_reason(exc: Exception) -> str:
    """Distil a Frappe/HRMS validation error into a short user-facing reason."""
    raw = str(exc).strip()
    if not raw:
        raw = exc.__class__.__name__
    # Frappe wraps throw() messages; keep only the first line, max 200 chars.
    first = raw.splitlines()[0] if raw else raw
    return first[:200]


def _delete_existing_slip(employee: str, period) -> int:
    """Idempotency (SL3): remove this employee's existing draft slips for the
    period window before regenerating, so "generate lại" replaces instead of
    duplicating. Returns the number of removed slips."""
    removed = 0
    try:
        existing = frappe.db.get_all(
            "Salary Slip",
            filters={
                "employee": employee,
                "start_date": period.from_date,
                "end_date": period.to_date,
                "docstatus": 0,
            },
            pluck="name",
        )
    except Exception:
        return 0
    for slip_name in existing or []:
        try:
            frappe.delete_doc("Salary Slip", slip_name, ignore_permissions=True, force=True)
            removed += 1
        except Exception:
            try:
                frappe.db.delete("Salary Slip", {"name": slip_name})
                removed += 1
            except Exception:
                pass
    return removed


def _stamp_slip_totals(slip, period, line: dict, component_map: dict) -> None:
    """Override the HRMS-computed totals with the REVIEWED line amounts.

    The slip is inserted via the standard HRMS flow (SSA → earnings computed by
    HRMS); afterwards these ``db_set`` calls make gross/deduction/net EQUAL the
    review line HR approved — the review line is the source of truth for pay.
    An audit earnings row ``Lương kỳ VN`` is stamped so accountants can see the
    reviewed amount inside the slip's earnings table.
    """
    gross = float(line.get("gross_pay") or 0)
    total_ded = float(
        line.get("total_deduction")
        or (
            (line.get("late_penalty_amount") or 0)
            + (line.get("salary_advance_deduction") or 0)
            + (line.get("other_deduction") or 0)
            + (line.get("checkout_miss_penalty") or 0)
        )
        or 0
    )
    net = float(line.get("net_pay") or 0)

    from frappe.utils import flt

    try:
        slip.db_set("gross_pay", flt(gross, 2))
        slip.db_set("total_deduction", flt(total_ded, 2))
        slip.db_set("net_pay", flt(net, 2))
        # OT earnings row (SL7): an explicit component so the OT amount the
        # line approved is visible inside the slip's earnings table.
        ot_comp = component_map.get("OT")
        ot_amount = float(line.get("overtime_amount") or 0)
        if ot_comp and ot_amount > 0 and slip.meta.has_field("earnings"):
            try:
                slip.db_set("vn_overtime_amount", flt(ot_amount, 2))
            except Exception:
                pass
        # VN breakdown custom fields (best-effort; ignored when absent).
        for field, val in {
            "vn_payable_days": line.get("payable_days"),
            "vn_regular_hours": line.get("regular_hours"),
            "vn_overtime_hours": line.get("overtime_hours"),
            "vn_overtime_amount": line.get("overtime_amount"),
            "vn_late_penalty_amount": line.get("late_penalty_amount"),
            "vn_salary_advance_deduction": line.get("salary_advance_deduction"),
            "vn_payroll_review_period": period.name,
        }.items():
            try:
                slip.db_set(field, val)
            except Exception:
                pass
    except Exception:
        # A custom-field miss must not fail the whole slip — the core totals
        # were already stamped above in the same try block only when the FIRST
        # db_set succeeded; re-stamp the three core totals bare to be safe.
        try:
            slip.db_set("gross_pay", flt(gross, 2))
            slip.db_set("total_deduction", flt(total_ded, 2))
            slip.db_set("net_pay", flt(net, 2))
        except Exception:
            pass

    # Audit row "Lương kỳ VN" — insert directly into the child table so the
    # parent's validate() (which would recompute from SSA) is not re-run.
    try:
        audit_component = component_map.get("PERIOD") or _first_earning_component(
            slip.salary_structure
        )
        if audit_component:
            row = frappe.get_doc(
                {
                    "doctype": "Salary Detail",
                    "parenttype": "Salary Slip",
                    "parent": slip.name,
                    "parentfield": "earnings",
                    "salary_component": audit_component,
                    "amount": flt(gross, 2),
                }
            )
            row.db_insert()
    except Exception:
        pass


def _first_earning_component(salary_structure: str | None) -> str | None:
    """First earnings component of a Salary Structure (audit-row fallback)."""
    if not salary_structure:
        return None
    try:
        comp = frappe.get_all(
            "Salary Detail",
            filters={"parent": salary_structure, "parentfield": "earnings"},
            pluck="salary_component",
            limit=1,
        )
        return comp[0] if comp else None
    except Exception:
        return None


def _resolve_ssa(employee: str, period) -> dict:
    """Resolve the employee's effective Salary Structure Assignment (WP-FIX-SSA).

    Mirrors HRMS ``SalarySlip.set_salary_structure_assignment`` semantics:
    the *latest submitted* SSA with ``from_date <= period.from_date`` wins —
    HRMS validates against the slip START date, not the end date (the old
    pre-check used ``from_date <= period.to_date`` and had no order_by, so it
    could pass mid-period SSAs that HRMS then rejected, or pick a stale SSA).
    Every failure mode is classified with a stable ``code``:

      SSA_MISSING            no submitted SSA at all
      SSA_DRAFT              only draft SSAs exist
      SSA_MID_PERIOD         SSA becomes effective inside the period
      SSA_FUTURE             SSA starts after the period ends
      SSA_STRUCTURE_INACTIVE linked Salary Structure not submitted / inactive
      SSA_COMPANY_MISMATCH   SSA belongs to another company

    Deliberately NO blanket ``except Exception`` — unexpected errors must
    bubble up to the caller (traceback logged there), never masquerade as a
    missing-SSA data problem (the old lookup swallowed them silently).
    """
    ssas = (
        frappe.db.get_all(
            "Salary Structure Assignment",
            filters={"employee": employee, "docstatus": 1},
            fields=["name", "salary_structure", "company", "from_date"],
            order_by="from_date desc",
        )
        or []
    )
    start = str(getattr(period, "from_date", "") or "")
    end = str(getattr(period, "to_date", "") or "")

    def _fd(rec) -> str:
        return str(rec.get("from_date") or "")

    # A record without from_date (legacy rows / stubs) counts as always-effective.
    started = [s for s in ssas if not _fd(s) or not start or _fd(s) <= start]
    if not started:
        if ssas:
            newest_fd = _fd(ssas[0])
            if end and newest_fd and newest_fd > end:
                raise SlipGenerationError(
                    _("SSA của nhân viên {0} hiệu lực từ {1} — sau ngày kết thúc kỳ {2}.").format(
                        employee, newest_fd, end
                    ),
                    code=SLIP_CODE_SSA_FUTURE,
                )
            raise SlipGenerationError(
                _(
                    "SSA của nhân viên {0} chỉ hiệu lực từ {1} (giữa kỳ) — HRMS yêu cầu hiệu lực"
                    " trước ngày bắt đầu kỳ {2}. Điều chỉnh from_date của SSA hoặc loại nhân viên khỏi kỳ."
                ).format(employee, newest_fd, start),
                code=SLIP_CODE_SSA_MID_PERIOD,
            )
        drafts = (
            frappe.db.get_all(
                "Salary Structure Assignment",
                filters={"employee": employee, "docstatus": 0},
                fields=["name"],
            )
            or []
        )
        if drafts:
            raise SlipGenerationError(
                _(
                    "Nhân viên {0} có {1} Salary Structure Assignment đang Draft"
                    " — cần Submit trước khi sinh phiếu."
                ).format(employee, len(drafts)),
                code=SLIP_CODE_SSA_DRAFT,
            )
        raise SlipGenerationError(
            _(
                "Nhân viên {0} chưa có Salary Structure Assignment đã submit hiệu lực ≤ {1}"
                " — cần gán cấu trúc lương (from_date thường là ngày vào việc)."
            ).format(employee, start or end),
            code=SLIP_CODE_SSA_MISSING,
        )

    ssa = started[0]

    # HRMS check_sal_struct: the linked Salary Structure must be submitted AND active.
    structure = frappe.db.get_value(
        "Salary Structure",
        ssa.get("salary_structure"),
        ["docstatus", "is_active"],
        as_dict=True,
    )
    structure = structure if isinstance(structure, dict) else {}
    if structure.get("docstatus") != 1 or structure.get("is_active") != "Yes":
        raise SlipGenerationError(
            _(
                "Cấu trúc lương {0} của nhân viên {1} chưa submit hoặc đã ngừng hoạt động"
                " (is_active=No)."
            ).format(ssa.get("salary_structure"), employee),
            code=SLIP_CODE_SSA_STRUCTURE_INACTIVE,
        )

    ssa_company = ssa.get("company")
    period_company = getattr(period, "company", None)
    if ssa_company and period_company and ssa_company != period_company:
        raise SlipGenerationError(
            _("SSA của nhân viên {0} thuộc công ty {1} — khác công ty của kỳ lương ({2}).").format(
                employee, ssa_company, period_company
            ),
            code=SLIP_CODE_SSA_COMPANY_MISMATCH,
        )

    return ssa


def _generate_for_line(period, line: dict, component_map: dict) -> str:
    """Create ONE real Salary Slip for a review line — or raise with a reason.

    WP1 (F-LC14b) rewrite of the old silently-falling-back flow:

    1. Resolve the employee's active Salary Structure Assignment — missing SSA
       raises immediately with a clear message (previously this failed deep
       inside HRMS validate() and fell back to silence).
    2. Delete any existing draft slip for the same employee+window (idempotent
       regenerate — SL3/SL5).
    3. Insert the slip via the standard HRMS path (``frappe.get_doc`` +
       ``insert()`` so SSA-driven earnings + validations run).
    4. Override the totals with the reviewed amounts (see
       :func:`_stamp_slip_totals`) — HRMS computes, the review line decides.
    """
    employee = line.get("employee")
    if not employee:
        raise SlipGenerationError(
            _("Dòng lương thiếu mã nhân viên — không thể sinh phiếu."),
            code=SLIP_CODE_LINE_NO_EMPLOYEE,
        )

    # 1) SSA covering the period — without it HRMS cannot build the slip.
    #    WP-FIX-SSA: classified resolver aligned with HRMS semantics (latest
    #    submitted SSA effective by the slip START date, structure + company
    #    verified); unexpected lookup errors are NOT swallowed anymore.
    ssa = _resolve_ssa(employee, period)
    salary_structure = (
        ssa.get("salary_structure") if isinstance(ssa, dict) else getattr(ssa, "salary_structure", None)
    )
    if not salary_structure:
        raise SlipGenerationError(
            _("Salary Structure Assignment của nhân viên không liên kết Structure lương hợp lệ."),
            code=SLIP_CODE_SSA_STRUCTURE_INVALID,
        )

    # 2) Idempotent regenerate: replace any prior draft slip of this window.
    _delete_existing_slip(employee, period)

    # 3) Standard HRMS insert — validations (earnings rows, exchange rate,
    #    duplicates…) run here and surface as SlipGenerationError on failure.
    try:
        slip = frappe.get_doc(
            {
                "doctype": "Salary Slip",
                "employee": employee,
                "salary_structure": salary_structure,
                "start_date": period.from_date,
                "end_date": period.to_date,
                "posting_date": period.to_date,
                "company": period.company,
                "docstatus": 0,
            }
        )
        slip.insert(ignore_permissions=True)
    except Exception as exc:
        raise SlipGenerationError(_slip_failure_reason(exc)) from exc

    # 4) Make the reviewed amounts authoritative.
    _stamp_slip_totals(slip, period, line, component_map)
    # 5) Plan v2 (confirm-early): the slip is employee-visible the MOMENT it
    # is generated — employees may confirm / request an adjustment right away
    # (the slip stays a replaceable Draft until 100% confirmed). publish only
    # broadcasts notifications after this.
    try:
        from frappe.utils import now_datetime

        frappe.db.set_value(
            SLIP_DOCTYPE,
            slip.name,
            {"vn_employee_visible": 1, "vn_visible_at": now_datetime()},
        )
    except Exception:
        pass
    return slip.name


def _get_withheld_employees(period) -> set:
    """FIX-7 (I-7): employees with an active Salary Withholding covering the period."""
    try:
        return set(
            frappe.get_all(
                "Salary Withholding",
                filters={
                    "docstatus": 1,
                    "from_date": ["<=", period.to_date],
                    "to_date": [">=", period.from_date],
                },
                pluck="employee",
            )
            or []
        )
    except Exception:
        return set()


@frappe.whitelist()
def publish_payslips(name: str | None = None) -> dict:
    """Plan §10.8 — mark payslips employee-visible and fire a realtime event.

    Returns ``{ published, message }``. Marks ``Slips Generated`` → ``Published``
    on the period.
    """
    _assert_closer()
    period = _get_period(name)
    _assert_period_not_confirmed(period)
    if period.status != "Slips Generated":
        frappe.throw(
            _("Kỳ lương phải ở trạng thái Slips Generated trước khi công bố."),
            frappe.ValidationError,
        )

    published = 0
    withheld = _get_withheld_employees(period)  # I-7: skip withheld employees
    lines = (
        frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": period.name, "docstatus": ["<", 2]},
            fields=["name", "employee", "salary_slip"],
        )
        or []
    )
    for ln in lines:
        if not ln.get("salary_slip"):
            continue
        if ln["employee"] in withheld:
            continue  # I-7: salary withheld — don't publish
        try:
            frappe.db.set_value("Salary Slip", ln["salary_slip"], "vn_employee_visible", 1)
            published += 1
        except Exception:
            pass
        # Drop an inbox notification per employee so the bell + payslips list
        # refreshes (best-effort, bench-guarded — never aborts the publish).
        try:
            notify.push_notification(
                employee=ln.get("employee"),
                notification_type="Payroll",
                title=_("Phiếu lương đã được công bố"),
                message=_("Phiếu lương kỳ {0}/{1} của bạn đã sẵn sàng để xem.").format(
                    period.payroll_month, period.payroll_year
                ),
                reference_doctype="Salary Slip",
                reference_name=ln.get("salary_slip"),
                action_url="/payroll",
            )
        except Exception:
            pass

    # Proper Frappe flow (see approve_review): save() runs validate + on_update.
    period.status = "Published"
    period.save()
    # Fire a realtime event so the NotificationBell / payslips list refresh.
    try:
        frappe.publish_realtime(
            event="payroll_published",
            message={"period": period.name, "company": period.company},
        )
    except Exception:
        pass
    audit_api.log(
        "Payroll Publish",
        doc=period.as_dict(),
        description=f"Công bố {published} phiếu lương",
    )
    return {
        "published": published,
        "message": _("Đã công bố {0} phiếu lương.").format(published),
    }


# --------------------------------------------------------------------------- #
# Endpoints — employee-facing payslips
# --------------------------------------------------------------------------- #
_PAYSLIP_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "start_date",
    "end_date",
    "posting_date",
    "gross_pay",
    "total_deduction",
    "net_pay",
    "status",
    # VN ack & payout snapshot (plans/payslip-ack-qr-payment-plan.md) — the
    # detail view drives the employee Confirm / Request-adjustment buttons
    # off these fields; unknown columns are simply absent on older sites.
    "vn_employee_visible",
    "vn_visible_at",
    "vn_ack_status",
    "vn_ack_source",
    "vn_ack_note",
    "vn_ack_rejected_at",
    "vn_ack_rejected_reason",
    "vn_payment_ref",
    "vn_payee_bank_name",
    "vn_payee_account_no",
    "vn_payee_qr_text",
    "vn_payment_proof",
    "vn_paid_at",
]


def _match_row(row: dict, search: str | None) -> bool:
    """Generic server-side free-text match across a row's values (DNA §6.6 D).

    Used by employee-scoped lists whose searchable projection is small and whose
    columns vary, so a schema-agnostic scan is simplest and never raises.
    """
    q = (search or "").strip().lower()
    if not q:
        return True
    return any(q in str(v).lower() for v in row.values() if v is not None)


@frappe.whitelist()
def export_bank_file(name: str, fmt: str = "napas", value_date: str | None = None) -> dict:
    """FIX-3 — build a Vietnamese bank payment file (NAPAS / ACCT / CSV) from the
    approved lines of a VN Payroll Review Period (decision: do NOT use Payroll
    Entry — keep the VN review flow). Returns ``{ok, filename, content, mime,
    total, count}`` on success, or ``{ok: False, missing: [...]}`` when employees
    are missing bank account info (HR fixes the data, then re-exports).
    """
    from frappe.utils import getdate

    from gege_hr.gege_hr.utils import bank_export

    _assert_closer()
    period = _get_period(name)
    # FINDING-LC15 (E2E golden path): the FE allows export from Approved
    # onward, but this gate required EXACTLY "Approved" — after generating
    # slips (Slips Generated) or publishing (Published) the bank export was
    # locked out forever, backwards vs the closing workflow.
    if (period.status or "") not in ("Approved", "Slips Generated", "Published"):
        frappe.throw(_("Chỉ xuất file ngân hàng cho kỳ đã duyệt (Approved)."))
    value_date = value_date or getattr(period, "to_date", None) or getdate().isoformat()

    raw = (
        frappe.get_all(
            "VN Payroll Review Line",
            filters={"payroll_review_period": name, "net_pay": [">", 0]},
            fields=["employee", "employee_name", "net_pay"],
        )
        or []
    )
    rows = []
    for r in raw:
        # F-LC16 (E2E golden path): ``ac_holder_name`` only exists after a
        # migrate that installs the custom field — query defensively.
        try:
            bank = (
                frappe.db.get_value(
                    "Employee",
                    r["employee"],
                    ["bank_ac_no", "bank_name", "ac_holder_name"],
                    as_dict=True,
                )
                or {}
            )
        except Exception:
            bank = (
                frappe.db.get_value(
                    "Employee", r["employee"], ["bank_ac_no", "bank_name"], as_dict=True
                )
                or {}
            )
        rows.append(
            {
                "employee": r["employee"],
                "employee_name": r.get("employee_name") or bank.get("ac_holder_name"),
                "account_no": bank.get("bank_ac_no"),
                "bank_name": bank.get("bank_name"),
                "bank_code": bank.get("bank_name"),
                "amount": r.get("net_pay"),
            }
        )

    missing = bank_export.missing_fields(rows)
    if missing:
        return {"ok": False, "missing": missing, "count": len(missing)}

    res = bank_export.build_file(rows, fmt=fmt, company=period.company, value_date=value_date)
    res["ok"] = True
    return res


@frappe.whitelist()
def my_payslips(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.8 — the caller's published payslips (employee-visible only).

    ``search`` OR-matches a free-text query across the row's values, applied
    server-side (DNA §6.6 D, HR-BL-09) — ready for the SPA payslip broad search.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": None}``
    (post-query filter → ``total`` is the filtered list length); without
    ``page_size`` the legacy bare-list return is preserved.
    """
    emp = _resolve(employee)
    _assert_payslip_access(emp)

    filters = {"employee": emp, "vn_employee_visible": 1, "docstatus": ["<", 2]}
    if from_date or to_date:
        filters["start_date"] = ["between", [from_date or to_date, to_date or from_date]]
    # Status filter (Law #2) — exact match on the Salary Slip `status` column
    # (Submitted / Draft / Cancelled / Paid). Applied pre-pagination so `total`
    # + paging are correct (DNA §6.6 A).
    if status:
        filters["status"] = status

    try:
        rows = frappe.db.get_all(
            "Salary Slip",
            filters=filters,
            fields=_PAYSLIP_FIELDS,
            order_by="start_date desc, name desc",
            limit_page_length=pagination.MAX_PAGE_SIZE * 10,  # newest-first bound
        )
    except Exception:
        return {"data": [], "total": 0, "summary": None} if page_size else []
    return pagination.paginate_filtered(
        [r for r in rows if _match_row(r, search)], page=page, page_size=page_size
    )


@frappe.whitelist()
def payslip_detail(name: str | None = None) -> dict:
    """Plan §10.8 — one payslip with the VN breakdown."""
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã phiếu lương."))

    try:
        slip = frappe.db.get_value("Salary Slip", name, _PAYSLIP_FIELDS, as_dict=True)
    except Exception:
        slip = None
    if not slip:
        frappe.throw(_("Phiếu lương {0} không tồn tại.").format(name))

    emp = slip.get("employee")
    if emp:
        _assert_payslip_access(emp)

    out = dict(slip)

    # Real earnings/deductions rows (WP-payslip-detail): employees must see the
    # same itemised amounts managers see. The old code only setdefault([]) —
    # the detail view never had any line items to render.
    def _slip_rows(parentfield: str) -> list[dict]:
        try:
            return [
                {
                    "salary_component": r.get("salary_component"),
                    "amount": r.get("amount"),
                }
                for r in frappe.get_all(
                    "Salary Detail",
                    filters={"parent": name, "parentfield": parentfield, "parenttype": "Salary Slip"},
                    fields=["salary_component", "amount"],
                    order_by="idx asc",
                )
            ]
        except Exception:
            return []

    out["earnings"] = _slip_rows("earnings")
    out["deductions"] = _slip_rows("deductions")
    # VN breakdown custom fields (best-effort; present after the custom-field
    # set in custom_fields.py ships).
    for f in (
        "vn_payable_days",
        "vn_regular_hours",
        "vn_overtime_hours",
        "vn_night_overtime_hours",
        "vn_late_penalty_amount",
        "vn_overtime_amount",
        "vn_salary_advance_deduction",
        "vn_night_allowance_amount",
        "vn_other_deduction",
        "vn_checkout_miss_penalty",
    ):
        try:
            out[f] = frappe.db.get_value("Salary Slip", name, f)
        except Exception:
            out[f] = None
    # WP-payslip-detail: slips generated via the VN flow stamp VN fields only
    # when the custom-field set exists; fall back to the linked review LINE
    # (same employee+window, authoritative VN breakdown) so employees never
    # see zeroes for hours/amounts that were actually reviewed.
    _line_fields = {
        "vn_payable_days": "payable_days",
        "vn_regular_hours": "regular_hours",
        "vn_overtime_hours": "overtime_hours",
        "vn_overtime_amount": "overtime_amount",
        "vn_late_penalty_amount": "late_penalty_amount",
        "vn_salary_advance_deduction": "salary_advance_deduction",
        "vn_other_deduction": "other_deduction",
        "vn_checkout_miss_penalty": "checkout_miss_penalty",
        "vn_night_allowance_amount": "night_allowance_amount",
    }
    try:
        ln = frappe.get_all(
            "VN Payroll Review Line",
            filters={"salary_slip": name},
            limit=1,
        )
    except Exception:
        ln = []
    if ln:
        line_doc = frappe.get_doc(LINE_DOCTYPE, ln[0]["name"])
        line = {f: line_doc.get(f) for f in _line_fields.values()}
        for slip_f, line_f in _line_fields.items():
            if not out.get(slip_f) and line.get(line_f) is not None:
                out[slip_f] = line.get(line_f)
        # Full formula breakdown (the same dict the manager's line-detail
        # popup renders) so employees can audit how their pay was computed.
        out["calculation"] = _line_breakdown(line_doc)
    return out


def _assert_payslip_access(employee: str) -> None:
    """HR/Manager may read anyone's payslip; an Employee reads only their own."""
    roles = set(emp_utils.get_user_roles() or [])
    if roles & emp_utils.HR_MANAGER_ROLES:
        return
    own = emp_utils.get_employee_for_user()
    if own != employee:
        frappe.throw(
            _("Bạn không có quyền truy cập phiếu lương của nhân viên khác."),
            frappe.PermissionError,
        )


# --------------------------------------------------------------------------- #
# Payroll Settings — get/save for the /hr/salary-structure settings hub
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_payroll_settings() -> dict:
    """Load all payroll configuration for the settings UI."""
    import json

    frappe.only_for(["HR Manager", "System Manager"])
    # Departments with hourly rates (non-group only).
    depts = frappe.db.get_all(
        "Department",
        filters={"is_group": 0},
        fields=["name", "department_name", "company", "vn_hourly_rate"],
        order_by="company, department_name",
    )
    # Time brackets.
    raw = frappe.db.get_single_value("VN HR Portal Setting", "vn_time_brackets") or "[]"
    try:
        brackets = json.loads(raw)
    except Exception:
        brackets = [
            {"from": 8, "to": 16, "coeff": 1.0},
            {"from": 16, "to": 24, "coeff": 1.2},
            {"from": 0, "to": 8, "coeff": 1.5},
        ]
    # Deduction rates.
    rates = {
        "bhxh": float(frappe.db.get_single_value("VN HR Portal Setting", "vn_ded_bhxh") or 8),
        "bhyt": float(frappe.db.get_single_value("VN HR Portal Setting", "vn_ded_bhyt") or 1.5),
        "bhtn": float(frappe.db.get_single_value("VN HR Portal Setting", "vn_ded_bhtn") or 1),
        "tncn": float(frappe.db.get_single_value("VN HR Portal Setting", "vn_ded_tncn") or 10),
    }
    default_rate = float(
        frappe.db.get_single_value("VN HR Portal Setting", "vn_default_hourly_rate") or 20000
    )
    # Checkout-miss penalty config (vn_cm_* on VN HR Portal Setting). The "Phạt
    # quên checkout" tab edits these. ``0`` is a valid value (e.g. free_first_n=0
    # => no freebies), so we treat ``None`` (unset) as "default" — never ``or``
    # (that would turn a legit 0 into the default). Mirrors _config() in
    # utils/checkout_miss.
    def _cm(field, cast, default):
        v = frappe.db.get_single_value("VN HR Portal Setting", field)
        return cast(v) if v is not None else default

    # BUG-6 fix: read defaults from the engine's shared DEFAULTS so the UI shows
    # exactly what the engine falls back to (previously 0 vs 2 freebies / 100k).
    checkout_miss = {
        "enabled": bool(_cm("vn_cm_enabled", int, int(CM_DEFAULTS["enabled"]))),
        "penalty_amount": _cm("vn_cm_penalty_amount", float, float(CM_DEFAULTS["penalty_amount"])),
        "free_first_n": _cm("vn_cm_free_first_n", int, int(CM_DEFAULTS["free_first_n"])),
        "grace_hours": _cm("vn_cm_grace_hours", int, int(CM_DEFAULTS["grace_hours"])),
        "window_days": _cm("vn_cm_window_days", int, int(CM_DEFAULTS["window_days"])),
        "buffer_minutes": _cm("vn_cm_buffer_minutes", int, int(CM_DEFAULTS["buffer_minutes"])),
    }
    # Adjustment presets ("Mẫu điều chỉnh" tab → review popup dropdown).
    presets = _load_adjustment_presets()
    # Penalty rules (from active VN Attendance Policy).
    penalty_rules = []
    for p in frappe.db.get_all(
        "VN Attendance Policy", filters={"is_active": 1}, fields=["name"]
    ):
        for r in frappe.db.get_all(
            "VN Attendance Penalty Rule",
            filters={"parent": p["name"], "parenttype": "VN Attendance Policy"},
            fields=["from_minutes", "to_minutes", "penalty_type", "penalty_value"],
        ):
            r["policy"] = p["name"]
            penalty_rules.append(r)
    return {
        "departments": depts,
        "time_brackets": brackets,
        "deduction_rates": rates,
        "default_hourly_rate": default_rate,
        "penalty_rules": penalty_rules,
        "checkout_miss": checkout_miss,
        "adjustment_presets": presets,
    }


# Checkout-miss penalty config (vn_cm_* on VN HR Portal Setting). The "Phạt quên
# checkout" tab in HrSalaryStructureView edits these. ``0`` is a valid value for
# every field (free_first_n=0, penalty_amount=0, ...), so reads/writes treat
# ``None`` (unset) as "default" — never ``or``. Mirrors utils/checkout_miss.
_CHECKOUT_MISS_FIELDS = {
    "enabled": "vn_cm_enabled",
    "penalty_amount": "vn_cm_penalty_amount",
    "free_first_n": "vn_cm_free_first_n",
    "grace_hours": "vn_cm_grace_hours",
    "window_days": "vn_cm_window_days",
    "buffer_minutes": "vn_cm_buffer_minutes",
}
_CHECKOUT_MISS_INT_FIELDS = {
    "vn_cm_free_first_n",
    "vn_cm_grace_hours",
    "vn_cm_window_days",
    "vn_cm_buffer_minutes",
}


def _cm_to_bool(value) -> bool:
    """Accept JS-style booleans or 0/1/"true" from the SPA payload."""
    if isinstance(value, bool):
        return value
    return value in (1, "1", "true", "True", "Yes", "yes")


def _apply_checkout_miss(setting, cm) -> None:
    """Validate + persist the ``checkout_miss`` dict onto the Portal Setting doc.

    ``None`` keys are skipped (the UI didn't touch them). Numeric fields must be
    non-negative; ``0`` is allowed (mirrors get_payroll_settings / _config()).
    """
    if not isinstance(cm, dict):
        return
    for key, field in _CHECKOUT_MISS_FIELDS.items():
        if key not in cm or cm[key] is None:
            continue
        value = cm[key]
        if key == "enabled":
            setting.set(field, 1 if _cm_to_bool(value) else 0)
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            frappe.throw(_("{0} phải là số.").format(key))
        if value < 0:
            frappe.throw(_("{0} không được âm.").format(key))
        setting.set(field, int(value) if field in _CHECKOUT_MISS_INT_FIELDS else value)


@frappe.whitelist()
def save_payroll_settings(**kwargs) -> dict:
    """Save payroll settings from the UI (Department rates, brackets, deductions)."""
    import json

    frappe.only_for(["HR Manager", "System Manager"])
    # 1. VN HR Portal Setting.
    setting = frappe.get_doc("VN HR Portal Setting", "VN HR Portal Setting")
    tb = kwargs.get("time_brackets")
    if tb is not None:
        setting.vn_time_brackets = json.dumps(tb) if isinstance(tb, list) else tb
    for key, field in [
        ("bhxh", "vn_ded_bhxh"),
        ("bhyt", "vn_ded_bhyt"),
        ("bhtn", "vn_ded_bhtn"),
        ("tncn", "vn_ded_tncn"),
    ]:
        if kwargs.get(key) is not None:
            setting.set(field, kwargs[key])
    if kwargs.get("default_hourly_rate") is not None:
        setting.vn_default_hourly_rate = kwargs["default_hourly_rate"]
    # Adjustment presets ("Mẫu điều chỉnh" tab) — validate then store as JSON.
    presets = kwargs.get("adjustment_presets")
    if presets is not None:
        if isinstance(presets, str):
            try:
                presets = json.loads(presets)
            except Exception:
                frappe.throw(_("Danh sách mẫu điều chỉnh không hợp lệ."))
        if not isinstance(presets, list):
            frappe.throw(_("Danh sách mẫu điều chỉnh không hợp lệ."))
        clean = []
        for it in presets:
            if not isinstance(it, dict):
                continue
            adj_type = it.get("adjustment_type") or "Bonus"
            if adj_type not in _ADJUSTMENT_TYPES:
                frappe.throw(_("Loại điều chỉnh không hợp lệ."))
            description = (it.get("description") or "").strip()
            if not description:
                frappe.throw(_("Nội dung mẫu điều chỉnh là bắt buộc."))
            try:
                amount = float(it.get("amount") or 0)
            except (TypeError, ValueError):
                frappe.throw(_("Số tiền mẫu điều chỉnh không hợp lệ."))
            if amount < 0:
                frappe.throw(_("Số tiền mẫu điều chỉnh không được âm."))
            clean.append({"adjustment_type": adj_type, "description": description, "amount": amount})
        setting.vn_adjustment_presets = json.dumps(clean, ensure_ascii=False)
    # Checkout-miss penalty config (vn_cm_*).
    _apply_checkout_miss(setting, kwargs.get("checkout_miss"))
    setting.flags.ignore_permissions = True
    setting.save(ignore_permissions=True)

    # 2. Department hourly rates.
    dept_rates = kwargs.get("department_rates")
    if isinstance(dept_rates, list):
        for dr in dept_rates:
            name = dr.get("name")
            rate = dr.get("vn_hourly_rate")
            if name and rate is not None:
                try:
                    frappe.db.set_value(
                        "Department", name, "vn_hourly_rate", float(rate), update_modified=False
                    )
                except Exception:
                    pass

    frappe.db.commit()
    return {"ok": True, "message": "Đã lưu cấu hình lương."}


# --------------------------------------------------------------------------- #
# WP6 — auto close LAST month's payroll (scheduler; stops at Calculated/Draft)
# --------------------------------------------------------------------------- #
def _previous_month(today=None):
    """(year, month, from_date, to_date) of the month BEFORE ``today``."""
    from datetime import date as _d

    t = today or _d.today()
    first = _d(t.year, t.month, 1)
    prev_last = first - _d.resolution  # last day of the previous month
    prev_first = prev_last.replace(day=1)
    return prev_last.year, prev_last.month, prev_first, prev_last


def _auto_close_blockers(company: str, from_date, to_date) -> list[str]:
    """Everything that makes the month un-closable right now (AC2)."""
    blockers: list[str] = []
    pending = calc.pending_checkout_miss_tickets(company, from_date, to_date)
    if pending:
        blockers.append(f"{pending} ticket quên checkout Pending")
    try:
        not_calculated = frappe.db.count(
            "VN Attendance Work Session",
            {
                "company": company,
                "work_date": ["between", [from_date, to_date]],
                "docstatus": ["<", 2],
                "calculation_status": ["not in", ["Calculated", "Locked"]],
            },
        )
    except Exception:
        not_calculated = 0
    if not_calculated:
        blockers.append(f"{not_calculated} Work Session chưa Calculated")
    return blockers


@frappe.whitelist()
def auto_close_payroll(today=None) -> dict:
    """WP6 — 07:30 daily: ensure LAST month's payroll is calculated & waiting.

    For each company with activity in the previous month:

    1. Idempotency: a period already past Draft → nothing to do (AC5).
    2. Blockers (Pending tickets / non-Calculated WS) → do NOT calculate;
       days 1–5 this is a silent retry, from day 6 a RED daily notification
       fires (AC4) so the blockage can't hide.
    3. Create the Review Period (never duplicating a manual one, AC5) and run
       ``calculate_payroll_review`` — status stops at ``Calculated``.
       **NEVER auto-approves** (plan: "tuyệt đối không auto-approve").
    4. Notify HR Managers the month is ready + record the health heartbeat.

    Runs as Administrator (scheduler) which holds System Manager → passes
    ``_assert_closer``.
    """
    from datetime import date as _d

    from gege_hr.gege_hr.utils import health as _health

    if today is None:
        today = _d.today()
    elif isinstance(today, str):
        today = _d.fromisoformat(today[:10])
    year, month, from_d, to_d = _previous_month(today)
    month_str = f"{month:02d}"
    results: list[dict] = []

    # Companies with work sessions in the previous month (AC6: one period each).
    try:
        companies = [
            r["company"]
            for r in frappe.db.get_all(
                WORK_SESSION_DOCTYPE,
                filters={"work_date": ["between", [from_d, to_d]], "docstatus": ["<", 2]},
                fields=["company"],
                distinct=True,
            )
            if r.get("company")
        ]
    except Exception:
        companies = []

    all_clean = True
    for company in companies:
        try:
            existing = frappe.db.get_all(
                PERIOD_DOCTYPE,
                filters={
                    "company": company,
                    "payroll_month": month_str,
                    "payroll_year": year,
                    "docstatus": ["<", 2],
                },
                fields=["name", "status"],
            )
        except Exception:
            existing = []
        if existing:
            # AC5: a manual (or previous) period exists — never touch it.
            status = existing[0].get("status")
            results.append({"company": company, "period": existing[0]["name"], "action": "exists", "status": status})
            continue

        blockers = _auto_close_blockers(company, from_d, to_d)
        if blockers:
            all_clean = False
            entry = {
                "company": company,
                "action": "blocked",
                "blockers": blockers,
            }
            # AC4: past day 5 still blocked → loud daily notification.
            if today.day > 5:
                _health._notify_users(
                    _health._hr_manager_users(),
                    f"[GeGe HR] Kỳ lương {month_str}/{year} của {company} CÒN VƯỚNG",
                    "Không thể tự tính kỳ lương tháng trước:\n• "
                    + "\n• ".join(blockers)
                    + "\nXử lý xong job sẽ tự tính lại lần chạy sau (07:30).",
                )
            results.append(entry)
            continue

        # 3) Create + calculate. Stops at Calculated — HR reviews, approves,
        #    generates slips (AC1/AC7); this job NEVER approves.
        try:
            period = frappe.get_doc(
                {
                    "doctype": PERIOD_DOCTYPE,
                    "company": company,
                    "payroll_month": month_str,
                    "payroll_year": year,
                    "from_date": str(from_d),
                    "to_date": str(to_d),
                    "status": "Draft",
                    "vn_auto_created": 1,
                }
            )
            period.insert(ignore_permissions=True)
            calc_res = calculate_payroll_review(name=period.name)
            lines_need_review = int(calc_res.get("total_employees") or 0)
            _health._notify_users(
                _health._hr_manager_users(),
                f"[GeGe HR] Kỳ lương {month_str}/{year} ({company}) đã tính sẵn",
                f"Kỳ lương tháng trước đã được tính tự động (Draft — chờ duyệt).\n"
                f"{lines_need_review} dòng lương cần HR rà soát.",
            )
            results.append(
                {
                    "company": company,
                    "period": period.name,
                    "action": "calculated",
                    "lines": lines_need_review,
                }
            )
        except Exception:
            all_clean = False
            try:
                frappe.log_error(
                    title=f"auto_close_payroll failed {company}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
            results.append({"company": company, "action": "error"})

    # Heartbeat ONLY when every company closed clean this month (AC4: a
    # blocked month leaves the heartbeat stale → health tab goes red).
    if companies and all_clean:
        try:
            _health.record_heartbeat("payroll.auto_close_payroll", summary={"results": results})
        except Exception:
            pass
    return {"ok": True, "month": month_str, "year": year, "results": results}
