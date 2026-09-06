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

import calendar
import datetime
import json

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
    st = frappe.db.get_value("Salary Structure", salary_structure, ["docstatus", "is_active"], as_dict=True)
    st = st if isinstance(st, dict) else {}
    if st.get("docstatus") != 1 or st.get("is_active") != "Yes":
        frappe.throw(_("Bảng lương {0} chưa submit hoặc đã ngừng hoạt động.").format(salary_structure))

    # Fixed-amount earnings travel into the hourly pay via _employee_salary_components —
    # surfaced so the UI can warn "re-calculate to pick up allowances".
    try:
        st_doc = frappe.get_doc("Salary Structure", salary_structure)
        fixed_allowance_count = sum(
            1 for r in (st_doc.earnings or []) if r.amount and not r.amount_based_on_formula
        )
    except Exception:
        fixed_allowance_count = 0

    assigned: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for eid in ids:
        per_emp_from = (from_date or "").strip()
        if not per_emp_from:
            per_emp_from = str(frappe.db.get_value("Employee", eid, "date_of_joining") or getdate())
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
        message = _("Đã gán {0} — bỏ qua {1}, lỗi {2}.").format(len(assigned), len(skipped), len(failed))
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
        message = _("Đã hủy {0} — bỏ qua {1}, lỗi {2}.").format(len(cancelled), len(skipped), len(failed))
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
    return any(k in r for k in ("overlap", "đã có", "đã tồn tại", "already", "trùng", "hiệu lực"))


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
def list_leave_periods(
    q: str = "",
    company: str = "",
    is_active: int | str | None = 1,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return Leave Periods for the admin table (server-side search, §2.15).

    ``is_active`` now also accepts ""/"all"/None = every status so the SPA can
    drop the old active+inactive double fetch (backlog HR-BL-leave).
    """
    _require_hr_admin()
    filters = []
    if company:
        filters.append(["company", "=", company])
    active = "" if is_active is None else str(is_active).strip()
    if active not in ("", "all", "None"):
        filters.append(["is_active", "=", 1 if _coerce_bool(active) else 0])
    or_filters = []
    q = (q or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [["name", "like", like], ["company", "like", like]]
    rows = frappe.get_all(
        "Leave Period",
        fields=["name", "from_date", "to_date", "company", "is_active"],
        filters=filters,
        or_filters=or_filters or None,
        limit_start=max(0, pagination.as_int(offset, 0)),
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


@frappe.whitelist()
def update_leave_period(
    name: str,
    from_date: str | None = None,
    to_date: str | None = None,
    company: str | None = None,
    is_active: int | str | None = None,
) -> dict:
    """Update a Leave Period's dates/company/active flag (plan §2.15).

    Dates are frozen once a submitted Leave Policy Assignment uses the period —
    changing them would silently drift live assignments' effective windows.
    """
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Period", name):
        frappe.throw(_("Kỳ nghỉ phép không tồn tại."))

    doc = frappe.get_doc("Leave Period", name)
    old = {
        "from_date": str(getattr(doc, "from_date", "") or ""),
        "to_date": str(getattr(doc, "to_date", "") or ""),
        "is_active": bool(getattr(doc, "is_active", 0)),
    }

    new_from = (from_date or "").strip()
    new_to = (to_date or "").strip()
    dates_change = (new_from and new_from != old["from_date"]) or (new_to and new_to != old["to_date"])
    if dates_change:
        used = frappe.get_all(
            "Leave Policy Assignment",
            filters={"leave_period": name, "docstatus": 1},
            fields=["name"],
            limit=1,
        )
        if used:
            frappe.throw(_("Không đổi được ngày của kỳ phép đang có lượt gán chính sách hiệu lực."))

    if new_from:
        doc.from_date = getdate(new_from)
    if new_to:
        doc.to_date = getdate(new_to)
    if getattr(doc, "from_date", None) and getattr(doc, "to_date", None):
        if getdate(doc.to_date) < getdate(doc.from_date):
            frappe.throw(_("Ngày kết thúc không được trước ngày bắt đầu."))
    if (company or "").strip():
        doc.company = company.strip()
    if is_active is not None and str(is_active).strip() != "":
        doc.is_active = _coerce_bool(is_active)
    doc.save()

    _audit_admin(
        _("Cập nhật kỳ nghỉ phép {0}").format(name),
        reference_doctype="Leave Period",
        reference_name=name,
        company=getattr(doc, "company", "") or None,
        new_value={
            "previous": old,
            "from_date": str(getattr(doc, "from_date", "") or ""),
            "to_date": str(getattr(doc, "to_date", "") or ""),
            "is_active": bool(getattr(doc, "is_active", 0)),
        },
    )
    frappe.db.commit()
    return {"name": name, "is_active": bool(getattr(doc, "is_active", 0))}


@frappe.whitelist()
def delete_leave_period(name: str) -> dict:
    """Delete a Leave Period — refused while assignments/allocations link it."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Period", name):
        frappe.throw(_("Kỳ nghỉ phép không tồn tại."))
    linked_assignment = frappe.get_all(
        "Leave Policy Assignment", filters={"leave_period": name}, fields=["name"], limit=1
    )
    linked_allocation = frappe.get_all(
        "Leave Allocation", filters={"leave_period": name}, fields=["name"], limit=1
    )
    if linked_assignment or linked_allocation:
        frappe.throw(_("Kỳ phép đang được sử dụng — không xoá được."))
    frappe.delete_doc("Leave Period", name)
    _audit_admin(
        _("Xoá kỳ nghỉ phép {0}").format(name),
        reference_doctype="Leave Period",
        reference_name=name,
    )
    frappe.db.commit()
    return {"name": name}


