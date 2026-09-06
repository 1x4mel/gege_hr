"""
Leave calendar cache helpers — plan v5 §11.3 / doctype-design §29.

The leave calendar renders a month of leave data for a (company, branch,
department) scope. Computing it on every render is expensive, so the result is
cached as a **VN Leave Calendar Cache** row keyed by
``{company}-{branch|ALL}-{dept|ALL}-{YYYY}-{MM}`` (v2.0). This module holds
the pure key/payload/expiry helpers; ``api/leave_calendar.py`` does the
frappe-aware build/fetch/invalidate.

Design (mirrors ``utils/report.py``): pure, bench-free, no Frappe imports
beyond the optional guard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover - outside bench
    frappe = None


# --------------------------------------------------------------------------- #
# Vocabulary (matches VN Leave Calendar Cache.month options)
# --------------------------------------------------------------------------- #
MONTH_VALUES = ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12")

# Default cache lifetime. The api layer may pass an explicit ``expires_at``.
DEFAULT_TTL_HOURS = 6


def normalize_month(value: Any) -> str | None:
    """Coerce an int/str month into a zero-padded ``"01".."12"`` (or None)."""
    if value in (None, ""):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 12:
        return f"{n:02d}"
    return None


def normalize_year(value: Any) -> int | None:
    """Coerce a year into a positive int (or None)."""
    if value in (None, ""):
        return None
    try:
        y = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return y if y > 0 else None


def _scope_token(value: Any) -> str:
    """Blank scope → ``"ALL"`` sentinel; otherwise the trimmed token."""
    if value in (None, ""):
        return "ALL"
    return str(value).strip()


def build_cache_key(
    *,
    company: str,
    branch: str | None = None,
    department: str | None = None,
    year: Any,
    month: Any,
) -> str | None:
    """Compose ``{company}-{branch|ALL}-{dept|ALL}-{YYYY}-{MM}-v2``.

    Returns ``None`` when company/year/month are missing/invalid — the api
    layer treats that as "cannot cache" and computes live.

    The ``-v2`` suffix marks the desk-free payload shape (``{leaves, holidays}``
    multi-status — plan leave-calendar-desk-free D1): rows written by the old
    flat-array code live under the unsuffixed key, so a deploy/rollback pair
    never reads the wrong shape (old rows also expire within the TTL).
    """
    if not (company or "").strip():
        return None
    y = normalize_year(year)
    m = normalize_month(month)
    if y is None or m is None:
        return None
    return f"{str(company).strip()}-{_scope_token(branch)}-{_scope_token(department)}-{y}-{m}-v2"


def month_window(year: Any, month: Any) -> tuple[str, str] | None:
    """``(from_date, to_date)`` ISO strings for the (year, month) calendar page."""
    import calendar as _cal

    y = normalize_year(year)
    m = normalize_month(month)
    if y is None or m is None:
        return None
    last = _cal.monthrange(y, int(m))[1]
    return f"{y}-{m}-01", f"{y}-{m}-{last:02d}"


def _coerce_date(value: Any):
    """Best-effort ``datetime.date`` from a date/datetime/ISO string (or None)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def months_between(from_date: Any, to_date: Any) -> list[tuple[str, str]]:
    """Every ``(year, month)`` touched by the inclusive window.

    Powers cache invalidation for a leave application spanning multiple
    months (e.g. 15/08 → 05/10 → ``[("2026","08"),("2026","09"),("2026","10")]``).
    Reversed / unparseable windows normalise to ``[]`` / swapped bounds.
    """
    start = _coerce_date(from_date)
    end = _coerce_date(to_date)
    if start is None or end is None:
        return []
    if end < start:
        start, end = end, start
    out: list[tuple[str, str]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((str(y), f"{m:02d}"))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def is_expired(expires_at: Any, now: Any = None) -> bool:
    """True when ``expires_at`` is in the past (or absent → expired)."""
    if expires_at in (None, ""):
        return True
    if isinstance(expires_at, str):
        text = expires_at.strip().replace("T", " ")
        try:
            exp = datetime.fromisoformat(text)
        except ValueError:
            return True
    elif isinstance(expires_at, datetime):
        exp = expires_at
    elif isinstance(expires_at, date):
        exp = datetime.combine(expires_at, datetime.max.time())
    else:
        return True
    now_dt: datetime
    if isinstance(now, datetime):
        now_dt = now
    elif isinstance(now, date):
        now_dt = datetime.combine(now, datetime.max.time())
    else:
        now_dt = datetime.utcnow()
    return exp <= now_dt


def compute_expiry(now: Any = None, ttl_hours: int = DEFAULT_TTL_HOURS) -> str:
    """ISO ``now + ttl_hours`` used to populate ``expires_at``."""
    base = datetime.utcnow() if not isinstance(now, datetime) else now
    try:
        ttl = int(ttl_hours)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_HOURS
    return (base + timedelta(hours=ttl)).isoformat(sep=" ")


# --------------------------------------------------------------------------- #
# Payload / row shapers
# --------------------------------------------------------------------------- #
def calendar_cache_payload(
    *,
    company: str,
    year: Any,
    month: Any,
    data: Any,
    branch: str | None = None,
    department: str | None = None,
    generated_at: Any = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict | None:
    """Assemble the field dict for a VN Leave Calendar Cache upsert.

    ``data`` is JSON-serialised by the caller (Frappe stores it in a Code
    field); here we accept any JSON-able structure and pass it through. Returns
    ``None`` when the cache key cannot be composed (invalid scope/month).
    """
    key = build_cache_key(company=company, branch=branch, department=department, year=year, month=month)
    if key is None:
        return None
    import json

    y = normalize_year(year)
    m = normalize_month(month)
    return {
        "doctype": "VN Leave Calendar Cache",
        "cache_key": key,
        "company": str(company).strip(),
        "branch": (branch or "").strip(),
        "department": (department or "").strip(),
        "month": m,
        "year": y,
        "data_json": json.dumps(data, default=str) if data is not None else "{}",
        "generated_at": generated_at or datetime.utcnow().isoformat(sep=" "),
        "expires_at": compute_expiry(ttl_hours=ttl_hours),
    }


CALENDAR_CACHE_ROW_FIELDS = (
    "name",
    "cache_key",
    "company",
    "branch",
    "department",
    "month",
    "year",
    "data_json",
    "generated_at",
    "expires_at",
    "modified",
)


def calendar_cache_row(row: Any) -> dict:
    """Normalise a DB row / dict into the SPA calendar cache shape."""
    if not isinstance(row, dict):
        return {}
    out: dict[str, Any] = {key: row.get(key) for key in CALENDAR_CACHE_ROW_FIELDS}
    for key in ("generated_at", "expires_at", "modified"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ")
    return out
