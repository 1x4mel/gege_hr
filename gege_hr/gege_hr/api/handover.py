"""Leave handover task API — plan v5 §11.5 / doctype-design §32 (Post-MVP).

CRUD + lifecycle for **VN Leave Handover Task**. The SPA contract (a future
``hr-ui`` handover view) maps to:

  * ``my_handovers``   → tasks where the caller is ``to_employee`` (or HR: all)
  * ``leave_handovers``→ manager list filtered by leave application / employee
  * ``create_handover``→ mint a Pending task
  * ``update_handover_status`` → drive Pending → In Progress → Completed

Pure lifecycle math lives in ``utils/handover.py`` (bench-free). Bench loaders
here are guarded so a missing table degrades gracefully.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now

from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import handover as handover_utils
from gege_hr.gege_hr.utils import notify
from gege_hr.gege_hr.utils import pagination

DOCTYPE = "VN Leave Handover Task"
_LIST_FIELDS = [
    "name",
    "leave_application",
    "from_employee",
    "to_employee",
    "handover_date",
    "status",
    "description",
    "attachment",
    "completed_at",
    "completed_by",
    "note",
    "docstatus",
    "owner",
    "modified",
]


# --------------------------------------------------------------------------- #
# Permission helpers
# --------------------------------------------------------------------------- #
def _is_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES) or bool(roles & emp_utils.HR_USER_ROLES)


def _require_manager() -> None:
    if not _is_manager():
        frappe.throw(_("Chỉ HR User / HR Manager mới được thực hiện."), frappe.PermissionError)


def _current_employee() -> str | None:
    return emp_utils.get_employee_for_user()


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
def _match_handover_row(row: dict, search: str | None) -> bool:
    """Generic server-side free-text match across a handover row's values
    (DNA §6.6 D, HR-BL-09)."""
    q = (search or "").strip().lower()
    if not q:
        return True
    return any(q in str(v).lower() for v in row.values() if v is not None)


@frappe.whitelist()
def my_handovers(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Tasks handed *to* the caller (the receiver must action them).

    ``search`` OR-matches a free-text query across the normalised row's values,
    applied server-side (DNA §6.6 D, HR-BL-09) — ready for the SPA broad search.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": None}``
    (post-query filter → ``total`` is the filtered list length); without
    ``page_size`` the legacy bare-list return is preserved.
    """
    emp = _current_employee()
    if not emp or not _table_ready():
        return {"data": [], "total": 0, "summary": None} if page_size else []
    # List filters (DNA §6.6 B): a dict cannot hold two conditions on the same
    # field, so the handover_date range is expressed as separate >= / <= entries.
    filters: list = [["to_employee", "=", emp]]
    if status:
        filters.append(["status", "=", status])
    if date_from:
        filters.append(["handover_date", ">=", date_from])
    if date_to:
        filters.append(["handover_date", "<=", date_to])
    try:
        rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=_LIST_FIELDS, order_by="handover_date desc")
    except Exception:
        frappe.log_error(title="handover.my_handovers failed")
        return {"data": [], "total": 0, "summary": None} if page_size else []
    out = [handover_utils.handover_row(r) for r in rows]
    return pagination.paginate_filtered(
        [r for r in out if _match_handover_row(r, search)], page=page, page_size=page_size
    )


