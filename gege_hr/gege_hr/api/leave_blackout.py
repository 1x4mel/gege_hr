"""Leave blackout period API — plan v5 §11.4 / doctype-design §31.

HR-only CRUD for **VN Leave Blackout Period** + a read endpoint the leave
preview engine consults. Pure date/range math lives in
``utils/leave_blackout.py`` (bench-free).

SPA contract:

  * ``blackout_periods``   → list (optionally scoped by company/leave type)
  * ``create_blackout``    → mint a rule
  * ``update_blackout``    → edit fields (HR Manager only)
  * ``delete_blackout``    → remove (HR Manager only)
  * ``evaluate_leave_blackout`` → decide block/require-approval/warn for a window
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.utils import leave_blackout as blackout_utils

DOCTYPE = "VN Leave Blackout Period"
_LIST_FIELDS = [
    "name",
    "blackout_name",
    "company",
    "branch",
    "department",
    "from_date",
    "to_date",
    "applies_to_leave_type",
    "is_active",
    "action",
    "reason",
    "modified",
]


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "System Manager"})


def _require_hr_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not (roles & {"HR Manager", "System Manager"}):
        frappe.throw(_("Yêu cầu quyền HR Manager."), frappe.PermissionError)


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def blackout_periods(
    company: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active: int | None = None,
) -> list[dict]:
    """List blackout rules. Active-only by default for the calendar/preview."""
    if not _table_ready():
        return []
    filters: dict = {}
    if company:
        filters["company"] = company
    if applies_to_leave_type:
        filters["applies_to_leave_type"] = applies_to_leave_type
    if is_active is not None:
        filters["is_active"] = int(is_active)
    else:
        filters["is_active"] = 1
    try:
        rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=_LIST_FIELDS, order_by="from_date desc")
    except Exception:
        frappe.log_error(title="blackout.blackout_periods failed")
        return []
    return [blackout_utils.blackout_row(r) for r in rows]


@frappe.whitelist()
def evaluate_leave_blackout(
    from_date: str,
    to_date: str,
    leave_type: str | None = None,
    company: str | None = None,
    employee: str | None = None,
) -> dict:
    """Decision endpoint for the leave preview/apply flow.

    Loads the active blackout rules (optionally company-scoped) and delegates
    to :func:`leave_blackout.evaluate_blackout`. When ``company`` is omitted but
    ``employee`` is supplied, the company is resolved from the Employee record so
    the SPA leave form — which knows the employee, not the company — still gets a
    correctly scoped decision (and rules from other companies do not leak in).
    """
    if not company and employee:
        try:
            company = frappe.db.get_value("Employee", employee, "company") or None
        except Exception:
            company = None
    rules = blackout_periods(company=company, is_active=1)
    # DB rows already normalised; pass back the dict shape evaluate expects.
    return blackout_utils.evaluate_blackout(
        from_date=from_date,
        to_date=to_date,
        leave_type=leave_type,
        rules=rules,
    )


# --------------------------------------------------------------------------- #
# Write endpoints (HR Manager)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_blackout(
    blackout_name: str,
    company: str,
    from_date: str,
    to_date: str,
    reason: str,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active: bool = True,
    action: str = "Warning",
) -> dict:
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    try:
        payload = blackout_utils.blackout_payload(
            blackout_name=blackout_name,
            company=company,
            from_date=from_date,
            to_date=to_date,
            reason=reason,
            branch=branch,
            department=department,
            applies_to_leave_type=applies_to_leave_type,
            is_active=is_active,
            action=action,
        )
    except ValueError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    doc = frappe.get_doc(payload)
    doc.insert()
    return {"name": doc.name, "action": doc.action, "message": _("Đã tạo kỳ cấm nghỉ.")}


@frappe.whitelist()
def update_blackout(name: str, **fields) -> dict:
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    doc = frappe.get_doc(DOCTYPE, name)
    allowed = {
        "blackout_name",
        "from_date",
        "to_date",
        "reason",
        "branch",
        "department",
        "applies_to_leave_type",
        "is_active",
        "action",
    }
    for key, value in fields.items():
        if key in allowed:
            doc.set(key, value)
    # Re-validate the date window before saving.
    if not blackout_utils.is_valid_date_range(doc.from_date, doc.to_date):
        frappe.throw(_("from_date phải trước hoặc bằng to_date."), frappe.ValidationError)
    doc.save()
    return {"name": doc.name, "message": _("Đã cập nhật.")}


@frappe.whitelist()
def delete_blackout(name: str) -> dict:
    _require_hr_manager()
    if not _table_ready():
        return {"name": name, "deleted": False}
    try:
        frappe.delete_doc(DOCTYPE, name)
    except Exception:
        frappe.log_error(title="blackout.delete_blackout failed")
        return {"name": name, "deleted": False}
    return {"name": name, "deleted": True, "message": _("Đã xóa.")}
