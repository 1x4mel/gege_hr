"""
Catalog Master API — Zero-Frappe org-structure & simple masters (plan §4.5,
gaps G5 + G6).

Lets an HR Manager browse + CRUD the simple catalog DocTypes surfaced in the
"Cài đặt" screen — Department, Designation, Branch, Employment Type, VN Work
Location and Leave Type (plus read-only browse of VN Attendance Policy) —
entirely from inside the HR app, without ever opening Frappe Desk.

Why a dedicated module (instead of the generic ``/api/resource/<doctype>`` path)
---------------------------------------------------------------------------
These are *standard* Frappe / gege_hr master DocTypes that the ``HR Manager`` /
``HR User`` roles are NOT granted read/write on by default. The generic REST
helpers therefore deny access::

    User <hr@example.com> does not have doctype access via role permission
    for document Department

This module exposes those masters through a dedicated ``@frappe.whitelist`` RPC
that honours Frappe's real role permissions — the ``HR Manager`` / ``HR User``
grants are provisioned by :mod:`gege_hr.gege_hr.api.setup_permissions`
(``grant_hr_permissions``, also wired into the ``after_migrate`` hook). On top
of the role permissions, every call is gated by ``frappe.only_for(HR_ADMIN_ROLES)``
(defense in depth — never a bypass).

A strict allowlist (``_CATALOG_DOCTYPES`` for read, ``_EDITABLE_SIMPLE`` for
write) ensures only the masters the settings screen is designed to manage may
pass through this generic path.

Permission + audit conventions are identical to ``api/admin.py``: every call is
gated by ``frappe.only_for(HR_ADMIN_ROLES)`` and emits a ``VN Audit Event``
through ``admin._audit_admin`` (audit_type ``"Manual Override"``).
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import (
    _audit_admin,
    _default_company,
    _require_hr_admin,
    _safe_fields,
)

# All catalog DocTypes surfaced in HrSettingsView (counts + read-only browse).
# Reading is safe for every entry — including the child-table / complex ones —
# because we only ever fetch a name/field projection, never the child rows.
_CATALOG_DOCTYPES = frozenset(
    {
        "VN Work Location",
        "VN Attendance Policy",
        "Leave Type",
        "Holiday List",
        "Department",
        "Designation",
        "Branch",
        "Employment Type",
    }
)

# Simple masters the MasterCatalogManager card edits via list/save/delete.
# Holiday List (child table → dedicated holiday_master RPC) and VN Attendance
# Policy (read-only / complex) are intentionally excluded from write.
_EDITABLE_SIMPLE = frozenset(
    {
        "VN Work Location",
        "Leave Type",
        "Department",
        "Designation",
        "Branch",
        "Employment Type",
    }
)

# Frappe meta keys the SPA may echo back that must never be written through
# this generic path (``name`` is handled separately by the caller).
_META_KEYS = frozenset(
    {
        "doctype",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "parent",
        "parentfield",
        "parenttype",
        "idx",
        "__islocal",
        "__unsaved",
        "__linked_with",
        "_user_tags",
        "_assign",
        "_comments",
        "_liked_by",
    }
)


def _require_readable(doctype: str) -> None:
    """Reject any DocType outside the read allowlist."""
    if doctype not in _CATALOG_DOCTYPES:
        frappe.throw(_("DocType {0} không được truy cập qua catalog API.").format(doctype))


def _require_writable(doctype: str) -> None:
    """Reject any DocType outside the (smaller) write allowlist."""
    if doctype not in _EDITABLE_SIMPLE:
        frappe.throw(_("DocType {0} không được chỉnh sửa qua catalog API.").format(doctype))


def _clean_values(values) -> dict:
    """Drop Frappe meta / internal keys so they are never written back.

    ``name`` is preserved here so the caller can decide create-vs-update.
    """
    if not isinstance(values, dict):
        try:
            values = dict(values or {})
        except Exception:
            values = {}
    return {k: v for k, v in values.items() if k not in _META_KEYS}


@frappe.whitelist()
def list_catalog_masters(doctype: str, fields=None, search: str = "", limit: int = 200) -> list[dict]:
    """Return a field projection of a catalog DocType.

    Honours Frappe role permissions — the ``HR Manager`` read grant on each
    master is provisioned by :mod:`gege_hr.gege_hr.api.setup_permissions`, and
    :func:`_require_hr_admin` adds an app-level gate on top.
    """
    _require_hr_admin()
    doctype = (doctype or "").strip()
    _require_readable(doctype)

    # Validate requested fields against the DocType meta so a client-requested
    # field that has no DB column (e.g. `is_paid_leave` on a Leave Type whose
    # column was never created) is dropped instead of raising
    # OperationalError 1054 "Unknown column". Always includes `name`.
    fields = _safe_fields(doctype, fields)

    filters = []
    search = (search or "").strip()
    if search:
        filters.append(["name", "like", f"%{search}%"])

    return frappe.get_all(
        doctype,
        fields=fields,
        filters=filters or None,
        limit_page_length=limit,
        order_by="name asc",
    )


@frappe.whitelist()
def get_catalog_master(doctype: str, name: str) -> dict:
    """Return a full master row by name (honours Frappe role permissions)."""
    _require_hr_admin()
    doctype = (doctype or "").strip()
    name = (name or "").strip()
    _require_readable(doctype)
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(_('{0} "{1}" không tồn tại.').format(doctype, name))
    doc = frappe.get_doc(doctype, name)
    return doc.as_dict()


@frappe.whitelist()
def save_catalog_master(doctype: str, values=None, is_new: int = 0) -> dict:
    """Create or update a simple catalog master.

    Honours Frappe role permissions — the ``HR Manager`` create/write grants on
    each editable master are provisioned by
    :mod:`gege_hr.gege_hr.api.setup_permissions`; :func:`_require_hr_admin` adds
    an app-level gate on top. ``is_new`` may be passed from the SPA, but is also
    derived from whether a non-empty ``name`` was supplied.
    """
    _require_hr_admin()
    doctype = (doctype or "").strip()
    _require_writable(doctype)

    payload = _clean_values(values)
    name = (payload.pop("name", "") or "").strip()
    created = bool(int(is_new or 0)) or not name

    if created:
        doc = frappe.get_doc({"doctype": doctype, **payload})
        doc.insert()
        ref = doc.name
        action = _("Tạo")
    else:
        if not name or not frappe.db.exists(doctype, name):
            frappe.throw(_('{0} "{1}" không tồn tại.').format(doctype, name))
        doc = frappe.get_doc(doctype, name)
        for key, val in payload.items():
            doc.set(key, val)
        doc.save()
        ref = doc.name
        action = _("Cập nhật")

    _audit_admin(
        _('{0} {1} "{2}"').format(action, doctype, ref),
        reference_doctype=doctype,
        reference_name=ref,
        company=_default_company(),
        new_value={"doctype": doctype, "is_new": created},
    )
    return {"name": ref}


@frappe.whitelist()
def delete_catalog_master(doctype: str, name: str) -> dict:
    """Delete a simple catalog master (honours Frappe role permissions).

    ``force`` is left at its default so Frappe still raises
    ``LinkValidationError`` when the master is referenced by Employees / other
    docs — preventing orphaned links in the org structure. The SPA surfaces that
    error as a friendly message.
    """
    _require_hr_admin()
    doctype = (doctype or "").strip()
    name = (name or "").strip()
    _require_writable(doctype)
    if not name or not frappe.db.exists(doctype, name):
        frappe.throw(_('{0} "{1}" không tồn tại.').format(doctype, name))
    frappe.delete_doc(doctype, name)

    _audit_admin(
        _('Xoá {0} "{1}"').format(doctype, name),
        reference_doctype=doctype,
        reference_name=name,
        company=_default_company(),
    )
    return {"name": name, "deleted": True}
