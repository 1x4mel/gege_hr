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

import frappe
from frappe import _

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import notify
from gege_hr.gege_hr.utils import pagination
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
    """
    claimed = frappe.db.sql(
        "UPDATE `tabVN Payroll Review Period` SET status = 'Calculating'"
        " WHERE name = %(name)s AND status IN ('Draft', 'Calculated', 'Calculating')",
        {"name": period_name},
    )
    frappe.db.commit()
    if not claimed:
        frappe.throw(
            _("Kỳ lương đang được tính hoặc đã chốt — không thể tính lại lúc này."),
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
    like = f"%{q}%"
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
    like = f"%{q}%"
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
    _claim_period_for_calculation(period.name)

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
        late_penalty = calc.compute_late_penalty(late_minutes_list, penalty_rules)

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
    from datetime import datetime as _dt, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo

    VN = ZoneInfo("Asia/Ho_Chi_Minh")
    UTC = ZoneInfo("UTC")
    try:
        rows = frappe.db.get_all(
            "Employee Checkin",
            filters={
                "employee": employee,
                "time": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]],
            },
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

    # --- Shift planned-end lookup (for OT capping) -------------------------
    shift_start = shift_end = None
    try:
        st_name = frappe.db.get_value(
            "Shift Assignment",
            {"employee": employee, "docstatus": 1, "status": "Active"},
            "shift_type",
        )
        if st_name:
            from frappe.utils import get_time

            shift_start = get_time(frappe.db.get_value("Shift Type", st_name, "start_time"))
            shift_end = get_time(frappe.db.get_value("Shift Type", st_name, "end_time"))
    except Exception:
        pass

    # --- Approved OT dates (VN Overtime Request submitted/docstatus=1) -----
    approved_ot: set[str] = set()
    try:
        for r in frappe.db.get_all(
            "VN Overtime Request",
            filters={"employee": employee, "docstatus": 1},
            fields=["work_date"],
        ):
            if r.get("work_date"):
                approved_ot.add(str(r["work_date"]))
    except Exception:
        pass

    def _planned_end_utc(vn_date):
        """UTC datetime of the shift's planned_end for the given VN date."""
        if not shift_end:
            return None
        end_vn = _dt.combine(vn_date, shift_end)
        end_vn = end_vn.replace(tzinfo=VN)
        if shift_start and shift_end <= shift_start:
            end_vn += _td(days=1)  # overnight shift (end < start)
        return end_vn.astimezone(UTC)

    # --- Pair IN→OUT, cap at planned_end, add OT only if approved ---------
    bracket_hours: dict[float, float] = {}
    cur_in = None
    for t, lt in logs:
        if lt == "IN":
            cur_in = t
        elif lt == "OUT" and cur_in is not None and t > cur_in:
            span_h = (t - cur_in).total_seconds() / 3600.0
            if span_h <= 16.0:
                try:
                    vn_date = cur_in.astimezone(VN).date()
                    pe = _planned_end_utc(vn_date)
                except Exception:
                    pe = None
                # Regular hours: IN → min(OUT, planned_end). Always counted.
                regular_out = min(t, pe) if pe else t
                split = calc.split_hours_by_bracket(cur_in, regular_out, brackets)
                for coeff, hours in split.items():
                    bracket_hours[coeff] = bracket_hours.get(coeff, 0.0) + hours
                # OT hours: planned_end → actual OUT. Only if approved.
                if pe and t > pe and str(vn_date) in approved_ot:
                    ot_split = calc.split_hours_by_bracket(pe, t, brackets)
                    for coeff, hours in ot_split.items():
                        bracket_hours[coeff] = bracket_hours.get(coeff, 0.0) + hours
            cur_in = None
    return bracket_hours


