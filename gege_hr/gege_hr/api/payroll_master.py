"""
Payroll / Leave Master API — Zero-Frappe P2 (plan §2.3 / §5-P2, gaps G8 + G9).

Lets the HR Manager build Salary Structures, assign them to employees, manage
Leave Periods / Leave Policies and grant annual leave entitlements entirely
from inside the HR app — without ever touching Frappe Desk.

These DocTypes carry complex child tables (earnings/deductions, leave policy
details) and submittable validation, so they get dedicated RPC endpoints here
rather than the generic ``frappe.client.*`` path used for simple masters (P1).

Permission + audit conventions are identical to ``api/admin.py``: every call is
gated by ``frappe.only_for(HR_ADMIN_ROLES)`` and emits a ``VN Audit Event``
through ``admin._audit_admin`` (audit_type ``"Manual Override"``).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api.admin import (
    _audit_admin,
    _company_for_employee,
    _default_company,
    _require_hr_admin,
)

EARNING_COMPONENTS = "earnings"
DEDUCTION_COMPONENTS = "deductions"

# Detail-row (Salary Detail) keys we recognise from the frontend payload.
_SALARY_DETAIL_KEYS = (
    "salary_component",
    "abbr",
    "amount",
    "amount_based_on_formula",
    "formula",
    "condition",
    "statistical_component",
    "depends_on_payment_days",
    "do_not_include_in_total",
    "is_tax_applicable",
    "is_flexible_benefit",
)

_LEAVE_POLICY_DETAIL_KEYS = ("leave_type", "annual_allocation")


def _coerce_bool(v):
    """Accept JS-style booleans (true/false) or 0/1 from the SPA payload."""
    if isinstance(v, bool):
        return v
    if v in (1, "1", "true", "True", "Yes", "yes"):
        return True
    return False


def _clean_salary_details(rows):
    """Normalise the earnings/deductions child-table payload from the SPA.

    Drops blank rows and Frappe meta keys; casts numeric/boolean fields so the
    backend never stores a JS string where Frappe expects a Float/Check.
    """
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        comp = (row.get("salary_component") or "").strip()
        if not comp:
            continue
        clean = {k: row[k] for k in _SALARY_DETAIL_KEYS if k in row}
        clean["salary_component"] = comp
        if "amount" in clean:
            try:
                clean["amount"] = frappe.utils.flt(clean["amount"])
            except (TypeError, ValueError):
                clean["amount"] = 0.0
        clean["amount_based_on_formula"] = _coerce_bool(clean.get("amount_based_on_formula"))
        clean["statistical_component"] = _coerce_bool(clean.get("statistical_component"))
        clean["depends_on_payment_days"] = _coerce_bool(clean.get("depends_on_payment_days"))
        clean["do_not_include_in_total"] = _coerce_bool(clean.get("do_not_include_in_total"))
        out.append(clean)
    return out


# ── G8: Salary Structure ───────────────────────────────────────────────────
@frappe.whitelist()
def list_salary_structures(company: str = "", is_active: int = 1, limit: int = 200) -> list[dict]:
    """Return Salary Structures (projection for the admin table)."""
    _require_hr_admin()
    filters = []
    if company:
        filters.append(["company", "=", company])
    if is_active:
        filters.append(["is_active", "=", 1])
    rows = frappe.get_all(
        "Salary Structure",
        fields=["name", "company", "payroll_frequency", "is_active", "currency"],
        filters=filters,
        limit_page_length=limit,
        order_by="name asc",
    )
    return rows


@frappe.whitelist()
def get_salary_structure(name: str) -> dict:
    """Return a full Salary Structure including earnings/deductions rows."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Salary Structure", name):
        frappe.throw(_("Bảng lương không tồn tại."))
    doc = frappe.get_doc("Salary Structure", name)
    return {
        "name": doc.name,
        "company": doc.company,
        "payroll_frequency": doc.payroll_frequency,
        "currency": doc.currency,
        "is_active": doc.is_active,
        "earnings": [
            {k: r.get(k) for k in _SALARY_DETAIL_KEYS if r.get(k) is not None} for r in (doc.earnings or [])
        ],
        "deductions": [
            {k: r.get(k) for k in _SALARY_DETAIL_KEYS if r.get(k) is not None} for r in (doc.deductions or [])
        ],
    }