# ── G9: Leave Policy ───────────────────────────────────────────────────────
@frappe.whitelist()
def list_leave_policies(
    q: str = "",
    docstatus: int | str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Return Leave Policies + their annual allocation lines (server-side search).

    Now projects ``title`` (the HRMS-standard field — G1 fix) and
    ``amended_from`` so the SPA can render the docstatus lifecycle.
    """
    _require_hr_admin()
    filters = []
    ds = "" if docstatus is None else str(docstatus).strip()
    if ds not in ("", "None"):
        filters.append(["docstatus", "=", pagination.as_int(ds, 0)])
    or_filters = []
    q = (q or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [["name", "like", like], ["title", "like", like]]
    rows = frappe.get_all(
        "Leave Policy",
        fields=["name", "title", "docstatus", "amended_from"],
        filters=filters,
        or_filters=or_filters or None,
        limit_start=max(0, pagination.as_int(offset, 0)),
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="modified desc",
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


def _clean_leave_policy_details(details) -> list[dict]:
    """Normalise ``[{leave_type, annual_allocation}]`` rows; refuse duplicates.

    Whitelist form-data delivers the list JSON-encoded → parse it first
    (mirror ``_parse_employee_list``).
    """
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            details = []
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
    seen: set[str] = set()
    for row in cleaned:
        if row["leave_type"] in seen:
            frappe.throw(_("Mỗi loại phép chỉ xuất hiện một lần trong chính sách."))
        seen.add(row["leave_type"])
    return cleaned


@frappe.whitelist()
def save_leave_policy(
    title: str = "",
    details: list | None = None,
    name: str = "",
    leave_policy: str = "",
) -> dict:
    """Create or update a Leave Policy **Draft** (plan leave-policy §2.1, G1 fix).

    The HRMS-standard field is ``title`` — the old ``leave_policy`` kwarg stays
    as a deprecated alias. Submitted/Cancelled policies are read-only: use
    :func:`amend_leave_policy` for a replacement draft.

    ``details`` is a list of ``{leave_type, annual_allocation}`` rows.
    """
    _require_hr_admin()
    label = (title or leave_policy or "").strip()
    if not label:
        frappe.throw(_("Tên chính sách phép là bắt buộc."))

    cleaned = _clean_leave_policy_details(details)
    if not cleaned:
        frappe.throw(_("Chính sách phép phải có ít nhất một loại phép với số ngày > 0."))

    name = (name or "").strip()
    if name and frappe.db.exists("Leave Policy", name):
        doc = frappe.get_doc("Leave Policy", name)
        if int(getattr(doc, "docstatus", 0) or 0) != 0:
            frappe.throw(_("Chỉ chính sách ở trạng thái Nháp mới sửa được — hãy Hủy và tạo bản thay thế."))
        if label != (getattr(doc, "title", "") or ""):
            doc.title = label
        doc.set("leave_policy_details", cleaned)
        doc.save()
        ref = doc.name
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Leave Policy",
                "title": label,
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
    frappe.db.commit()
    return {
        "name": ref,
        "title": label,
        "docstatus": int(getattr(doc, "docstatus", 0) or 0),
    }


@frappe.whitelist()
def get_leave_policy(name: str) -> dict:
    """One Leave Policy with Leave Type meta + a ``can.*`` action matrix."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    doc = frappe.get_doc("Leave Policy", name)
    ds = int(getattr(doc, "docstatus", 0) or 0)

    meta_rows = frappe.get_all(
        "Leave Type",
        fields=[
            "name",
            "max_leaves_allowed",
            "is_carry_forward",
            "is_earned_leave",
            "is_lwp",
            "is_compensatory",
        ],
    )
    meta = {m["name"]: m for m in meta_rows or []}
    details = []
    for row in doc.get("leave_policy_details") or []:
        m = meta.get(row.get("leave_type"), {})
        details.append(
            {
                "leave_type": row.get("leave_type"),
                "annual_allocation": row.get("annual_allocation"),
                "max_leaves_allowed": m.get("max_leaves_allowed", 0),
                "is_carry_forward": bool(m.get("is_carry_forward")),
                "is_earned_leave": bool(m.get("is_earned_leave")),
                "is_lwp": bool(m.get("is_lwp")),
                "is_compensatory": bool(m.get("is_compensatory")),
            }
        )
    assignments = frappe.get_all(
        "Leave Policy Assignment",
        filters={"leave_policy": name, "docstatus": 1},
        fields=["name"],
        limit_page_length=0,
    )
    return {
        "name": doc.name,
        "title": getattr(doc, "title", "") or "",
        "docstatus": ds,
        "amended_from": getattr(doc, "amended_from", "") or "",
        "leave_policy_details": details,
        "assignment_count": len(assignments or []),
        "can": {
            "edit": ds == 0,
            "submit": ds == 0,
            "cancel": ds == 1,
            "amend": ds in (1, 2),
            "delete": ds in (0, 2),
            "rename": ds == 1,  # HRMS marks `title` allow_on_submit
        },
    }


@frappe.whitelist()
def submit_leave_policy(name: str) -> dict:
    """Submit a Draft Leave Policy (docstatus 0 → 1, plan §2.2)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    doc = frappe.get_doc("Leave Policy", name)
    if int(getattr(doc, "docstatus", 0) or 0) != 0:
        frappe.throw(_("Chỉ chính sách ở trạng thái Nháp mới duyệt được."))
    doc.submit()
    _audit_admin(
        _("Duyệt chính sách phép {0}").format(name),
        reference_doctype="Leave Policy",
        reference_name=name,
        new_value={"docstatus": 1},
    )
    frappe.db.commit()
    return {"name": name, "docstatus": 1}


def _assert_policy_cancel(doc, name: str) -> None:
    """Shared guard + cancel for cancel/amend (maps LinkExists → Vietnamese)."""
    if int(getattr(doc, "docstatus", 0) or 0) != 1:
        frappe.throw(_("Chỉ chính sách đã duyệt mới hủy được."))
    try:
        doc.cancel()
    except Exception as exc:
        low = str(exc).lower()
        if "linkexists" in exc.__class__.__name__.lower() or "linked" in low or "cannot cancel" in low:
            frappe.throw(
                _("Không hủy được: chính sách đang được gán cho nhân viên. Hãy hủy các lượt gán trước.")
            )
        raise


@frappe.whitelist()
def cancel_leave_policy(name: str, reason: str = "") -> dict:
    """Cancel a submitted Leave Policy (docstatus 1 → 2, plan §2.3)."""
    _require_hr_admin()
    name = (name or "").strip()
    reason = (reason or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    if not reason:
        frappe.throw(_("Lý do hủy là bắt buộc."))
    doc = frappe.get_doc("Leave Policy", name)
    _assert_policy_cancel(doc, name)
    _audit_admin(
        _("Hủy chính sách phép {0}").format(name),
        reference_doctype="Leave Policy",
        reference_name=name,
        new_value={"docstatus": 2, "reason": reason[:500]},
    )
    frappe.db.commit()
    return {"name": name, "docstatus": 2}


@frappe.whitelist()
def amend_leave_policy(name: str, title: str = "", details: list | None = None) -> dict:
    """Cancel (if submitted) + open a replacement Draft with ``amended_from``."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    doc = frappe.get_doc("Leave Policy", name)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    if ds == 0:
        frappe.throw(_("Bản Nháp sửa trực tiếp được, không cần tạo bản thay thế."))
    if ds == 1:
        _assert_policy_cancel(doc, name)

    new_doc = frappe.copy_doc(doc)
    new_doc.docstatus = 0
    new_doc.amended_from = name
    new_title = (title or "").strip()
    if new_title:
        new_doc.title = new_title
    cleaned = _clean_leave_policy_details(details) if details is not None else []
    if cleaned:
        new_doc.set("leave_policy_details", cleaned)
    new_doc.insert()

    _audit_admin(
        _("Tạo bản thay thế {0} cho chính sách {1}").format(new_doc.name, name),
        reference_doctype="Leave Policy",
        reference_name=new_doc.name,
        new_value={"amended_from": name},
    )
    frappe.db.commit()
    return {"name": new_doc.name, "amended_from": name}


@frappe.whitelist()
def delete_leave_policy(name: str) -> dict:
    """Permanently delete a Draft/Cancelled Leave Policy (plan §2.5)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    doc = frappe.get_doc("Leave Policy", name)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    if ds == 1:
        frappe.throw(_("Chính sách đã duyệt phải hủy trước khi xoá."))
    try:
        frappe.delete_doc("Leave Policy", name)
    except Exception as exc:
        low = str(exc).lower()
        if "linkexists" in exc.__class__.__name__.lower() or "linked" in low:
            frappe.throw(_("Chính sách đang được tham chiếu, không xoá được."))
        raise
    _audit_admin(
        _("Xoá chính sách phép {0}").format(name),
        reference_doctype="Leave Policy",
        reference_name=name,
        new_value={"docstatus": ds},
    )
    frappe.db.commit()
    return {"name": name}


@frappe.whitelist()
def duplicate_leave_policy(name: str, title: str) -> dict:
    """Copy a policy into a fresh Draft under a new title (plan §2.6)."""
    _require_hr_admin()
    name = (name or "").strip()
    new_title = (title or "").strip()
    if not name or not frappe.db.exists("Leave Policy", name):
        frappe.throw(_("Chính sách phép không tồn tại."))
    if not new_title:
        frappe.throw(_("Tên chính sách mới là bắt buộc."))
    doc = frappe.get_doc("Leave Policy", name)
    new_doc = frappe.copy_doc(doc)
    new_doc.docstatus = 0
    new_doc.amended_from = ""
    new_doc.title = new_title
    new_doc.insert()
    _audit_admin(
        _("Nhân bản chính sách phép {0} thành {1}").format(name, new_doc.name),
        reference_doctype="Leave Policy",
        reference_name=new_doc.name,
        new_value={"title": new_title},
    )
    frappe.db.commit()
    return {"name": new_doc.name, "title": new_title}


def _friendly_leave_error(msg: str) -> str:
    """Map stock HRMS/Desk validation texts to Vietnamese for the SPA."""
    m = (msg or "").lower()
    if "already assigned" in m or "overlap" in m:
        return _("Nhân viên đã có chính sách phép trong khoảng thời gian này (chồng kỳ).")
    if "value missing for title" in m:
        return _("Thiếu tên chính sách phép.")
    return (msg or "")[:300]


def _assignment_payload(
    employee: str,
    leave_policy: str,
    leave_period: str = "",
    effective_from: str = "",
    effective_to: str = "",
    company: str = "",
    assignment_based_on: str = "Leave Period",
    carry_forward=0,
) -> dict:
    """Validate + build a Leave Policy Assignment payload (shared assign/amend).

    Enforces the Desk rule that only **submitted** policies may be assigned
    (plan §2.8, G2 fix) and carries ``carry_forward`` through.
    """
    employee = (employee or "").strip()
    leave_policy = (leave_policy or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not leave_policy or not frappe.db.exists("Leave Policy", leave_policy):
        frappe.throw(_("Chính sách phép không tồn tại."))
    if int(frappe.db.get_value("Leave Policy", leave_policy, "docstatus") or 0) != 1:
        frappe.throw(_("Chỉ gán được chính sách phép đã duyệt."))

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
        "carry_forward": _coerce_bool(carry_forward),
    }
    if based_on == "Leave Period":
        payload["leave_period"] = leave_period
        # effective_from/to are auto-filled from the leave period by Frappe.
    else:
        payload["effective_from"] = getdate(effective_from)
        if effective_to:
            payload["effective_to"] = getdate(effective_to)
    return payload


def _allocations_for_assignment(assignment_name: str) -> list[dict]:
    """Leave Allocations generated by one submitted assignment.

    NOTE: no ``carry_forwarded_leaves`` projection — deployed sites may run an
    older HRMS schema without that column (smoke on erp-hr.local: 1054).
    """
    return (
        frappe.get_all(
            "Leave Allocation",
            filters={"leave_policy_assignment": assignment_name},
            fields=[
                "name",
                "employee",
                "employee_name",
                "leave_type",
                "from_date",
                "to_date",
                "new_leaves_allocated",
                "docstatus",
            ],
            order_by="from_date asc",
        )
        or []
    )


def _cancel_live_allocations(assignment_name: str) -> int:
    """Cancel the submitted Leave Allocations granted by one assignment.

    Desk cancels these via client-side ``ignore_doctypes_on_cancel_all`` flags;
    a server-side ``doc.cancel()`` alone is blocked by LinkExists — so the
    endpoint must cascade them itself (smoke on erp-hr.local).
    """
    live = frappe.get_all(
        "Leave Allocation",
        filters={"leave_policy_assignment": assignment_name, "docstatus": 1},
        fields=["name"],
        limit_page_length=0,
    )
    for alloc in live or []:
        frappe.get_doc("Leave Allocation", alloc["name"]).cancel()
    return len(live or [])


@frappe.whitelist()
def assign_leave_policy(
    employee: str,
    leave_policy: str,
    leave_period: str = "",
    effective_from: str = "",
    effective_to: str = "",
    company: str = "",
    assignment_based_on: str = "Leave Period",
    carry_forward=0,
) -> dict:
    """Create + submit a Leave Policy Assignment (grants annual leave).

    Default ``assignment_based_on`` is ``Leave Period``; if no Leave Period is
    supplied we fall back to ``Joining Date`` with explicit effective dates.
    Returns the generated Leave Allocations so the SPA can toast the grant.
    """
    _require_hr_admin()
    payload = _assignment_payload(
        employee=employee,
        leave_policy=leave_policy,
        leave_period=leave_period,
        effective_from=effective_from,
        effective_to=effective_to,
        company=company,
        assignment_based_on=assignment_based_on,
        carry_forward=carry_forward,
    )
    doc = frappe.get_doc(payload)
    doc.insert()
    doc.submit()  # submit → Frappe creates Leave Allocations

    _audit_admin(
        _("Gán chính sách phép {0} cho {1}").format(leave_policy, employee),
        reference_doctype="Leave Policy Assignment",
        reference_name=doc.name,
        company=payload.get("company") or None,
        employee=employee,
        new_value={
            "leave_policy": leave_policy,
            "leave_period": leave_period,
            "assignment_based_on": payload["assignment_based_on"],
            "carry_forward": payload["carry_forward"],
        },
    )
    frappe.db.commit()
    return {"name": doc.name, "allocations": _allocations_for_assignment(doc.name)}


@frappe.whitelist()
def list_leave_policy_assignments(
    q: str = "",
    employee: str = "",
    leave_policy: str = "",
    leave_period: str = "",
    company: str = "",
    docstatus: int | str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Assignments for the admin table — every docstatus by default (G3 fix).

    Draft/Cancelled rows are now returned so the SPA can render the full
    lifecycle (cancel/amend actions) instead of only submitted ones.
    """
    _require_hr_admin()
    filters = []
    if employee:
        filters.append(["employee", "=", employee])
    if leave_policy:
        filters.append(["leave_policy", "=", leave_policy])
    if leave_period:
        filters.append(["leave_period", "=", leave_period])
    if company:
        filters.append(["company", "=", company])
    ds = "" if docstatus is None else str(docstatus).strip()
    if ds not in ("", "None"):
        filters.append(["docstatus", "=", pagination.as_int(ds, 0)])
    or_filters = []
    q = (q or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [
            ["employee_name", "like", like],
            ["employee", "like", like],
            ["leave_policy", "like", like],
            ["leave_period", "like", like],
        ]
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
            "docstatus",
            "assignment_based_on",
            "carry_forward",
            "leaves_allocated",
            "amended_from",
        ],
        filters=filters,
        or_filters=or_filters or None,
        limit_start=max(0, pagination.as_int(offset, 0)),
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="modified desc",
    )
    return rows


