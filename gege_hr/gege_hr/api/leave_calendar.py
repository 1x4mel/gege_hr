"""Leave calendar cache API — plan v5 §11.3 / doctype-design §29.

Builds / fetches / invalidates **VN Leave Calendar Cache** rows for a
(company, branch, department, year, month) scope. The cache stores a month of
leave data as JSON so the SPA calendar renders in one read. Pure key/expiry
math lives in ``utils/leave_calendar.py`` (bench-free).

SPA contract:

  * ``get_leave_calendar`` → return cached JSON if fresh, else build + cache
  * ``build_leave_calendar``→ force a (re)build
  * ``invalidate_leave_calendar`` → drop a cache row
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import getdate, now

from gege_hr.gege_hr.utils import leave_calendar as calendar_utils

DOCTYPE = "VN Leave Calendar Cache"


def _is_hr() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR User", "HR Manager", "System Manager"})


def _require_hr() -> None:
    if not _is_hr():
        frappe.throw(_("Yêu cầu quyền HR."), frappe.PermissionError)


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Cache read
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_leave_calendar(
    company: str,
    year,
    month,
    branch: str | None = None,
    department: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Return ``{cache_key, data, generated_at, from_cache}``.

    Serves the cached row when fresh; otherwise builds (and stores) a new one.
    """
    # Resolve sensible defaults so the calendar renders before the SPA has
    # chosen a company (the HR admin account often has no linked Employee, so
    # the client's defaultCompany() is empty). Year/month default to today.
    if not (company or "").strip():
        # Default to the first Company (VN HR Portal Setting has no default_company
        # field; reading it would raise ValidationError). Respects Company read
        # permission now granted to the HR roles.
        company = frappe.db.get_value("Company", {}, "name", order_by="name asc") or ""
    if year in (None, ""):
        year = getdate().year
    if month in (None, ""):
        month = getdate().month
    key = calendar_utils.build_cache_key(
        company=company, branch=branch, department=department, year=year, month=month
    )
    if key is None:
        # Still unbuildable (e.g. no Company exists yet) — return an empty
        # payload instead of raising so the calendar screen loads cleanly.
        return {"cache_key": "", "data": [], "generated_at": "", "from_cache": False}
    if not force_refresh and _table_ready():
        cached = _fetch_cached(key)
        if cached is not None:
            cached["from_cache"] = True
            return cached
    # No fresh cache → build live.
    return build_leave_calendar(company=company, year=year, month=month, branch=branch, department=department)


def _fetch_cached(cache_key: str) -> dict | None:
    try:
        row = frappe.db.get_value(
            DOCTYPE,
            cache_key,
            ["cache_key", "data_json", "generated_at", "expires_at"],
            as_dict=True,
        )
    except Exception:
        frappe.log_error(title="calendar._fetch_cached failed")
        return None
    if not row:
        return None
    if calendar_utils.is_expired(row.get("expires_at")):
        return None
    return _shape(row)


def _shape(row) -> dict:
    data_json = row.get("data_json") or "{}"
    try:
        data = json.loads(data_json)
    except (TypeError, ValueError):
        data = {}
    return {
        "cache_key": row.get("cache_key"),
        "data": data,
        "generated_at": row.get("generated_at"),
        "from_cache": False,
    }


# --------------------------------------------------------------------------- #
# Cache build / invalidate
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def build_leave_calendar(
    company: str,
    year,
    month,
    branch: str | None = None,
    department: str | None = None,
) -> dict:
    """Compute the month's leave data and persist it as a cache row."""
    _require_hr()
    window = calendar_utils.month_window(year, month)
    if window is None:
        frappe.throw(_("Thiếu hoặc sai year/month."), frappe.ValidationError)
    from_date, to_date = window
    data = _load_leave_data(company, from_date, to_date, branch, department)
    payload = calendar_utils.calendar_cache_payload(
        company=company,
        year=year,
        month=month,
        branch=branch,
        department=department,
        data=data,
        generated_at=now(),
    )
    if payload is None:
        frappe.throw(_("Không thể tạo cache key."), frappe.ValidationError)
    _upsert(payload)
    return {
        "cache_key": payload["cache_key"],
        "data": data,
        "generated_at": payload["generated_at"],
        "from_cache": False,
    }


def _load_leave_data(
    company: str, from_date: str, to_date: str, branch: str | None, department: str | None
) -> list[dict]:
    """Approved leave applications overlapping the month window."""
    if not _leave_table_ready():
        return []
    filters = {
        "docstatus": 1,
        "status": "Approved",
        "from_date": ("<=", to_date),
        "to_date": (">=", from_date),
    }
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters=filters,
            fields=[
                "name",
                "employee",
                "employee_name",
                "leave_type",
                "from_date",
                "to_date",
                "total_leave_days",
            ],
            order_by="from_date",
        )
    except Exception:
        frappe.log_error(title="calendar._load_leave_data failed")
        return []
    return rows or []


def _leave_table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists("Leave Application"))
    except Exception:
        return False


def _upsert(payload: dict) -> None:
    if not _table_ready():
        return
    try:
        existing = frappe.db.get_value(DOCTYPE, payload["cache_key"], "name")
    except Exception:
        existing = None
    try:
        # Cache maintenance — may run from an HR request or a system hook
        # (on_leave_submit), so persist directly regardless of the caller's role.
        if existing:
            doc = frappe.get_doc(DOCTYPE, existing)
            doc.data_json = payload["data_json"]
            doc.generated_at = payload["generated_at"]
            doc.expires_at = payload["expires_at"]
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc(payload)
            doc.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title="calendar._upsert failed")


@frappe.whitelist()
def invalidate_leave_calendar(
    company: str,
    year,
    month,
    branch: str | None = None,
    department: str | None = None,
) -> dict:
    """Drop a cache row so the next read rebuilds it."""
    _require_hr()
    key = calendar_utils.build_cache_key(
        company=company, branch=branch, department=department, year=year, month=month
    )
    if key is None or not _table_ready():
        return {"cache_key": key, "invalidated": False}
    try:
        existing = frappe.db.get_value(DOCTYPE, key, "name")
    except Exception:
        existing = None
    if not existing:
        return {"cache_key": key, "invalidated": False}
    try:
        # Cache maintenance — drop the row directly (see _upsert note above).
        frappe.delete_doc(DOCTYPE, existing, ignore_permissions=True)
    except Exception:
        frappe.log_error(title="calendar.invalidate failed")
        return {"cache_key": key, "invalidated": False}
    return {"cache_key": key, "invalidated": True, "message": _("Đã làm mới cache.")}
