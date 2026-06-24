"""
Time-zone utilities for gege_hr.

Per plan v5 §2.7: Vietnam = UTC+7 (Asia/Ho_Chi_Minh), no DST. Frappe stores
`Datetime` columns as UTC; every business calculation (night split, work_date,
late/early, OT-over-midnight) MUST convert to the portal timezone first.

Use stdlib `zoneinfo` (Python 3.9+) — do NOT hardcode +07:00.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

try:  # Frappe is only available inside a bench; keep this import-safe for tests.
    import frappe  # type: ignore
except Exception:  # pragma: no cover - dev/test context without bench
    frappe = None

DEFAULT_PORTAL_TZ = "Asia/Ho_Chi_Minh"
_NIGHT_DEFAULT = (time(22, 0), time(6, 0))


def as_time(value):
    """Normalise a time-like value to ``datetime.time``.

    MariaDB / pymysql returns ``TIME`` columns as ``datetime.timedelta`` while
    Frappe ``Time`` fields are ``datetime.time``. Accept both (and ISO strings)
    so the shift-window math never sees a timedelta where it expects a time.
    """
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    if isinstance(value, str) and value:
        try:
            return time.fromisoformat(value[:8])
        except ValueError:
            return value
    return value


def get_portal_timezone() -> str:
    """Return the configured portal timezone (VN HR Portal Setting.timezone).

    Falls back to Asia/Ho_Chi_Minh when the setting is unavailable so that
    utility functions remain import-safe outside a bench.
    """
    if frappe is None:
        return DEFAULT_PORTAL_TZ
    try:
        tz = frappe.db.get_single_value("VN HR Portal Setting", "timezone")
    except Exception:
        tz = None
    return tz or DEFAULT_PORTAL_TZ


def get_tzinfo(tz: str | None = None) -> ZoneInfo:
    return ZoneInfo(tz or get_portal_timezone())


def now_in_portal(tz: str | None = None) -> datetime:
    """Current moment expressed in the portal timezone."""
    return datetime.now(get_tzinfo(tz))


def to_portal(dt: datetime, tz: str | None = None) -> datetime:
    """Convert any aware/naive datetime to the portal timezone.

    Naive datetimes are assumed to be UTC (Frappe storage convention).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(get_tzinfo(tz))


def combine_work_date(planned_start_local: datetime) -> date:
    """Work Date = the calendar date (portal TZ) the shift STARTS on.

    A night shift that crosses midnight keeps the start date as work_date.
    """
    return to_portal(planned_start_local).date()


def is_overnight(start_time: time, end_time: time) -> bool:
    """A shift is overnight when end_time <= start_time (crosses midnight)."""
    start_time = as_time(start_time)
    end_time = as_time(end_time)
    return end_time <= start_time


def planned_window(work_date: date, start_time: time, end_time: time) -> tuple[datetime, datetime]:
    """Build (planned_start, planned_end) as portal-local aware datetimes.

    For overnight shifts, planned_end falls on work_date + 1.
    """
    start_time = as_time(start_time)
    end_time = as_time(end_time)
    tz = get_tzinfo()
    planned_start = datetime.combine(work_date, start_time, tzinfo=tz)
    end_day = work_date + timedelta(days=1) if is_overnight(start_time, end_time) else work_date
    planned_end = datetime.combine(end_day, end_time, tzinfo=tz)
    return planned_start, planned_end


def split_by_night(start_local: datetime, end_local: datetime, night: tuple[time, time] | None = None):
    """Split an interval [start, end) (portal-local, aware) into regular/night spans.

    Night band defaults to 22:00→06:00 portal time (from policy if provided).
    Returns a list of dicts: ``{ "start", "end", "is_night": bool }`` ordered
    chronologically. A regular chunk before the night band is always emitted
    first (the previous implementation dropped it — see plan §9.2 test B/C).
    """
    ns, ne = (as_time(x) for x in (night or _NIGHT_DEFAULT))
    spans = []
    cursor = start_local
    safety = 0
    while cursor < end_local and safety < 10:
        safety += 1
        day = cursor.date()
        tz = start_local.tzinfo
        night_start = datetime.combine(day, ns, tzinfo=tz)
        # Night band end: if it crosses midnight (ns >= ne) it ends on day + 1.
        night_end = datetime.combine(day + timedelta(days=1) if ns >= ne else day, ne, tzinfo=tz)

        # 1) Regular chunk before this night band starts.
        if cursor < night_start:
            regular_end = min(end_local, night_start)
            if regular_end > cursor:
                spans.append({"start": cursor, "end": regular_end, "is_night": False})
                cursor = regular_end
                if cursor >= end_local:
                    break
                continue

        # 2) Night chunk.
        if cursor < night_end:
            night_seg_end = min(end_local, night_end)
            if night_seg_end > cursor:
                spans.append({"start": cursor, "end": night_seg_end, "is_night": True})
                cursor = night_seg_end
                if cursor >= end_local:
                    break
                continue

        # 3) cursor >= night_end and still before end_local → next day's band.
        # The loop recomputes night_start/night_end from cursor.date().
    return spans


def minutes_between(start: datetime, end: datetime) -> float:
    """Whole minutes between two datetimes (end - start), clamped >= 0."""
    delta = (end - start).total_seconds() / 60.0
    return max(0.0, delta)


def utc_iso(dt: datetime) -> str:
    """ISO-8601 string in UTC with trailing Z (for the API / frontend)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
