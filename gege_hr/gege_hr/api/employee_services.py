"""NEW-4 (hr-gap-audit 🟥) — Employee Grievance + Travel Request API.

Reuses Frappe HR's ``Employee Grievance`` and ``Travel Request`` DocTypes and
exposes a DNA-compliant surface: an employee files a grievance / travel request;
HR lists all + resolves/approves. Mirrors ``api/expense.py``.
"""
from __future__ import annotations

import frappe

GRIEVANCE_DOCTYPE = "Employee Grievance"
TRAVEL_DOCTYPE = "Travel Request"

_GRIEVANCE_FIELDS = ["name", "employee", "employee_name", "grievance_type", "subject", "status", "raised_on"]
_TRAVEL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "purpose_of_travel",
    "from_date",
    "to_date",
    "total_travel_cost",
    "status",
]


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


def _field_options(doctype, fieldname):
    """Read a Select field's option list straight from the DocType meta."""
    try:
        df = frappe.get_meta(doctype).get_field(fieldname)
        if df and df.fieldtype == "Select":
            return [o.strip() for o in (df.options or "").split("\n") if o.strip()]
    except Exception:
        pass
    return []


def _list(doctype, fields, filters, status, search, page, page_size,
          extra_filters=None, search_fields=None) -> dict:
    """DNA §6.6 — server-side list + filter + broad search + pagination.

    ``status`` / ``extra_filters`` are AND conditions (list form, keeps multiple
    conditions on the same field — DNA §6.6 B). ``search`` becomes ``or_filters``
    LIKE over ``search_fields`` (incl. numeric, DNA §6.6 A). Returns a
    server-aggregated ``summary`` (counts by status over the FULL filtered set —
    not page-scoped, DNA §3.5).
    """
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    if extra_filters:
        flt.extend(extra_filters)
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{q}%"
        or_filters = [[f, "like", like] for f in (search_fields or ["employee_name", "name", "subject"])]
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
        all_rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=["name", "status"],
                limit_page_length=0,
            )
            or []
        )
        total = len(all_rows)
        by_status = {}
        for r in all_rows:
            s = r.get("status") or "—"
            by_status[s] = by_status.get(s, 0) + 1
        summary = {"total": total, "by_status": by_status}
    except Exception:
        frappe.log_error(title=f"employee_services.list {doctype} failed")
        rows, total, summary = [], 0, {"total": 0, "by_status": {}}
    return {"data": rows, "total": total, "summary": summary}


# --------------------------------------------------------------------------- #
# Grievance
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_grievances(employee=None, status=None, grievance_type=None, search=None,
                 date_from=None, date_to=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    extra = []
    if grievance_type:
        extra.append(["grievance_type", "=", grievance_type])
    if date_from:
        extra.append(["raised_on", ">=", date_from])
    if date_to:
        extra.append(["raised_on", "<=", date_to])
    return _list(
        GRIEVANCE_DOCTYPE,
        _GRIEVANCE_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "subject", "grievance_type"],
    )


@frappe.whitelist()
def all_grievances(status=None, grievance_type=None, search=None,
                   date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if grievance_type:
        extra.append(["grievance_type", "=", grievance_type])
    if date_from:
        extra.append(["raised_on", ">=", date_from])
    if date_to:
        extra.append(["raised_on", "<=", date_to])
    return _list(
        GRIEVANCE_DOCTYPE,
        _GRIEVANCE_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "subject", "grievance_type"],
    )


@frappe.whitelist()
def submit_grievance(employee=None, grievance_type=None, subject=None, description=None):
    emp = _resolve(employee)
    if not (subject or "").strip():
        frappe.throw("Cần chủ đề khiếu nại.")
    doc = frappe.new_doc(GRIEVANCE_DOCTYPE)
    doc.employee = emp
    doc.grievance_type = grievance_type or None
    doc.subject = subject.strip()
    doc.description = description or ""
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "subject": doc.subject}


@frappe.whitelist()
def resolve_grievance(name=None, resolution=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xử lý khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    doc.status = "Resolved"
    doc.resolution_details = resolution or ""
    doc.save()  # proper: runs validate + on_update (HR Manager grant via setup_permissions)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def grievance_options() -> dict:
    try:
        types = frappe.get_all("Grievance Type", pluck="name", order_by="name asc") or []
    except Exception:
        types = []
    return {
        "grievance_types": types,
        "grievance_statuses": _field_options(GRIEVANCE_DOCTYPE, "status"),
        "travel_statuses": _field_options(TRAVEL_DOCTYPE, "status"),
    }


# --------------------------------------------------------------------------- #
# Travel Request
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_travel_requests(employee=None, status=None, search=None,
                       date_from=None, date_to=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    extra = []
    if date_from:
        extra.append(["from_date", ">=", date_from])
    if date_to:
        extra.append(["to_date", "<=", date_to])
    return _list(
        TRAVEL_DOCTYPE,
        _TRAVEL_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "purpose_of_travel", "total_travel_cost"],
    )


@frappe.whitelist()
def all_travel_requests(status=None, search=None, date_from=None, date_to=None,
                        page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if date_from:
        extra.append(["from_date", ">=", date_from])
    if date_to:
        extra.append(["to_date", "<=", date_to])
    return _list(
        TRAVEL_DOCTYPE,
        _TRAVEL_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "purpose_of_travel", "total_travel_cost"],
    )


@frappe.whitelist()
def submit_travel_request(
    employee=None, purpose_of_travel=None, from_date=None, to_date=None, estimated_cost=None
):
    emp = _resolve(employee)
    if not (from_date and to_date and (purpose_of_travel or "").strip()):
        frappe.throw("Cần mục đích + ngày đi + ngày về.")
    doc = frappe.new_doc(TRAVEL_DOCTYPE)
    doc.employee = emp
    doc.purpose_of_travel = purpose_of_travel.strip()
    doc.from_date = from_date
    doc.to_date = to_date
    if estimated_cost:
        try:
            doc.total_travel_cost = float(estimated_cost)
        except (TypeError, ValueError):
            pass
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "from_date": from_date, "to_date": to_date}


@frappe.whitelist()
def approve_travel_request(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    doc.status = "Approved"
    doc.save()  # proper: runs validate + on_update (HR Manager grant via setup_permissions)
    return {"name": doc.name, "status": doc.status}