@frappe.whitelist()
def cancel_leave_policy_assignment(name: str, reason: str = "") -> dict:
    """Cancel a submitted assignment — Frappe cascades the linked allocations."""
    _require_hr_admin()
    name = (name or "").strip()
    reason = (reason or "").strip()
    if not name or not frappe.db.exists("Leave Policy Assignment", name):
        frappe.throw(_("Lượt gán chính sách không tồn tại."))
    if not reason:
        frappe.throw(_("Lý do hủy là bắt buộc."))
    doc = frappe.get_doc("Leave Policy Assignment", name)
    if int(getattr(doc, "docstatus", 0) or 0) != 1:
        frappe.throw(_("Chỉ có thể hủy lượt gán đã duyệt."))
    cancelled_allocations = _cancel_live_allocations(name)
    # Allocation cancel chạm vào assignment (hrms ledger) → reload để tránh
    # TimestampMismatch "Document has been modified" (smoke erp-hr.local).
    if hasattr(doc, "reload"):
        doc.reload()
    doc.cancel()
    _audit_admin(
        _("Hủy gán chính sách {0} của {1}").format(
            getattr(doc, "leave_policy", ""), getattr(doc, "employee", "")
        ),
        reference_doctype="Leave Policy Assignment",
        reference_name=name,
        employee=getattr(doc, "employee", "") or None,
        new_value={
            "docstatus": 2,
            "reason": reason[:500],
            "cancelled_allocations": cancelled_allocations,
        },
    )
    frappe.db.commit()
    return {
        "name": name,
        "docstatus": 2,
        "cancelled_allocations": cancelled_allocations,
    }


