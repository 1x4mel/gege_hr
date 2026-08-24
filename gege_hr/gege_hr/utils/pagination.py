"""Server-side pagination + total helpers — DNA §6.6 A.

Frappe's ``frappe.db.count`` does **not** accept ``or_filters`` (the mechanism
we use for broad free-text search, DNA §6.6 D), so it cannot be used to count a
searched list. The correct total is counted via
``frappe.db.get_all(..., fields=["name"], limit_page_length=0)`` and ``len()``
— the same pattern the platform-wallet / suppliers endpoints already use.

Every HR list endpoint that opts into pagination returns the same envelope::

    {"data": [...], "total": int, "summary": {...}}

Pagination is **opt-in**: when the caller passes a positive ``page_size`` the
endpoint returns the envelope above; otherwise it keeps its legacy bare-list
return so internal callers (e.g. ``evaluate_leave_blackout`` reuses
``blackout_periods``) and existing bench tests are untouched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

# Hard cap for any client-supplied page/limit (DNA §6.6 A). Without it a single
# ``page_size=1000000`` dumps an entire table (audit logs, work sessions...)
# through one HTTP response.
MAX_PAGE_SIZE = 200


def escape_like(value: Any) -> str:
    """Escape LIKE wildcards in a client search string.

    A raw ``%``/``_`` in ``search`` matched everything, defeating the filter's
    intent and surfacing extra rows the user is allowed to see. Use for every
    ``f"%{q}%"`` LIKE build."""
    s = str(value or "")
    s = s.replace("\\", "\\\\")
    s = s.replace("%", "\\%")
    s = s.replace("_", "\\_")
    return s.strip()


def as_int(value: Any, default: int) -> int:
    """Coerce a whitelist arg (Frappe passes strings) to int with a fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_limit(value: Any, default: int = 100, maximum: int = MAX_PAGE_SIZE) -> int:
    """Coerce a client ``limit``/``page_size`` and clamp it into [1, maximum].

    Used by every list endpoint that reads a raw ``limit`` kwarg (legacy
    non-enveloped paths) so a malicious/hungry client cannot bypass pagination.
    """
    return max(1, min(as_int(value, default), maximum))


def count_all(
    doctype: str,
    *,
    filters: Any = None,
    or_filters: Any = None,
) -> int:
    """Row count honouring ``or_filters`` (DNA §6.6 A — get_all().len, not db.count)."""
    import frappe

    try:
        names = frappe.db.get_all(
            doctype,
            filters=filters,
            or_filters=or_filters,
            fields=["name"],
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(title=f"pagination.count_all({doctype}) failed")
        return 0
    return len(names or [])


def all_rows(
    doctype: str,
    *,
    fields: Sequence[str],
    filters: Any = None,
    or_filters: Any = None,
    order_by: str | None = None,
) -> list[dict]:
    """Every matching row restricted to ``fields`` (``limit_page_length=0``).

    Used to compute a server-side ``summary`` aggregate (per-bucket counts /
    distinct counts) over the *full* filtered set — not just the current page —
    so the SPA summary tiles stay correct under pagination (DNA §6.6 A).
    """
    import frappe

    try:
        return (
            frappe.db.get_all(
                doctype,
                filters=filters,
                or_filters=or_filters,
                fields=list(fields),
                order_by=order_by,
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"pagination.all_rows({doctype}) failed")
        return []


def page_slice(
    doctype: str,
    *,
    fields: Sequence[str],
    filters: Any = None,
    or_filters: Any = None,
    order_by: str | None = None,
    page: Any = 1,
    page_size: Any = 20,
    transform: Callable[[dict], dict] | None = None,
) -> dict:
    """Return ``{"data": [...], "total": int}`` for one page.

    ``total`` reflects the full filtered set (counted via :func:`count_all`),
    not just the page. ``transform`` (optional) maps each raw DB row before it
    lands in ``data`` (e.g. a per-doctype ``*_row`` normaliser).
    """
    import frappe

    page = max(1, as_int(page, 1))
    page_size = clamp_limit(page_size, default=20)
    total = count_all(doctype, filters=filters, or_filters=or_filters)
    start = (page - 1) * page_size
    try:
        rows = (
            frappe.db.get_all(
                doctype,
                filters=filters,
                or_filters=or_filters,
                fields=list(fields),
                order_by=order_by,
                limit_start=start,
                limit_page_length=page_size,
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"pagination.page_slice({doctype}) failed")
        rows = []
        total = 0
    if transform:
        rows = [transform(r) for r in rows]
    return {"data": rows, "total": total}


def paginate_filtered(
    rows: list[dict],
    *,
    page: Any = 1,
    page_size: Any = 0,
    summary: dict | None = None,
) -> list[dict] | dict:
    """Paginate an already-filtered in-memory list (DNA §6.6 A).

    For endpoints whose filtering is **post-query** — the searchable columns
    differ per DocType, so an ``or_filters`` SQL push-down would risk a "column
    does not exist" error (e.g. the ``my_*`` request lists, ``my_payslips``).
    The endpoint loads every matching row, applies its free-text filter in
    Python, then hands the filtered list here.

    Returns the bare ``rows`` when ``page_size`` is omitted (legacy callers +
    bench tests keep their bare-list contract); otherwise returns
    ``{"data": page_slice, "total": len(rows), "summary": summary}`` where
    ``total`` reflects the *full* filtered set (DNA §6.6 A — total counted on
    the filtered list, not just the page).
    """
    if not page_size:
        return rows
    page = max(1, as_int(page, 1))
    page_size = clamp_limit(page_size, default=20)
    start = (page - 1) * page_size
    return {"data": rows[start : start + page_size], "total": len(rows), "summary": summary}


def bucket_counts(rows: Iterable[dict], field: str) -> dict:
    """Map each distinct ``field`` value → row count (server-side summary helper)."""
    counts: dict = {}
    for r in rows or []:
        key = r.get(field)
        counts[key] = counts.get(key, 0) + 1
    return counts


def distinct_count(rows: Iterable[dict], field: str) -> int:
    """Number of distinct non-empty ``field`` values (server-side summary helper)."""
    seen = set()
    for r in rows or []:
        val = r.get(field)
        if val is None or val == "":
            continue
        seen.add(val)
    return len(seen)
