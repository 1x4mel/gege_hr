"""Bench-free tests for ``utils/checkin_parity.py`` — the parity + duplicate
guards that protect payroll integrity in ``api/attendance.mobile_checkin``.

Full matrix (21 cases) for the two production risks:

  R1 OVERNIGHT PARITY — the old day-only parity (``"OUT" if has_in_only(today)
     else "IN"``) mis-fired when a session spanned midnight. The fix decides by
     the NEWEST log across YESTERDAY+TODAY: newest IN → OUT (close it), newest
     OUT → IN (new session).

  R2 DUPLICATE INTENT — a re-tap seconds after a persisted-but-unacknowledged
     log used to create OUT-right-after-IN (0-hour day → wrong pay). The guard
     skips intents within the gap window; offline replay (old intent stamps)
     still passes.

Plus the orphan-OUT self-heal detectors (``has_out_only``) powering the
warning mobile_checkin returns with ``need_review``.
"""

from datetime import datetime, timedelta

import pytest

from gege_hr.gege_hr.utils.checkin_parity import (
    decide_log_type,
    has_in_only,
    has_out_only,
    is_duplicate_intent,
    parse_log_dt,
)

Y, T = "2026-08-31", "2026-09-01"  # yesterday / today fixtures


def log(day: str, hhmm: str, lt: str) -> dict:
    """A raw Employee Checkin row as DB-shaped dict (SQL string time)."""
    return {"time": f"{day} {hhmm}:00", "log_type": lt}


# ── parse_log_dt ────────────────────────────────────────────────────────────

def test_parse_naive_sql_string():
    assert parse_log_dt("2026-09-01 00:30:00") == datetime(2026, 9, 1, 0, 30)


def test_parse_aware_iso_converted_to_naive_utc():
    # +05:30 offset → UTC 00:30
    assert parse_log_dt("2026-09-01T06:00:00+05:30") == datetime(2026, 9, 1, 0, 30)


def test_parse_none_and_garbage():
    assert parse_log_dt(None) is None
    assert parse_log_dt("") is None
    assert parse_log_dt("not-a-date") is None
    assert parse_log_dt(42) is None


# ── R1: overnight-aware parity (decide_log_type) ────────────────────────────

def test_p01_empty_today_and_yesterday_in():
    assert decide_log_type([]) == "IN"


def test_p02_today_single_in_tap_becomes_out():
    assert decide_log_type([log(T, "08:00", "IN")]) == "OUT"


def test_p03_today_completed_day_tap_becomes_in():
    logs = [log(T, "08:00", "IN"), log(T, "17:00", "OUT")]
    assert decide_log_type(logs) == "IN"


def test_p04_today_orphan_out_selfheals_as_in():
    assert decide_log_type([log(T, "20:00", "OUT")]) == "IN"


def test_p05_overnight_open_session_tap_becomes_out():
    """R1 chính: IN 23:30 hôm qua chưa có OUT → cú 00:30 hôm nay = OUT."""
    logs = [log(Y, "23:30", "IN")]
    assert decide_log_type(logs) == "OUT"


def test_p06_yesterday_session_fully_closed_tap_becomes_in():
    logs = [log(Y, "23:30", "IN"), log(Y, "23:50", "OUT")]
    assert decide_log_type(logs) == "IN"


def test_p07_yesterday_auto_closed_overnight_tap_becomes_in():
    """OUT giả của auto-close là log mới nhất → phiên mới = IN."""
    logs = [log(Y, "23:30", "IN"), log(T, "06:30", "OUT")]
    assert decide_log_type(logs) == "IN"


def test_p08_yesterday_orphan_out_selfheals_as_in():
    assert decide_log_type([log(Y, "23:00", "OUT")]) == "IN"


def test_p09_yesterday_dayshift_completed_tap_becomes_in():
    logs = [log(Y, "08:00", "IN"), log(Y, "17:00", "OUT")]
    assert decide_log_type(logs) == "IN"


def test_p10_today_second_session_tap_becomes_out():
    logs = [log(T, "08:00", "IN"), log(T, "12:00", "OUT"), log(T, "13:00", "IN")]
    assert decide_log_type(logs) == "OUT"


def test_p11_mixed_string_and_datetime_sources():
    logs = [
        {"time": datetime(2026, 8, 31, 23, 30), "log_type": "IN"},
        log(T, "00:10", "OUT"),
        {"time": "2026-09-01T05:00:00+00:00", "log_type": "IN"},
    ]
    assert decide_log_type(logs) == "OUT"


def test_p12_unparseable_times_fall_back_to_in():
    assert decide_log_type([{"time": "??", "log_type": "IN"}]) == "IN"


def test_p13_localized_clock_in_token():
    assert decide_log_type([{"time": "2026-09-01 08:00:00", "log_type": "Clock In"}]) == "OUT"


# ── R2: duplicate-intent guard (is_duplicate_intent) ─────────────────────────

def test_d01_retab_seconds_after_persisted_log_is_duplicate():
    last = "2026-09-01 08:00:00"
    assert is_duplicate_intent(last, datetime(2026, 9, 1, 8, 0, 10)) is True


def test_d02_tap_ten_minutes_later_is_not_duplicate():
    last = "2026-09-01 08:00:00"
    assert is_duplicate_intent(last, datetime(2026, 9, 1, 8, 10)) is False


def test_d03_offline_replay_old_intent_is_not_duplicate():
    """IN(08:00) + OUT(08:05) replayed to server at 09:00: OUT's intent is
    BEFORE the last persisted log → negative delta → replay passes."""
    last_persisted = datetime(2026, 9, 1, 9, 0)  # IN written at replay time
    replayed_intent = datetime(2026, 9, 1, 8, 5)  # OUT's original stamp
    assert is_duplicate_intent(last_persisted, replayed_intent) is False


def test_d04_aware_intent_normalised_before_compare():
    last = "2026-09-01 08:00:00"
    assert is_duplicate_intent(last, "2026-09-01T08:00:30Z") is True


def test_d05_missing_values_never_duplicate():
    assert is_duplicate_intent(None, datetime(2026, 9, 1, 8, 0)) is False
    assert is_duplicate_intent("2026-09-01 08:00:00", None) is False


# ── orphan-OUT detectors (self-heal warning path) ────────────────────────────

def test_s01_has_out_only_flags_orphan_out_day():
    assert has_out_only([log(T, "20:00", "OUT")]) is True
    assert has_out_only([log(T, "20:00", "OUT"), log(T, "21:00", "OUT")]) is True


def test_s02_has_out_only_false_for_normal_shapes():
    assert has_out_only([]) is False
    assert has_out_only([log(T, "08:00", "IN")]) is False
    assert has_out_only([log(T, "08:00", "IN"), log(T, "17:00", "OUT")]) is False


def test_s03_has_in_only_kept_for_backcompat():
    assert has_in_only([log(T, "08:00", "IN")]) is True
    assert has_in_only([log(T, "08:00", "IN"), log(T, "17:00", "OUT")]) is False
    assert has_in_only([]) is False