@frappe.whitelist()
def amend_leave_policy_assignment(
    name: str,
    leave_policy: str = "",
    leave_period: str = "",
    assignment_based_on: str = "",
    effective_from: str = "",
    effective_to: str = "",
    carry_forward=None,
) -> dict:
    """Cancel a submitted assignment + submit a replacement (``amended_from``)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Leave Policy Assignment", name):
        frappe.throw(_("Lượt gán chính sách không tồn tại."))
    old = frappe.get_doc("Leave Policy Assignment", name)
    ds = int(getattr(old, "docstatus", 0) or 0)
    if ds == 0:
        frappe.throw(_("Bản Nháp sửa trực tiếp được, không cần tạo bản thay thế."))
    if ds == 1:
        _cancel_live_allocations(name)
        if hasattr(old, "reload"):
            old.reload()
        old.cancel()

    cf = carry_forward
    if cf is None or str(cf).strip() == "":
        cf = getattr(old, "carry_forward", 0)
    payload = _assignment_payload(
        employee=getattr(old, "employee", ""),
        leave_policy=leave_policy or getattr(old, "leave_policy", ""),
        leave_period=leave_period or str(getattr(old, "leave_period", "") or ""),
        effective_from=effective_from or str(getattr(old, "effective_from", "") or ""),
        effective_to=effective_to or str(getattr(old, "effective_to", "") or ""),
        company=getattr(old, "company", "") or "",
        assignment_based_on=assignment_based_on or getattr(old, "assignment_based_on", "") or "Leave Period",
        carry_forward=cf,
    )
    payload["amended_from"] = name
    doc = frappe.get_doc(payload)
    doc.insert()
    doc.submit()

    _audit_admin(
        _("Đổi gán chính sách {0} → {1}").format(name, doc.name),
        reference_doctype="Leave Policy Assignment",
        reference_name=doc.name,
        employee=payload["employee"],
        new_value={"amended_from": name, "leave_policy": payload["leave_policy"]},
    )
    frappe.db.commit()
    return {
        "name": doc.name,
        "amended_from": name,
        "allocations": _allocations_for_assignment(doc.name),
    }


@frappe.whitelist()
def bulk_assign_leave_policy(
    employees,
    leave_policy: str,
    leave_period: str = "",
    effective_from: str = "",
    effective_to: str = "",
    company: str = "",
    assignment_based_on: str = "Leave Period",
    carry_forward=0,
) -> dict:
    """Assign one policy to many employees — partial-safe (plan §2.11).

    An HRMS overlap throw for one employee never kills the batch; each failure
    is reported per-row with a Vietnamese reason.
    """
    _require_hr_admin()
    ids = _parse_employee_list(employees)
    if not ids:
        frappe.throw(_("Danh sách nhân viên trống."))

    assigned: list[dict] = []
    failed: list[dict] = []
    for idx, eid in enumerate(ids):
        savepoint = f"lpa_bulk_{idx}"
        try:
            frappe.db.savepoint(savepoint)
            payload = _assignment_payload(
                employee=eid,
                leave_policy=leave_policy,
                leave_period=leave_period,
                effective_from=effective_from,
                effective_to=effective_to,
                company=company,
                assignment_based_on=assignment_based_on,
                carry_forward=carry_forward,
            )
            doc = frappe.get_doc(payload)
            doc.insert()
            doc.submit()
            assigned.append({"employee": eid, "name": doc.name})
        except Exception as exc:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
            raw = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            failed.append({"employee": eid, "reason": _friendly_leave_error(raw)})
            try:
                frappe.log_error(
                    title="bulk assign leave policy failed",
                    message=f"{eid}\n{frappe.get_traceback()}",
                )
            except Exception:
                pass

    _audit_admin(
        _("Gán hàng loạt chính sách {0} cho {1} nhân viên").format(leave_policy, len(assigned)),
        reference_doctype="Leave Policy",
        reference_name=leave_policy,
        new_value={"assigned": len(assigned), "failed": len(failed)},
    )
    frappe.db.commit()
    message = _("Đã gán {0} nhân viên.").format(len(assigned))
    if failed:
        message = _("Đã gán {0} — lỗi {1}.").format(len(assigned), len(failed))
    return {
        "assigned": assigned,
        "failed": failed,
        "total": len(ids),
        "message": message,
    }


@frappe.whitelist()
def list_leave_allocations(
    assignment: str = "",
    employee: str = "",
    leave_period: str = "",
    limit: int = 200,
) -> list[dict]:
    """Leave Allocations generated by assignments (the granted leave)."""
    _require_hr_admin()
    filters = []
    if assignment:
        filters.append(["leave_policy_assignment", "=", assignment])
    if employee:
        filters.append(["employee", "=", employee])
    if leave_period:
        filters.append(["leave_period", "=", leave_period])
    rows = frappe.get_all(
        "Leave Allocation",
        fields=[
            "name",
            "employee",
            "employee_name",
            "leave_type",
            "from_date",
            "to_date",
            "new_leaves_allocated",
            "leave_policy_assignment",
            "docstatus",
        ],
        filters=filters,
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="from_date desc",
    )
    return rows


# ── G10: Per-employee salary settings (plan per-employee-salary) ──────────────
def _setting_for_employee(emp: dict) -> dict:
    """Effective payroll setting row for one Employee projection.

    Merges the stored ``vn_payroll_mode`` / ``vn_hourly_rate`` with the
    resolver chain (rate_source) and the active SSA (monthly base).
    """
    from gege_hr.gege_hr.utils import payroll as pay_calc

    eid = emp.get("name")
    rate, source = pay_calc.resolve_hourly_rate_detail(eid)
    dept = emp.get("department")
    dept_rate = frappe.db.get_value("Department", dept, "vn_hourly_rate") if dept else None
    ssa = frappe.db.get_value(
        "Salary Structure Assignment",
        {"employee": eid, "docstatus": 1},
        ["name", "salary_structure", "from_date", "base"],
        as_dict=True,
        order_by="from_date desc",
    )
    ssa = ssa if isinstance(ssa, dict) else {}
    return {
        "employee": eid,
        "employee_name": emp.get("employee_name"),
        "department": dept,
        "status": emp.get("status"),
        "company": emp.get("company"),
        "payroll_mode": emp.get("vn_payroll_mode") or "Hourly",
        "hourly_rate": pay_calc._num(emp.get("vn_hourly_rate")),
        "rate_source": source,
        "effective_hourly_rate": rate,
        "dept_rate": pay_calc._num(dept_rate),
        "monthly_base": pay_calc._num(ssa.get("base")),
        "ssa_name": ssa.get("name"),
        "ssa_from_date": str(ssa.get("from_date") or ""),
        "salary_structure": ssa.get("salary_structure"),
    }


@frappe.whitelist()
def list_employee_salary_settings(
    company: str = "",
    department: str = "",
    mode: str = "",
    q: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Per-employee salary settings table (plan per-employee-salary §4.5).

    Returns ``{total, items}`` — each item merges the stored Employee fields
    with the effective resolver chain (``rate_source``) and the active SSA.
    """
    _require_hr_admin()
    filters = [["status", "=", "Active"]]
    if company:
        filters.append(["company", "=", company])
    if department:
        filters.append(["department", "=", department])
    if mode in ("Hourly", "Monthly"):
        filters.append(["vn_payroll_mode", "=", mode])
    or_filters = []
    if q:
        or_filters = [["employee_name", "like", f"%{q}%"], ["name", "like", f"%{q}%"]]
    kwargs: dict = {
        "fields": [
            "name",
            "employee_name",
            "department",
            "status",
            "company",
            "vn_payroll_mode",
            "vn_hourly_rate",
        ],
        "filters": filters,
        "limit_page_length": pagination.clamp_limit(limit, default=100),
        "limit_start": max(0, int(offset or 0)),
        "order_by": "employee_name asc",
    }
    if or_filters:
        kwargs["or_filters"] = or_filters
    rows = frappe.get_all("Employee", **kwargs)
    try:
        total = frappe.db.count("Employee", filters)
    except Exception:
        total = len(rows)
    return {"total": total, "items": [_setting_for_employee(r) for r in rows]}


