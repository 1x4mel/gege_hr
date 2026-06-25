"""Audit event API — plan v5 §13.5 / doctype-design §26.

The **VN Audit Event** trail is append-only: rows are written programmatically
by :func:`record` (called from the sensitive flows — leave/OT/correction/
advance submit/approve, work-session recalc, monthly lock/unlock, payroll
calculate/approve/publish, manual overrides) and read via :func:`audit_events`.
Pure payload/row/vocabulary helpers live in ``utils/audit.py`` (bench-free).

SPA contract:

  * ``audit_events`` → HR/System read with filters (type/category/employee/date)
  * ``audit_categories`` → coarse type grouping for the filter UI
  * ``record`` → internal helper used by the domain flows (not for end users)
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.utils import audit as audit_utils

DOCTYPE = "VN Audit Event"
_LIST_FIELDS = [
    "name",
    "audit_type",
    "company",
    "employee",
    "work_date",
    "actor",
    "actor_ip",
    "reference_doctype",
    "reference_name",
    "description",
    "old_value",
    "new_value",
    "created_at",
    "owner",
]


def _is_hr() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _require_hr() -> None:
    if not _is_hr():
        frappe.throw(_("Nhật ký kiểm toán chỉ dành cho HR."), frappe.PermissionError)


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def audit_categories() -> dict:
    """Return the coarse category → types map for the audit filter UI."""
    return {
        "categories": dict(audit_utils.AUDIT_CATEGORIES),
        "types": list(audit_utils.AUDIT_TYPES),
    }


@frappe.whitelist()
def audit_events(
    company: str | None = None,
    employee: str | None = None,
    audit_type: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """HR/System read. Filter by type, coarse category, employee or date window."""
    _require_hr()
    if not _table_ready():
        return []
    filters: dict = {}
    if company:
        filters["company"] = company
    if employee:
        filters["employee"] = employee
    # Category expands to its member types.
    if category and not audit_type:
        types = audit_utils.AUDIT_CATEGORIES.get(category)
        if types:
            filters["audit_type"] = ("in", list(types))
    elif audit_type:
        filters["audit_type"] = audit_type
    if from_date or to_date:
        window = _date_window(from_date, to_date)
        if window:
            filters["work_date"] = window
    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            fields=_LIST_FIELDS,
            order_by="created_at desc",
            limit_page_length=int(limit or 200),
        )
    except Exception:
        frappe.log_error(title="audit.audit_events failed")
        return []
    return [audit_utils.audit_row(r) for r in rows]


@frappe.whitelist()
def approval_logs(
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    actor: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read-only trail of every approval action from ``VN Approval Log``.

    Surfaced in HrAuditView so HR can trace who approved/rejected/delegated each
    request (the VN Audit Event captures the *what*, this captures the granular
    approval workflow transitions). HR-gated, degrades gracefully when the
    DocType or table isn't installed.
    """
    _require_hr()
    if not frappe.db.table_exists("VN Approval Log"):
        return []
    filters: dict = {}
    if reference_doctype:
        filters["reference_doctype"] = reference_doctype
    if reference_name:
        filters["reference_name"] = reference_name
    if actor:
        filters["actor"] = actor
    try:
        rows = frappe.db.get_all(
            "VN Approval Log",
            filters=filters,
            fields=[
                "name",
                "reference_doctype",
                "reference_name",
                "action",
                "from_state",
                "to_state",
                "actor",
                "actor_employee",
                "comment",
                "action_at",
            ],
            order_by="action_at desc",
            limit_page_length=int(limit or 200),
        )
    except Exception:
        frappe.log_error(title="audit.approval_logs failed")
        return []
    return rows


def _date_window(from_date: str | None, to_date: str | None):
    frm = getdate(from_date) if from_date else None
    to = getdate(to_date) if to_date else None
    if frm and to:
        return ("between", [frm, to])
    if frm:
        return (">=", frm)
    if to:
        return ("<=", to)
    return None


# --------------------------------------------------------------------------- #
# Write endpoint — internal, used by domain flows
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def record(
    audit_type: str,
    company: str,
    actor: str | None = None,
    employee: str | None = None,
    work_date=None,
    actor_ip: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str | None = None,
    old_value=None,
    new_value=None,
) -> str | None:
    """Mint one append-only audit row. Best-effort: never aborts the caller.

    Returns the new row name on success, ``None`` on any failure (the caller's
    business transition must not depend on auditing succeeding).
    """
    if not _table_ready():
        return None
    try:
        payload = audit_utils.audit_payload(
            audit_type=audit_type,
            company=company,
            actor=actor or frappe.session.user,
            employee=employee,
            work_date=work_date,
            actor_ip=actor_ip or _client_ip(),
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except ValueError:
        return None
    try:
        doc = frappe.get_doc(payload)
        # Privileged system audit trail — must always persist regardless of the
        # acting user's role; an audit write must never block a business op.
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(title="audit.record failed")
        return None


# Map of transaction type → audit_type for the matrix-driven approval inbox.
# Leave Application has no dedicated "approve" vocab (its submit/cancel are
# logged at the leave endpoints), so only the three request DocTypes map here.
APPROVE_AUDIT_TYPE = {
    "Overtime Request": "OT Approve",
    "Correction Request": "Correction Approve",
    "Salary Advance Request": "Advance Approve",
}


def log(
    audit_type: str,
    *,
    doc: dict | None = None,
    company: str | None = None,
    employee: str | None = None,
    work_date=None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str | None = None,
    old_value=None,
    new_value=None,
) -> str | None:
    """Doc-aware convenience wrapper around :func:`record` for the domain flows.

    Resolves ``company`` / ``employee`` / ``work_date`` / reference from ``doc``
    when not supplied explicitly (the usual case — the flow already holds the
    document). Fully best-effort: any failure is swallowed so the caller's
    business transition never depends on auditing succeeding. Returns the new
    row name on success, ``None`` otherwise.

    Mirrors :func:`utils.notify.push_notification`'s swallow-and-return contract.
    """
    try:
        d = dict(doc or {})
        company = company or d.get("company")
        if not company:
            return None  # nothing safe to attribute the event to
        employee = employee or d.get("employee")
        if work_date in (None, ""):
            work_date = d.get("work_date") or d.get("posting_date")
        if reference_doctype is None:
            reference_doctype = d.get("doctype")
        if reference_name is None:
            reference_name = d.get("name")
        return record(
            audit_type=audit_type,
            company=company,
            employee=employee,
            work_date=work_date,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except Exception:
        return None


def _client_ip() -> str | None:
    try:
        return frappe.local.request_ip or None
    except Exception:
        return None
