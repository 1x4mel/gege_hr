"""VN Approval Delegation CRUD — desk-free Phase B2 (§3.5).

Fronts the SPA "Ủy quyền duyệt" modal: list my given/received delegations,
create/update one, delete one. The approval-inbox side effects (expanding the
step holder set + the ``Delegate`` approval-log action) live in
:mod:`gege_hr.gege_hr.api.approval` (``_delegated_users`` /
``delegate_request``) so every decide-path shares ONE resolution rule.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.utils import employee as emp_utils

DOCTYPE = "VN Approval Delegation"

_DELEGATION_FIELDS = [
    "name",
    "from_user",
    "to_user",
    "transaction_type",
    "request_name",
    "from_date",
    "to_date",
    "reason",
    "is_active",
]


def _is_hr() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _get_all(filters: dict) -> list[dict]:
    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            fields=_DELEGATION_FIELDS,
            order_by="modified desc",
            limit_page_length=100,
        )
    except Exception:
        return []
    return [dict(r) for r in rows]


@frappe.whitelist()
def list_delegations(user: str | None = None) -> dict:
    """Delegations the caller has given + received (§3.5).

    ``user`` defaults to the session user; an HR Manager may pass any user to
    audit their coverage (the same trust rule as the inbox ``approver`` param).
    """
    target = (user or emp_utils.get_current_user() or "").strip()
    if target and target != (emp_utils.get_current_user() or "") and not _is_hr():
        target = emp_utils.get_current_user() or ""
    if not target:
        return {"mine": [], "received": []}
    return {
        "mine": _get_all({"from_user": target}),
        "received": _get_all({"to_user": target}),
    }


def _coerce_values(values) -> dict:
    if isinstance(values, str):
        try:
            values = frappe.parse_json(values)
        except Exception:
            values = None
    return dict(values or {})


@frappe.whitelist()
def save_delegation(values=None) -> dict:
    """Create or update one delegation (the doctype ``validate`` enforces the
    window/self/span rules; the permission gate mirrors it for the API path).

    ``values``: { name?, from_user, to_user, transaction_type?, request_name?,
    from_date, to_date, reason?, is_active? }.
    """
    values = _coerce_values(values)
    name = (values.get("name") or "").strip()
    from_user = (values.get("from_user") or "").strip()

    if name:
        doc = frappe.get_doc(DOCTYPE, name)
        # Only the original delegator (or HR) may edit.
        if doc.from_user != (emp_utils.get_current_user() or "") and not _is_hr():
            frappe.throw(
                _("Chỉ chính người ủy quyền hoặc HR mới sửa được ủy quyền này."),
                frappe.PermissionError,
            )
    else:
        doc = frappe.new_doc(DOCTYPE)

    for key in (
        "from_user",
        "to_user",
        "transaction_type",
        "request_name",
        "from_date",
        "to_date",
        "reason",
    ):
        if key in values:
            doc.set(key, values.get(key))
    if "is_active" in values:
        doc.is_active = 1 if str(values.get("is_active")) in ("1", "True", "true") else 0
    if not doc.transaction_type:
        doc.transaction_type = "All"

    doc.insert(ignore_permissions=True) if not name else doc.save(ignore_permissions=True)
    frappe.db.commit() if hasattr(frappe.db, "commit") else None
    return {"name": doc.name, "message": _("Đã lưu ủy quyền duyệt.")}


@frappe.whitelist()
def delete_delegation(name: str | None = None) -> dict:
    """Remove a delegation — only the delegator (or HR) may delete it."""
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã ủy quyền."))
    doc = frappe.get_doc(DOCTYPE, name)
    if doc.from_user != (emp_utils.get_current_user() or "") and not _is_hr():
        frappe.throw(
            _("Chỉ chính người ủy quyền hoặc HR mới xoá được ủy quyền này."),
            frappe.PermissionError,
        )
    frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
    return {"name": name, "message": _("Đã xoá ủy quyền duyệt.")}
