"""Settings Single API — desk-free editors for the two HR Single DocTypes.

Plan: plans/plan-hr-settings-desk-free.md §2.1 (gap G1). The SPA settings tab
"Cấu hình chung" edits *Single* DocTypes (``issingle: 1``) — the Frappe pattern
for global settings:

* ``VN HR Portal Setting`` (gege_hr) — portal/attendance behaviour. The field
  allowlist is shared with :mod:`gege_hr.gege_hr.api.admin`
  (:data:`PORTAL_SETTING_FIELDS`) so the legacy ``save_portal_setting`` RPC and
  this module can never drift apart.
* ``HR Settings`` (HRMS) — standard employee/reminder/leave/shift switches that
  the HR Manager owns per the stock hrms permissions.

Why a dedicated module (not ``/api/resource``): identical rationale to
:mod:`catalog_master` — Single writes must never accept arbitrary keys, so a
strict per-doctype readable/writable allowlist + ``_require_hr_admin`` gate +
``_audit_admin`` trail wrap every call (convention §5.3).

``save_single_settings`` mirrors :func:`admin.save_portal_setting` semantics
(coerce by meta fieldtype, only changed fields, audit the diff) and adds
Select-option / Link-existence / numeric-range validation so bad payloads die
server-side with a Vietnamese message.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import (
    PORTAL_SETTING_FIELDS,
    _audit_admin,
    _meta_has_active,
    _require_hr_admin,
)

# HRMS ``HR Settings`` fields the SPA may edit (verified against the site meta —
# plan Bước 0.3; every field below exists on the installed hrms).
HR_SETTINGS_FIELDS = frozenset(
    {
        # Employee settings
        "emp_created_by",
        "retirement_age",
        "standard_working_hours",
        # Reminders
        "send_birthday_reminders",
        "send_work_anniversary_reminders",
        "send_holiday_reminders",
        "frequency",
        "sender",
        # Leave & expense
        "send_leave_notification",
        "leave_approval_notification_template",
        "leave_status_notification_template",
        "leave_approver_mandatory_in_leave_application",
        "restrict_backdated_leave_application",
        "role_allowed_to_create_backdated_leave_application",
        "prevent_self_leave_approval",
        "prevent_self_expense_approval",
        "expense_approver_mandatory_in_expense_claim",
        "auto_leave_encashment",
        "show_leaves_of_all_department_members_in_calendar",
        # Shift
        "allow_multiple_shift_assignments",
        # Attendance
        "allow_employee_checkin_from_mobile_app",
        "allow_geolocation_tracking",
    }
)

# Read-only companions surfaced next to the editable fields (never written).
HR_SETTINGS_READONLY = frozenset({"sender_email"})

# Link dropdown sources per field (for the SPA SearchableSelect).
_PORTAL_LINK_OPTIONS = {
    "default_work_location": "VN Work Location",
    "default_attendance_policy": "VN Attendance Policy",
}
_HR_SETTINGS_LINK_OPTIONS = {
    "sender": "Email Account",
    "leave_approval_notification_template": "Email Template",
    "leave_status_notification_template": "Email Template",
    "role_allowed_to_create_backdated_leave_application": "Role",
}

_SETTINGS_REGISTRY: dict[str, dict] = {
    "VN HR Portal Setting": {
        "writable": frozenset(PORTAL_SETTING_FIELDS),
        "readonly": frozenset(),
        "link_options": _PORTAL_LINK_OPTIONS,
    },
    "HR Settings": {
        "writable": HR_SETTINGS_FIELDS,
        "readonly": HR_SETTINGS_READONLY,
        "link_options": _HR_SETTINGS_LINK_OPTIONS,
    },
}

# DocTypes whose ``name`` list may be fetched for the Link dropdowns above.
_LINK_OPTION_DOCTYPES = frozenset(
    {
        "Role",
        "Email Account",
        "Email Template",
        "Company",
        "Branch",
        "Department",
        "VN Work Location",
        "VN Attendance Policy",
    }
)

# Numeric guards beyond "must be an int" (plan §2.1).
_FIELD_RANGES = {
    "payroll_cutoff_day": (1, 28, "Ngày cắt lương phải từ 1 đến 28."),
    "lock_attendance_after_days": (1, 365, "Số ngày khoá chấm công phải từ 1 đến 365."),
}


def _registry(doctype: str) -> dict:
    entry = _SETTINGS_REGISTRY.get(doctype)
    if not entry:
        frappe.throw(_("DocType {0} không được truy cập qua settings API.").format(doctype))
    return entry


def _select_options(meta, fieldname: str) -> list[str]:
    df = meta.get_field(fieldname)
    if not df or not df.options:
        return []
    return [o.strip() for o in str(df.options).split("\n") if o.strip()]


def _coerce(meta, fieldname: str, value):
    """Cast ``value`` to the meta fieldtype; validate Select/Link/ranges.

    Returns the coerced value. Raises with a Vietnamese message on bad input.
    ``None``/"" for Link/Select means "clear" and is allowed.
    """
    df = meta.get_field(fieldname)
    ftype = df.fieldtype if df else "Data"

    if value in (None, ""):
        return None if ftype in ("Link", "Select") else value

    if ftype == "Check":
        return 1 if str(value) in ("1", "true", "True", "on", "Yes", "yes") else 0
    if ftype == "Int":
        try:
            coerced = int(float(value))
        except (TypeError, ValueError):
            frappe.throw(_("{0} phải là số nguyên.").format(df.label if df else fieldname))
    elif ftype in ("Float", "Currency", "Percent"):
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            frappe.throw(_("{0} phải là số.").format(df.label if df else fieldname))
    elif ftype == "Select":
        options = _select_options(meta, fieldname)
        if options and str(value) not in options:
            frappe.throw(
                _("Giá trị “{0}” không hợp lệ cho {1}.").format(value, df.label if df else fieldname)
            )
        return str(value)
    elif ftype == "Link":
        target = df.options if df else None
        if target and not frappe.db.exists(target, value):
            frappe.throw(_("{0} “{1}” không tồn tại.").format(target, value))
        return str(value)
    else:
        return value

    if ftype == "Int" and fieldname in _FIELD_RANGES:
        low, high, message = _FIELD_RANGES[fieldname]
        if not (low <= coerced <= high):
            frappe.throw(_(message))
    return coerced


@frappe.whitelist()
def get_single_settings(doctype: str) -> dict:
    """Return the editable values of one registered Single DocType.

    Response shape (mirrors ``admin.get_portal_setting`` so the SPA reuses the
    same renderer): ``{doctype, values: {field: value}, __options:
    {field: [choices]}, modified, modified_by}``.
    """
    _require_hr_admin()
    doctype = (doctype or "").strip()
    entry = _registry(doctype)
    doc = frappe.get_cached_doc(doctype, doctype)

    fields = [f for f in entry["writable"] | entry["readonly"] if doc.meta.has_field(f)]
    values = {f: doc.get(f) for f in fields}

    options: dict[str, list] = {}
    for field, target in entry["link_options"].items():
        if not doc.meta.has_field(field):
            continue
        if target in ("VN Work Location", "VN Attendance Policy") and _meta_has_active(target):
            rows = frappe.db.get_all(target, {"is_active": 1}, ["name"], limit=100)
        else:
            rows = frappe.db.get_all(target, ["name"], limit=100)
        options[field] = [r.name for r in rows]
    for field in fields:
        if doc.meta.get_field(field) and doc.meta.get_field(field).fieldtype == "Select":
            opts = _select_options(doc.meta, field)
            if opts:
                options[field] = opts

    return {
        "doctype": doctype,
        "values": values,
        "__options": options,
        "modified": doc.modified,
        "modified_by": doc.modified_by,
    }


@frappe.whitelist()
def save_single_settings(doctype: str, values=None) -> dict:
    """Persist allowlisted fields of one registered Single DocType.

    Only keys inside ``writable`` are honoured (unknown keys are dropped,
    defence-in-depth). Each *changed* field is coerced + validated by meta type,
    the doc is saved once, the diff is audited as a Manual Override and a
    realtime ``vn_settings_updated`` event pings the SPA caches.
    """
    _require_hr_admin()
    doctype = (doctype or "").strip()
    entry = _registry(doctype)
    if not isinstance(values, dict):
        try:
            values = dict(values or {})
        except Exception:
            values = {}

    payload = {k: v for k, v in values.items() if k in entry["writable"]}
    if not payload:
        frappe.throw(_("Không có trường hợp lệ để lưu."))

    doc = frappe.get_doc(doctype, doctype)
    changes: dict[str, list] = {}
    for field in entry["writable"]:
        if field not in payload or not doc.meta.has_field(field):
            continue
        new_val = _coerce(doc.meta, field, payload[field])
        old_val = doc.get(field)
        if new_val in (None, "") and old_val in (None, ""):
            continue
        if old_val == new_val:
            continue
        doc.set(field, new_val)
        changes[field] = [old_val, new_val]

    if not changes:
        return {"ok": True, "changed": [], "message": "Không có thay đổi."}

    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    diff_text = "; ".join(f"{f}: {old!r} → {new!r}" for f, (old, new) in changes.items())
    _audit_admin(
        _("Cập nhật cấu hình {0}: {1}").format(doctype, diff_text),
        reference_doctype=doctype,
        reference_name=doctype,
        new_value={"fields": {f: new for f, (_old, new) in changes.items()}},
    )
    try:
        frappe.publish_realtime("vn_settings_updated", {"doctype": doctype})
    except Exception:  # best-effort ping — never fail the save
        pass

    return {
        "ok": True,
        "changed": sorted(changes.keys()),
        "changes": diff_text,
        "modified": doc.modified,
        "modified_by": doc.modified_by,
    }


@frappe.whitelist()
def settings_link_options(doctype: str, search: str = "", limit: int = 100) -> list[dict]:
    """Name list of one allowlisted DocType for the Link dropdowns (debounced
    SearchableSelect). Gate identical to the settings editors."""
    _require_hr_admin()
    doctype = (doctype or "").strip()
    if doctype not in _LINK_OPTION_DOCTYPES:
        frappe.throw(_("DocType {0} không được truy cập qua settings API.").format(doctype))
    search = (search or "").strip()
    filters = [["name", "like", f"%{search}%"]] if search else None
    rows = frappe.db.get_all(
        doctype,
        filters=filters,
        fields=["name"],
        limit_page_length=min(int(limit or 100), 200),
        order_by="name asc",
    )
    return [{"value": r.name, "label": r.name} for r in rows]