@frappe.whitelist()
def save_employee_salary_setting(
    employee: str,
    payroll_mode: str | None = None,
    hourly_rate: float | None = None,
    base: float | None = None,
    from_date: str = "",
    salary_structure: str = "",
) -> dict:
    """Update one employee's payroll mode / hourly rate / monthly base.

    * ``payroll_mode`` / ``hourly_rate`` → stored on the Employee.
    * ``base`` (+ optional ``from_date`` / ``salary_structure``) → creates a
      NEW submitted Salary Structure Assignment via the battle-tested
      :func:`assign_salary_structure` engine (overlap validation included).
    * Monthly mode requires a monthly base > 0 (existing SSA or the ``base``
      param) — enforced up-front with a Vietnamese message.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))

    old = _setting_for_employee(
        frappe.db.get_value(
            "Employee",
            employee,
            ["name", "employee_name", "department", "status", "company", "vn_payroll_mode", "vn_hourly_rate"],
            as_dict=True,
        )
    )

    updates: dict = {}
    if payroll_mode is not None:
        payroll_mode = (str(payroll_mode) or "").strip()
        if payroll_mode not in ("Hourly", "Monthly"):
            frappe.throw(_("Kiểu tính lương phải là Hourly hoặc Monthly."))
        updates["vn_payroll_mode"] = payroll_mode
    if hourly_rate is not None:
        rate = frappe.utils.flt(hourly_rate)
        if rate < 0:
            frappe.throw(_("Lương giờ không được âm."))
        updates["vn_hourly_rate"] = rate

    # Monthly needs a base > 0 — from this payload or an existing SSA.
    new_mode = updates.get("vn_payroll_mode", old["payroll_mode"])
    effective_base = frappe.utils.flt(base) if base is not None else old["monthly_base"]
    if new_mode == "Monthly" and effective_base <= 0:
        frappe.throw(
            _(
                "NV {0} chuyển sang tính lương tháng cần mức lương tháng (SSA.base) > 0 —"
                " truyền thêm base hoặc gán bảng lương trước."
            ).format(employee)
        )

    ssa_name = None
    if base is not None and frappe.utils.flt(base) > 0:
        if not salary_structure:
            salary_structure = old.get("salary_structure") or ""
        if not salary_structure:
            salary_structure = frappe.db.get_value(
                "Salary Structure", {"is_active": "Yes", "docstatus": 1}, "name"
            )
        if not salary_structure:
            frappe.throw(_("Không tìm thấy bảng lương để gán lương tháng — truyền salary_structure."))
        if not from_date:
            from_date = frappe.utils.today()
        res = assign_salary_structure(
            employee=employee,
            salary_structure=salary_structure,
            from_date=from_date,
            base=frappe.utils.flt(base),
        )
        ssa_name = res.get("name")

    if updates:
        frappe.db.set_value("Employee", employee, updates)

    _audit_admin(
        _("Cập nhật thiết lập lương NV {0}").format(employee),
        reference_doctype="Employee",
        reference_name=employee,
        employee=employee,
        # NOTE: _audit_admin has no old_value param — the previous snapshot
        # travels inside new_value under "old".
        new_value={
            "old": {
                "payroll_mode": old["payroll_mode"],
                "hourly_rate": old["hourly_rate"],
                "monthly_base": old["monthly_base"],
            },
            "payroll_mode": new_mode,
            "hourly_rate": updates.get("vn_hourly_rate", old["hourly_rate"]),
            "monthly_base": frappe.utils.flt(base) if base is not None else old["monthly_base"],
            "ssa": ssa_name,
        },
    )
    return {"name": employee, "updated": {**updates, "ssa": ssa_name}}


@frappe.whitelist()
def bulk_set_hourly_rates(rows, payroll_mode: str = "") -> dict:
    """Set hourly rates (and optionally one mode) for many employees.

    ``rows`` = ``[{employee, hourly_rate}]`` (list or JSON string). Every
    employee lands in ``updated`` or ``skipped`` (unknown employee / bad rate)
    — mirrors the bulk-assign buckets convention.
    """
    _require_hr_admin()
    if isinstance(rows, str):
        import json

        try:
            rows = json.loads(rows)
        except Exception:
            rows = []
    payroll_mode = (payroll_mode or "").strip()
    if payroll_mode and payroll_mode not in ("Hourly", "Monthly"):
        frappe.throw(_("Kiểu tính lương phải là Hourly hoặc Monthly."))

    updated: list[str] = []
    skipped: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        eid = (row.get("employee") or "").strip()
        rate = frappe.utils.flt(row.get("hourly_rate"))
        if not eid or not frappe.db.exists("Employee", eid):
            skipped.append({"employee": eid, "reason": "Nhân viên không tồn tại"})
            continue
        if rate < 0:
            skipped.append({"employee": eid, "reason": "Lương giờ âm"})
            continue
        values = {"vn_hourly_rate": rate}
        if payroll_mode:
            values["vn_payroll_mode"] = payroll_mode
        frappe.db.set_value("Employee", eid, values)
        updated.append(eid)

    if updated:
        _audit_admin(
            _("Bulk cập nhật lương giờ cho {0} nhân viên").format(len(updated)),
            reference_doctype="Employee",
            new_value={"employees": updated, "payroll_mode": payroll_mode or None},
        )
    return {"updated": updated, "skipped": skipped}


# ── G11: Payroll Period — chuẩn HRMS (plan payroll-periods-desk-free P1) ────
# Config năm tài chính cho Benefits / Tax / Salary Slip. Trước đây chỉ đọc từ
# SPA (seed/Desk-một-lần) — giờ CRUD đủ trên /hr/payroll/periods tab "Kỳ chuẩn".
_PP_WARNING_DAYS = 60


def _hrms_get_payroll_period():
    """Resolve HRMS's cached ``get_payroll_period`` (lazy, import-safe).

    Imported lazily so bench-free unit tests (stub ``frappe`` in sys.modules)
    never walk the hrms import chain, and a site without HRMS still boots.
    """
    try:
        from hrms.payroll.doctype.payroll_period.payroll_period import (
            get_payroll_period,
        )

        return get_payroll_period
    except Exception:
        return None


def _clear_pp_cache() -> None:
    """Invalidate the redis-cached ``get_payroll_period`` after a write.

    HRMS's PayrollPeriod.clear_cache already runs on controller save; the
    explicit call is idempotent insurance against framework-version drift
    (without it Benefits keeps resolving the stale period list).
    """
    fn = _hrms_get_payroll_period()
    if fn is None:
        return
    try:
        fn.clear_cache()
    except Exception:
        pass


def _pp_shift_months(d: datetime.date, months: int) -> datetime.date:
    """``d`` shifted by whole ``months`` (day clamped to month length) — stdlib."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _pp_parse_date(v):
    """ISO-date parser bằng stdlib — G11 cố tình không dùng ``getdate`` của module.

    Trong bộ test bench-free, module này được import lần đầu bởi harness stub
    tuỳ file và tên ``getdate`` bị stale-bind theo stub đó (có stub trả str
    hoặc None cho input rỗng) → so sánh date vỡ khi chạy full-suite. Parser
    stdlib cho kết quả nhất quán ở mọi ngữ cảnh, production kể cả.
    """
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _pp_usage(names) -> dict:
    """Usage counts per Payroll Period: Benefit Applications + Tax Declarations.

    Exactly two grouped queries regardless of row count (never N+1). A missing
    doctype (site without that module) is skipped — usage stays 0 there.
    """
    out = {n: {"benefit_applications": 0, "tax_declarations": 0} for n in (names or [])}
    if not out:
        return out
    sources = (
        ("Employee Benefit Application", "benefit_applications"),
        ("Employee Tax Exemption Declaration", "tax_declarations"),
    )
    for doctype, key in sources:
        try:
            rows = frappe.get_all(
                doctype,
                filters={"payroll_period": ["in", list(out.keys())]},
                fields=["payroll_period", "count(name) as n"],
                group_by="payroll_period",
            )
        except Exception:
            continue
        for row in rows or []:
            period = row.get("payroll_period")
            if period in out:
                out[period][key] = int(row.get("n") or 0)
    return out


