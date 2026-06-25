"""Attendance Policy API — HR-gated CRUD for ``VN Attendance Policy`` (+ child penalty rules).

The policy is the master config behind the work-session calculation engine:
grace thresholds, OT settings, night windows, missing-log actions, and a child
table of penalty rules (``VN Attendance Penalty Rule``). Previously this DocType
was read-only in the portal (editable only via Desk); this module makes it fully
editable so HR can define penalty bands without leaving the app.

Design (mirrors ``approval_matrix``):
* HR-gated via :func:`_require_hr_admin`.
* Read returns the parent + its child ``penalty_rules``.
* Write is transactional create-or-update with a *replace* strategy for the
  child penalty table.
* Every mutation audited as a ``Manual Override`` :doc:`VN Audit Event`.
"""
from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import _audit_admin, _default_company, _require_hr_admin

DOCTYPE = "VN Attendance Policy"
CHILD = "VN Attendance Penalty Rule"

# Parent fields the SPA may write (excludes meta + read-only like version,
# policy_snapshot_json which are engine-managed).
PARENT_FIELDS = [
    "policy_name",
    "company",
    "apply_to",
    "branch",
    "department",
    "employee_grade",
    "is_active",
    "effective_from",
    "effective_to",
    # Grace & thresholds
    "grace_late_minutes",
    "grace_early_leave_minutes",
    "min_working_hours_full_day",
    "min_working_hours_half_day",
    "multiple_logs_strategy",
    "min_overtime_minutes",
    "max_overtime_hours_per_shift",
    "max_total_work_hours_per_shift",
    # OT settings
    "allow_pre_shift_overtime",
    "allow_post_shift_overtime",
    "require_overtime_approval",
    "overtime_rounding_method",
    "overtime_rounding_minutes",
    "allow_ot_compensate_late",
    "allow_ot_compensate_early_leave",
    "max_overtime_hours_per_day",
    "minimum_rest_hours_between_shifts",
    # Night & missing-log
    "night_start_time",
    "night_end_time",
    "missing_checkin_action",
    "missing_checkout_action",
    "auto_mark_absent",
]

# Child penalty rule fields.
PENALTY_FIELDS = [
    "from_minutes",
    "to_minutes",
    "penalty_type",
    "penalty_value",
    "salary_component",
]


def _project(doc) -> dict:
    out = {f: doc.get(f) for f in PARENT_FIELDS if doc.meta.has_field(f)}
    out["name"] = doc.name
    out["penalty_rules"] = [_project_penalty(r) for r in (doc.get("penalty_rules") or [])]
    return out


def _project_penalty(row) -> dict:
    return {f: row.get(f) for f in PENALTY_FIELDS} | {"name": getattr(row, "name", "")}


def _project_list_row(name: str) -> dict:
    """Lightweight row for the list view (no child fetch)."""
    return frappe.db.get_value(
        DOCTYPE,
        name,
        ["name", "policy_name", "company", "is_active", "apply_to", "grace_late_minutes"],
        as_dict=True,
    )


@frappe.whitelist()
def list_policies(company: str | None = None) -> list[dict]:
    """Return all policies (lightweight list rows)."""
    _require_hr_admin()
    filters: dict = {}
    if company:
        filters["company"] = company
    names = frappe.db.get_all(DOCTYPE, filters, ["name"], order_by="policy_name")
    return [_project_list_row(r.name) for r in names]


@frappe.whitelist()
def get_policy(name: str) -> dict:
    """Return one policy + its penalty rules."""
    _require_hr_admin()
    if not frappe.db.exists(DOCTYPE, name):
        frappe.throw(_("Chính sách {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    return _project(frappe.get_doc(DOCTYPE, name))


@frappe.whitelist()
def save_policy(values: dict | None = None, **kwargs) -> dict:
    """Create or update a policy + its penalty rules in one transaction."""
    _require_hr_admin()
    payload = values or kwargs or {}
    name = (payload.get("name") or "").strip()
    rules = payload.get("penalty_rules") or []
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

    # Replace penalty rules (child table).
    doc.set("penalty_rules", [])
    for r in rules:
        child = doc.append("penalty_rules", {})
        for f in PENALTY_FIELDS:
            if f in r:
                child.set(f, r[f])

    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()

    action = "Tạo" if is_new else "Cập nhật"
    _audit_admin(
        f"{action} chính sách chấm công: {doc.policy_name} ({len(rules)} quy tắc phạt)",
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        new_value=f"rules={len(rules)}",
    )
    return _project(doc)


@frappe.whitelist()
def delete_policy(name: str) -> dict:
    """Delete a policy (and its child rules cascade)."""
    _require_hr_admin()
    if not frappe.db.exists(DOCTYPE, name):
        return {"deleted": False, "message": "Không tồn tại."}
    label = frappe.db.get_value(DOCTYPE, name, "policy_name") or name
    frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
    frappe.db.commit()
    _audit_admin(
        f"Xoá chính sách chấm công: {label}",
        reference_doctype=DOCTYPE,
        reference_name=name,
    )
    return {"deleted": True, "name": name}


@frappe.whitelist()
def policy_options() -> dict:
    """Dropdown/select options for the editor (read from the meta)."""
    _require_hr_admin()
    meta = frappe.get_meta(DOCTYPE)

    def _opts(fieldname):
        f = meta.get_field(fieldname)
        return (f.options or "").split("\n") if f else []

    return {
        "companies": [r.name for r in frappe.db.get_all("Company", ["name"])],
        "apply_to_options": _opts("apply_to"),
        "multiple_logs_strategy_options": _opts("multiple_logs_strategy"),
        "overtime_rounding_method_options": _opts("overtime_rounding_method"),
        "missing_checkin_action_options": _opts("missing_checkin_action"),
        "missing_checkout_action_options": _opts("missing_checkout_action"),
        "penalty_type_options": _opts("penalty_type") or ["Fixed Amount", "Per Minute", "Percentage", "Half Day", "Full Day"],
    }
