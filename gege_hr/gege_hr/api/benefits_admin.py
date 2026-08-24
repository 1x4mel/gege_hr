"""NEW-6 (hr-gap-audit 🟥) — Benefits + Gratuity + Promotion API.

Reuses Frappe HR's ``Employee Benefit Application``, ``Gratuity``,
``Employee Promotion`` DocTypes. Covers the last missing domains. Lower-priority
items (Skills, Transfer) are noted in the audit as 🟢 and deferred. Mirrors
``api/expense.py``.
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import pagination

BENEFIT_DOCTYPE = "Employee Benefit Application"
GRATUITY_DOCTYPE = "Gratuity"
PROMOTION_DOCTYPE = "Employee Promotion"

_BENEFIT_FIELDS = ["name", "employee", "employee_name", "date", "max_benefits", "status"]
_GRATUITY_FIELDS = ["name", "employee", "employee_name", "gratuity_rule", "amount", "status", "posting_date"]
_PROMOTION_FIELDS = ["name", "employee", "employee_name", "promotion_date", "status"]


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if employee != own:
        frappe.throw("Bạn chỉ xem được của chính mình.")


def _list(
    doctype,
    fields,
    filters,
    status,
    search,
    page,
    page_size,
    date_field=None,
    date_from=None,
    date_to=None,
    amount_field=None,
    amount_min=None,
    amount_max=None,
    numeric_fields=None,
) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    # Date range — two list conditions (NOT "between" — DNA §6.6 B).
    if date_field:
        if date_from:
            flt.append([date_field, ">=", date_from])
        if date_to:
            flt.append([date_field, "<=", date_to])
    # Numeric range — two list conditions on the same field (DNA §6.6 B).
    if amount_field:
        if amount_min not in (None, ""):
            try:
                flt.append([amount_field, ">=", float(amount_min)])
            except (TypeError, ValueError):
                pass
        if amount_max not in (None, ""):
            try:
                flt.append([amount_field, "<=", float(amount_max)])
            except (TypeError, ValueError):
                pass
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        of = [["employee_name", "like", like], ["name", "like", like]]
        # Numeric columns also join the broad search (DNA §6.6 A — typing a
        # number must match max_benefits / amount columns too).
        for nf in numeric_fields or []:
            of.append([nf, "like", like])
        or_filters = of
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=fields,
                order_by="creation desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"benefits_admin.list {doctype} failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


# ── Benefits ─────────────────────────────────────────────────────────────────
@frappe.whitelist()
def my_benefit_applications(employee=None, status=None, search=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(BENEFIT_DOCTYPE, _BENEFIT_FIELDS, [["employee", "=", emp]], status, search, page, page_size)


@frappe.whitelist()
def all_benefit_applications(status=None, search=None, date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    return _list(
        BENEFIT_DOCTYPE,
        _BENEFIT_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        date_field="date",
        date_from=date_from,
        date_to=date_to,
        numeric_fields=["max_benefits"],
    )


@frappe.whitelist()
def submit_benefit_application(employee=None, max_benefits=None):
    emp = _resolve(employee)
    _assert_own(emp)
    doc = frappe.new_doc(BENEFIT_DOCTYPE)
    doc.employee = emp
    doc.date = frappe.utils.today()
    if max_benefits:
        try:
            amount = float(max_benefits)
            if amount >= 0:
                doc.max_benefits = amount
        except (TypeError, ValueError):
            pass
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}


# ── Gratuity (HR read) ───────────────────────────────────────────────────────
@frappe.whitelist()
def list_gratuities(
    status=None,
    search=None,
    date_from=None,
    date_to=None,
    amount_min=None,
    amount_max=None,
    page=1,
    page_size=20,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    return _list(
        GRATUITY_DOCTYPE,
        _GRATUITY_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        date_field="posting_date",
        date_from=date_from,
        date_to=date_to,
        amount_field="amount",
        amount_min=amount_min,
        amount_max=amount_max,
        numeric_fields=["amount"],
    )


# ── Promotion ────────────────────────────────────────────────────────────────
@frappe.whitelist()
def my_promotions(employee=None, status=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        PROMOTION_DOCTYPE, _PROMOTION_FIELDS, [["employee", "=", emp]], status, None, page, page_size
    )


@frappe.whitelist()
def all_promotions(status=None, search=None, date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    return _list(
        PROMOTION_DOCTYPE,
        _PROMOTION_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        date_field="promotion_date",
        date_from=date_from,
        date_to=date_to,
    )


@frappe.whitelist()
def get_benefit_filter_options():
    """Distinct ``status`` values per doctype so the gear popover's
    ``SearchableSelect`` is never empty (DNA §6.3 — auto-fetch filter options)."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")

    def _statuses(doctype):
        rows = frappe.get_all(doctype, fields=["status"], distinct=True, limit_page_length=0) or []
        seen, out = set(), []
        for r in rows:
            s = (r or {}).get("status")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "benefits": _statuses(BENEFIT_DOCTYPE),
        "gratuity": _statuses(GRATUITY_DOCTYPE),
        "promotion": _statuses(PROMOTION_DOCTYPE),
    }


@frappe.whitelist()
def create_promotion(employee=None, promotion_date=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager tạo thăng cấp.")
    if not employee:
        frappe.throw("Cần nhân viên.")
    doc = frappe.new_doc(PROMOTION_DOCTYPE)
    doc.employee = employee
    doc.promotion_date = promotion_date or frappe.utils.today()
    doc.insert(ignore_permissions=True)
    return {"name": doc.name}