def _pp_overlapping(company: str, start_date, end_date, exclude: str = ""):
    """Name of an existing Payroll Period overlapping ``[start, end]`` (or None).

    Mirrors the window condition of HRMS's validate_overlap so the friendly
    Vietnamese error can name the conflicting period WITHOUT parsing the
    native HTML message (which embeds a Desk /app/ link).
    """
    filters = [
        ["company", "=", company],
        ["start_date", "<=", end_date],
        ["end_date", ">=", start_date],
    ]
    if exclude:
        filters.append(["name", "!=", exclude])
    try:
        rows = frappe.get_all("Payroll Period", filters=filters, fields=["name"], limit=1)
    except Exception:
        return None
    return rows[0].get("name") if rows else None


def _pp_rethrow_overlap(e: Exception, company: str, start_date, end_date, exclude: str = "") -> None:
    """Map the native overlap ValidationError to Vietnamese (no Desk HTML).

    Overlap errors (keyword "exists between") become a Vietnamese throw that
    names the conflicting period. Any other exception re-raises untouched so
    callers keep the real message.
    """
    msg = str(e)
    if "exists between" not in msg:
        raise e
    overlap_name = _pp_overlapping(company, start_date, end_date, exclude=exclude)
    hint = f" ({overlap_name})" if overlap_name else ""
    clean = msg.split("<a")[0].strip()
    frappe.throw(
        _("Kỳ lương chuẩn của công ty đã bao trùm khoảng ngày này{0} — chọn khoảng ngày khác. {1}").format(
            hint, clean
        )
    )


