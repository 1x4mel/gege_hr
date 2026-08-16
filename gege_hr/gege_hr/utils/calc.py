"""
Calculation engine — plan v5 §9 (pure functions, TZ-aware).

Three core pure functions drive attendance computation:

* ``calculate_work_session(...)``   → §9.1  hours/late/early/OT/compensate
* ``generate_segments(...)``         → §9.2  Regular / Night / OT / Late / Early segments
* ``calculate_payable_day(...)``     → §9.3  payable-day rounding (0 / 0.5 / 1.0)

They operate on **plain dicts / datetimes** so they can be unit-tested without
a bench (see the 33 test cases A/B/C/D/E in plan §9.6). The persistence layer
(``persist_work_session``) lives at the bottom for the API hook to call.

Time-zone rule (plan §2.7): every datetime that reaches these functions is a
portal-local *aware* datetime. Frappe stores UTC; ``utils/tz.to_portal`` does
the conversion at the boundary. ``split_by_night`` reuses ``utils/tz``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any

from gege_hr.gege_hr.utils import tz as tz_utils

# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def hours_between(start: datetime | None, end: datetime | None) -> float:
    """Hours between two aware datetimes, clamped >= 0. None → 0."""
    if not start or not end:
        return 0.0
    delta = (end - start).total_seconds() / 3600.0
    return max(0.0, delta)


def _first_log_of_type(logs: Iterable[dict], log_type: str) -> datetime | None:
    """Earliest check-in log matching ``log_type`` ('IN'/'OUT'). Returns portal dt."""
    lt = log_type.upper()
    for lg in sorted(logs, key=lambda x: _as_dt(x.get("time"))):
        if (lg.get("log_type") or "").upper() in (lt, f"CLOCK {lt}"):
            return _as_dt(lg.get("time"))
    return None


def _last_log_of_type(logs: Iterable[dict], log_type: str) -> datetime | None:
    """Latest check-in log matching ``log_type`` ('IN'/'OUT'). Returns portal dt."""
    lt = log_type.upper()
    matched = [lg for lg in logs if (lg.get("log_type") or "").upper() in (lt, f"CLOCK {lt}")]
    if not matched:
        return None
    return _as_dt(max(matched, key=lambda x: _as_dt(x.get("time"))).get("time"))


def _as_dt(value: Any) -> datetime:
    """Coerce a Frappe datetime string / datetime into a portal-aware datetime."""
    if isinstance(value, datetime):
        return tz_utils.to_portal(value)
    if not value:
        return None
    # Frappe stores "YYYY-MM-DD HH:MM:SS" (UTC, naive). Normalize then convert.
    text = str(value).replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    return tz_utils.to_portal(naive)


def _db_dt(value) -> str | None:
    """Convert an ISO-8601/ISO-Z/datetime value into a Frappe Datetime DB string.

    ``calculate_work_session`` emits ``planned_start``/``actual_checkin`` etc. as
    ``tz_utils.utc_iso(...)`` (e.g. ``2026-06-24T01:00:00Z``). MariaDB rejects the
    ``T…Z`` form for ``Datetime`` columns ("Incorrect datetime value"), which
    silently broke every ``VN Attendance Work Session`` insert. This normalises
    the value to ``YYYY-MM-DD HH:MM:SS`` in UTC for safe persistence.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz_utils.ZoneInfo("UTC")).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    raw = str(value).strip()
    if not raw:
        return None
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw.replace("T", " ")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return raw  # let the caller decide; better than dropping the value
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz_utils.ZoneInfo("UTC")).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _night_band(policy: dict) -> tuple[time, time]:
    ns = policy.get("night_start_time") or "22:00:00"
    ne = policy.get("night_end_time") or "06:00:00"

    def _parse(s):
        if hasattr(s, "strftime"):
            return s  # already a time
        parts = str(s).split(":")
        if len(parts) < 2:
            return time(0, 0)
        # Frappe/pymysql can hand back Time values with fractional seconds
        # ("22:00:04.271516") or as timedelta strings — int() on "04.271516"
        # raised ValueError and aborted the whole Work-Session calculation.
        # Parse via float and clamp to a valid time so night-band detection
        # degrades gracefully instead of crashing.
        def _i(x):
            try:
                return int(float(x))
            except (TypeError, ValueError):
                return 0

        h = min(23, max(0, _i(parts[0])))
        m = min(59, max(0, _i(parts[1])))
        sec = min(59, max(0, _i(parts[2]))) if len(parts) > 2 else 0
        return time(h, m, sec)

    return _parse(ns), _parse(ne)


