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
from gege_hr.gege_hr.utils import pagination

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
    # Salary Structure.is_active is a Select("Yes"/"No"), NOT a Check. Map the
    # truthy payload: 1 → Yes, 0 → No (0 used to fall through and return the
    # unfiltered list, duplicating rows when callers merged both halves).
    filters.append(["is_active", "=", "Yes" if is_active else "No"])
    rows = frappe.get_all(
        "Salary Structure",
        fields=["name", "company", "payroll_frequency", "is_active", "currency"],
        filters=filters,
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="name asc",
    )
    # WP-QA-SSA: fixed (non-formula) earnings travel into the hourly pay via
    # _employee_salary_components — expose the count so the Quick-Assign modal
    # can warn "re-calculate to pick up allowances" BEFORE assigning.
    try:
        names = [r["name"] for r in rows if r.get("name")]
        counts: dict = {}
        if names:
            for d in frappe.get_all(
                "Salary Detail",
                filters={
                    "parent": ["in", names],
                    "parentfield": "earnings",
                    "amount_based_on_formula": 0,
                },
                fields=["parent"],
            ):
                counts[d["parent"]] = counts.get(d["parent"], 0) + 1
        for r in rows:
            r["fixed_allowance_count"] = counts.get(r["name"], 0)
    except Exception:
        for r in rows:
            r.setdefault("fixed_allowance_count", 0)
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

    # Salary Structure.is_active is a Select("Yes"/"No") — a raw boolean fails
    # validation ("Is Active cannot be True").
    is_active_flag = "Yes" if _coerce_bool(is_active) else "No"
    payload = {
        "doctype": "Salary Structure",
        "company": company,
        "payroll_frequency": payroll_frequency or "",
        "currency": currency or "VND",
        "is_active": is_active_flag,
        "earnings": earnings,
        "deductions": deductions,
    }

    name = (name or "").strip()
    is_new = not name
    if is_new:
        if frappe.db.exists("Salary Structure", label):
            frappe.throw(_("Bảng lương {0} đã tồn tại.").format(label))
        payload["salary_structure"] = label
        # Salary Structure carries no autoname (prompt/field-name doc) — the
        # label must double as the document name or insert() throws
        # "Please set the document name".
        payload["name"] = label
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
        doc.is_active = is_active_flag
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


def _parse_employee_list(employees) -> list[str]:
    """Whitelist params arrive as list or JSON-encoded list — coerce + dedupe."""
    if isinstance(employees, str):
        import json

        try:
            employees = json.loads(employees)
        except Exception:
            employees = [employees]
    if not isinstance(employees, (list, tuple)):
        employees = [employees] if employees else []
    seen: list[str] = []
    for e in employees:
        eid = (e or "").strip() if isinstance(e, str) else ""
        if eid and eid not in seen:
            seen.append(eid)
    return seen


@frappe.whitelist()
def ssa_status(employees) -> dict:
    """Per-employee SSA readiness for the Quick-Assign modal (WP-QA-SSA).

    ``assignable=True`` ⇔ the employee has NO submitted SSA at all — the only
    case where bulk-assign is safe (draft-only employees are surfaced with
    ``DRAFT_ONLY`` so HR can fix them at the Desk instead of double-creating).
    """
    _require_hr_admin()
    ids = _parse_employee_list(employees)
    items: list[dict] = []
    for eid in ids:
        emp_name = frappe.db.get_value("Employee", eid, "employee_name") or ""
        submitted = frappe.get_all(
            "Salary Structure Assignment",
            filters={"employee": eid, "docstatus": 1},
            fields=["name", "salary_structure", "from_date"],
            order_by="from_date desc",
            limit=1,
        )
        has_draft = bool(
            frappe.get_all(
                "Salary Structure Assignment",
                filters={"employee": eid, "docstatus": 0},
                limit=1,
            )
        )
        if submitted:
            row = submitted[0]
            items.append(
                {
                    "employee": eid,
                    "employee_name": emp_name,
                    "has_submitted": True,
                    "has_draft": has_draft,
                    "structure": row.get("salary_structure"),
                    "from_date": str(row.get("from_date") or ""),
                    "assignable": False,
                    "reason": "HAS_SSA",
                }
            )
        elif has_draft:
            items.append(
                {
                    "employee": eid,
                    "employee_name": emp_name,
                    "has_submitted": False,
                    "has_draft": True,
                    "structure": None,
                    "from_date": "",
                    "assignable": False,
                    "reason": "DRAFT_ONLY",
                }
            )
        else:
            items.append(
                {
                    "employee": eid,
                    "employee_name": emp_name,
                    "has_submitted": False,
                    "has_draft": False,
                    "structure": None,
                    "from_date": "",
                    "assignable": True,
                    "reason": "OK",
                }
            )
    return {"items": items}


