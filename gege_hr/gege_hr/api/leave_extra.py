"""NEW-3 (hr-gap-audit 🟥) — Leave Encashment + Compensatory Leave API.

Reuses Frappe HR's ``Leave Encashment`` and ``Compensatory Leave Request``
DocTypes (no duplicate doctype) and exposes a DNA-compliant surface to the
portal: an employee lists/submits leave encashment (nghỉ phép đổi tiền) and
comp-off requests (nghỉ bù); an HR/Manager lists all + approves/rejects. Pure
helpers split out for unit testing. Mirrors ``api/expense.py``.
"""
from __future__ import annotations

import frappe

ENCASHMENT_DOCTYPE = "Leave Encashment"
COMPOFF_DOCTYPE = "Compensatory Leave Request"

_ENCASH_FIELDS = ["name", "employee", "employee_name", "leave_type", "encashment_days", "encashment_amount", "status"]
_COMPOFF_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "leave_type",
    "work_from_date",
    "work_to_date",
    "reason",
    "status",
]


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def encashment_amount(days, per_day: float = 0.0) -> float:
    """Estimated encashment payout = days × per-day rate (helper for the form)."""
    return round(_num(days) * _num(per_day), 2)


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
        frappe.throw("Bạn chỉ xem được yêu cầu của chính mình.")


def _or_filters_for(doctype, q):
    """Broad-search OR LIKE covering every text + numeric content field (DNA §6.6 A).

    Datetime columns (creation / work_*_date) are deliberately NOT `like`-d —
    Frappe casts the value to datetime and raises ParserError, breaking the whole
    query. Date filtering uses a `creation` day-range in the popover instead.
    """
    like = f"%{q}%"
    base = [
        ["employee_name", "like", like],
        ["name", "like", like],
        ["leave_type", "like", like],
    ]
    if doctype == ENCASHMENT_DOCTYPE:
        return base + [
            ["encashment_days", "like", like],
            ["encashment_amount", "like", like],
        ]
    return base + [["reason", "like", like]]


def _list(
    doctype,
    fields,
    filters,
    status,
    search,
    page,
    page_size,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_field=None,
    days_min=None,
    days_max=None,
) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    if leave_type:
        flt.append(["leave_type", "=", leave_type])
    # Date range on `creation` (day-range, NOT `like` datetime — DNA §6.6 A).
    if date_from:
        flt.append(["creation", ">=", f"{date_from} 00:00:00"])
    if date_to:
        flt.append(["creation", "<=", f"{date_to} 23:59:59"])
    # Numeric range as two list conditions (DNA §6.6 B — NOT `between`).
    if days_field:
        if days_min not in (None, ""):
            flt.append([days_field, ">=", _num(days_min)])
        if days_max not in (None, ""):
            flt.append([days_field, "<=", _num(days_max)])

    or_filters = None
    q = (search or "").strip()
    if q:
        or_filters = _or_filters_for(doctype, q)

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
        # `frappe.db.count` does not accept `or_filters` → count via names (DNA §6.6 A).
        total = len(
            frappe.get_all(doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0)
            or []
        )
    except Exception:
        frappe.log_error(title=f"leave_extra.list {doctype} failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


# --------------------------------------------------------------------------- #
# Leave Encashment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_leave_encashments(
    employee=None,
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_min=None,
    days_max=None,
):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        ENCASHMENT_DOCTYPE,
        _ENCASH_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
        days_field="encashment_days",
        days_min=days_min,
        days_max=days_max,
    )


@frappe.whitelist()
def all_leave_encashments(
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_min=None,
    days_max=None,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    return _list(
        ENCASHMENT_DOCTYPE,
        _ENCASH_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
        days_field="encashment_days",
        days_min=days_min,
        days_max=days_max,
    )


@frappe.whitelist()
def submit_leave_encashment(employee=None, leave_type=None, encashment_days=None, earning_component=None):
    emp = _resolve(employee)
    _assert_own(emp)
    days = _num(encashment_days)
    if not leave_type or days <= 0:
        frappe.throw("Cần loại phép + số ngày đổi > 0.")
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(ENCASHMENT_DOCTYPE)
    doc.employee = emp
    doc.leave_type = leave_type
    doc.encashment_days = days
    if earning_component:
        doc.earning_component = earning_component
    doc.company = company
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "encashment_days": days, "encashment_amount": doc.get("encashment_amount")}


