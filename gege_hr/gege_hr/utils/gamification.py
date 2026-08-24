"""
Gamification engine — XP, streaks, badges (attendance-gamification-design §P3).

XP is finalised *once per work-day*, at the moment the employee checks out (or
when the day is auto-closed with a missing checkout). The function
``apply_session_xp`` is idempotent: it uses ``last_xp_date`` to guarantee a day
is never double-counted, so retries / re-syncs are safe.

All persistent state lives on the ``VN Employee Portal Profile`` doctype
(per-employee, never touches Frappe HR's standard tables).
"""

from __future__ import annotations

import json
from typing import Any

try:  # import-safe outside a bench (tests)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None


# ---------------------------------------------------------------------------
# Level ladder — (level, name, xp threshold, tailwind color key).
# ---------------------------------------------------------------------------
LEVELS = [
    (1, "Tân binh", 0, "slate"),
    (2, "Tích cực", 100, "sky"),
    (3, "Chuyên nghiệp", 250, "indigo"),
    (4, "Hạng A", 500, "violet"),
    (5, "Hạng S", 1000, "amber"),
    (6, "Quán quân", 2000, "rose"),
]


# ---------------------------------------------------------------------------
# Badge registry — code → (name, emoji, predicate). Predicates receive the
# profile dict *after* the session XP has been applied, so counters are current.
# ---------------------------------------------------------------------------
BADGES = [
    {
        "code": "first_checkin",
        "name": "Bước đầu tiên",
        "emoji": "🎫",
        "desc": "Lần chấm công đầu tiên",
        "check": lambda p: (p.get("xp_total") or 0) > 0,
    },
    {
        "code": "early5",
        "name": "Người chim sớm",
        "emoji": "🐦",
        "desc": "Vào sớm 5 lần trong tháng",
        "check": lambda p: (p.get("early_count_month") or 0) >= 5,
    },
    {
        "code": "early10",
        "name": "Kỷ lục gia rạng đông",
        "emoji": "🌅",
        "desc": "Vào sớm 10 lần trong tháng",
        "check": lambda p: (p.get("early_count_month") or 0) >= 10,
    },
    {
        "code": "streak7",
        "name": "Tuần vàng",
        "emoji": "🔥",
        "desc": "Đúng giờ 7 ngày liên tiếp",
        "check": lambda p: (p.get("current_streak") or 0) >= 7,
    },
    {
        "code": "streak30",
        "name": "Tháng sắt",
        "emoji": "💎",
        "desc": "Đúng giờ 30 ngày liên tiếp",
        "check": lambda p: (p.get("current_streak") or 0) >= 30,
    },
    {
        "code": "perfect_day",
        "name": "Ngày hoàn hảo",
        "emoji": "✨",
        "desc": "Sớm + đủ ca + check-out đúng",
        "check": lambda p: (p.get("perfect_day_count") or 0) >= 1,
    },
    {
        "code": "level_a",
        "name": "Hạng A",
        "emoji": "🅰️",
        "desc": "Đạt level 4",
        "check": lambda p: (p.get("level") or 1) >= 4,
    },
    {
        "code": "level_s",
        "name": "Hạng S",
        "emoji": "🅢",
        "desc": "Đạt level 5",
        "check": lambda p: (p.get("level") or 1) >= 5,
    },
]


def _profile_doc(employee: str):
    """Return the VN Employee Portal Profile document for ``employee``.

    Creates it lazily if missing (autoname = field:employee) so the first
    check-in ever is enough to bootstrap the gamification record.
    """
    name = frappe.db.exists("VN Employee Portal Profile", {"employee": employee})
    if name:
        return frappe.get_doc("VN Employee Portal Profile", name)
    doc = frappe.get_doc({"doctype": "VN Employee Portal Profile", "employee": employee})
    doc.insert(ignore_permissions=True)
    return doc