@frappe.whitelist()
def bulk_assign_salary_structure(
    employees,
    salary_structure: str,
    from_date: str = "",
    base: float = 0,
) -> dict:
    """Assign one Salary Structure to many employees (WP-QA-SSA Quick-Assign).

    Loops the battle-tested :func:`assign_salary_structure` engine per employee
    (overlap validation + audit rows included). No global transaction: every
    employee lands in exactly one bucket —

      assigned  SSA created + submitted
      skipped   already has / overlap (Vietnamese reason from the engine)
      failed    unexpected error (traceback logged per employee)

    ``from_date=""`` (default) uses each employee's ``date_of_joining`` — always
    retro-correct (≤ any period start). ``base`` is nominal metadata only: pay
    amounts come from the hourly engine and are stamped over slip totals.
    """
    _require_hr_admin()
    ids = _parse_employee_list(employees)
    salary_structure = (salary_structure or "").strip()
    if not ids:
        frappe.throw(_("Danh sách nhân viên trống."))
    if not salary_structure or not frappe.db.exists("Salary Structure", salary_structure):
        frappe.throw(_("Bảng lương không tồn tại."))
    st = frappe.db.get_value(
        "Salary Structure", salary_structure, ["docstatus", "is_active"], as_dict=True
    )
    st = st if isinstance(st, dict) else {}
    if st.get("docstatus") != 1 or st.get("is_active") != "Yes":
        frappe.throw(_("Bảng lương {0} chưa submit hoặc đã ngừng hoạt động.").format(salary_structure))

    # Fixed-amount earnings travel into the hourly pay via _employee_salary_components —
    # surfaced so the UI can warn "re-calculate to pick up allowances".
    try:
        st_doc = frappe.get_doc("Salary Structure", salary_structure)
        fixed_allowance_count = sum(
            1
            for r in (st_doc.earnings or [])
            if r.amount and not r.amount_based_on_formula
        )
    except Exception:
        fixed_allowance_count = 0

    assigned: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for eid in ids:
        per_emp_from = (from_date or "").strip()
        if not per_emp_from:
            per_emp_from = str(
                frappe.db.get_value("Employee", eid, "date_of_joining")
                or getdate()
            )
        try:
            assign_salary_structure(
                employee=eid,
                salary_structure=salary_structure,
                from_date=per_emp_from,
                base=base,
            )
            assigned.append(eid)
        except Exception as exc:
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            bucket = skipped if _is_overlap_reason(reason) else failed
            bucket.append({"employee": eid, "reason": reason[:200]})
            if bucket is failed:
                try:
                    frappe.log_error(
                        title="bulk assign SSA failed",
                        message=f"{eid} → {salary_structure}\n{frappe.get_traceback()}",
                    )
                except Exception:
                    pass

    _audit_admin(
        _("Bulk gán bảng lương {0} cho {1} nhân viên").format(salary_structure, len(assigned)),
        reference_doctype="Salary Structure",
        reference_name=salary_structure,
        new_value={
            "assigned": len(assigned),
            "skipped": len(skipped),
            "failed": len(failed),
            "employees": assigned[:50],
        },
    )
    frappe.db.commit()
    message = _("Đã gán {0} nhân viên.").format(len(assigned))
    if skipped or failed:
        message = _("Đã gán {0} — bỏ qua {1}, lỗi {2}.").format(
            len(assigned), len(skipped), len(failed)
        )
    return {
        "assigned": assigned,
        "skipped": skipped,
        "failed": failed,
        "total": len(ids),
        "fixed_allowance_count": fixed_allowance_count,
        "message": message,
    }


