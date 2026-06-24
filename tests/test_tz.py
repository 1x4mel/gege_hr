"""Unit tests for ``gege_hr.gege_hr.utils.tz`` (plan v5 §2.7).

All cases run without a Frappe bench — the module guards the ``frappe`` import
and falls back to Asia/Ho_Chi_Minh.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from gege_hr.gege_hr.utils import tz

VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = ZoneInfo("UTC")


def _vn(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=VN)


# --------------------------------------------------------------------------- #
# to_portal / naive assumption
# --------------------------------------------------------------------------- #
def test_to_portal_naive_assumed_utc():
    naive = datetime(2026, 6, 20, 1, 0)  # 01:00 UTC
    out = tz.to_portal(naive)
    assert out.tzinfo is not None
    # 01:00 UTC == 08:00 VN
    assert out.hour == 8
    assert out.tzinfo.key == "Asia/Ho_Chi_Minh"


def test_to_portal_aware_passthrough():
    dt = datetime(2026, 6, 20, 8, 0, tzinfo=VN)
    assert tz.to_portal(dt) == dt


def test_utc_iso_appends_z():
    dt = _vn(2026, 6, 20, 8, 0)  # 01:00 UTC
    assert tz.utc_iso(dt) == "2026-06-20T01:00:00Z"


def test_utc_iso_naive_treated_utc():
    assert tz.utc_iso(datetime(2026, 6, 20, 1, 30)) == "2026-06-20T01:30:00Z"


# --------------------------------------------------------------------------- #
# is_overnight / planned_window
# --------------------------------------------------------------------------- #
def test_is_overnight_true_when_end_le_start():
    assert tz.is_overnight(time(20, 0), time(8, 0)) is True


def test_is_overnight_false_same_day():
    assert tz.is_overnight(time(8, 0), time(17, 0)) is False


def test_planned_window_day_shift():
    import datetime as _dt

    start, end = tz.planned_window(_dt.date(2026, 6, 20), time(8, 0), time(17, 0))
    assert start.tzinfo is not None
    assert start.date() == _dt.date(2026, 6, 20)
    assert end.date() == _dt.date(2026, 6, 20)
    assert start.hour == 8 and end.hour == 17


def test_planned_window_overnight_shift_ends_next_day():
    import datetime as _dt

    start, end = tz.planned_window(_dt.date(2026, 6, 20), time(22, 0), time(6, 0))
    assert end.date() == _dt.date(2026, 6, 21)
    assert end.hour == 6


# --------------------------------------------------------------------------- #
# minutes_between
# --------------------------------------------------------------------------- #
def test_minutes_between_positive():
    a = _vn(2026, 6, 20, 8, 0)
    b = _vn(2026, 6, 20, 8, 15)
    assert tz.minutes_between(a, b) == 15.0


def test_minutes_between_inverted_clamped_zero():
    a = _vn(2026, 6, 20, 9, 0)
    b = _vn(2026, 6, 20, 8, 0)
    assert tz.minutes_between(a, b) == 0.0


# --------------------------------------------------------------------------- #
# split_by_night  (the bugfix scenario from handoff session 3)
# --------------------------------------------------------------------------- #
def test_split_by_night_day_only_no_night():
    spans = tz.split_by_night(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 17, 0))
    assert len(spans) == 1
    assert spans[0]["is_night"] is False


def test_split_by_night_regular_prefix_not_dropped():
    # 20:00 -> 23:00 with band 22:00-06:00 must yield regular 20-22 THEN night 22-23.
    spans = tz.split_by_night(_vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 23, 0))
    assert len(spans) == 2
    assert spans[0]["is_night"] is False
    assert spans[0]["start"].hour == 20
    assert spans[0]["end"].hour == 22
    assert spans[1]["is_night"] is True
    assert spans[1]["start"].hour == 22
    assert spans[1]["end"].hour == 23


def test_split_by_night_full_overnight_three_chunks():
    # 20:00 -> next day 08:00 : regular 2h + night 8h + regular 2h.
    spans = tz.split_by_night(_vn(2026, 6, 20, 20, 0), _vn(2026, 6, 21, 8, 0))
    assert [s["is_night"] for s in spans] == [False, True, False]
    # 2h + 8h + 2h
    durations = [(s["end"] - s["start"]).total_seconds() / 3600 for s in spans]
    assert durations == [2.0, 8.0, 2.0]