@frappe.whitelist()
def leave_handovers(
    leave_application: str | None = None,
    employee: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Manager list. HR sees all; otherwise scoped to the caller's involvement.

    ``search`` OR-matches a free-text query across the normalised row's values,
    applied server-side (DNA §6.6 D) — the same broad-search contract as
    ``my_handovers``.
    """
    if not _table_ready():
        return []
    # List filters (DNA §6.6 B): handover_date range as separate >= / <= entries.
    filters: list = []
    if leave_application:
        filters.append(["leave_application", "=", leave_application])
    if status:
        filters.append(["status", "=", status])
    if employee:
        filters.append(["employee", "=", employee])
    if date_from:
        filters.append(["handover_date", ">=", date_from])
    if date_to:
        filters.append(["handover_date", "<=", date_to])
    try:
        rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=_LIST_FIELDS, order_by="handover_date desc")
    except Exception:
        frappe.log_error(title="handover.leave_handovers failed")
        return []
    # Broad search (DNA §6.6 D) over the normalised row's values, then scope.
    out = [r for r in (handover_utils.handover_row(r) for r in rows) if _match_handover_row(r, search)]
    if _is_manager():
        return out
    # A non-HR user may only see rows where they are from or to.
    emp = _current_employee()
    if not emp:
        return []
    return [r for r in out if r.get("from_employee") == emp or r.get("to_employee") == emp]


@frappe.whitelist()
def suggest_handover_receivers(employee: str | None = None) -> list[dict]:
    """Suggest likely receivers for a handover (auto-suggest in the create form).

    Resolves the departing employee's ``reports_to`` / ``department`` / ``company``,
    then pulls active colleagues (same department + their direct reports + the
    manager) and ranks them via ``utils.handover.suggest_receivers``. The pool
    is **company-scoped** to the departing employee so that, in a multi-company
    tenant, an HR user can only receive suggestions from their own company's
    headcount. HR may pass any ``employee``; a plain employee resolves to
    themselves. Best-effort: any failure (missing Employee record / no table)
    degrades to ``[]`` so the create form never breaks.
    """
    if not _table_ready():
        return []
    emp = employee or _current_employee()
    if not emp:
        return []
    try:
        meta = (
            frappe.db.get_value(
                "Employee",
                emp,
                ["reports_to", "department", "company"],
                as_dict=True,
            )
            or {}
        )
    except Exception:
        frappe.log_error(title="handover.suggest_handover_receivers meta failed")
        return []
    reports_to = meta.get("reports_to")
    department = meta.get("department")
    company = (meta.get("company") or "").strip() or None
    try:
        # Pool: the manager (if any), direct reports of this employee, and
        # same-department peers. We fetch a flat list and let the pure helper
        # rank + de-dupe + exclude self. When we know the departing employee's
        # company, scope both queries to it so multi-company tenants never leak
        # headcount across company boundaries. A blank company (older bench /
        # Employee without company) falls back to the unscoped behaviour.
        colleague_rows: list[dict] = []
        clauses: list[list] = [["status", "=", "Active"]]
        if company:
            clauses.append(["company", "=", company])
        if department:
            clauses.append(["department", "=", department])
        rows = frappe.db.get_list(
            "Employee",
            filters=clauses,
            fields=["name", "employee_name", "reports_to", "department"],
            order_by="employee_name asc",
            limit_page_length=80,
        )
        colleague_rows.extend(rows)
        # Direct reports: people whose manager is this employee (same company).
        report_clauses: list[list] = [["status", "=", "Active"], ["reports_to", "=", emp]]
        if company:
            report_clauses.append(["company", "=", company])
        reports = frappe.db.get_list(
            "Employee",
            filters=report_clauses,
            fields=["name", "employee_name", "reports_to", "department"],
            order_by="employee_name asc",
            limit_page_length=40,
        )
        colleague_rows.extend(reports)
    except Exception:
        frappe.log_error(title="handover.suggest_handover_receivers load failed")
        return []
    return handover_utils.suggest_receivers(emp, reports_to, department, colleague_rows)


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_handover(
    leave_application: str,
    from_employee: str,
    to_employee: str,
    handover_date: str,
    description: str,
    attachment: str | None = None,
    note: str | None = None,
    status: str = "Pending",
) -> dict:
    """Mint a handover task (manager or the departing employee)."""
    _require_table()
    if not _is_manager():
        # A non-HR user may only create a handover for themselves.
        emp = _current_employee()
        if emp and from_employee and emp != from_employee:
            frappe.throw(_("Bạn chỉ có thể tạo bàn giao cho chính mình."), frappe.PermissionError)
    try:
        payload = handover_utils.handover_payload(
            leave_application=leave_application,
            from_employee=from_employee,
            to_employee=to_employee,
            handover_date=handover_date,
            description=description,
            attachment=attachment,
            note=note,
            status=status,
        )
    except ValueError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    doc = frappe.get_doc(payload)
    doc.insert(ignore_permissions=_is_manager())
    # Best-effort notification to the receiver.
    notify.push_notification(
        employee=to_employee,
        notification_type="Leave",
        title="Có nhiệm vụ bàn giao mới",
        message=f"Bạn vừa nhận bàn giao công việc từ {from_employee}.",
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
    )
    return {
        "name": doc.name,
        "status": doc.status,
        "message": _("Đã tạo nhiệm vụ bàn giao."),
    }


@frappe.whitelist()
def update_handover_status(name: str, status: str, note: str | None = None) -> dict:
    """Drive the handover lifecycle. Completing stamps ``completed_at/by``."""
    _require_table()
    doc = _get_owned_or_managed(name)
    current = doc.status
    if not handover_utils.can_transition(current, status):
        frappe.throw(
            _("Không thể chuyển từ '{0}' sang '{1}'.").format(current, status),
            frappe.ValidationError,
        )
    doc.status = status
    if status == "Completed":
        doc.completed_at = now()
        doc.completed_by = frappe.session.user
    if note and str(note).strip():
        doc.note = str(note).strip()
    doc.save(ignore_permissions=_is_manager())
    # Notify the original owner when their handover is completed/cancelled.
    if status in ("Completed", "Cancelled") and doc.from_employee:
        notify.push_notification(
            employee=doc.from_employee,
            notification_type="Leave",
            title=f"Bàn giao đã {status.lower()}",
            message=f"Nhiệm vụ bàn giao {name} đã được cập nhật.",
            reference_doctype=DOCTYPE,
            reference_name=name,
        )
    return {"name": doc.name, "status": doc.status, "message": _("Đã cập nhật.")}


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _require_table() -> None:
    if not _table_ready():
        frappe.throw(_("Tính năng bàn giao chưa được cài đặt."), frappe.ValidationError)


def _get_owned_or_managed(name: str):
    doc = frappe.get_doc(DOCTYPE, name)
    if _is_manager():
        return doc
    emp = _current_employee()
    if emp and (doc.from_employee == emp or doc.to_employee == emp):
        return doc
    frappe.throw(_("Bạn không có quyền trên nhiệm vụ bàn giao này."), frappe.PermissionError)