@frappe.whitelist()
def payroll_period_context() -> dict:
    """Defaults + health for tab "Kỳ chuẩn (năm tài chính)" (plan P1/P3).

    ``suggested`` reproduces the Desk form's smart defaults server-side:
    start = latest end_date + 1 day (or Jan 1st of the current year when no
    period exists); end = start + 12 months − 1 day
    (hrms/payroll/doctype/payroll_period/payroll_period.js parity).
    """
    _require_hr_admin()
    company = _default_company()
    # stdlib (không qua frappe.utils.getdate): chống stale-bind khi module bị
    # import từ nhiều stub harness khác nhau; today luôn là date thật.
    today = datetime.date.today()
    try:
        companies = frappe.get_all("Company", fields=["name", "abbr"], order_by="name asc")
    except Exception:
        companies = []

    rows = []
    try:
        rows = frappe.get_all(
            "Payroll Period",
            filters=[["company", "=", company]] if company else None,
            fields=["end_date"],
            order_by="end_date desc",
            limit=1,
        )
    except Exception:
        rows = []
    parsed_last_end = _pp_parse_date(rows[0].get("end_date")) if rows else None
    if parsed_last_end:
        suggested_start = parsed_last_end + datetime.timedelta(days=1)
    else:
        suggested_start = datetime.date(today.year, 1, 1)
    suggested_end = _pp_shift_months(suggested_start, 12) - datetime.timedelta(days=1)

    active = None
    if company:
        overlap_name = _pp_overlapping(company, today, today)
        if overlap_name:
            try:
                docs = frappe.get_all(
                    "Payroll Period",
                    filters=[["name", "=", overlap_name]],
                    fields=["name", "company", "start_date", "end_date"],
                    limit=1,
                )
            except Exception:
                docs = []
            if docs:
                end = _pp_parse_date(docs[0].get("end_date"))
                active = {
                    "name": docs[0]["name"],
                    "company": docs[0].get("company"),
                    "start_date": str(docs[0].get("start_date") or ""),
                    "end_date": str(docs[0].get("end_date") or ""),
                    "days_remaining": (end - today).days if end else None,
                }

    return {
        "companies": companies,
        "suggested": {"start_date": str(suggested_start), "end_date": str(suggested_end)},
        "active": active,
        "health": "ok" if active else "missing",
        "warning_days": _PP_WARNING_DAYS,
    }


