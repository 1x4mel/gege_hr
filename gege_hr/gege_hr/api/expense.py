"""NEW-1 (hr-gap-audit 🟥) — Expense Claim API.

Reuses Frappe HR's ``Expense Claim`` DocType (no duplicate doctype) and exposes a
server-side, DNA-compliant surface to the portal: an employee lists/submits their
claims; an HR/Manager lists all + approves/rejects. Mirrors the request pattern of
``api/overtime.py`` / ``api/leave.py`` (resolve → assert-own → server-side list →
submit/approve/reject). Pure helpers are split out for unit testing.
"""
from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import pagination

CLAIM_DOCTYPE = "Expense Claim"

_CLAIM_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "posting_date",
    "total_claimed_amount",
    "total_sanctioned_amount",
    "approval_status",
    "status",
    "remark",
    "company",
    "docstatus",
]


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager", "Expense Claim Approver"})


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if employee != own:
        frappe.throw("Bạn chỉ xem được chi phí của chính mình.")


def normalize_expenses(expenses) -> list:
    """Coerce the FE ``expenses`` child payload into Expense Claim Detail rows."""
    out = []
    for e in (expenses or []):
        amount = _num(e.get("amount") if isinstance(e, dict) else getattr(e, "amount", None))
        if amount <= 0:
            continue
        etype = (e.get("expense_type") if isinstance(e, dict) else getattr(e, "expense_type", None)) or ""
        out.append(
            {
                "expense_type": etype,
                "amount": amount,
                "sanction_amount": amount,
                "description": (e.get("description") if isinstance(e, dict) else getattr(e, "description", "")) or "",
            }
        )
    return out


def claim_total(expenses) -> float:
    return round(sum(_num(e.get("amount")) for e in normalize_expenses(expenses)), 2)


@frappe.whitelist()
def my_expense_claims(
    employee: str | None = None,
    status: str | None = None,
    search: str | None = None,
    employee_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    emp = _resolve(employee)
    _assert_own(emp)
    _assert_own(emp)
    return _list(
        filters=[["employee", "=", emp]],
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        page=page,
        page_size=page_size,
    )


@frappe.whitelist()
def all_expense_claims(
    status: str | None = None,
    search: str | None = None,
    employee_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả chi phí.")
    return _list(
        filters=None,
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        page=page,
        page_size=page_size,
    )


def _list(
    filters,
    status,
    search,
    employee_name=None,
    date_from=None,
    date_to=None,
    amount_min=None,
    amount_max=None,
    page=1,
    page_size=20,
) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["approval_status", "=", status])
    # Nhân viên — LIKE trên employee_name (DNA Law #2 §6.2). Chỉ HR/Manager
    # lọc qua toàn bộ nhân viên; ô này chỉ render ở popover khi canApprove.
    ename = (employee_name or "").strip()
    if ename:
        flt.append(["employee_name", "like", f"%{ename}%"])
    # Date range trên posting_date — 2 điều kiện riêng (DNA §3.4.4 / §6.6 B;
    # KHÔNG "between" cho date để tránh lệch SQL).
    if date_from:
        flt.append(["posting_date", ">=", date_from])
    if date_to:
        flt.append(["posting_date", "<=", date_to])
    # Amount range trên total_claimed_amount — list filter giữ cả 2 điều kiện
    # >= / <= (DNA §6.6 B — KHÔNG dùng "between" cho filter số).
    amin = _num(amount_min, None) if amount_min not in (None, "") else None
    amax = _num(amount_max, None) if amount_max not in (None, "") else None
    if amin is not None:
        flt.append(["total_claimed_amount", ">=", amin])
    if amax is not None:
        flt.append(["total_claimed_amount", "<=", amax])
    # Broad search (Law #3, DNA §6.6 A) — cover text + numeric total_claimed_amount
    # (user hay gõ số tiền vào ô search; KHÔNG miễn trừ field numeric).
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [
            ["employee_name", "like", like],
            ["name", "like", like],
            ["remark", "like", like],
            ["total_claimed_amount", "like", like],
        ]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                CLAIM_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=_CLAIM_FIELDS,
                order_by="posting_date desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                CLAIM_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=["name"],
                limit_page_length=0,
            )
            or [],
        )
    except Exception:
        frappe.log_error(title="expense.list failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


@frappe.whitelist()
def expense_claim_options() -> dict:
    """Active Expense Claim Types (+ the caller as a default approver)."""
    try:
        types = frappe.get_all("Expense Claim Type", pluck="name", order_by="name asc") or []
    except Exception:
        types = []
    return {"expense_types": types, "expense_approver": frappe.session.user}


@frappe.whitelist()
def submit_expense_claim(
    employee: str | None = None,
    expenses: list | None = None,
    posting_date: str | None = None,
    remark: str | None = None,
    expense_approver: str | None = None,
) -> dict:
    emp = _resolve(employee)
    rows = normalize_expenses(expenses)
    if not rows:
        frappe.throw("Cần ít nhất 1 khoản chi phí hợp lệ (loại + số tiền > 0).")
    total = claim_total(expenses)
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(CLAIM_DOCTYPE)
    doc.employee = emp
    doc.company = company
    doc.posting_date = posting_date or frappe.utils.today()
    doc.expense_approver = expense_approver or frappe.session.user
    doc.remark = remark or ""
    doc.total_claimed_amount = total
    doc.total_sanctioned_amount = 0  # sanctioned by the approver, not the submitter
    for r in rows:
        doc.append("expenses", r)
    doc.insert(ignore_permissions=True)
    submit_note = ""
    try:
        doc.submit()
    except Exception:
        # Submit may require accounts setup; keep it Draft so HR can still action it.
        frappe.log_error(title="expense_claim.submit skipped (accounts not ready)")
        submit_note = "Đơn đang ở nháp (chưa submit được) — HR sẽ xử lý."
    return {
        "name": doc.name,
        "total": total,
        "status": doc.status,
        "approval_status": doc.approval_status,
        "message": submit_note,
    }


@frappe.whitelist()
def approve_expense_claim(name: str | None = None) -> dict:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt chi phí.")
    doc = frappe.get_doc(CLAIM_DOCTYPE, name)
    if doc.approval_status in ("Approved", "Rejected"):
        frappe.throw(f"Đơn chi phí đã ở trạng thái {doc.approval_status} — không duyệt lại.")
    doc.approval_status = "Approved"
    doc.save()  # proper: runs validate + on_update (HR Manager grant via setup_permissions)
    return {"name": doc.name, "approval_status": doc.approval_status}


@frappe.whitelist()
def reject_expense_claim(name: str | None = None, reason: str | None = None) -> dict:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối chi phí.")
    doc = frappe.get_doc(CLAIM_DOCTYPE, name)
    if doc.approval_status in ("Approved", "Rejected"):
        frappe.throw(f"Đơn chi phí đã ở trạng thái {doc.approval_status} — không đổi được.")
    doc.approval_status = "Rejected"
    if reason:
        doc.remark = reason
    doc.save()  # proper: runs validate + on_update (HR Manager grant via setup_permissions)
    return {"name": doc.name, "approval_status": doc.approval_status}