@frappe.whitelist()
def save_salary_structure(
    name: str = "",
    salary_structure: str = "",
    company: str = "",
    payroll_frequency: str = "Monthly",
    currency: str = "VND",
    is_active: int = 1,
    earnings: list | None = None,
    deductions: list | None = None,
) -> dict:
    """Create or update a Salary Structure (Draft → keep editable).

    The structure is intentionally left ``docstatus=0`` so HR can revise
    earnings/deductions freely. Assignments reference it by name and are not
    affected by later edits to the structure itself.
    """
    _require_hr_admin()
    label = (salary_structure or "").strip()
    if not label:
        frappe.throw(_("Tên bảng lương là bắt buộc."))
    if not company:
        company = _default_company()
    if not company:
        frappe.throw(_("Không xác định được công ty."))

    earnings = _clean_salary_details(earnings)
    deductions = _clean_salary_details(deductions)
    if not earnings:
        frappe.throw(_("Bảng lương phải có ít nhất một khoản thu nhập (earnings)."))

    payload = {
        "doctype": "Salary Structure",
        "company": company,
        "payroll_frequency": payroll_frequency or "",
        "currency": currency or "VND",
        "is_active": _coerce_bool(is_active),
        "earnings": earnings,
        "deductions": deductions,
    }

    name = (name or "").strip()
    is_new = not name
    if is_new:
        payload["salary_structure"] = label
        doc = frappe.get_doc(payload)
        doc.insert()
        ref = doc.name
    else:
        if not frappe.db.exists("Salary Structure", name):
            frappe.throw(_("Bảng lương không tồn tại."))
        doc = frappe.get_doc("Salary Structure", name)
        doc.company = company
        doc.payroll_frequency = payroll_frequency or ""
        doc.currency = currency or "VND"
        doc.is_active = _coerce_bool(is_active)
        doc.set("earnings", earnings)
        doc.set("deductions", deductions)
        doc.save()
        ref = doc.name

    _audit_admin(
        _("Cập nhật bảng lương {0}").format(ref),
        reference_doctype="Salary Structure",
        reference_name=ref,
        company=company,
        new_value={
            "earnings": len(earnings),
            "deductions": len(deductions),
            "is_active": _coerce_bool(is_active),
        },
    )
    return {"name": ref}