@frappe.whitelist()
def list_payroll_periods(
    q: str = "",
    company: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Payroll Periods for the admin table (server-side search, plan §2.1)."""
    _require_hr_admin()
    filters = []
    if company:
        filters.append(["company", "=", company])
    or_filters = []
    query = (q or "").strip()
    if query:
        like = f"%{pagination.escape_like(query)}%"
        or_filters = [
            ["name", "like", like],
            ["company", "like", like],
            ["start_date", "like", like],
            ["end_date", "like", like],
        ]
    rows = frappe.get_all(
        "Payroll Period",
        fields=["name", "company", "start_date", "end_date"],
        filters=filters,
        or_filters=or_filters or None,
        limit_start=max(0, pagination.as_int(offset, 0)),
        limit_page_length=pagination.clamp_limit(limit, default=200),
        order_by="start_date desc",
    )
    today = datetime.date.today()
    usage = _pp_usage([r.get("name") for r in rows])
    for r in rows:
        start = _pp_parse_date(r.get("start_date")) if r.get("start_date") else None
        end = _pp_parse_date(r.get("end_date")) if r.get("end_date") else None
        r["is_active_today"] = 1 if (start and end and start <= today <= end) else 0
        r["usage"] = usage.get(r.get("name"), {"benefit_applications": 0, "tax_declarations": 0})
    return rows


@frappe.whitelist()
def get_payroll_period(name: str) -> dict:
    """One Payroll Period + usage + the SPA action matrix (plan §2.1)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Payroll Period", name):
        frappe.throw(_("Kỳ lương chuẩn không tồn tại."))
    doc = frappe.get_doc("Payroll Period", name)
    usage = _pp_usage([name]).get(name, {})
    total = int(usage.get("benefit_applications", 0)) + int(usage.get("tax_declarations", 0))
    return {
        "name": doc.name,
        "company": doc.get("company"),
        "start_date": str(doc.get("start_date") or ""),
        "end_date": str(doc.get("end_date") or ""),
        "usage": usage,
        "can": {"edit": 1, "delete": 0 if total else 1},
    }


@frappe.whitelist()
def save_payroll_period(
    name: str = "",
    company: str = "",
    start_date: str = "",
    end_date: str = "",
    label: str = "",
) -> dict:
    """Create or update a Payroll Period — HRMS controller validates overlap."""
    _require_hr_admin()
    name = (name or "").strip()
    company = (company or "").strip() or _default_company()
    if not company:
        frappe.throw(_("Không xác định được công ty."))
    if not start_date or not end_date:
        frappe.throw(_("Ngày bắt đầu và ngày kết thúc là bắt buộc."))
    start = _pp_parse_date(start_date)
    end = _pp_parse_date(end_date)
    if not start or not end:
        frappe.throw(_("Ngày bắt đầu và ngày kết thúc phải đúng định dạng YYYY-MM-DD."))
    if end < start:
        frappe.throw(_("Ngày kết thúc không được trước ngày bắt đầu."))

    if name:
        # Update path — config stays editable; only DELETE is usage-guarded.
        if not frappe.db.exists("Payroll Period", name):
            frappe.throw(_("Kỳ lương chuẩn không tồn tại."))
        doc = frappe.get_doc("Payroll Period", name)
        previous = {
            "start_date": str(doc.get("start_date") or ""),
            "end_date": str(doc.get("end_date") or ""),
            "company": doc.get("company") or "",
        }
        doc.company = company
        doc.start_date = start
        doc.end_date = end
        try:
            doc.save()
        except Exception as e:  # noqa: BLE001 — mapped below
            _pp_rethrow_overlap(e, company, start, end, exclude=name)
        _clear_pp_cache()
        _audit_admin(
            _("Cập nhật kỳ lương chuẩn {0}").format(doc.name),
            reference_doctype="Payroll Period",
            reference_name=doc.name,
            company=company or None,
            new_value={
                "previous": previous,
                "start_date": str(start),
                "end_date": str(end),
            },
        )
        frappe.db.commit()
        return {
            "name": doc.name,
            "company": company,
            "start_date": str(start),
            "end_date": str(end),
        }

    payload = {
        "doctype": "Payroll Period",
        "company": company,
        "start_date": start,
        "end_date": end,
    }
    label_clean = (label or "").strip()
    if label_clean:
        payload["name"] = label_clean
    doc = frappe.get_doc(payload)
    try:
        doc.insert()
    except Exception as e:  # noqa: BLE001 — mapped below
        _pp_rethrow_overlap(e, company, start, end)
    _clear_pp_cache()
    _audit_admin(
        _("Tạo kỳ lương chuẩn {0}").format(doc.name),
        reference_doctype="Payroll Period",
        reference_name=doc.name,
        company=company or None,
        new_value={"start_date": str(start), "end_date": str(end)},
    )
    frappe.db.commit()
    return {
        "name": doc.name,
        "company": company,
        "start_date": str(start),
        "end_date": str(end),
    }


@frappe.whitelist()
def delete_payroll_period(name: str) -> dict:
    """Delete a Payroll Period — refused while Benefit/Tax docs link it."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Payroll Period", name):
        frappe.throw(_("Kỳ lương chuẩn không tồn tại."))
    usage = _pp_usage([name]).get(name, {})
    used = int(usage.get("benefit_applications", 0)) + int(usage.get("tax_declarations", 0))
    if used:
        frappe.throw(_("Kỳ chuẩn đang được Benefit/Tax sử dụng ({0} bản ghi) — không xoá được.").format(used))
    frappe.delete_doc("Payroll Period", name)
    _clear_pp_cache()
    _audit_admin(
        _("Xoá kỳ lương chuẩn {0}").format(name),
        reference_doctype="Payroll Period",
        reference_name=name,
    )
    frappe.db.commit()
    return {"name": name}
