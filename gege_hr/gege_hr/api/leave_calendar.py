"""Leave calendar cache API — plan v5 §11.3 / doctype-design §29 + desk-free
parity (plans/plan-leave-calendar-desk-free.md).

Builds / fetches / invalidates **VN Leave Calendar Cache** rows for a
(company, branch, department, year, month) scope. The cache stores a month of
leave data as JSON so the SPA calendar renders in one read. Pure key/expiry
math lives in ``utils/leave_calendar.py`` (bench-free).

SPA contract:

  * ``get_leave_calendar`` → return cached JSON if fresh, else build + cache
  * ``build_leave_calendar``→ force a (re)build
  * ``invalidate_leave_calendar`` → drop a cache row
  * ``touch_leave_calendar`` → invalidate every scope-key touched by a Leave
    Application decision + broadcast ``leave_calendar_updated`` (called from
    ``api/leave.py`` decision paths and the doc_events hooks)
  * ``calendar_employee_options`` → active-employee picker feed (desk-free)

v2 payload (cache key suffix ``-v2``): ``data = {leaves: [...], holidays: [...]}``
where ``leaves`` covers Open/Approved/Rejected (``docstatus < 2`` — Cancelled
excluded) and ``holidays`` comes from the company's default Holiday List.
``statuses`` / ``employee`` / ``leave_type`` / ``branch`` / ``department`` are
**read-time filters** applied after the cache read, so the key count stays at
one row per (scope, month).
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import getdate, now

from gege_hr.gege_hr.api.leave import _application_fields
from gege_hr.gege_hr.utils import leave_calendar as calendar_utils

DOCTYPE = "VN Leave Calendar Cache"

#: Leave Application statuses allowed in the ``statuses`` read-time filter.
CALENDAR_STATUSES = ("Open", "Approved", "Rejected")


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
    statuses: str | None = None,
    employee: str | None = None,
    leave_type: str | None = None,
    force_refresh: bool = False,
) -> dict:
    """Return ``{cache_key, data, generated_at, from_cache}``.

    Serves the cached row when fresh; otherwise builds (and stores) a new one.
    ``statuses`` (csv subset of Open/Approved/Rejected), ``employee``,
    ``leave_type`` and the scope ``branch`` / ``department`` are applied as
    read-time filters — the cache itself stays unfiltered per scope.
    """
    # D2 (plan leave-calendar-desk-free): gate the read too — a cached row
    # used to be readable by any logged-in user, leaking company-wide leave.
    _require_hr()
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
        return {"cache_key": "", "data": {"leaves": [], "holidays": []}, "generated_at": "", "from_cache": False}
    if not force_refresh and _table_ready():
        cached = _fetch_cached(key)
        if cached is not None:
            cached["from_cache"] = True
            return _apply_read_filters(
                cached, statuses=statuses, employee=employee, leave_type=leave_type,
                branch=branch, department=department,
            )
    # No fresh cache → build live.
    payload = build_leave_calendar(company=company, year=year, month=month, branch=branch, department=department)
    return _apply_read_filters(
        payload, statuses=statuses, employee=employee, leave_type=leave_type,
        branch=branch, department=department,
    )


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


def _apply_read_filters(
    payload: dict,
    *,
    statuses=None,
    employee: str | None = None,
    leave_type: str | None = None,
    branch: str | None = None,
    department: str | None = None,
) -> dict:
    """Filter ``payload['data']['leaves']`` in place (read-time — the cache row
    itself stays unfiltered). Tolerates legacy flat-array payloads."""
    data = payload.get("data")
    if isinstance(data, list):
        # Legacy v1 shape (flat Approved-only array) — filter as leaves.
        payload["data"] = {"leaves": _filter_leaves(data, statuses, employee, leave_type, branch, department), "holidays": []}
        return payload
    if not isinstance(data, dict):
        payload["data"] = {"leaves": [], "holidays": []}
        return payload
    data["leaves"] = _filter_leaves(
        data.get("leaves") or [], statuses, employee, leave_type, branch, department
    )
    return payload


def _filter_leaves(
    rows: list[dict],
    statuses,
    employee: str | None,
    leave_type: str | None,
    branch: str | None,
    department: str | None,
) -> list[dict]:
    wanted = _parse_statuses(statuses)
    out = []
    for row in rows or []:
        if wanted and (row.get("status") or "") not in wanted:
            continue
        if employee and (row.get("employee") or "") != employee:
            continue
        if leave_type and (row.get("leave_type") or "") != leave_type:
            continue
        if branch and (row.get("branch") or "") != branch:
            continue
        if department and (row.get("department") or "") != department:
            continue
        out.append(row)
    return out


def _parse_statuses(statuses) -> set[str] | None:
    """Validate + split the csv ``statuses`` read-time filter (None = no filter)."""
    if statuses in (None, ""):
        return None
    if isinstance(statuses, (list, tuple)):
        tokens = [str(s).strip() for s in statuses]
    else:
        tokens = [t.strip() for t in str(statuses).split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    bad = [t for t in tokens if t not in CALENDAR_STATUSES]
    if bad:
        frappe.throw(
            _("Trạng thái không hợp lệ: {0}.").format(", ".join(bad)),
            frappe.ValidationError,
        )
    return set(tokens)


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
    data = {
        "leaves": _load_leave_data(company, from_date, to_date, branch, department),
        "holidays": _load_holidays(company, from_date, to_date),
    }
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


#: Standard columns that always exist on every DocType (not listed in meta.fields).
_STANDARD_COLUMNS = {"name", "owner", "modified", "created_by", "creation", "docstatus", "parent"}


def _calendar_leave_fields() -> list[str]:
    """List fields for one calendar leave row.

    Reuses leave.py's migrate-safe :func:`_application_fields` (base columns +
    guarded ``vn_`` blackout custom fields), adding the display columns the
    desk-free calendar chips / drawer need. EVERY field is meta-guarded:
    ``frappe.get_all`` validates the field list and a missing column (e.g.
    ``branch`` is NOT a stock HRMS Leave Application field) raises DataError,
    which the loader's except would swallow into an EMPTY cache row.
    """
    wanted = list(_application_fields())
    for extra in ("employee_name", "branch", "owner"):
        if extra not in wanted:
            wanted.append(extra)
    try:
        meta = frappe.get_meta("Leave Application")
        if meta:
            table = {df.fieldname for df in meta.fields}
            wanted = [f for f in wanted if f in table or f in _STANDARD_COLUMNS]
    except Exception:
        # Meta unavailable (bench-free harness) → drop the site-optional extras.
        wanted = [f for f in wanted if f not in ("branch", "department")]
    return wanted


def _load_leave_data(
    company: str, from_date: str, to_date: str, branch: str | None, department: str | None
) -> list[dict]:
    """Open/Approved/Rejected leave applications overlapping the month window.

    Cancelled (``docstatus 2``) rows are excluded. ``branch`` / ``department``
    are NOT applied here — the cache stays scope-unfiltered and the api read
    filters rows after serving (see ``_apply_read_filters``).
    """
    if not _leave_table_ready():
        return []
    filters = {
        "docstatus": ("<", 2),
        "status": ("in", list(CALENDAR_STATUSES)),
        "from_date": ("<=", to_date),
        "to_date": (">=", from_date),
    }
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters=filters,
            fields=_calendar_leave_fields(),
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


def _load_holidays(company: str, from_date: str, to_date: str) -> list[dict]:
    """Holidays of the company's default Holiday List within the window.

    Desk-free D4: one overlay source for the month grid (parity with HRMS's
    Holiday event source). Falls back to the single Holiday List when the
    company has no default; empty overlay otherwise (never throws).
    """
    if not _holiday_table_ready():
        return []
    try:
        holiday_list = frappe.db.get_value("Company", company, "default_holiday_list")
        if not holiday_list:
            names = frappe.get_all("Holiday List", limit=2, pluck="name")
            holiday_list = names[0] if len(names) == 1 else None
        if not holiday_list:
            return []
        rows = frappe.get_all(
            "Holiday",
            filters={
                "parent": holiday_list,
                "holiday_date": ("between", [from_date, to_date]),
            },
            fields=["holiday_date", "description", "weekly_off"],
            order_by="holiday_date",
        )
        return [
            {
                "holiday_date": str(r.get("holiday_date")),
                "description": r.get("description") or "",
                "weekly_off": 1 if r.get("weekly_off") else 0,
                "holiday_list": holiday_list,
            }
            for r in rows or []
        ]
    except Exception:
        frappe.log_error(title="calendar._load_holidays failed")
        return []


def _holiday_table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists("Holiday"))
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
    return {"cache_key": key, "invalidated": _drop_cache_row(key), "message": _("Đã làm mới cache.")}


def _drop_cache_row(cache_key: str) -> bool:
    """Delete one cache row by key (best-effort). Returns whether it existed."""
    try:
        existing = frappe.db.get_value(DOCTYPE, cache_key, "name")
    except Exception:
        existing = None
    if not existing:
        return False
    try:
        # Cache maintenance — drop the row directly (see _upsert note above).
        frappe.delete_doc(DOCTYPE, existing, ignore_permissions=True, ignore_missing=True)
    except Exception:
        frappe.log_error(title="calendar.invalidate failed")
        return False
    return True


# --------------------------------------------------------------------------- #
# Decision-side invalidation + realtime (plan leave-calendar-desk-free C3)
# --------------------------------------------------------------------------- #
def touch_leave_calendar(doc) -> None:
    """Invalidate every calendar cache row a Leave Application decision affects
    + broadcast ``leave_calendar_updated``.

    Scope keys (4 combinations): ``{company}-{ALL|branch}-{ALL|dept}-{y}-{m}-v2``
    for every month the leave window touches. Called (best-effort) from
    ``api/leave.py`` — apply / approve / reject / cancellation decisions — and
    the ``on_submit`` / ``on_cancel`` doc_events hooks. Never raises.
    """
    try:
        employee = getattr(doc, "employee", None)
        emp = (
            frappe.db.get_value("Employee", employee, ["company", "branch", "department"], as_dict=True)
            if employee
            else None
        )
        company = (emp and emp.get("company")) or getattr(doc, "company", None) or ""
        if not company:
            return
        branch = (emp and emp.get("branch")) or None
        department = (emp and emp.get("department")) or None
        months = calendar_utils.months_between(
            getattr(doc, "from_date", None), getattr(doc, "to_date", None)
        )
        if not months:
            return
        scopes = {
            (None, None),
            (branch, None),
            (None, department),
            (branch, department),
        }
        if not _table_ready():
            return
        for (y, m) in months:
            for b, d in scopes:
                key = calendar_utils.build_cache_key(company=company, branch=b, department=d, year=y, month=m)
                if key:
                    _drop_cache_row(key)
        frappe.publish_realtime(
            "leave_calendar_updated",
            {
                "employee": employee,
                "from_date": str(getattr(doc, "from_date", "") or ""),
                "to_date": str(getattr(doc, "to_date", "") or ""),
            },
        )
    except Exception:
        frappe.log_error(title="calendar.touch_leave_calendar failed")


# --------------------------------------------------------------------------- #
# Employee picker feed (desk-free D8 — create-for-employee SearchableSelect)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def calendar_employee_options(search: str | None = None, limit=50) -> list[dict]:
    """Active employees for the calendar create-modal picker (HR-only).

    ``{value, label, description}`` rows ordered by name, ``search`` matches
    ``employee_name`` (LIKE), ``limit`` clamped to 200.
    """
    _require_hr()
    try:
        n = int(limit or 50)
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 200))
    filters = {"status": "Active"}
    text = (search or "").strip()
    if text:
        filters["employee_name"] = ("like", f"%{text}%")
    try:
        rows = frappe.get_all(
            "Employee",
            filters=filters,
            fields=["name", "employee_name", "department", "branch"],
            order_by="employee_name",
            limit_page_length=n,
        )
    except Exception:
        frappe.log_error(title="calendar.calendar_employee_options failed")
        return []
    out = []
    for r in rows or []:
        desc = " · ".join(p for p in (r.get("department"), r.get("branch")) if p)
        out.append(
            {
                "value": r.get("name"),
                "label": r.get("employee_name") or r.get("name"),
                "description": desc,
            }
        )
    return out