@frappe.whitelist()
def cancel_salary_structure_assignment(employee: str, name: str, reason: str = "") -> dict:
    """Cancel ONE submitted Salary Structure Assignment (WP-QA-SSA Sprint 3).

    ``reason`` is mandatory and lands in the audit row — HR must justify every
    unassignment (payroll traceability).
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    name = (name or "").strip()
    reason = (reason or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not name or not frappe.db.exists("Salary Structure Assignment", name):
        frappe.throw(_("Bản gán lương không tồn tại."))
    if not reason:
        frappe.throw(_("Lý do hủy gán là bắt buộc."))

    doc = frappe.get_doc("Salary Structure Assignment", name)
    if doc.employee != employee:
        frappe.throw(_("Bản gán lương không thuộc nhân viên này."))
    if doc.docstatus != 1:
        frappe.throw(_("Chỉ có thể hủy bản gán đã submit."))

    doc.cancel()
    _audit_admin(
        _("Hủy gán bảng lương {0} của {1}").format(doc.salary_structure, employee),
        reference_doctype="Salary Structure Assignment",
        reference_name=name,
        employee=employee,
        old_value={"salary_structure": doc.salary_structure, "from_date": str(doc.from_date)},
        new_value={"cancelled": True, "reason": reason[:500]},
    )
    frappe.db.commit()
    return {"name": name, "cancelled": True}


@frappe.whitelist()
def bulk_cancel_assignments(employees, reason: str = "") -> dict:
    """Cancel the newest submitted SSA of each employee (WP-QA-SSA Sprint 3).

    Mirrors ``bulk_assign_salary_structure`` buckets: ``cancelled`` /
    ``skipped`` (no submitted SSA) / ``failed`` (linked slips & other errors,
    traceback logged). ``reason`` is mandatory and audited once per employee.
    """
    _require_hr_admin()
    ids = _parse_employee_list(employees)
    reason = (reason or "").strip()
    if not ids:
        frappe.throw(_("Danh sách nhân viên trống."))
    if not reason:
        frappe.throw(_("Lý do hủy gán là bắt buộc."))

    cancelled: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for eid in ids:
        latest = frappe.get_all(
            "Salary Structure Assignment",
            filters={"employee": eid, "docstatus": 1},
            fields=["name"],
            order_by="from_date desc",
            limit=1,
        )
        if not latest:
            skipped.append({"employee": eid, "reason": "Không có SSA đã submit"})
            continue
        try:
            cancel_salary_structure_assignment(employee=eid, name=latest[0]["name"], reason=reason)
            cancelled.append(eid)
        except Exception as exc:
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            failed.append({"employee": eid, "reason": msg[:200]})
            try:
                frappe.log_error(
                    title="bulk cancel SSA failed",
                    message=f"{eid}\n{frappe.get_traceback()}",
                )
            except Exception:
                pass
    message = _("Đã hủy gán {0} nhân viên.").format(len(cancelled))
    if skipped or failed:
        message = _("Đã hủy {0} — bỏ qua {1}, lỗi {2}.").format(
            len(cancelled), len(skipped), len(failed)
        )
    return {
        "cancelled": cancelled,
        "skipped": skipped,
        "failed": failed,
        "total": len(ids),
        "message": message,
    }


@frappe.whitelist()
def ssa_timeline(employee: str) -> dict:
    """Full SSA history of one employee for the modal's timeline view.

    Returns ``{employee, events: [{name, salary_structure, from_date, base,
    docstatus, state, creation}]}`` with state ∈ Draft/Submitted/Cancelled —
    newest first.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    rows = frappe.get_all(
        "Salary Structure Assignment",
        filters={"employee": employee},
        fields=["name", "salary_structure", "from_date", "base", "docstatus", "creation"],
        order_by="creation desc",
        limit_page_length=50,
    )
    state = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    events = [
        {
            **r,
            "from_date": str(r.get("from_date") or ""),
            "state": state.get(r.get("docstatus"), "?"),
        }
        for r in rows
    ]
    return {"employee": employee, "events": events}


def _is_overlap_reason(reason: str) -> bool:
    """Engine already-has / overlap messages — retry-safe skips, not errors."""
    r = (reason or "").lower()
    return any(
        k in r
        for k in ("overlap", "đã có", "đã tồn tại", "already", "trùng", "hiệu lực")
    )


def _ensure_no_overlapping_assignment(employee, start, end):
    """Raise on an overlapping active Salary Structure Assignment.

    Standard Frappe ``Salary Structure Assignment`` has no ``to_date`` column
    (only ``from_date``) — so an assignment is "active indefinitely from
    from_date". We treat any existing submitted assignment whose ``from_date``
    precedes ``end`` as an overlap (same logic the HRMS native check uses).
    """
    filters = [["employee", "=", employee], ["docstatus", "=", 1]]
    existing = frappe.get_all(
        "Salary Structure Assignment",
        fields=["name", "from_date"],
        filters=filters,
    )
    for row in existing:
        r_from = getdate(row["from_date"])
        # Interval overlap [r_from, r_to?] ∩ [start, end?]. A missing bound
        # means open-ended (+∞): standard Frappe SSA has no ``to_date`` column,
        # but rows that DO carry one (VN-seeded / custom) must bound the check —
        # the previous ``start <= r_from or r_from <= start`` was a tautology
        # that flagged every disjoint past assignment.
        r_to = getdate(row["to_date"]) if row.get("to_date") else None
        new_to = getdate(end) if end else None
        starts_before_new_end = new_to is None or r_from <= new_to
        old_ends_after_new_start = r_to is None or r_to >= start
        if starts_before_new_end and old_ends_after_new_start:
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
        limit_page_length=pagination.clamp_limit(limit, default=200),
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
        limit_page_length=pagination.clamp_limit(limit, default=200),
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
        "Leave Policy",
        fields=["name", "docstatus"],
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="name asc",
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
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="effective_from desc",
    )
    return rows
