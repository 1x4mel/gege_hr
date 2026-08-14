"""Unit tests for the calculation engine — plan v5 §9 (pure functions).

Covers the 33-case matrix A/B/C/D/E plus the holiday & OT-request scenarios
(session 4). All tests are bench-free: the engine's ``frappe`` access is
guarded, and we only exercise the pure entry points.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from gege_hr.gege_hr.utils import calc
from gege_hr.gege_hr.utils import tz as tz_utils

VN = ZoneInfo("Asia/Ho_Chi_Minh")


def _vn(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=VN)


def base_policy(**over):
    p = {
        "name": "P-TEST",
        "version": 1,
        "grace_late_minutes": 5,
        "grace_early_leave_minutes": 0,
        "min_working_hours_full_day": 4.0,
        "min_working_hours_half_day": 2.0,
        "allow_ot_compensate_late": False,
        "allow_ot_compensate_early_leave": False,
        "night_start_time": "22:00:00",
        "night_end_time": "06:00:00",
        "require_overtime_approval": False,
        "min_overtime_minutes": 0,
        "overtime_rounding_method": "No Rounding",
        "overtime_rounding_minutes": 15,
        "holiday_multipliers": {},
    }
    p.update(over)
    return p


def base_shift(planned_start, planned_end, **over):
    s = {
        "name": "SI-TEST",
        "employee": "HR-EMP-001",
        "work_date": str(planned_start.date()),
        "planned_start": planned_start,
        "planned_end": planned_end,
        "vn_allow_overtime_before_shift": False,
        "vn_allow_overtime_after_shift": False,
        "vn_max_overtime_hours": 4.0,
        "vn_max_total_work_hours": 20.0,
    }
    s.update(over)
    return s


def log(t, kind):
    return {"time": t, "log_type": kind}


# =========================================================================== #
# Small pure helpers
# =========================================================================== #
class TestHoursBetween:
    def test_positive(self):
        a = _vn(2026, 6, 20, 8, 0)
        b = _vn(2026, 6, 20, 12, 0)
        assert calc.hours_between(a, b) == 4.0

    def test_none_returns_zero(self):
        assert calc.hours_between(None, _vn(2026, 6, 20, 12, 0)) == 0.0

    def test_inverted_clamped(self):
        a = _vn(2026, 6, 20, 12, 0)
        b = _vn(2026, 6, 20, 8, 0)
        assert calc.hours_between(a, b) == 0.0


class TestSplitByHoliday:
    def test_same_day_non_holiday(self):
        chunks = calc.split_by_holiday(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 17, 0), set())
        assert len(chunks) == 1
        assert chunks[0]["is_holiday"] is False

    def test_crosses_midnight_two_chunks(self):
        chunks = calc.split_by_holiday(_vn(2026, 6, 20, 23, 0), _vn(2026, 6, 21, 2, 0), {date(2026, 6, 21)})
        assert len(chunks) == 2
        assert chunks[0]["is_holiday"] is False  # 20th
        assert chunks[1]["is_holiday"] is True  # 21st

    def test_empty_range(self):
        assert calc.split_by_holiday(None, None, None) == []


# =========================================================================== #
# §9.4  OT helpers
# =========================================================================== #
class TestCalculateOverlap:
    def test_full_overlap(self):
        a = (_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))
        b = (_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))
        assert calc.calculate_overlap(*a, *b) == 2.0

    def test_partial_overlap(self):
        a = (_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 12, 0))
        b = (_vn(2026, 6, 20, 10, 0), _vn(2026, 6, 20, 14, 0))
        assert calc.calculate_overlap(*a, *b) == 2.0

    def test_no_overlap(self):
        a = (_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))
        b = (_vn(2026, 6, 20, 11, 0), _vn(2026, 6, 20, 13, 0))
        assert calc.calculate_overlap(*a, *b) == 0.0

    def test_missing_window(self):
        assert calc.calculate_overlap(None, None, None, None) == 0.0


class TestMatchOvertimeRequest:
    def test_full_match(self):
        win = [{"start": _vn(2026, 6, 20, 6, 0), "end": _vn(2026, 6, 20, 8, 0)}]
        req = [{"from_datetime": _vn(2026, 6, 20, 6, 0), "to_datetime": _vn(2026, 6, 20, 8, 0)}]
        assert calc.match_overtime_request(win, req) == 2.0

    def test_partial_match(self):
        win = [{"start": _vn(2026, 6, 20, 6, 0), "end": _vn(2026, 6, 20, 8, 0)}]
        req = [{"from_datetime": _vn(2026, 6, 20, 7, 0), "to_datetime": _vn(2026, 6, 20, 9, 0)}]
        assert calc.match_overtime_request(win, req) == 1.0

    def test_no_request_no_hours(self):
        win = [{"start": _vn(2026, 6, 20, 6, 0), "end": _vn(2026, 6, 20, 8, 0)}]
        assert calc.match_overtime_request(win, []) == 0.0

    def test_multiple_windows_and_requests(self):
        win = [
            {"start": _vn(2026, 6, 20, 6, 0), "end": _vn(2026, 6, 20, 8, 0)},
            {"start": _vn(2026, 6, 20, 20, 0), "end": _vn(2026, 6, 20, 22, 0)},
        ]
        req = [
            {"from_datetime": _vn(2026, 6, 20, 7, 0), "to_datetime": _vn(2026, 6, 20, 8, 0)},  # 1h on first
            {
                "from_datetime": _vn(2026, 6, 20, 20, 0),
                "to_datetime": _vn(2026, 6, 20, 21, 30),
            },  # 1.5h on second
        ]
        assert calc.match_overtime_request(win, req) == 2.5


class TestRoundOvertime:
    def test_no_rounding(self):
        assert calc.round_overtime(1.25, base_policy()) == 1.25

    def test_nearest_15(self):
        p = base_policy(overtime_rounding_method="Nearest 15min", overtime_rounding_minutes=15)
        # 1.25h = 75min → nearest 15 = 75 → 1.25
        assert calc.round_overtime(1.25, p) == 1.25
        # 1.17h ≈ 70min → nearest 15 = 75 → 1.25
        assert calc.round_overtime(1.17, p) == 1.25

    def test_up_to_30(self):
        p = base_policy(overtime_rounding_method="Up to 30min", overtime_rounding_minutes=30)
        # 1.1h = 66min → up to 30 = 90min → 1.5
        assert calc.round_overtime(1.1, p) == 1.5


class TestGetHolidayMultiplier:
    def test_regular(self):
        assert calc.get_holiday_multiplier("Regular", False, False, {}) == 1.0

    def test_regular_night_still_one(self):
        # Night premium handled elsewhere — not doubled here.
        assert calc.get_holiday_multiplier("Regular Night", False, True, {}) == 1.0

    def test_regular_on_holiday(self):
        assert calc.get_holiday_multiplier("Regular", True, False, {}) == 2.0

    def test_ot_normal(self):
        assert calc.get_holiday_multiplier("OT", False, False, {}) == 1.5

    def test_ot_night(self):
        assert calc.get_holiday_multiplier("OT Night", False, True, {}) == 1.5

    def test_ot_holiday(self):
        assert calc.get_holiday_multiplier("OT Holiday", True, False, {}) == 3.0

    def test_ot_holiday_night(self):
        assert calc.get_holiday_multiplier("OT Holiday Night", True, True, {}) == 3.0

    def test_policy_override(self):
        p = base_policy(holiday_multipliers={"OT": 2.0, "Regular": 1.5})
        assert calc.get_holiday_multiplier("OT", False, False, p) == 2.0
        assert calc.get_holiday_multiplier("Regular", False, False, p) == 1.5


# =========================================================================== #
# §9.1  calculate_work_session
# =========================================================================== #
class TestCalculateWorkSession:
    # Day shift 08:00 -> 20:00 used across most cases.
    PS = _vn(2026, 6, 20, 8, 0)
    PE = _vn(2026, 6, 20, 20, 0)

    def test_case_A_normal_on_time_no_ot(self):
        si = base_shift(self.PS, self.PE)
        logs = [log(_vn(2026, 6, 20, 7, 55), "IN"), log(_vn(2026, 6, 20, 20, 5), "OUT")]
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["late_minutes"] == 0
        assert r["early_leave_minutes"] == 0
        assert r["actual_within_shift_hours"] == 12.0
        assert r["regular_hours"] == 12.0
        assert r["raw_overtime_hours"] == 0.0
        assert r["approved_overtime_hours"] == 0.0
        assert r["need_review"] == 0
        assert r["payable_day"] == 1.0

    def test_case_B_late_beyond_grace(self):
        si = base_shift(self.PS, self.PE)
        logs = [log(_vn(2026, 6, 20, 8, 15), "IN"), log(_vn(2026, 6, 20, 20, 0), "OUT")]
        r = calc.calculate_work_session(si, logs, base_policy())
        # 15min late - 5 grace = 10
        assert r["late_minutes"] == 10
        assert r["actual_within_shift_hours"] == pytest.approx(11.75, abs=1e-6)
        assert r["payable_day"] == 1.0

    def test_case_C_pre_and_post_overtime(self):
        si = base_shift(
            self.PS, self.PE, vn_allow_overtime_before_shift=True, vn_allow_overtime_after_shift=True
        )
        logs = [
            log(_vn(2026, 6, 20, 6, 0), "IN"),  # 2h pre-OT
            log(_vn(2026, 6, 20, 22, 0), "OUT"),
        ]  # 2h post-OT
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["raw_pre_overtime_hours"] == 2.0
        assert r["raw_post_overtime_hours"] == 2.0
        assert r["raw_overtime_hours"] == 4.0
        # No approval required → all raw OT auto-approves.
        assert r["approved_overtime_hours"] == 4.0
        assert r["need_review"] == 0
        assert r["regular_hours"] == 12.0

    def test_overtime_disabled_yields_zero_ot(self):
        si = base_shift(self.PS, self.PE)  # both OT flags False
        logs = [log(_vn(2026, 6, 20, 6, 0), "IN"), log(_vn(2026, 6, 20, 22, 0), "OUT")]
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["raw_pre_overtime_hours"] == 0.0
        assert r["raw_post_overtime_hours"] == 0.0
        assert r["approved_overtime_hours"] == 0.0
        # Within shift still clamped to planned window.
        assert r["actual_within_shift_hours"] == 12.0

    def test_ot_compensate_late(self):
        # Late 30min, grace 0; 1h post-OT allowed; compensate ON.
        si = base_shift(self.PS, self.PE, vn_allow_overtime_after_shift=True)
        logs = [log(_vn(2026, 6, 20, 8, 30), "IN"), log(_vn(2026, 6, 20, 21, 0), "OUT")]  # 1h post-OT
        p = base_policy(grace_late_minutes=0, allow_ot_compensate_late=True)
        r = calc.calculate_work_session(si, logs, p)
        # 30 late fully compensated by 30min of OT → late 0, OT 0.5h
        assert r["late_minutes"] == 0
        assert r["ot_compensated_late_minutes"] == 30
        assert r["raw_overtime_hours"] == pytest.approx(0.5, abs=1e-6)
        assert r["approved_overtime_hours"] == pytest.approx(0.5, abs=1e-6)

    def test_ot_compensate_early_leave(self):
        # Pre-OT (06:00 in → 2h) + early checkout (19:30 → 30min early).
        # Compensate early-leave with the pre-OT: 30min OT absorbs the 30min
        # early leave, leaving 1.5h OT and 0 early-leave.
        si = base_shift(self.PS, self.PE, vn_allow_overtime_before_shift=True)
        logs = [
            log(_vn(2026, 6, 20, 6, 0), "IN"),  # 2h pre-OT
            log(_vn(2026, 6, 20, 19, 30), "OUT"),
        ]  # 30min early
        p = base_policy(allow_ot_compensate_early_leave=True)
        r = calc.calculate_work_session(si, logs, p)
        assert r["early_leave_minutes"] == 0
        assert r["ot_compensated_early_minutes"] == 30
        assert r["raw_overtime_hours"] == pytest.approx(1.5, abs=1e-6)

    def test_missing_checkout_need_review_and_zero_within(self):
        si = base_shift(self.PS, self.PE)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN")]  # no OUT
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["missing_checkout"] == 1
        assert r["need_review"] == 1
        # safe_out falls back to planned_start → within-shift 0.
        assert r["actual_within_shift_hours"] == 0.0
        assert r["payable_day"] == 0.0

    def test_missing_checkin_need_review(self):
        si = base_shift(self.PS, self.PE)
        logs = [log(_vn(2026, 6, 20, 20, 0), "OUT")]  # no IN
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["missing_checkin"] == 1
        assert r["need_review"] == 1
        # safe_in falls back to planned_start so within-shift is still computed,
        # but the session is flagged for review regardless.
        assert r["actual_within_shift_hours"] == 12.0

    def test_half_day_payable(self):
        # 3h within shift → between half-day (2h) and full-day (4h) → 0.5
        si = base_shift(self.PS, self.PE)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 11, 0), "OUT")]
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["actual_within_shift_hours"] == 3.0
        assert r["payable_day"] == 0.5

    def test_max_total_hours_triggers_review(self):
        si = base_shift(self.PS, self.PE, vn_max_total_work_hours=10.0)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 20, 0), "OUT")]  # 12h total
        r = calc.calculate_work_session(si, logs, base_policy())
        assert r["need_review"] == 1

    def test_min_overtime_filter_drops_small_ot(self):
        si = base_shift(self.PS, self.PE, vn_allow_overtime_after_shift=True)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 20, 10), "OUT")]  # 10min post-OT
        p = base_policy(min_overtime_minutes=30)
        r = calc.calculate_work_session(si, logs, p)
        # raw OT 10min < 30 → payable raw OT 0 → approved 0
        assert r["raw_post_overtime_hours"] == pytest.approx(10 / 60, abs=1e-3)
        assert r["approved_overtime_hours"] == 0.0

    def test_ot_approval_required_without_request(self):
        si = base_shift(self.PS, self.PE, vn_allow_overtime_after_shift=True)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 22, 0), "OUT")]  # 2h post-OT
        p = base_policy(require_overtime_approval=True)
        r = calc.calculate_work_session(si, logs, p, ot_requests=[])
        assert r["raw_overtime_hours"] == 2.0
        assert r["approved_overtime_hours"] == 0.0  # no approved request

    def test_ot_approval_required_with_request(self):
        si = base_shift(self.PS, self.PE, vn_allow_overtime_after_shift=True)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 22, 0), "OUT")]  # 2h post-OT
        req = [
            {"from_datetime": _vn(2026, 6, 20, 20, 0), "to_datetime": _vn(2026, 6, 20, 21, 0)}
        ]  # approved 1h
        p = base_policy(require_overtime_approval=True)
        r = calc.calculate_work_session(si, logs, p, ot_requests=req)
        assert r["approved_overtime_hours"] == 1.0


# =========================================================================== #
# match_overtime_request_detailed — per-request overlap (plan T3 / BUG-2)
# =========================================================================== #
def _win(start, end):
    return {"start": start, "end": end}


def _req(name, start, end):
    return {"name": name, "from_datetime": start, "to_datetime": end}


class TestMatchOvertimeRequestDetailed:
    def test_actual_inside_request(self):
        # TC-U-01: actual OT window fully inside the approved request.
        out = calc.match_overtime_request_detailed(
            [_win(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))],
            [_req("OR-1", _vn(2026, 6, 20, 7, 0), _vn(2026, 6, 20, 11, 0))],
        )
        assert out == {"OR-1": 2.0}

    def test_request_inside_actual(self):
        # TC-U-02: request fully inside the actual OT window.
        out = calc.match_overtime_request_detailed(
            [_win(_vn(2026, 6, 20, 7, 0), _vn(2026, 6, 20, 11, 0))],
            [_req("OR-1", _vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))],
        )
        assert out == {"OR-1": 2.0}

    def test_partial_overlap(self):
        # TC-U-03: only the intersection counts.
        out = calc.match_overtime_request_detailed(
            [_win(_vn(2026, 6, 20, 9, 0), _vn(2026, 6, 20, 12, 0))],
            [_req("OR-1", _vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 10, 0))],
        )
        assert out == {"OR-1": 1.0}

    def test_disjoint_returns_empty(self):
        # TC-U-04: no overlap → request absent from the breakdown (not 0).
        out = calc.match_overtime_request_detailed(
            [_win(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 9, 0))],
            [_req("OR-1", _vn(2026, 6, 20, 10, 0), _vn(2026, 6, 20, 11, 0))],
        )
        assert out == {}

    def test_multiple_windows_requests_no_double_count(self):
        # TC-U-05: pre-OT + post-OT, each matched to its own request.
        windows = [
            _win(_vn(2026, 6, 20, 6, 0), _vn(2026, 6, 20, 8, 0)),
            _win(_vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 22, 0)),
        ]
        reqs = [
            _req("OR-PRE", _vn(2026, 6, 20, 6, 0), _vn(2026, 6, 20, 8, 0)),
            _req("OR-POST", _vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 22, 0)),
        ]
        assert calc.match_overtime_request_detailed(windows, reqs) == {
            "OR-PRE": 2.0,
            "OR-POST": 2.0,
        }

    def test_sum_equals_aggregate(self):
        # TC-U-07: contract — Σ detailed == match_overtime_request (pre-cap).
        windows = [
            _win(_vn(2026, 6, 20, 6, 0), _vn(2026, 6, 20, 8, 0)),
            _win(_vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 22, 0)),
        ]
        reqs = [
            _req("OR-PRE", _vn(2026, 6, 20, 6, 0), _vn(2026, 6, 20, 8, 0)),
            _req("OR-POST", _vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 22, 0)),
        ]
        total = calc.match_overtime_request(windows, reqs)
        detailed = calc.match_overtime_request_detailed(windows, reqs)
        assert round(sum(detailed.values()), 4) == total

    def test_tz_same_instant_different_zone(self):
        # TC-U-09: 20:00+07 == 13:00Z → overlap is the full hour (not 0, not 8).
        out = calc.match_overtime_request_detailed(
            [_win("2026-06-20 13:00:00+00:00", "2026-06-20 14:00:00+00:00")],
            [_req("OR-1", _vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 21, 0))],
        )
        assert out == {"OR-1": 1.0}

    def test_fe_offset_string_matches_local_instant(self):
        # The +07:00 string the FE now sends resolves to the same instant as the
        # tz-aware local datetime (locks down EC-3 / plan T5).
        assert calc._as_dt("2026-08-14 18:00:00+07:00") == calc._as_dt(_vn(2026, 8, 14, 18, 0))

    def test_breakdown_stamped_on_work_session_result(self):
        # The calc result now carries the per-request breakdown for write-back.
        si = base_shift(
            _vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 20, 0),
            vn_allow_overtime_after_shift=True,
        )
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 22, 0), "OUT")]
        reqs = [_req("OR-1", _vn(2026, 6, 20, 20, 0), _vn(2026, 6, 20, 21, 0))]
        r = calc.calculate_work_session(
            si, logs, base_policy(require_overtime_approval=True), ot_requests=reqs
        )
        assert r["_ot_request_breakdown"] == {"OR-1": 1.0}
        assert r["approved_overtime_hours"] == 1.0


# =========================================================================== #
# §9.2  generate_segments
# =========================================================================== #
class TestGenerateSegments:
    def test_day_shift_regular_only(self):
        si = base_shift(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 20, 0))
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 20, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws)
        assert len(segs) == 1
        assert segs[0]["segment_type"] == "Regular"
        assert segs[0]["hours"] == 12.0
        assert segs[0]["is_night"] == 0

    def test_ot_pre_and_post_segments(self):
        si = base_shift(
            _vn(2026, 6, 20, 8, 0),
            _vn(2026, 6, 20, 20, 0),
            vn_allow_overtime_before_shift=True,
            vn_allow_overtime_after_shift=True,
        )
        logs = [log(_vn(2026, 6, 20, 6, 0), "IN"), log(_vn(2026, 6, 20, 22, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws)
        types = [s["segment_type"] for s in segs]
        # Pre-OT (06-08) + Regular (08-20) + Post-OT (20-22), all daytime.
        assert types == ["OT", "Regular", "OT"]
        assert sum(s["hours"] for s in segs) == 16.0

    def test_night_shift_split_into_regular_and_night(self):
        # 20:00 -> 08:00 next day, no OT. split_by_night yields 3 logical spans
        # (reg 2h / night 8h / reg 2h), but the night span is also midnight-split
        # by _split_night_holiday → 22:00-00:00 + 00:00-06:00. Total regular 4h,
        # total night 8h.
        si = base_shift(_vn(2026, 6, 20, 20, 0), _vn(2026, 6, 21, 8, 0))
        logs = [log(_vn(2026, 6, 20, 20, 0), "IN"), log(_vn(2026, 6, 21, 8, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws)
        night_hours = sum(s["hours"] for s in segs if s["segment_type"] == "Regular Night")
        reg_hours = sum(s["hours"] for s in segs if s["segment_type"] == "Regular")
        assert night_hours == 8.0
        assert reg_hours == 4.0
        # All segments are non-holiday, multiplier 1.0.
        assert all(s["is_holiday"] == 0 for s in segs)
        assert all(s["multiplier"] == 1.0 for s in segs)

    def test_holiday_split_ot_into_holiday_segments(self):
        # Post-OT 20:00 -> 23:00, the 20th is a holiday.
        si = base_shift(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 20, 0), vn_allow_overtime_after_shift=True)
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 23, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws, holiday_dates={date(2026, 6, 20)})
        ot_segs = [s for s in segs if s["segment_type"].startswith("OT")]
        # 20-22 OT Holiday (regular part), 22-23 OT Holiday Night.
        types = sorted(s["segment_type"] for s in ot_segs)
        assert types == ["OT Holiday", "OT Holiday Night"]
        assert all(s["is_holiday"] == 1 for s in ot_segs)
        assert all(s["multiplier"] == 3.0 for s in ot_segs)

    def test_regular_holiday_flagged(self):
        si = base_shift(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 12, 0))
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 12, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws, holiday_dates={date(2026, 6, 20)})
        assert len(segs) == 1
        assert segs[0]["segment_type"] == "Regular"
        assert segs[0]["is_holiday"] == 1
        assert segs[0]["multiplier"] == 2.0

    def test_late_segment_emitted(self):
        si = base_shift(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 20, 0))
        logs = [
            log(_vn(2026, 6, 20, 8, 30), "IN"),  # 30 late - 5 grace = 25
            log(_vn(2026, 6, 20, 20, 0), "OUT"),
        ]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws)
        late = [s for s in segs if s["segment_type"] == "Late"]
        assert len(late) == 1
        # Engine rounds segment hours to 4 decimals (0.4167).
        assert late[0]["hours"] == pytest.approx(25 / 60, abs=1e-3)

    def test_segment_calendar_date_and_hours_filled(self):
        si = base_shift(_vn(2026, 6, 20, 8, 0), _vn(2026, 6, 20, 20, 0))
        logs = [log(_vn(2026, 6, 20, 8, 0), "IN"), log(_vn(2026, 6, 20, 20, 0), "OUT")]
        ws = calc.calculate_work_session(si, logs, base_policy())
        segs = calc.generate_segments(ws)
        for s in segs:
            assert s["calendar_date"] == "2026-06-20"
            assert s["hours"] > 0


# =========================================================================== #
# §9.3  calculate_payable_day
# =========================================================================== #
class TestCalculatePayableDay:
    def test_absent_is_zero(self):
        ws = {"absent": 1, "actual_within_shift_hours": 0}
        assert calc.calculate_payable_day(ws, base_policy()) == 0.0

    def test_leave_paid_full(self):
        ws = {"absent": 0, "actual_within_shift_hours": 0}
        info = {"has_leave": True, "salary_impact_type": "Paid", "leave_days_equivalent": 1.0}
        assert calc.calculate_payable_day(ws, base_policy(), info) == 1.0

    def test_leave_half_paid(self):
        ws = {"absent": 0, "actual_within_shift_hours": 0}
        info = {"has_leave": True, "salary_impact_type": "Half Paid", "leave_days_equivalent": 1.0}
        assert calc.calculate_payable_day(ws, base_policy(), info) == 0.5

    def test_leave_unpaid(self):
        ws = {"absent": 0, "actual_within_shift_hours": 0}
        info = {"has_leave": True, "salary_impact_type": "Unpaid", "leave_days_equivalent": 1.0}
        assert calc.calculate_payable_day(ws, base_policy(), info) == 0.0

    def test_full_day_by_min_hours(self):
        ws = {"absent": 0, "actual_within_shift_hours": 5.0}
        assert calc.calculate_payable_day(ws, base_policy()) == 1.0

    def test_half_day_by_min_hours(self):
        ws = {"absent": 0, "actual_within_shift_hours": 3.0}
        assert calc.calculate_payable_day(ws, base_policy()) == 0.5

    def test_below_half_day_zero(self):
        ws = {"absent": 0, "actual_within_shift_hours": 1.0}
        assert calc.calculate_payable_day(ws, base_policy()) == 0.0


# --------------------------------------------------------------------------- #
# _db_dt — Frappe Datetime DB serialization (root cause of empty /hr/attendance)
# --------------------------------------------------------------------------- #
class TestDbDt:
    """``calculate_work_session`` emits ISO-Z strings (``2026-06-24T01:00:00Z``);
    MariaDB rejects them for Datetime columns, silently killing every Work
    Session insert. ``_db_dt`` must normalise to ``YYYY-MM-DD HH:MM:SS``."""

    def test_iso_z_normalized(self):
        assert calc._db_dt("2026-06-24T01:00:00Z") == "2026-06-24 01:00:00"

    def test_lowercase_z(self):
        assert calc._db_dt("2026-06-24T01:00:00z") == "2026-06-24 01:00:00"

    def test_offset_converted_to_utc(self):
        # +07:00 → 00:00 UTC
        assert calc._db_dt("2026-06-24T08:00:00+07:00") == "2026-06-24 01:00:00"

    def test_datetime_input(self):
        dt = datetime(2026, 6, 24, 1, 0, 0, tzinfo=ZoneInfo("UTC"))
        assert calc._db_dt(dt) == "2026-06-24 01:00:00"

    def test_naive_datetime_passthrough(self):
        dt = datetime(2026, 6, 24, 1, 0, 0)
        assert calc._db_dt(dt) == "2026-06-24 01:00:00"

    def test_none_returns_none(self):
        assert calc._db_dt(None) is None

    def test_output_is_db_safe(self):
        # The exact failure trigger: no 'T' and no 'Z' in the result.
        out = calc._db_dt("2026-06-24T01:00:00Z")
        assert "T" not in out and "Z" not in out
        assert out.count("-") == 2 and out.count(":") == 2