def _num(value, default=0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# §9.2.2  split_by_holiday (pure) + night/holiday combined splitter
# ---------------------------------------------------------------------------


def _split_by_midnight(start: datetime, end: datetime) -> list[tuple[datetime, datetime, date]]:
    """Yield ``(chunk_start, chunk_end, calendar_date)`` per portal calendar day.

    Used so a span crossing midnight can be classified against per-date holiday
    lists. Returns ``[]`` for empty/inverted ranges.
    """
    if not start or not end or end <= start:
        return []
    chunks: list[tuple[datetime, datetime, date]] = []
    cur = start
    tz = getattr(cur, "tzinfo", None)
    while cur < end:
        nm = datetime.combine(cur.date() + timedelta(days=1), time(0, 0), tzinfo=tz)
        chunk_end = min(nm, end)
        chunks.append((cur, chunk_end, cur.date()))
        cur = chunk_end
    return chunks


def split_by_holiday(start: datetime, end: datetime, holiday_dates: set[date] | None) -> list[dict]:
    """Split a time range into holiday / non-holiday chunks (plan §9.2.2, pure).

    Args:
        start, end: portal-aware datetimes.
        holiday_dates: set of ``date`` objects that are holidays.

    Returns:
        list of ``{"start", "end", "is_holiday"}`` dicts (portal-aware).
    """
    hset = holiday_dates or set()
    out: list[dict] = []
    for cs, ce, d in _split_by_midnight(start, end):
        out.append({"start": cs, "end": ce, "is_holiday": d in hset})
    return out


def _split_night_holiday(
    start: datetime,
    end: datetime,
    night: tuple[time, time],
    holiday_dates: set[date] | None,
) -> list[dict]:
    """Night-split first, then holiday-split each night chunk.

    Returns list of ``{"start", "end", "is_night", "is_holiday"}``.
    """
    out: list[dict] = []
    for n in tz_utils.split_by_night(start, end, night):
        for h in split_by_holiday(n["start"], n["end"], holiday_dates):
            out.append(
                {
                    "start": h["start"],
                    "end": h["end"],
                    "is_night": n["is_night"],
                    "is_holiday": h["is_holiday"],
                }
            )
    return out


# OT segment-type lookup: (is_night, is_holiday) -> segment_type
_OT_SEGMENT_TYPES = {
    (False, False): "OT",
    (True, False): "OT Night",
    (False, True): "OT Holiday",
    (True, True): "OT Holiday Night",
}


# ---------------------------------------------------------------------------
# §9.4  match_overtime_request (pure) + OT rounding (policy)
# ---------------------------------------------------------------------------


def calculate_overlap(
    a_start: datetime | None,
    a_end: datetime | None,
    b_start: datetime | None,
    b_end: datetime | None,
) -> float:
    """Hours of overlap between two datetime windows (clamped >= 0). Pure."""
    a_start = _as_dt(a_start)
    a_end = _as_dt(a_end)
    b_start = _as_dt(b_start)
    b_end = _as_dt(b_end)
    if not (a_start and a_end and b_start and b_end):
        return 0.0
    return hours_between(max(a_start, b_start), min(a_end, b_end))


def match_overtime_request(actual_ot_windows: list[dict] | None, ot_requests: list[dict] | None) -> float:
    """Sum the overlap (hours) of actual OT windows with approved OT requests.

    Pure (plan §9.4). Both inputs are lists of plain dicts with
    ``{"start"/"from_datetime", "end"/"to_datetime"}``. Only approved hours are
    matched upstream — pass already-approved requests here.
    """
    approved = 0.0
    for win in actual_ot_windows or []:
        ws = _as_dt(win.get("start"))
        we = _as_dt(win.get("end"))
        if not ws or not we:
            continue
        for req in ot_requests or []:
            rs = _as_dt(req.get("from_datetime") or req.get("start"))
            re_ = _as_dt(req.get("to_datetime") or req.get("end"))
            approved += calculate_overlap(ws, we, rs, re_)
    return round(approved, 4)


def match_overtime_request_detailed(
    actual_ot_windows: list[dict] | None, ot_requests: list[dict] | None
) -> dict:
    """Per-request overlap (hours) of actual OT windows with approved OT requests.

    Sibling of :func:`match_overtime_request` — same overlap math, but the result
    is broken down per request ``name`` so the persistence layer can stamp
    ``actual_hours`` / ``approved_hours`` back onto each ``VN Overtime Request``
    (plan T3 / BUG-2). Requests without a ``name`` are aggregated under ``""``.

    Contract: the summed values always equal :func:`match_overtime_request` over
    the same inputs (TC-U-07) — this keeps the pure function testable in isolation.
    """
    per_name: dict[str, float] = {}
    for win in actual_ot_windows or []:
        ws = _as_dt(win.get("start"))
        we = _as_dt(win.get("end"))
        if not ws or not we:
            continue
        for req in ot_requests or []:
            rs = _as_dt(req.get("from_datetime") or req.get("start"))
            re_ = _as_dt(req.get("to_datetime") or req.get("end"))
            overlap = calculate_overlap(ws, we, rs, re_)
            if overlap:
                key = req.get("name") or ""
                per_name[key] = per_name.get(key, 0.0) + overlap
    return {k: round(v, 4) for k, v in per_name.items()}


def round_overtime(hours: float, policy: dict) -> float:
    """Apply the Policy ``overtime_rounding_method`` (plan §9.4.1). Pure.

    Methods (see VN Attendance Policy): No Rounding / Nearest Nmin / Up to Nmin,
    where N comes from ``overtime_rounding_minutes`` (15/30).
    """
    method = (policy.get("overtime_rounding_method") or "No Rounding").strip()
    minutes = int(_num(policy.get("overtime_rounding_minutes"), 15)) or 15
    if method == "No Rounding" or minutes <= 0:
        return round(_num(hours), 4)
    total_min = _num(hours) * 60.0
    if method.lower().startswith("nearest"):
        total_min = round(total_min / minutes) * minutes
    elif method.lower().startswith("up to"):
        total_min = math.ceil(total_min / minutes) * minutes
    return round(total_min / 60.0, 4)


def get_holiday_multiplier(
    segment_type: str, is_holiday: int | bool, is_night: int | bool, policy: dict
) -> float:
    """Wage multiplier for a segment (plan §9.5).

    Resolution: explicit override in ``policy["holiday_multipliers"][type]`` →
    else sensible defaults (Regular=1.0, Regular on holiday=2.0, OT=1.5,
    OT Holiday=3.0). Night premium is intentionally *not* doubled here; night
    hours are tracked separately and the payroll component mapping handles the
    night rate.
    """
    multipliers = policy.get("holiday_multipliers") or {}
    override = multipliers.get(segment_type)
    if override is not None:
        return _num(override, 1.0)
    st = (segment_type or "").strip()
    if st.startswith("OT Holiday"):
        return 3.0
    if st.startswith("OT"):
        return 1.5
    if is_holiday and st.startswith("Regular"):
        return 2.0
    return 1.0


# ---------------------------------------------------------------------------
# §9.1  calculate_work_session
# ---------------------------------------------------------------------------


def calculate_work_session(
    shift_instance: dict,
    checkin_logs: list[dict],
    policy: dict,
    calculate_mode: str = "realtime",
    leave_info: dict | None = None,
    ot_requests: list[dict] | None = None,
) -> dict:
    """Compute every Work-Session numeric field for one shift + its logs.

    Args:
        shift_instance: dict with ``planned_start``, ``planned_end`` (UTC strings
            or datetimes) plus ``shift_type`` VN custom-field settings
            (``vn_allow_overtime_before_shift``, ``vn_allow_overtime_after_shift``,
            ``vn_max_overtime_hours``, ``vn_max_total_work_hours``).
        checkin_logs: list of ``{"time", "log_type"}`` rows (Employee Checkin).
        policy: dict snapshot of VN Attendance Policy (grace/min-hours/compensate flags).
        calculate_mode: 'realtime' | 'batch' | 'recalculate' (informational).
        leave_info: optional ``{"has_leave", "salary_impact_type", "leave_days_equivalent"}``.

    Returns:
        dict mirroring VN Attendance Work Session fields. Segments are NOT
        generated here — call ``generate_segments(result)`` afterwards.
    """
    planned_start = _as_dt(shift_instance.get("planned_start"))
    planned_end = _as_dt(shift_instance.get("planned_end"))

    # --- 1. First IN / Last OUT (MVP strategy) -----------------------------
    actual_checkin = _first_log_of_type(checkin_logs, "IN")
    actual_checkout = _last_log_of_type(checkin_logs, "OUT")

    missing_checkin = actual_checkin is None
    missing_checkout = actual_checkout is None
    need_review = missing_checkin or missing_checkout

    # If either end is missing, fall back to a zero-length window so downstream
    # math stays numerically safe (paid 0, no OT, no negative hours).
    safe_in = actual_checkin or planned_start
    safe_out = actual_checkout or planned_start

    # --- 3. late_minutes (only when the employee checked in) ----------------
    grace_late = int(_num(policy.get("grace_late_minutes"), 5))
    late_minutes = (
        max(0.0, tz_utils.minutes_between(planned_start, safe_in) - grace_late)
        if actual_checkin
        else 0.0
    )

    # --- 4. early_leave_minutes (only when the employee checked out) --------
    # When there's no checkout, early_leave MUST be 0 — otherwise safe_out
    # falls back to planned_start, giving minutes_between(planned_start,
    # planned_end) = full shift = 720' for a 12h shift → wrongly "Về sớm".
    grace_early = int(_num(policy.get("grace_early_leave_minutes"), 0))
    early_leave_minutes = (
        max(0.0, tz_utils.minutes_between(safe_out, planned_end) - grace_early)
        if actual_checkout
        else 0.0
    )

    # --- 5. Total actual hours ---------------------------------------------
    total_actual_hours = hours_between(safe_in, safe_out)

    # --- 6. Scheduled regular hours ----------------------------------------
    scheduled_regular_hours = hours_between(planned_start, planned_end)

    # --- 7. Actual within shift hours (clamp >= 0; test S8) ----------------
    overlap_start = max(safe_in, planned_start)
    overlap_end = min(safe_out, planned_end)
    actual_within_shift_hours = max(
        0.0,
        min(scheduled_regular_hours, hours_between(overlap_start, overlap_end)),
    )

    # --- 8. Regular hours --------------------------------------------------
    regular_hours = actual_within_shift_hours

    # --- 9. Raw overtime (PRE + POST) --------------------------------------
    raw_pre_overtime_hours = 0.0
    raw_post_overtime_hours = 0.0

    allow_pre = bool(shift_instance.get("vn_allow_overtime_before_shift"))
    allow_post = bool(shift_instance.get("vn_allow_overtime_after_shift"))

    if allow_pre and actual_checkin and actual_checkin < planned_start:
        raw_pre_overtime_hours = hours_between(actual_checkin, planned_start)
    if allow_post and actual_checkout and actual_checkout > planned_end:
        raw_post_overtime_hours = hours_between(planned_end, actual_checkout)

    raw_overtime_hours = raw_pre_overtime_hours + raw_post_overtime_hours

    # --- 10. OT compensate (late first, then early — plan §5.5) ------------
    ot_compensated_late_minutes = 0.0
    ot_compensated_early_minutes = 0.0

    if policy.get("allow_ot_compensate_late") and late_minutes > 0 and raw_overtime_hours > 0:
        compensated = min(late_minutes, raw_overtime_hours * 60.0)
        late_minutes -= compensated
        raw_overtime_hours -= compensated / 60.0
        ot_compensated_late_minutes = compensated

    if policy.get("allow_ot_compensate_early_leave") and early_leave_minutes > 0 and raw_overtime_hours > 0:
        compensated = min(early_leave_minutes, raw_overtime_hours * 60.0)
        early_leave_minutes -= compensated
        raw_overtime_hours -= compensated / 60.0
        ot_compensated_early_minutes = compensated

    # --- 10b. Approved OT (plan §9.4) ---------------------------------------
    # Min-OT filter (policy): OT below min_overtime_minutes is not payable.
    min_ot_min = _num(policy.get("min_overtime_minutes"), 0)
    payable_raw_ot = 0.0 if (raw_overtime_hours * 60.0 < min_ot_min > 0) else raw_overtime_hours

    actual_ot_windows: list[dict] = []
    if raw_pre_overtime_hours > 0 and actual_checkin and planned_start:
        actual_ot_windows.append({"start": actual_checkin, "end": planned_start})
    if raw_post_overtime_hours > 0 and planned_end and actual_checkout:
        actual_ot_windows.append({"start": planned_end, "end": actual_checkout})

    require_approval = bool(policy.get("require_overtime_approval"))
    if require_approval:
        approved_hours = match_overtime_request(actual_ot_windows, ot_requests)
        approved_hours = min(approved_hours, payable_raw_ot)
    else:
        # No approval required → all payable raw OT auto-approves.
        approved_hours = payable_raw_ot
    approved_overtime_hours = round_overtime(approved_hours, policy)

    # Per-request OT breakdown for write-back to the VN Overtime Request rows
    # (actual_hours / approved_hours — plan T3 / BUG-2). Uncapped overlap per
    # request; the aggregate cap/rounding above still governs the WS total.
    ot_request_breakdown = match_overtime_request_detailed(actual_ot_windows, ot_requests)

    # --- 11. Threshold checks → need_review --------------------------------
    max_total = _num(shift_instance.get("vn_max_total_work_hours"), 20.0)
    max_ot = _num(shift_instance.get("vn_max_overtime_hours"), 4.0)
    if total_actual_hours > max_total > 0:
        need_review = True
    if raw_overtime_hours > max_ot > 0:
        need_review = True

    regular_shortage_minutes = max(
        0.0,
        (scheduled_regular_hours - actual_within_shift_hours) * 60.0 - late_minutes - early_leave_minutes,
    )

    # --- Build the Work-Session dict (pre-segments; pre-payable-day) -------
    result = {
        # Times (UTC ISO for storage; portal-aware used inside)
        "planned_start": tz_utils.utc_iso(planned_start) if planned_start else None,
        "planned_end": tz_utils.utc_iso(planned_end) if planned_end else None,
        "actual_checkin": tz_utils.utc_iso(actual_checkin) if actual_checkin else None,
        "actual_checkout": tz_utils.utc_iso(actual_checkout) if actual_checkout else None,
        # Hours
        "total_actual_hours": round(total_actual_hours, 4),
        "scheduled_regular_hours": round(scheduled_regular_hours, 4),
        "actual_within_shift_hours": round(actual_within_shift_hours, 4),
        "regular_hours": round(regular_hours, 4),
        "raw_pre_overtime_hours": round(raw_pre_overtime_hours, 4),
        "raw_post_overtime_hours": round(raw_post_overtime_hours, 4),
        "raw_overtime_hours": round(raw_overtime_hours, 4),
        "approved_overtime_hours": approved_overtime_hours,
        # Late / early
        "late_minutes": int(round(late_minutes)),
        "early_leave_minutes": int(round(early_leave_minutes)),
        "regular_shortage_minutes": int(round(regular_shortage_minutes)),
        "ot_compensated_late_minutes": int(round(ot_compensated_late_minutes)),
        "ot_compensated_early_minutes": int(round(ot_compensated_early_minutes)),
        # Flags
        "missing_checkin": int(missing_checkin),
        "missing_checkout": int(missing_checkout),
        "absent": 0,
        "need_review": int(bool(need_review)),
        # Audit
        "calculate_mode": calculate_mode,
        "policy_version": int(_num(policy.get("version"), 1)),
    }
    # keep portal-aware datetimes handy for segment generation
    result["_planned_start"] = planned_start
    result["_planned_end"] = planned_end
    result["_actual_checkin"] = actual_checkin
    result["_actual_checkout"] = actual_checkout
    # Per-request OT breakdown consumed by persist_work_session write-back (T3).
    result["_ot_request_breakdown"] = ot_request_breakdown
    result["_policy"] = policy
    result["_shift_instance"] = shift_instance
    result["_actual_ot_windows"] = actual_ot_windows
    result["_require_overtime_approval"] = require_approval

    # --- 12. Payable day ---------------------------------------------------
    result["payable_regular_hours"] = round(regular_hours, 4)
    result["payable_day"] = calculate_payable_day(result, policy, leave_info)

    return result


# ---------------------------------------------------------------------------
# §9.2  generate_segments
# ---------------------------------------------------------------------------


def generate_segments(work_session: dict, holiday_dates: set[date] | None = None) -> list[dict]:
    """Produce detailed VN Attendance Segment rows from a calc result.

    Segment types follow the DocType enum: Regular / Regular Night / OT /
    OT Night / OT Holiday / OT Holiday Night / Late / Early Leave / Break /
    Unpaid / Leave. Night split uses ``utils/tz.split_by_night`` (plan §9.2.1);
    holiday split uses :func:`split_by_holiday` (plan §9.2.2) and the wage
    multiplier is filled via :func:`get_holiday_multiplier` (plan §9.5).

    Args:
        work_session: result dict from :func:`calculate_work_session`.
        holiday_dates: set of portal ``date`` objects that are holidays; used to
            split OT spans into ``OT Holiday``/``OT Holiday Night`` and to flag
            regular spans. Pass ``None``/empty to skip holiday handling.
    """
    policy = work_session.get("_policy") or {}
    night = _night_band(policy)
    hset = holiday_dates or set()
    planned_start = work_session.get("_planned_start")
    planned_end = work_session.get("_planned_end")
    actual_checkin = work_session.get("_actual_checkin")
    actual_checkout = work_session.get("_actual_checkout")

    segments: list[dict] = []

    def _emit_ot(start: datetime, end: datetime):
        """4-way split: night × holiday → OT / OT Night / OT Holiday / OT Holiday Night."""
        for span in _split_night_holiday(start, end, night, hset):
            seg_type = _OT_SEGMENT_TYPES[(bool(span["is_night"]), bool(span["is_holiday"]))]
            segments.append(
                _segment(
                    span["start"],
                    span["end"],
                    seg_type,
                    is_night=int(span["is_night"]),
                    is_holiday=int(span["is_holiday"]),
                )
            )

    def _emit_regular(start: datetime, end: datetime):
        """Night-split regular spans; holiday flagged (type stays Regular / Regular Night)."""
        for span in _split_night_holiday(start, end, night, hset):
            seg_type = "Regular Night" if span["is_night"] else "Regular"
            segments.append(
                _segment(
                    span["start"],
                    span["end"],
                    seg_type,
                    is_night=int(span["is_night"]),
                    is_holiday=int(span["is_holiday"]),
                )
            )

    # 1. Pre-shift OT
    if work_session.get("raw_pre_overtime_hours", 0) > 0 and actual_checkin and planned_start:
        _emit_ot(actual_checkin, planned_start)

    # 2. Regular: overlap of [actual] with [planned]
    reg_start = max(filter(None, [actual_checkin, planned_start]))
    reg_end = min(filter(None, [actual_checkout, planned_end]))
    if reg_end and reg_start and reg_end > reg_start:
        _emit_regular(reg_start, reg_end)

    # 3. Post-shift OT
    if work_session.get("raw_post_overtime_hours", 0) > 0 and planned_end and actual_checkout:
        _emit_ot(planned_end, actual_checkout)

    # 4. Late segment
    if work_session.get("late_minutes", 0) > 0 and planned_start:
        end = planned_start + timedelta(minutes=work_session["late_minutes"])
        segments.append(_segment(planned_start, end, "Late"))

    # 5. Early leave segment
    if work_session.get("early_leave_minutes", 0) > 0 and planned_end and actual_checkout:
        segments.append(_segment(actual_checkout, planned_end, "Early Leave"))

    # Fill hours / calendar_date / multiplier for every row.
    for seg in segments:
        seg["hours"] = round(hours_between(_as_dt(seg["from_datetime"]), _as_dt(seg["to_datetime"])), 4)
        cd = _as_dt(seg["from_datetime"])
        seg["calendar_date"] = cd.date().isoformat() if cd else None
        seg["multiplier"] = get_holiday_multiplier(
            seg["segment_type"], seg["is_holiday"], seg["is_night"], policy
        )
    return segments


def _segment(
    start: datetime,
    end: datetime,
    segment_type: str,
    is_night: int = 0,
    is_holiday: int = 0,
) -> dict:
    return {
        "segment_type": segment_type,
        "from_datetime": tz_utils.utc_iso(start),
        "to_datetime": tz_utils.utc_iso(end),
        "hours": 0.0,
        "is_night": is_night,
        "is_holiday": is_holiday,
        "is_weekend": 0,
        "multiplier": 1.0,
    }


# ---------------------------------------------------------------------------
# §9.3  calculate_payable_day
# ---------------------------------------------------------------------------


def calculate_payable_day(work_session: dict, policy: dict, leave_info: dict | None = None) -> float:
    """Return the payable day value (0 / 0.5 / 1.0) per plan §9.3."""
    if work_session.get("absent"):
        return 0.0

    if leave_info and leave_info.get("has_leave"):
        equiv = _num(leave_info.get("leave_days_equivalent"), 0.0)
        impact = (leave_info.get("salary_impact_type") or "Paid").lower()
        if impact == "paid":
            return round(equiv, 2)
        if impact == "half paid":
            return round(equiv * 0.5, 2)
        return 0.0  # Unpaid

    actual_hours = _num(work_session.get("actual_within_shift_hours"))
    min_full = _num(policy.get("min_working_hours_full_day"), 4.0)
    min_half = _num(policy.get("min_working_hours_half_day"), 2.0)
    if actual_hours >= min_full > 0:
        return 1.0
    if actual_hours >= min_half > 0:
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# Policy / shift loading helpers (bench-required; import-safe outside bench)
# ---------------------------------------------------------------------------


def load_policy(policy_name: str | None, employee: str | None = None) -> dict:
    """Fetch a VN Attendance Policy snapshot as a plain dict.

    Resolution order: explicit policy_name → employee's default_attendance_policy
    → company default → first active policy. Falls back to safe defaults when
    none exists, so the engine always returns a usable snapshot.
    """
    if policy_name:
        doc = frappe_get("VN Attendance Policy", policy_name)
    else:
        name = None
        if employee:
            name = frappe_db_get_value("Employee", employee, "default_attendance_policy")
        if not name:
            name = frappe_db_get_value("VN Attendance Policy", {"is_active": 1}, "name")
        doc = frappe_get("VN Attendance Policy", name) if name else None

    if not doc:
        return _default_policy()

    return {
        "name": doc.name,
        "version": int(doc.version or 1),
        "grace_late_minutes": doc.grace_late_minutes,
        "grace_early_leave_minutes": doc.grace_early_leave_minutes,
        "min_working_hours_full_day": doc.min_working_hours_full_day,
        "min_working_hours_half_day": doc.min_working_hours_half_day,
        "allow_ot_compensate_late": bool(doc.allow_ot_compensate_late),
        "allow_ot_compensate_early_leave": bool(doc.allow_ot_compensate_early_leave),
        "night_start_time": str(doc.night_start_time or "22:00:00"),
        "night_end_time": str(doc.night_end_time or "06:00:00"),
        "missing_checkin_action": doc.missing_checkin_action,
        "missing_checkout_action": doc.missing_checkout_action,
        # Overtime settings (plan §9.4)
        "require_overtime_approval": bool(doc.require_overtime_approval),
        "min_overtime_minutes": int(doc.min_overtime_minutes or 0),
        "overtime_rounding_method": doc.overtime_rounding_method or "No Rounding",
        "overtime_rounding_minutes": int(doc.overtime_rounding_minutes or 15),
        "holiday_multipliers": _load_holiday_multipliers(doc),
    }


def _load_holiday_multipliers(policy_doc) -> dict:
    """Resolve explicit ``{segment_type: multiplier}`` overrides (plan §9.5).

    Resolution order:
      1. An explicit child table on the policy (``holiday_multipliers`` attr),
         if a future policy shape adds one — kept for forward-compat.
      2. Active ``VN Payroll Component Mapping`` rules for the policy's company
         (plan §19). Each ``day_type='All'`` rule contributes its segment_type →
         multiplier; specific day types are ignored here because the engine's
         multiplier lookup is segment-type keyed (day-type granularity is applied
         downstream by the payroll component mapping consumer).
      3. Fall back to the sensible defaults baked into
         :func:`get_holiday_multiplier` (returns ``{}``).

    Bench-free safe: returns ``{}`` when frappe/doctype/table is unavailable.
    """
    explicit = getattr(policy_doc, "holiday_multipliers", None)
    if explicit:
        try:
            return {m.segment_type: float(m.multiplier) for m in explicit}
        except Exception:
            pass

    company = getattr(policy_doc, "company", None)
    if not company:
        return {}
    try:
        import frappe
    except Exception:
        return {}
    try:
        if not frappe.db.table_exists("tabVN Payroll Component Mapping"):  # type: ignore[attr-defined]
            return {}
        rows = (
            frappe.db.get_all(
                "VN Payroll Component Mapping",
                filters={"company": company, "is_active": 1, "day_type": "All"},
                fields=["segment_type", "multiplier"],
            )
            or []
        )
        return {r["segment_type"]: float(r["multiplier"] or 1.0) for r in rows}
    except Exception:
        return {}


def _default_policy() -> dict:
    return {
        "name": None,
        "version": 0,
        "grace_late_minutes": 5,
        "grace_early_leave_minutes": 0,
        "min_working_hours_full_day": 4.0,
        "min_working_hours_half_day": 2.0,
        "allow_ot_compensate_late": False,
        "allow_ot_compensate_early_leave": False,
        "night_start_time": "22:00:00",
        "night_end_time": "06:00:00",
        "missing_checkin_action": "Need Review",
        "missing_checkout_action": "Need Review",
        "require_overtime_approval": False,
        "min_overtime_minutes": 0,
        "overtime_rounding_method": "No Rounding",
        "overtime_rounding_minutes": 15,
        "holiday_multipliers": {},
    }


def load_shift_instance(shift_instance_name: str) -> dict:
    """Flatten a VN Employee Shift Instance + its Shift Type VN custom-fields."""
    si = frappe_get("VN Employee Shift Instance", shift_instance_name)
    if not si:
        return {}
    st_name = si.shift_type
    shift_type = frappe_get("Shift Type", st_name) if st_name else None

    def st_attr(field, default=None):
        return getattr(shift_type, field, default) if shift_type else default

    return {
        "name": si.name,
        "employee": si.employee,
        "work_date": str(si.work_date) if si.work_date else None,
        "shift_type": st_name,
        "company": si.company,
        "attendance_policy": si.attendance_policy,
        "planned_start": si.planned_start,
        "planned_end": si.planned_end,
        # Shift Type VN custom fields (custom_fields hook ships these)
        "vn_allow_overtime_before_shift": bool(st_attr("vn_allow_overtime_before_shift")),
        "vn_allow_overtime_after_shift": bool(st_attr("vn_allow_overtime_after_shift")),
        "vn_max_overtime_hours": st_attr("vn_max_overtime_hours", 4.0),
        "vn_max_total_work_hours": st_attr("vn_max_total_work_hours", 20.0),
        "vn_max_checkout_after_end_minutes": st_attr("vn_max_checkout_after_end_minutes", 360),
        # SI check-in/out windows (set by _ensure_shift_instance in shift.py;
        # used by _filter_logs_to_window to avoid pulling adjacent-day checkins).
        "checkin_window_start": getattr(si, "checkin_window_start", None),
        "checkout_window_end": getattr(si, "checkout_window_end", None),
        "max_checkout_time": getattr(si, "max_checkout_time", None),
    }


# ---------------------------------------------------------------------------
# §9.5  Holiday List + OT Request loaders (bench-required; import-safe outside)
# ---------------------------------------------------------------------------


def _resolve_holiday_list(emp) -> str | None:
    """Resolve an employee's Holiday List WITHOUT crashing session calculation.

    Priority: ``Department.holiday_list`` → ``Company.default_holiday_list``.

    Some sites ship a ``tabDepartment`` whose doctype has no ``holiday_list``
    column (custom/minimal Department). Querying it raised
    ``OperationalError(1054, "Unknown column 'holiday_list'")`` which aborted
    the ENTIRE Work-Session calculation — so a brand-new check-in never became a
    Work Session and stayed invisible in the HR admin view. Holiday detection is
    non-essential (a payable-day flag), so each lookup is guarded: a missing
    field/column or any DB error simply yields ``None`` ("no holiday list")
    instead of propagating.
    """
    try:
        import frappe
    except Exception:
        return None
    if not emp:
        return None

    def _safe(doctype, name, field):
        if not name:
            return None
        try:
            # Skip the query entirely when the field isn't part of the doctype
            # meta (covers a custom Department without `holiday_list`).
            meta = frappe.get_meta(doctype)
            if meta and not meta.has_field(field):
                return None
            return frappe.db.get_value(doctype, name, field)
        except Exception:
            return None

    if emp.department:
        hl = _safe("Department", emp.department, "holiday_list")
        if hl:
            return hl
    if emp.company:
        hl = _safe("Company", emp.company, "default_holiday_list")
        if hl:
            return hl
    return None


def is_holiday(day: date, employee: str | None) -> bool:
    """Check Frappe HR Holiday List for a date (plan §9.5).

    Priority: Department Holiday List → Company default_holiday_list.
    """
    try:
        import frappe
    except Exception:
        return False
    if not employee or not day:
        return False
    emp = frappe.db.get_value("Employee", employee, ["department", "company"], as_dict=True)
    if not emp:
        return False
    holiday_list = _resolve_holiday_list(emp)
    if not holiday_list:
        return False
    return bool(frappe.db.exists("Holiday", {"parent": holiday_list, "holiday_date": day}))


def load_holiday_dates(
    start: datetime | date | None, end: datetime | date | None, employee: str | None
) -> set[date]:
    """Return the set of holiday ``date`` objects within ``[start, end]``.

    Pulls at most the dates from the resolved Holiday List that fall inside the
    window (inclusive), so overnight shifts crossing midnight only flag the days
    that are actually holidays.
    """
    try:
        import frappe
    except Exception:
        return set()
    if not employee or not start or not end:
        return set()
    sd = start.date() if isinstance(start, datetime) else start
    ed = end.date() if isinstance(end, datetime) else end
    emp = frappe.db.get_value("Employee", employee, ["department", "company"], as_dict=True)
    if not emp:
        return set()
    holiday_list = _resolve_holiday_list(emp)
    if not holiday_list:
        return set()
    rows = frappe.db.get_all(
        "Holiday",
        filters={"parent": holiday_list, "holiday_date": ["between", [sd, ed]]},
        pluck="holiday_date",
    )
    return {(r.date() if isinstance(r, datetime) else r) for r in rows}


def get_approved_ot_requests(employee: str | None, work_date) -> list[dict]:
    """Load approved VN Overtime Requests for an employee/work_date (plan §9.4).

    Only rows whose workflow state is approved/confirmed are returned. Returns
    ``[]`` when the doctype is not installed or no bench is available.
    """
    try:
        import frappe
    except Exception:
        return []
    if not employee or not work_date:
        return []
    # NOTE: ``db.table_exists`` takes the bare DocType name here — passing the
    # ``tab``-prefixed table name returns False on this Frappe build, which used
    # to short-circuit this function to [] and silently drop EVERY approved OT
    # request (root cause of "approved_overtime_hours always 0"). See E2E probe.
    if not frappe.db.table_exists("VN Overtime Request"):  # type: ignore[attr-defined]
        return []
    rows = (
        frappe.db.get_all(
            "VN Overtime Request",
            filters={
                "employee": employee,
                "work_date": work_date,
                "workflow_state": ["in", ["Approved", "Confirmed"]],
                "docstatus": ["<", 2],
            },
            fields=["name", "from_datetime", "to_datetime"],
        )
        or []
    )
    return rows


# ---------------------------------------------------------------------------
# Persistence — called by api.attendance.on_employee_checkin_create
# ---------------------------------------------------------------------------


def persist_work_session(shift_instance_name: str, calculate_mode: str = "realtime") -> str:
    """Compute + upsert the Work Session for a Shift Instance. Returns WS name."""
    import frappe  # local import: this path only runs inside a bench

    si = load_shift_instance(shift_instance_name)
    if not si:
        return None

    # H2 race guard: checkin hook, OT-approval write-back and manual recalc can
    # all enqueue persist_work_session for the SAME shift instance at once.
    # Both used to read ws=None → both INSERT → duplicate Work Session (double
    # hours in payroll). Serialize on the Shift Instance row itself (FOR UPDATE,
    # held until the request/job commits): the second runner re-reads the WS
    # created by the first and takes the update path instead.
    frappe.db.sql(
        "SELECT name FROM `tabVN Employee Shift Instance` WHERE name = %(name)s FOR UPDATE",
        {"name": shift_instance_name},
    )

    logs = (
        frappe.db.get_all(
            "Employee Checkin",
            filters={"employee": si["employee"], "shift": si.get("shift_type")},
            fields=["name", "time", "log_type"],
            order_by="time asc",
        )
        or []
    )
    # Narrow to logs inside the planned window ± 24h to avoid pulling history.
    logs = _filter_logs_to_window(logs, si["planned_start"], si["planned_end"], si)

    policy = load_policy(si.get("attendance_policy"), si["employee"])

    # Holiday list (plan §9.5) — load once for the whole planned window.
    holiday_dates = load_holiday_dates(_as_dt(si["planned_start"]), _as_dt(si["planned_end"]), si["employee"])
    # Approved OT requests (plan §9.4) — only needed when approval is required.
    ot_requests = []
    if policy.get("require_overtime_approval"):
        ot_requests = get_approved_ot_requests(si["employee"], si.get("work_date"))

    calc = calculate_work_session(si, logs, policy, calculate_mode=calculate_mode, ot_requests=ot_requests)
    calc["segments"] = generate_segments(calc, holiday_dates)

    ws_name = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": shift_instance_name})
    review_reasons = _review_reasons(calc)

    payload = _ws_payload(si, calc, policy)
    payload["review_reason"] = "\n".join(review_reasons) if review_reasons else None

    if ws_name:
        ws = frappe.get_doc("VN Attendance Work Session", ws_name)
        # CAS lock (plan §19.3): refuse to overwrite a Locked / Recalculating
        # session (the comment always said both — the code now matches it).
        if ws.calculation_status in ("Locked", "Recalculating"):
            return ws_name
        for k, v in payload.items():
            if k == "segments":
                continue
            try:
                ws.set(k, v)
            except Exception:
                pass
        ws.set("segments", [])  # clear + re-add
        for seg in payload["segments"]:
            ws.append("segments", seg)
        ws.calculation_status = "Calculated"
        ws.calculated_at = frappe.utils.now()
        ws.save(ignore_permissions=True)
    else:
        payload["doctype"] = "VN Attendance Work Session"
        payload["calculation_status"] = "Calculated"
        payload["calculated_at"] = frappe.utils.now()
        ws = frappe.get_doc(payload)
        ws.insert(ignore_permissions=True)
        ws_name = ws.name

    # Stamp actual/approved hours back onto the day's OT requests so /hr/overtime
    # reflects how much of each request was actually served (BUG-2 / plan T3).
    # Best-effort: a failure here never aborts the Work Session save.
    _writeback_ot_request_hours(si, calc, ot_requests)

    _maybe_raise_exceptions(ws_name, calc, si)
    return ws_name


def _writeback_ot_request_hours(shift_instance, calc_result, ot_requests) -> None:
    """Write ``actual_hours`` / ``approved_hours`` onto the day's OT requests.

    Reads the per-request overlap breakdown stashed by ``calculate_work_session``
    (``_ot_request_breakdown``). Matched requests get the served overlap; any
    approved-but-unserved request for the day is zeroed so the per-request view
    stays honest. No-op outside a bench / when the doctype is absent. Pure
    side-effect: safe to call from any persist path (plan T3 / BUG-2).
    """
    import frappe  # calc.py lazy-imports frappe per-function (no module-level import)

    try:
        breakdown = (calc_result or {}).get("_ot_request_breakdown") or {}
        if not breakdown and not (ot_requests or []):
            return
        if not frappe.db.table_exists("VN Overtime Request"):  # type: ignore[attr-defined]
            return
        matched = set()
        for name, hours in breakdown.items():
            if not name:
                continue
            matched.add(name)
            frappe.db.set_value(
                "VN Overtime Request",
                name,
                {"actual_hours": hours, "approved_hours": hours},
            )
        # Approved requests the employee did NOT serve OT for → 0.
        for req in ot_requests or []:
            name = req.get("name")
            if name and name not in matched:
                frappe.db.set_value(
                    "VN Overtime Request",
                    name,
                    {"actual_hours": 0, "approved_hours": 0},
                )
    except Exception:
        si_name = shift_instance.get("name") if isinstance(shift_instance, dict) else shift_instance
        frappe.log_error(
            title="OT request hours write-back failed",
            message=f"shift_instance={si_name}",
        )


def _filter_logs_to_window(
    logs: list[dict], planned_start, planned_end, si: dict | None = None
) -> list[dict]:
    ps = _as_dt(planned_start)
    pe = _as_dt(planned_end)
    if not ps or not pe:
        return logs
    # Use the Shift Instance's configured check-in/out window (tight — avoids
    # pulling checkins from ADJACENT DAYS which caused cross-day OT/hours bugs
    # with the old ±24h margin). Fall back to ±2h if SI window fields are missing.
    lo = _as_dt(si.get("checkin_window_start")) if si else None
    hi = _as_dt(si.get("max_checkout_time")) if si else None
    # H5: the SI window is the *permitted check-in/out* window, which is
    # NARROWER than the hours the engine must credit — an early pre-OT IN
    # (before checkin_window_start) or a long approved post-shift OT OUT
    # (past max_checkout_time, still under vn_max_total_work_hours) was being
    # dropped from the session entirely (missing_checkin / zero hours).
    # Widen each side to the engine's own work-hour caps: total span may not
    # exceed vn_max_total_work_hours (default 20h) around the planned shift.
    try:
        max_total_h = float(si.get("vn_max_total_work_hours") or 20) if si else 20.0
    except (TypeError, ValueError):
        max_total_h = 20.0
    cap_lo = pe - timedelta(hours=max_total_h)
    cap_hi = ps + timedelta(hours=max_total_h)
    # WIDEN (never narrow): union of the SI window and the engine's cap.
    if lo is None or cap_lo < lo:
        lo = cap_lo
    if hi is None or cap_hi > hi:
        hi = cap_hi
    return [lg for lg in logs if lo <= _as_dt(lg.get("time")) <= hi]


def _review_reasons(calc: dict) -> list[str]:
    reasons = []
    if calc["missing_checkin"]:
        reasons.append("Thiếu check-in (IN).")
    if calc["missing_checkout"]:
        reasons.append("Thiếu check-out (OUT).")
    if calc["total_actual_hours"] > calc.get("vn_max_total_work_hours", 20) and calc.get(
        "vn_max_total_work_hours"
    ):
        pass  # threshold already encoded; message optional
    return reasons


def _ws_payload(si: dict, calc: dict, policy: dict) -> dict:
    segments = [
        {
            "segment_type": s["segment_type"],
            "from_datetime": _db_dt(s["from_datetime"]),
            "to_datetime": _db_dt(s["to_datetime"]),
            "hours": s["hours"],
            "calendar_date": s["calendar_date"],
            "is_night": s["is_night"],
            "is_holiday": s["is_holiday"],
            "is_weekend": s["is_weekend"],
            "multiplier": s["multiplier"],
        }
        for s in calc.get("segments", [])
    ]
    return {
        "employee": si["employee"],
        "work_date": si["work_date"],
        "shift_instance": si["name"],
        "shift_type": si.get("shift_type"),
        "company": si.get("company"),
        "attendance_policy": policy.get("name"),
        "planned_start": _db_dt(calc["planned_start"]),
        "planned_end": _db_dt(calc["planned_end"]),
        "actual_checkin": _db_dt(calc["actual_checkin"]),
        "actual_checkout": _db_dt(calc["actual_checkout"]),
        "total_actual_hours": calc["total_actual_hours"],
        "scheduled_regular_hours": calc["scheduled_regular_hours"],
        "actual_within_shift_hours": calc["actual_within_shift_hours"],
        "regular_hours": calc["regular_hours"],
        "regular_night_hours": _sum_night_hours(segments, ["Regular Night"]),
        "raw_pre_overtime_hours": calc["raw_pre_overtime_hours"],
        "raw_post_overtime_hours": calc["raw_post_overtime_hours"],
        "raw_overtime_hours": calc["raw_overtime_hours"],
        "approved_overtime_hours": calc["approved_overtime_hours"],
        "overtime_normal_hours": _sum_night_hours(segments, ["OT"]),
        "overtime_night_hours": _sum_night_hours(segments, ["OT Night", "OT Holiday Night"]),
        "overtime_holiday_hours": _sum_night_hours(segments, ["OT Holiday", "OT Holiday Night"]),
        "late_minutes": calc["late_minutes"],
        "early_leave_minutes": calc["early_leave_minutes"],
        "regular_shortage_minutes": calc["regular_shortage_minutes"],
        "ot_compensated_late_minutes": calc["ot_compensated_late_minutes"],
        "ot_compensated_early_minutes": calc["ot_compensated_early_minutes"],
        "missing_checkin": calc["missing_checkin"],
        "missing_checkout": calc["missing_checkout"],
        "absent": calc["absent"],
        "need_review": calc["need_review"],
        "payable_regular_hours": calc["payable_regular_hours"],
        "payable_day": calc["payable_day"],
        "policy_version": calc["policy_version"],
        "policy_snapshot": json.dumps(policy, default=str),
        "segments": segments,
    }


def _sum_night_hours(segments: list[dict], types: list[str]) -> float:
    return round(sum(s["hours"] for s in segments if s["segment_type"] in types), 4)


def _maybe_raise_exceptions(ws_name: str, calc: dict, si: dict) -> None:
    """Late-checkout warning (plan §10 note 9): OUT exists but > planned_end+360m."""
    import frappe

    if calc["missing_checkout"] or not calc.get("_actual_checkout"):
        return
    max_after = _num(si.get("vn_max_checkout_after_end_minutes"), 360)
    pe = calc.get("_planned_end")
    out = calc.get("_actual_checkout")
    if pe and out and (out - pe).total_seconds() / 60.0 > max_after:
        existing = frappe.db.exists(
            "VN Attendance Exception",
            {"work_session": ws_name, "exception_type": "Late Checkout"},
        )
        if existing:
            return
        frappe.get_doc(
            {
                "doctype": "VN Attendance Exception",
                "work_session": ws_name,
                "shift_instance": si["name"],
                "employee": si["employee"],
                "work_date": si["work_date"],
                "exception_type": "Late Checkout",
                "severity": "Warning",
                "status": "Open",
                "description": (
                    f"Check-out {(out - pe).total_seconds() / 60:.0f} phút sau kết thúc ca "
                    f"(ngưỡng {max_after} phút)."
                ),
            }
        ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Bench-aware shims (so unit tests can import this module without frappe)
# ---------------------------------------------------------------------------


def frappe_get(doctype, name):
    try:
        import frappe

        return frappe.get_doc(doctype, name) if name else None
    except Exception:
        return None


def frappe_db_get_value(*args, **kwargs):
    try:
        import frappe

        return frappe.db.get_value(*args, **kwargs)
    except Exception:
        return None