def _employee_late_minutes(employee: str, from_date, to_date) -> list[float]:
    """Minutes-late for each ``late_entry=1`` Attendance row."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    VN = ZoneInfo("Asia/Ho_Chi_Minh")
    try:
        rows = frappe.db.get_all(
            "Attendance",
            filters={
                "employee": employee,
                "attendance_date": ["between", [from_date, to_date]],
                "docstatus": 1,
                "late_entry": 1,
            },
            fields=["attendance_date", "shift", "in_time"],
        )
    except Exception:
        return []

    result: list[float] = []
    for r in rows:
        if not r.get("in_time"):
            continue
        try:
            in_t = _dt.strptime(str(r["in_time"])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        except Exception:
            continue
        in_portal = in_t.astimezone(VN)
        shift_name = r.get("shift")
        if not shift_name:
            continue
        st = frappe.db.get_value("Shift Type", shift_name, "start_time")
        if st is None:
            continue
        from gege_hr.gege_hr.utils.tz import as_time

        shift_start = as_time(st)
        planned = _dt.combine(in_portal.date(), shift_start).replace(tzinfo=VN)
        minutes = (in_portal - planned).total_seconds() / 60.0
        if minutes > 0:
            result.append(round(minutes, 1))
    return result


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


@frappe.whitelist()
def approve_review(name: str | None = None, comment: str | None = None) -> dict:
    """Plan §10.8 — approve the review (all lines must be Confirmed/Approved)."""
    _assert_closer()
    period = _get_period(name)

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
def generate_salary_slips(name: str | None = None) -> dict:
    """Plan §10.8 — create Additional Salary rows per line (and link them).

    For each approved-period review line this materialises the overtime,
    night-allowance and deduction components as Additional Salary documents for
    the period's payroll run, then stamps the line's ``salary_slip`` reference
    once a Salary Slip exists.

    Returns ``{ generated, message }``.
    """
    _assert_closer()
    period = _get_period(name)
    if period.status != "Approved":
        frappe.throw(
            _("Kỳ lương phải ở trạng thái Approved trước khi tạo phiếu lương."),
            frappe.ValidationError,
        )

    component_map = calc.load_component_map(period.company)
    generated = 0
    lines = (
        frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": period.name, "docstatus": ["<", 2]},
            fields=[
                "name",
                "employee",
                "overtime_amount",
                "night_allowance_amount",
                "late_penalty_amount",
                "salary_advance_deduction",
                "other_deduction",
                "gross_pay",
                "net_pay",
            ],
        )
        or []
    )

    for ln in lines:
        slip_name = _generate_for_line(period, ln, component_map)
        if slip_name:
            generated += 1
        try:
            if slip_name:
                frappe.db.set_value(LINE_DOCTYPE, ln["name"], "salary_slip", slip_name)
        except Exception:
            pass

    # Proper Frappe flow (see approve_review): save() runs validate + on_update.
    period.status = "Slips Generated"
    period.save()
    return {
        "generated": generated,
        "message": _("Đã tạo {0} phiếu lương.").format(generated),
    }


def _generate_for_line(period, line: dict, component_map: dict) -> str | None:
    """Create Additional Salary rows for a line; return a Salary Slip name if any.

    Best-effort: returns ``None`` when Frappe Payroll tables are unavailable so
    the closing flow still advances the period status.
    """
    try:
        slip = frappe.new_doc("Salary Slip")
        slip.employee = line["employee"]
        slip.start_date = period.from_date
        slip.end_date = period.to_date
        slip.posting_date = period.to_date
        slip.company = period.company
        slip.gross_pay = line.get("gross_pay") or 0
        slip.total_deduction = (
            (line.get("late_penalty_amount") or 0)
            + (line.get("salary_advance_deduction") or 0)
            + (line.get("other_deduction") or 0)
        )
        slip.net_pay = line.get("net_pay") or 0
        # Stamp the VN breakdown custom fields (best-effort; ignored if absent).
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
                slip.set(field, val)
            except Exception:
                pass
        slip.insert()
        return slip.name
    except Exception:
        # Fallback: at least create the OT Additional Salary if possible.
        try:
            ot_comp = component_map.get("OT")
            if ot_comp and (line.get("overtime_amount") or 0) > 0:
                add = frappe.new_doc("Additional Salary")
                add.employee = line["employee"]
                add.salary_component = ot_comp
                add.amount = line.get("overtime_amount")
                add.from_date = period.from_date
                add.to_date = period.to_date
                add.company = period.company
                add.insert()
        except Exception:
            pass
        return None


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
    "start_date",
    "end_date",
    "posting_date",
    "gross_pay",
    "total_deduction",
    "net_pay",
    "status",
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
    from gege_hr.gege_hr.utils import bank_export
    from frappe.utils import getdate

    _assert_closer()
    period = _get_period(name)
    if (period.status or "") != "Approved":
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
        bank = (
            frappe.db.get_value(
                "Employee",
                r["employee"],
                ["bank_ac_no", "bank_name", "ac_holder_name"],
                as_dict=True,
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
            order_by="start_date desc",
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
    out.setdefault("earnings", [])
    out.setdefault("deductions", [])
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
    ):
        try:
            out[f] = frappe.db.get_value("Salary Slip", name, f)
        except Exception:
            out[f] = None
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