@frappe.whitelist()
def approve_leave_encashment(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt.")
    doc = frappe.get_doc(ENCASHMENT_DOCTYPE, name)
    try:
        doc.submit()
    except Exception as exc:
        frappe.log_error(title="leave_encashment.submit failed")
        frappe.throw(f"Duyệt đổi phép thất bại: {exc}")
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def reject_leave_encashment(name=None, reason=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối.")
    doc = frappe.get_doc(ENCASHMENT_DOCTYPE, name)
    if doc.docstatus == 0:
        doc.status = "Rejected"
        if reason:
            doc.reason = (doc.reason or "") + f" | Từ chối: {reason}"
        doc.save(ignore_permissions=True)
    else:
        doc.cancel()
    return {"name": doc.name, "status": doc.status}


# --------------------------------------------------------------------------- #
# Compensatory Leave (comp-off)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_comp_off_requests(
    employee=None,
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        COMPOFF_DOCTYPE,
        _COMPOFF_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
    )


@frappe.whitelist()
def all_comp_off_requests(
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    return _list(
        COMPOFF_DOCTYPE,
        _COMPOFF_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
    )


@frappe.whitelist()
def submit_comp_off(employee=None, leave_type=None, work_from_date=None, work_to_date=None, reason=None):
    emp = _resolve(employee)
    _assert_own(emp)
    if not (work_from_date and work_to_date):
        frappe.throw("Cần ngày bắt đầu + kết thúc làm bù.")
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(COMPOFF_DOCTYPE)
    doc.employee = emp
    doc.leave_type = leave_type or "Compensatory Off"
    doc.work_from_date = work_from_date
    doc.work_to_date = work_to_date
    doc.reason = reason or ""
    doc.company = company
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "work_from_date": work_from_date, "work_to_date": work_to_date}


@frappe.whitelist()
def approve_comp_off(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt.")
    doc = frappe.get_doc(COMPOFF_DOCTYPE, name)
    try:
        doc.submit()
    except Exception as exc:
        frappe.log_error(title="comp_off.submit failed")
        frappe.throw(f"Duyệt làm bù thất bại: {exc}")
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def reject_comp_off(name=None, reason=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối.")
    doc = frappe.get_doc(COMPOFF_DOCTYPE, name)
    if doc.docstatus == 0:
        doc.status = "Rejected"
        if reason:
            doc.reason = (doc.reason or "") + f" | Từ chối: {reason}"
        doc.save(ignore_permissions=True)
    else:
        doc.cancel()
    return {"name": doc.name, "status": doc.status}


def _status_options(doctype: str) -> list:
    """Distinct `status` values for the gear-popover dropdown (DNA §6.4 / §6.2).

    Reads Select options from the doctype meta and unions them with values
    actually present in the table, so the dropdown is never empty even when the
    meta/DB disagree. Encashment vs comp-off statuses can differ, hence per-doctype.
    """
    opts: list = []
    try:
        meta_opts = frappe.get_meta(doctype).get_field("status").options
        if meta_opts:
            opts += [o.strip() for o in meta_opts.split("\n") if o and o.strip()]
    except Exception:
        pass
    try:
        opts += [
            r for r in frappe.db.get_all(doctype, fields=["status"], pluck=True, limit_page_length=0) or [] if r
        ]
    except Exception:
        pass
    seen, out = set(), []
    for o in opts:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


@frappe.whitelist()
def leave_extra_options() -> dict:
    """Filter options for the gear popover (DNA §6.4 / §6.2)."""
    try:
        leave_types = (
            frappe.get_all("Leave Type", filters={"is_encash": 1}, pluck="name", order_by="name asc") or []
        )
    except Exception:
        leave_types = []
    return {
        "leave_types": leave_types,
        "encash_statuses": _status_options(ENCASHMENT_DOCTYPE),
        "compoff_statuses": _status_options(COMPOFF_DOCTYPE),
    }
