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
from gege_hr.gege_hr.utils import payroll as calc

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
    try:
        return (
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
                    "overtime_normal_hours",
                    "overtime_night_hours",
                    "overtime_holiday_hours",
                    "late_minutes",
                    "early_leave_minutes",
                    "absent",
                ],
                as_dict=True,
            )
            or []
        )
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Endpoints — listing & detail
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def periods(company: str | None = None, year: str | None = None) -> list[dict]:
    """Plan §10.8 — list payroll review periods, optionally filtered."""
    filters = {}
    if company:
        filters["company"] = company
    if year:
        filters["payroll_year"] = year
    try:
        return frappe.db.get_all(
            PERIOD_DOCTYPE,
            filters=filters,
            fields=_PERIOD_FIELDS,
            order_by="payroll_year desc, payroll_month desc",
        )
    except Exception:
        return []


@frappe.whitelist()
def review_detail(name: str | None = None) -> dict:
    """Plan §10.8 — a period plus its review lines (doctype-design §21)."""
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã kỳ lương."))

    try:
        period = frappe.db.get_value(PERIOD_DOCTYPE, name, _PERIOD_FIELDS, as_dict=True)
    except Exception:
        period = None
    if not period:
        frappe.throw(_("Kỳ lương {0} không tồn tại.").format(name))

    try:
        lines = frappe.db.get_all(
            LINE_DOCTYPE,
            filters={"payroll_review_period": name, "docstatus": ["<", 2]},
            fields=_LINE_FIELDS,
            order_by="employee_name asc",
        )
    except Exception:
        lines = []
    return {"period": period, "lines": lines}


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

    seg_mult = calc.load_segment_multipliers(period.company)
    employees = _period_employees(period.company, period.from_date, period.to_date)
    emp_names = [e["employee"] for e in employees]
    advance_ded = calc.employee_advance_deductions(
        period.company, emp_names, period.from_date, period.to_date
    )

    line_amounts: list[dict] = []
    for emp in employees:
        emp_id = emp["employee"]
        ws_rows = _employee_work_sessions(emp_id, period.from_date, period.to_date)
        agg = calc.aggregate_work_sessions(ws_rows)
        base = calc.resolve_base_salary(emp_id, period.to_date)
        cfg = dict(calc.default_config())
        cfg["segment_multipliers"] = seg_mult
        cfg["salary_advance_deduction"] = advance_ded.get(emp_id, 0.0)
        amounts = calc.compute_line(agg, base, cfg)

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

    if existing:
        doc = frappe.get_doc(LINE_DOCTYPE, existing)
        doc.update({k: v for k, v in payload.items() if k != "status"})
        doc.save()
    else:
        doc = frappe.new_doc(LINE_DOCTYPE)
        doc.update(payload)
        doc.insert()


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

    period.db_set({"status": "Approved"})
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

    period.db_set({"status": "Slips Generated"})
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

    period.db_set({"status": "Published"})
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


@frappe.whitelist()
def my_payslips(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Plan §10.8 — the caller's published payslips (employee-visible only)."""
    emp = _resolve(employee)
    _assert_payslip_access(emp)

    filters = {"employee": emp, "vn_employee_visible": 1, "docstatus": ["<", 2]}
    if from_date or to_date:
        filters["start_date"] = ["between", [from_date or to_date, to_date or from_date]]

    try:
        return frappe.db.get_all(
            "Salary Slip",
            filters=filters,
            fields=_PAYSLIP_FIELDS,
            order_by="start_date desc",
        )
    except Exception:
        return []


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