@frappe.whitelist()
def assign_salary_structure(
    employee: str,
    salary_structure: str,
    from_date: str,
    base: float = 0,
    variable: float = 0,
    to_date: str = "",
    company: str = "",
) -> dict:
    """Create + submit a Salary Structure Assignment for one employee.

    Frappe enforces one active assignment per employee per date range; we add a
    friendly Vietnamese message on overlap and emit an audit row on success.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    salary_structure = (salary_structure or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not salary_structure or not frappe.db.exists("Salary Structure", salary_structure):
        frappe.throw(_("Bảng lương không tồn tại."))
    if not from_date:
        frappe.throw(_("Ngày áp dụng là bắt buộc."))

    if not company:
        company = _company_for_employee(employee)
    start = getdate(from_date)
    end = getdate(to_date) if to_date else None

    _ensure_no_overlapping_assignment(employee, start, end)

    doc = frappe.get_doc(
        {
            "doctype": "Salary Structure Assignment",
            "employee": employee,
            "salary_structure": salary_structure,
            "company": company,
            "from_date": start,
            "to_date": end,
            "base": frappe.utils.flt(base or 0),
            "variable": frappe.utils.flt(variable or 0),
        }
    )
    doc.insert()
    doc.submit()

    _audit_admin(
        _("Gán bảng lương {0} cho {1}").format(salary_structure, employee),
        reference_doctype="Salary Structure Assignment",
        reference_name=doc.name,
        company=company,
        employee=employee,
        new_value={
            "salary_structure": salary_structure,
            "from_date": str(start),
            "to_date": str(end) if end else None,
            "base": frappe.utils.flt(base or 0),
        },
    )
    return {"name": doc.name}


def _ensure_no_overlapping_assignment(employee, start, end):
    """Raise on an overlapping active Salary Structure Assignment."""
    filters = [["employee", "=", employee], ["docstatus", "=", 1]]
    existing = frappe.get_all(
        "Salary Structure Assignment",
        fields=["name", "from_date", "to_date"],
        filters=filters,
    )
    for row in existing:
        r_from = getdate(row["from_date"])
        r_to = getdate(row["to_date"]) if row.get("to_date") else None
        overlap = (not end or r_from <= end) and (not r_to or start <= r_to)
        if overlap:
            frappe.throw(_("Nhân viên đã có bảng lương áp dụng trong khoảng này ({0}).").format(row["name"]))


@frappe.whitelist()
def list_salary_assignments(employee: str = "", limit: int = 200) -> list[dict]:
    """Return submitted Salary Structure Assignments for the admin table."""
    _require_hr_admin()
    filters = [["docstatus", "=", 1]]
    if employee:
        filters.append(["employee", "=", employee])
    rows = frappe.get_all(
        "Salary Structure Assignment",
        # NOTE: standard Frappe "Salary Structure Assignment" has no `to_date`
        # column (only `from_date`) — projecting it raised OperationalError
        # 1054 "Unknown column 'to_date'". Removed.
        fields=["name", "employee", "employee_name", "salary_structure", "from_date", "base", "company"],
        filters=filters,
        limit_page_length=limit,
        order_by="from_date desc",
    )
    return rows


# ── G9: Leave Period ───────────────────────────────────────────────────────
@frappe.whitelist()
def list_leave_periods(company: str = "", is_active: int = 1, limit: int = 100) -> list[dict]:
    """Return Leave Periods for the admin table."""
    _require_hr_admin()
    filters = []
    if company:
        filters.append(["company", "=", company])
    if is_active:
        filters.append(["is_active", "=", 1])
    rows = frappe.get_all(
        "Leave Period",
        fields=["name", "from_date", "to_date", "company", "is_active"],
        filters=filters,
        limit_page_length=limit,
        order_by="from_date desc",
    )
    return rows


@frappe.whitelist()
def create_leave_period(
    from_date: str,
    to_date: str,
    company: str = "",
    is_active: int = 1,
    name: str = "",
) -> dict:
    """Create a Leave Period (annual leave year, e.g. 2026).

    ``name`` is optional — Frappe can auto-name; passing an explicit label makes
    the period easier to pick in the assignment form.
    """
    _require_hr_admin()
    if not from_date or not to_date:
        frappe.throw(_("Ngày bắt đầu và ngày kết thúc là bắt buộc."))
    start = getdate(from_date)
    end = getdate(to_date)
    if end < start:
        frappe.throw(_("Ngày kết thúc không được trước ngày bắt đầu."))
    if not company:
        company = _default_company()

    payload = {
        "doctype": "Leave Period",
        "from_date": start,
        "to_date": end,
        "company": company or "",
        "is_active": _coerce_bool(is_active),
    }
    label = (name or "").strip()
    if label:
        payload["name"] = label

    doc = frappe.get_doc(payload)
    doc.insert()

    _audit_admin(
        _("Tạo kỳ nghỉ phép {0}").format(doc.name),
        reference_doctype="Leave Period",
        reference_name=doc.name,
        company=company or None,
        new_value={"from_date": str(start), "to_date": str(end)},
    )
    return {"name": doc.name}


# ── G9: Leave Policy ───────────────────────────────────────────────────────
@frappe.whitelist()
def list_leave_policies(limit: int = 200) -> list[dict]:
    """Return Leave Policies + their annual allocation lines."""
    _require_hr_admin()
    rows = frappe.get_all(
        "Leave Policy", fields=["name", "docstatus"], limit_page_length=limit, order_by="name asc"
    )
    out = []
    for r in rows:
        details = frappe.get_all(
            "Leave Policy Detail",
            fields=["leave_type", "annual_allocation"],
            filters={"parent": r["name"]},
        )
        out.append({**r, "leave_policy_details": details})
    return out


@frappe.whitelist()
def save_leave_policy(
    leave_policy: str,
    details: list | None = None,
    name: str = "",
) -> dict:
    """Create or update a Leave Policy (Draft → keep editable).

    ``details`` is a list of ``{leave_type, annual_allocation}`` rows.
    """
    _require_hr_admin()
    label = (leave_policy or "").strip()
    if not label:
        frappe.throw(_("Tên chính sách phép là bắt buộc."))

    cleaned = []
    for row in details or []:
        if not isinstance(row, dict):
            continue
        lt = (row.get("leave_type") or "").strip()
        if not lt:
            continue
        try:
            allocation = frappe.utils.flt(row.get("annual_allocation"))
        except (TypeError, ValueError):
            allocation = 0.0
        if allocation <= 0:
            continue
        cleaned.append({"leave_type": lt, "annual_allocation": allocation})
    if not cleaned:
        frappe.throw(_("Chính sách phép phải có ít nhất một loại phép với số ngày > 0."))

    name = (name or "").strip()
    if name and frappe.db.exists("Leave Policy", name):
        doc = frappe.get_doc("Leave Policy", name)
        doc.set("leave_policy_details", cleaned)
        doc.save()
        ref = doc.name
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Leave Policy",
                "leave_policy": label,
                "leave_policy_details": cleaned,
            }
        )
        doc.insert()
        ref = doc.name

    _audit_admin(
        _("Cập nhật chính sách phép {0}").format(ref),
        reference_doctype="Leave Policy",
        reference_name=ref,
        new_value={"lines": len(cleaned)},
    )
    return {"name": ref}


@frappe.whitelist()
def assign_leave_policy(
    employee: str,
    leave_policy: str,
    leave_period: str = "",
    effective_from: str = "",
    effective_to: str = "",
    company: str = "",
    assignment_based_on: str = "Leave Period",
) -> dict:
    """Create + submit a Leave Policy Assignment (grants annual leave).

    Default ``assignment_based_on`` is ``Leave Period``; if no Leave Period is
    supplied we fall back to ``Joining Date`` with explicit effective dates.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    leave_policy = (leave_policy or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not leave_policy or not frappe.db.exists("Leave Policy", leave_policy):
        frappe.throw(_("Chính sách phép không tồn tại."))

    if not company:
        company = _company_for_employee(employee)

    based_on = assignment_based_on or "Leave Period"
    if based_on == "Leave Period" and not leave_period:
        frappe.throw(_("Kỳ nghỉ phép là bắt buộc khi gán theo kỳ."))
    if based_on == "Joining Date" and not effective_from:
        frappe.throw(_("Ngày hiệu lực là bắt buộc khi gán theo ngày vào."))

    payload = {
        "doctype": "Leave Policy Assignment",
        "employee": employee,
        "leave_policy": leave_policy,
        "company": company or "",
        "assignment_based_on": based_on,
    }
    if based_on == "Leave Period":
        payload["leave_period"] = leave_period
        # effective_from/to are auto-filled from the leave period by Frappe.
    else:
        payload["effective_from"] = getdate(effective_from)
        if effective_to:
            payload["effective_to"] = getdate(effective_to)

    doc = frappe.get_doc(payload)
    doc.insert()
    doc.submit()  # submit → Frappe creates Leave Allocations

    _audit_admin(
        _("Gán chính sách phép {0} cho {1}").format(leave_policy, employee),
        reference_doctype="Leave Policy Assignment",
        reference_name=doc.name,
        company=company,
        employee=employee,
        new_value={
            "leave_policy": leave_policy,
            "leave_period": leave_period,
            "assignment_based_on": based_on,
        },
    )
    return {"name": doc.name}


@frappe.whitelist()
def list_leave_policy_assignments(employee: str = "", limit: int = 200) -> list[dict]:
    """Return submitted Leave Policy Assignments for the admin table."""
    _require_hr_admin()
    filters = [["docstatus", "=", 1]]
    if employee:
        filters.append(["employee", "=", employee])
    rows = frappe.get_all(
        "Leave Policy Assignment",
        fields=[
            "name",
            "employee",
            "employee_name",
            "leave_policy",
            "leave_period",
            "effective_from",
            "effective_to",
            "company",
        ],
        filters=filters,
        limit_page_length=limit,
        order_by="effective_from desc",
    )
    return rows
