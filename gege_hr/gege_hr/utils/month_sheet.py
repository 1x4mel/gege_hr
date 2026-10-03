"""Pure helpers for the per-employee monthly attendance sheet
(plans/plan-employee-month-sheet.md).

No ``frappe`` import — the API layer loads rows and hands plain dicts in, so
every rule here is unit-testable without a bench.

Business rules (owner, 2026-10-03):

* 24/7 business: EVERY calendar day is a working day — Sundays, holidays and
  days without a shift included. Only an **approved** leave makes a day off.
* A past day with no punch and no approved leave is **absent without leave**.
  The Work Session ``absent`` flag is NOT used (the engine resets it to 0).
* Days are counted per DAY (not per session); minutes / hours are summed.
* "Quên chấm ra" = a VN Checkout Miss ticket for the day OR a session closed by
  the auto-checkout job (``vn_auto_checkout``) — never ``missing_checkout``,
  which is also set mid-shift and on absent days.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable

# Day statuses (primary — one per day).
BEFORE_JOIN = "before_join"
AFTER_RELIEVE = "after_relieve"
FUTURE = "future"
LEAVE_PAID = "leave_paid"
LEAVE_UNPAID = "leave_unpaid"
WORKED = "worked"
ABSENT = "absent"

# Statuses that are outside the employment window / not yet happened → not counted.
NOT_COUNTED = {BEFORE_JOIN, AFTER_RELIEVE, FUTURE}

# Late / early buckets (owner decision D3): ≤15 / 16–30 / >30 minutes.
BUCKETS = ("le15", "16_30", "gt30")


def minute_bucket(minutes: float) -> str | None:
    """Bucket key for a day's late/early minutes (``None`` when 0)."""
    m = _num(minutes)
    if m <= 0:
        return None
    if m <= 15:
        return "le15"
    if m <= 30:
        return "16_30"
    return "gt30"


