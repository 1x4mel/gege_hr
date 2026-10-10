"""Approval Matrix API — HR-gated CRUD for ``VN Approval Matrix`` (+ steps).

The matrix defines *who approves what*: one parent row per (transaction type,
company, scope) carrying a child table of ordered approval steps
(``VN Approval Step``). This is the configuration UI behind every multi-step
approval flow (leave / OT / correction / salary advance).

Design (mirrors ``catalog_master`` / ``holiday_master`` patterns):
* HR-gated via :func:`_require_hr_admin`.
* Read returns the parent + its child steps (the generic catalog reader can't
  return child rows).
* Write is a single transactional create-or-update: parent fields + full step
  list (replace strategy — the SPA always sends the complete step array).
* Every mutation audited as a ``Manual Override`` :doc:`VN Audit Event`.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import _audit_admin, _default_company, _require_hr_admin

DOCTYPE = "VN Approval Matrix"
CHILD = "VN Approval Step"

# Parent fields the SPA may write. Whitelisting prevents arbitrary field
# injection (e.g. meta keys) through the RPC.
PARENT_FIELDS = [
    "matrix_name",
    "company",
    "transaction_type",
    "apply_to",
    "branch",
    "department",
    "employee_grade",
    "is_active",
    "min_amount",
    "max_amount",
    "min_hours",
    "max_hours",
]

# Child step fields.
STEP_FIELDS = [
    "step_no",
    "approver_type",
    "approver_user",
    "approver_role",
    "can_edit_value",
    "allow_delegate",
    "condition",
]


def _project_parent(doc) -> dict:
    return {f: doc.get(f) for f in PARENT_FIELDS if doc.meta.has_field(f)} | {
        "name": doc.name,
        "steps": [_project_step(s) for s in (doc.get("steps") or [])],
    }


def _project_step(row) -> dict:
    return {f: row.get(f) for f in STEP_FIELDS} | {"name": getattr(row, "name", "")}


@frappe.whitelist()
def list_approval_matrices(
    company: str | None = None,
    transaction_type: str | None = None,
) -> list[dict]:
    """Return all matrices (optionally filtered) with their nested steps."""
    _require_hr_admin()
    filters: dict = {}
    if company:
        filters["company"] = company
    if transaction_type:
        filters["transaction_type"] = transaction_type
    names = frappe.db.get_all(DOCTYPE, filters, ["name"], order_by="transaction_type, matrix_name")
    out = []
    for r in names:
        doc = frappe.get_cached_doc(DOCTYPE, r.name)
        out.append(_project_parent(doc))
    return out


@frappe.whitelist()
def get_approval_matrix(name: str) -> dict:
    """Return one matrix + its steps."""
    _require_hr_admin()
    if not frappe.db.exists(DOCTYPE, name):
        frappe.throw(_("Ma trận duyệt {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    return _project_parent(frappe.get_doc(DOCTYPE, name))


@frappe.whitelist()
def save_approval_matrix(values: dict | None = None, **kwargs) -> dict:
    """Create or update a matrix + its steps in one transaction.

    ``values`` is the flat dict the SPA sends (parent fields + a ``steps`` array).
    Step rows are applied with a *replace* strategy: the existing child table is
    cleared then repopulated from the payload, so add/remove/reorder all work
    without per-row diffing.
    """
    _require_hr_admin()
    payload = values or kwargs or {}
    name = (payload.get("name") or "").strip()
    steps = payload.get("steps") or []
    company = (payload.get("company") or "").strip() or _default_company()

    if name:
        doc = frappe.get_doc(DOCTYPE, name)
        is_new = False
    else:
        doc = frappe.new_doc(DOCTYPE)
        is_new = True

    for f in PARENT_FIELDS:
        if f in payload and doc.meta.has_field(f):
            doc.set(f, payload[f])
    if not doc.get("company"):
        doc.company = company

    # Replace steps (child table).
    doc.set("steps", [])
    for st in steps:
        child = doc.append("steps", {})
        for f in STEP_FIELDS:
            if f in st:
                child.set(f, st[f])

    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()

    action = "Tạo" if is_new else "Cập nhật"
    _audit_admin(
        f"{action} ma trận duyệt: {doc.matrix_name} ({doc.transaction_type}, {len(steps)} bước)",
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        new_value=f"{doc.transaction_type}; steps={len(steps)}",
    )
    return _project_parent(doc)


@frappe.whitelist()
def delete_approval_matrix(name: str) -> dict:
    """Delete a matrix (and its child steps cascade)."""
    _require_hr_admin()
    if not frappe.db.exists(DOCTYPE, name):
        return {"deleted": False, "message": "Không tồn tại."}
    label = frappe.db.get_value(DOCTYPE, name, "matrix_name") or name
    frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
    frappe.db.commit()
    _audit_admin(
        f"Xoá ma trận duyệt: {label}",
        reference_doctype=DOCTYPE,
        reference_name=name,
    )
    return {"deleted": True, "name": name}


@frappe.whitelist()
def approval_matrix_options() -> dict:
    """Dropdown options for the editor (companies + transaction types)."""
    _require_hr_admin()
    meta = frappe.get_meta(DOCTYPE)
    tt_field = meta.get_field("transaction_type")
    at_field = meta.get_field("apply_to")
    return {
        "companies": [r.name for r in frappe.db.get_all("Company", ["name"])],
        "transaction_types": (tt_field.options or "").split("\n") if tt_field else [],
        "apply_to_options": (at_field.options or "").split("\n") if at_field else [],
        "approver_types": [
            "Line Manager",
            "Department Head",
            "HR User",
            "HR Manager",
            "Specific User",
            "Specific Role",
        ],
    }