def _parse_badges(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _level_for(xp: int) -> tuple[int, str, str]:
    """Return (level, level_name, color) for an XP total."""
    level, name, _, color = LEVELS[0]
    for lv, nm, threshold, clr in LEVELS:
        if xp >= threshold:
            level, name, color = lv, nm, clr
        else:
            break
    return level, name, color


def _xp_to_next(xp: int) -> int:
    """XP remaining to reach the *next* level (0 at max level)."""
    next_threshold = None
    for _lv, _nm, threshold, _clr in LEVELS:
        if threshold > xp:
            next_threshold = threshold
            break
    if next_threshold is None:
        return 0
    return next_threshold - xp


def _is_early(status_key: str) -> bool:
    return status_key == "early"


def _is_on_time(status_key: str) -> bool:
    return status_key in ("early", "on_time")


def _is_late(status_key: str) -> bool:
    return status_key in ("late", "very_late")


def apply_session_xp(
    employee: str,
    *,
    status_key: str,
    work_date: str,
    checkout_deviation_minutes: int | None = None,
    planned_duration_minutes: int | None = None,
    elapsed_minutes: int | None = None,
    is_overnight: bool = False,
) -> dict:
    """Finalise XP/streak/badges for a single work-day session (idempotent).

    Called once the employee checks out. Returns a summary dict describing what
    changed (xp gained, new badges, streak) so the caller can surface a toast.

    Idempotency: ``last_xp_date`` is compared against ``work_date``; a repeat
    call for the same day is a no-op.
    """
    doc = _profile_doc(employee)
    if not (doc.gamification_enabled or 0):
        return {"skipped": True}

    if doc.last_xp_date == work_date:
        # Already credited for this day — do not double count.
        return {"skipped": True, "already": True}

    gained = 0

    # Punctuality XP.
    if _is_early(status_key):
        gained += 15
    elif status_key == "on_time":
        gained += 10
    elif _is_late(status_key):
        gained += 0  # late earns nothing but doesn't deduct

    # Full-shift XP: worked roughly the planned duration.
    planned = planned_duration_minutes or 0
    elapsed = elapsed_minutes or 0
    if planned and elapsed and elapsed >= planned * 0.95:
        gained += 10

    # Night shift bonus.
    if is_overnight:
        gained += 20

    # Did not forget to check out.
    if checkout_deviation_minutes is not None:
        gained += 5

    # ----- mutate profile -----
    new_xp = (doc.xp_total or 0) + gained
    level, _name, _color = _level_for(new_xp)

    # Streak: on-time extends, late/absent resets.
    if _is_on_time(status_key):
        streak = (doc.current_streak or 0) + 1
    else:
        streak = 0
    best = max(doc.best_streak or 0, streak)

    # Monthly counters (early / on-time). Note: month-boundary reset is a
    # separate concern; here we only increment.
    early_m = doc.early_count_month or 0
    ontime_m = doc.ontime_count_month or 0
    if status_key == "early":
        early_m += 1
    elif status_key == "on_time":
        ontime_m += 1

    perfect = doc.perfect_day_count or 0
    is_perfect = (
        status_key == "early"
        and checkout_deviation_minutes is not None
        and checkout_deviation_minutes >= 0
        and planned
        and elapsed
        and elapsed >= planned * 0.95
    )
    if is_perfect:
        perfect += 1

    doc.update(
        {
            "xp_total": new_xp,
            "level": level,
            "current_streak": streak,
            "best_streak": best,
            "early_count_month": early_m,
            "ontime_count_month": ontime_m,
            "perfect_day_count": perfect,
            "last_xp_date": work_date,
        }
    )

    # ----- badges -----
    profile_view = {
        "xp_total": new_xp,
        "level": level,
        "current_streak": streak,
        "early_count_month": early_m,
        "ontime_count_month": ontime_m,
        "perfect_day_count": perfect,
    }
    earned = set(_parse_badges(doc.badges_earned))
    new_badges = []
    for b in BADGES:
        if b["code"] not in earned and b["check"](profile_view):
            earned.add(b["code"])
            new_badges.append(b)
    doc.badges_earned = json.dumps(sorted(earned))

    doc.save(ignore_permissions=True)

    return {
        "xp_gained": gained,
        "xp_total": new_xp,
        "level": level,
        "streak": streak,
        "new_badges": new_badges,
    }


def get_snapshot(employee: str) -> dict | None:
    """Return the ``gamification`` block for ``today_status`` (read-only)."""
    name = frappe.db.exists("VN Employee Portal Profile", {"employee": employee})
    if not name:
        # No profile yet → a minimal default snapshot so the UI degrades gracefully.
        return _shape(0, 1, "Tân binh", "slate", 0, 0, [], True, False)

    doc = frappe.get_doc("VN Employee Portal Profile", name)
    if not (doc.gamification_enabled or 0):
        return None

    xp = doc.xp_total or 0
    level, level_name, color = _level_for(xp)
    badges = _parse_badges(doc.badges_earned)
    return _shape(
        xp,
        level,
        level_name,
        color,
        doc.current_streak or 0,
        doc.best_streak or 0,
        badges,
        bool(doc.gamification_enabled or 0),
        bool(doc.leaderboard_opt_in or 0),
        xp_to_next=_xp_to_next(xp),
    )


def _shape(xp, level, level_name, color, streak, best, badges, enabled, opt_in, xp_to_next=0):
    return {
        "xp_total": xp,
        "level": level,
        "level_name": level_name,
        "level_color": color,
        "xp_to_next": xp_to_next,
        "current_streak": streak,
        "best_streak": best,
        "badges_earned": badges,
        "gamification_enabled": enabled,
        "leaderboard_opt_in": opt_in,
        "all_badges": BADGES,
    }


def badge_catalog() -> list[dict[str, Any]]:
    """Static badge catalog (code/name/emoji/desc) for the badge shelf UI."""
    return [{"code": b["code"], "name": b["name"], "emoji": b["emoji"], "desc": b["desc"]} for b in BADGES]