def iter_days(start: _dt.date, end: _dt.date) -> Iterable[_dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += _dt.timedelta(days=1)


def leave_fraction(leave: dict, day: _dt.date) -> float:
    """0.5 when the approved leave is a half day on ``day``, else 1.0."""
    if not _truthy(leave.get("half_day")):
        return 1.0
    hdd = as_date(leave.get("half_day_date"))
    if hdd is None:
        # Single-day half-day leave carries no half_day_date in older rows.
        return 0.5 if as_date(leave.get("from_date")) == as_date(leave.get("to_date")) else 1.0
    return 0.5 if hdd == day else 1.0


def leaves_by_date(leaves: Iterable[dict], lwp_types: set[str], start, end) -> dict[str, dict]:
    """Expand approved leave rows to ``{iso: {name, leave_type, paid, fraction}}``
    clipped to ``[start, end]``. When two leaves overlap a day the larger
    fraction wins (a data error, but never double-count the day)."""
    out: dict[str, dict] = {}
    for la in leaves or []:
        f, t = as_date(la.get("from_date")), as_date(la.get("to_date"))
        if f is None or t is None:
            continue
        for d in iter_days(max(f, start), min(t, end)):
            frac = leave_fraction(la, d)
            iso = d.isoformat()
            if iso in out and out[iso]["fraction"] >= frac:
                continue
            out[iso] = {
                "name": la.get("name"),
                "leave_type": la.get("leave_type") or "",
                "paid": (la.get("leave_type") or "") not in lwp_types,
                "fraction": frac,
            }
    return out


def _session_view(ws: dict) -> dict:
    checkin = ws.get("actual_checkin")
    checkout = ws.get("actual_checkout")
    raw_ot = _num(ws.get("raw_overtime_hours"))
    approved_ot = _num(ws.get("approved_overtime_hours"))
    return {
        "name": ws.get("name"),
        "shift_type": ws.get("shift_type") or "",
        "planned_start": _s(ws.get("planned_start")),
        "planned_end": _s(ws.get("planned_end")),
        "checkin": _s(checkin),
        "checkout": _s(checkout),
        "late_minutes": _num(ws.get("late_minutes")) if checkin else 0.0,
        "early_minutes": _num(ws.get("early_leave_minutes")) if checkout else 0.0,
        "regular_hours": _num(ws.get("regular_hours")),
        "actual_hours": _num(ws.get("total_actual_hours")),
        "ot_raw_hours": raw_ot,
        "ot_approved_hours": approved_ot,
        "ot_night_hours": _num(ws.get("overtime_night_hours")),
        "auto_checkout": _truthy(ws.get("vn_auto_checkout")),
        "checkin_miss": bool(checkout) and not checkin,
        "need_review": _truthy(ws.get("need_review")),
        "calc_error": (ws.get("calculation_status") or "") == "Error",
        "has_punch": bool(checkin or checkout),
    }


def build_day(
    day: _dt.date,
    *,
    today: _dt.date,
    date_of_joining: _dt.date | None = None,
    relieving_date: _dt.date | None = None,
    sessions: list[dict] | None = None,
    leave: dict | None = None,
    checkout_miss: dict | None = None,
    pending: list[dict] | None = None,
    explanations: dict | None = None,
    edited: int = 0,
    locked: bool = False,
) -> dict:
    """Classify one calendar day + its flags (plan §2).

    ``sessions`` are raw Work Session dicts of that day; ``leave`` the expanded
    approved-leave entry (see :func:`leaves_by_date`); ``explanations`` maps
    ``"late"`` / ``"early"`` → explanation status for the day.
    """
    sv = [_session_view(ws) for ws in (sessions or [])]
    punched = [s for s in sv if s["has_punch"]]
    has_punch = bool(punched)

    if date_of_joining and day < date_of_joining:
        status = BEFORE_JOIN
    elif relieving_date and day > relieving_date:
        status = AFTER_RELIEVE
    elif leave:
        status = LEAVE_PAID if leave.get("paid") else LEAVE_UNPAID
    elif has_punch:
        status = WORKED
    elif day >= today:
        # Today without a punch yet is still open — not absent.
        status = FUTURE
    else:
        status = ABSENT

    leave_frac = float(leave["fraction"]) if leave and status in (LEAVE_PAID, LEAVE_UNPAID) else 0.0
    if status == WORKED:
        worked = 1.0
    elif status in (LEAVE_PAID, LEAVE_UNPAID) and leave_frac < 1 and has_punch:
        worked = 1.0 - leave_frac
    else:
        worked = 0.0
    # A half-day leave with no punch on a past day: the other half is absent.
    absent = 0.0
    if status == ABSENT:
        absent = 1.0
    elif status in (LEAVE_PAID, LEAVE_UNPAID) and leave_frac < 1 and not has_punch and day < today:
        absent = 1.0 - leave_frac

    counted = status not in NOT_COUNTED
    late = sum(s["late_minutes"] for s in punched) if counted else 0.0
    early = sum(s["early_minutes"] for s in punched) if counted else 0.0
    ot_approved = sum(s["ot_approved_hours"] for s in punched) if counted else 0.0
    ot_pending = (
        sum(max(0.0, s["ot_raw_hours"] - s["ot_approved_hours"]) for s in punched) if counted else 0.0
    )
    expl = explanations or {}
    return {
        "date": day.isoformat(),
        "weekday": day.weekday(),  # 0 = Monday
        "status": status,
        "counted": counted,
        "elapsed": counted and day <= today,
        "leave": (
            {
                "name": leave.get("name"),
                "leave_type": leave.get("leave_type"),
                "paid": bool(leave.get("paid")),
                "fraction": leave_frac,
            }
            if leave_frac
            else None
        ),
        "worked": worked,
        "absent": absent,
        "sessions": sv,
        "checkin": punched[0]["checkin"] if punched else None,
        "checkout": punched[-1]["checkout"] if punched else None,
        "late_minutes": round(late),
        "early_minutes": round(early),
        "late_bucket": minute_bucket(late),
        "early_bucket": minute_bucket(early),
        "late_explained": bool(late) and expl.get("late") == "Approved",
        "early_explained": bool(early) and expl.get("early") == "Approved",
        "ot_approved_hours": round(ot_approved, 2),
        "ot_pending_hours": round(ot_pending, 2),
        "ot_night_hours": round(sum(s["ot_night_hours"] for s in punched), 2) if counted else 0.0,
        "regular_hours": round(sum(s["regular_hours"] for s in punched), 2),
        "actual_hours": round(sum(s["actual_hours"] for s in punched), 2),
        "checkout_miss": counted and (bool(checkout_miss) or any(s["auto_checkout"] for s in punched)),
        "checkout_miss_status": (checkout_miss or {}).get("status"),
        "checkin_miss": counted and any(s["checkin_miss"] for s in punched),
        "pending": list(pending or []),
        "edited": int(edited or 0),
        "locked": bool(locked),
        "multi_session": len(punched) > 1,
        "need_review": any(s["need_review"] or s["calc_error"] for s in punched),
        "worked_on_leave": status in (LEAVE_PAID, LEAVE_UNPAID) and leave_frac >= 1 and has_punch,
    }


def aggregate_month(days: list[dict]) -> dict:
    """Monthly totals from :func:`build_day` rows (plan §3).

    ``standard_days`` = every day of the employment window in the range (24/7);
    ``elapsed_days`` = those up to today. Approved leave in the future is still
    counted in the leave totals (it is a decided fact, not a guess).
    """
    t = {
        "standard_days": 0,
        "elapsed_days": 0,
        "worked_days": 0.0,
        "leave_paid_days": 0.0,
        "leave_unpaid_days": 0.0,
        "absent_days": 0.0,
        "leave_by_type": {},
        "late": {"days": 0, "minutes": 0, "explained_days": 0, "buckets": dict.fromkeys(BUCKETS, 0)},
        "early": {"days": 0, "minutes": 0, "explained_days": 0, "buckets": dict.fromkeys(BUCKETS, 0)},
        "ot": {"days": 0, "approved_hours": 0.0, "pending_hours": 0.0, "night_hours": 0.0},
        "checkout_miss_count": 0,
        "checkin_miss_count": 0,
        "regular_hours": 0.0,
        "actual_hours": 0.0,
        "pending_count": 0,
        "edited_days": 0,
        "anomalies": {"multi_session": 0, "need_review": 0, "worked_on_leave": 0},
    }
    for d in days or []:
        if d["status"] not in (BEFORE_JOIN, AFTER_RELIEVE):
            t["standard_days"] += 1
        if not d["counted"]:
            continue
        t["elapsed_days"] += 1 if d["elapsed"] else 0
        t["worked_days"] += d["worked"]
        t["absent_days"] += d["absent"]
        lv = d.get("leave")
        if lv:
            key = "leave_paid_days" if lv["paid"] else "leave_unpaid_days"
            t[key] += lv["fraction"]
            bt = t["leave_by_type"].setdefault(lv["leave_type"], {"days": 0.0, "paid": lv["paid"]})
            bt["days"] += lv["fraction"]
        for kind in ("late", "early"):
            mins = d[f"{kind}_minutes"]
            if mins > 0:
                t[kind]["days"] += 1
                t[kind]["minutes"] += mins
                t[kind]["buckets"][d[f"{kind}_bucket"]] += 1
                if d[f"{kind}_explained"]:
                    t[kind]["explained_days"] += 1
        if d["ot_approved_hours"] > 0:
            t["ot"]["days"] += 1
        t["ot"]["approved_hours"] += d["ot_approved_hours"]
        t["ot"]["pending_hours"] += d["ot_pending_hours"]
        t["ot"]["night_hours"] += d["ot_night_hours"]
        t["checkout_miss_count"] += 1 if d["checkout_miss"] else 0
        t["checkin_miss_count"] += 1 if d["checkin_miss"] else 0
        t["regular_hours"] += d["regular_hours"]
        t["actual_hours"] += d["actual_hours"]
        t["edited_days"] += 1 if d["edited"] else 0
        for k in ("multi_session", "need_review", "worked_on_leave"):
            t["anomalies"][k] += 1 if d[k] else 0
    t["pending_count"] = sum(len(d["pending"]) for d in days or [])
    for k in (
        "worked_days",
        "leave_paid_days",
        "leave_unpaid_days",
        "absent_days",
        "regular_hours",
        "actual_hours",
    ):
        t[k] = round(t[k], 2)
    for k in ("approved_hours", "pending_hours", "night_hours"):
        t["ot"][k] = round(t["ot"][k], 2)
    return t


# ---- small private helpers ------------------------------------------------- #
def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


def _s(v) -> str | None:
    return str(v) if v not in (None, "") else None


def as_date(v) -> _dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None
