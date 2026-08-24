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

from gege_hr.gege_hr.utils import leave_blackout as blackout_utils, pagination

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


# Broad-search fields for the blackout list (DNA §6.6 D — OR-combined free text).
_BLACKOUT_SEARCH_FIELDS = (
    "name",
    "blackout_name",
    "company",
    "branch",
    "department",
    "applies_to_leave_type",
    "reason",
)


def _blackout_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text blackout search, or None."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [[field, "like", like] for field in _BLACKOUT_SEARCH_FIELDS]


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
_BLACKOUT_SUMMARY_FIELDS = ["name", "is_active", "action"]


def _blackout_summary(light_rows) -> dict:
    """Per-bucket counts over the full filtered set (SPA summary tiles)."""
    actions = pagination.bucket_counts(light_rows, "action")
    active = sum(1 for r in light_rows or [] if r.get("is_active"))
    return {
        "total": len(light_rows or []),
        "active": active,
        "block": actions.get("Block", 0),
        "require_approval": actions.get("Require HR Approval", 0),
    }


@frappe.whitelist()
def blackout_periods(
    company: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active: int | None = None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    limit: int = 200,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """List blackout rules. Active-only by default for the calendar/preview.

    ``search`` performs a server-side broad LIKE across the rule's text fields
    (DNA §6.6 D, HR-BL blackout) so the SPA broad-search box no longer needs to
    re-filter an already-loaded list client-side.

    Per-column popover filters (DNA Law #2 — gear covers every content column)
    are all server-side: ``action`` (exact), ``company`` /
    ``applies_to_leave_type`` (exact), and a date-window ``from_date``–``to_date``
    (intersection: rules whose ``[from_date, to_date]`` window overlaps the
    selected window — i.e. ``rule.to_date >= from_date`` AND
    ``rule.from_date <= to_date``).

    Filters use the **list form** (DNA §6.6 B): a dict cannot hold two
    conditions touching different date columns, and ``between`` renders broken
    SQL for the same field twice.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates the *full* filtered set so the
    SPA summary tiles stay correct under pagination. Without ``page_size`` the
    legacy bare-list return is preserved (``evaluate_leave_blackout`` reuses it
    as a plain list, so it must keep working un-paginated).
    """
    if not _table_ready():
        if page_size:
            return {"data": [], "total": 0, "summary": _blackout_summary([])}
        return []
    # List-form filters (DNA §6.6 B) — keeps the date-window overlap (two
    # conditions on different date columns) which a dict cannot express.
    filters: list = []
    if company:
        filters.append(["company", "=", company])
    if applies_to_leave_type:
        filters.append(["applies_to_leave_type", "=", applies_to_leave_type])
    if is_active is not None:
        filters.append(["is_active", "=", int(is_active)])
    else:
        filters.append(["is_active", "=", 1])
    if action:
        filters.append(["action", "=", action])
    # Date-window overlap (rule window ∩ selected window). NOT `between`
    # (DNA §6.6 B): two conditions on different date columns.
    if from_date:
        filters.append(["to_date", ">=", from_date])
    if to_date:
        filters.append(["from_date", "<=", to_date])
    or_filters = _blackout_search_or_filters(search)

    if page_size:
        summary = _blackout_summary(
            pagination.all_rows(
                DOCTYPE,
                fields=_BLACKOUT_SUMMARY_FIELDS,
                filters=filters,
                or_filters=or_filters,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.db.get_all(
                    DOCTYPE,
                    filters=filters,
                    or_filters=or_filters,
                    fields=_LIST_FIELDS,
                    order_by="from_date desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="blackout.blackout_periods failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {
            "data": [blackout_utils.blackout_row(r) for r in rows],
            "total": summary["total"],
            "summary": summary,
        }

    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            or_filters=or_filters,
            fields=_LIST_FIELDS,
            order_by="from_date desc",
            limit_page_length=pagination.clamp_limit(limit, default=200),
        )
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
