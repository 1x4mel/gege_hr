"""Bench-free tests for gege_hr.gege_hr.utils.attendance_period.

These exercise the pure aggregation/rollup core (no bench import needed) the
same way test_payroll.py / test_calc.py do.
"""

from gege_hr.gege_hr.utils import attendance_period as ap


def _ws(
    employee,
    *,
    work_date="2026-06-01",
    regular=8.0,
    night=0.0,
    ot_normal=0.0,
    ot_night=0.0,
    ot_holiday=0.0,
    raw_ot=0.0,
    late=0,
    early=0,
    payable_day=1.0,
    absent=0,
    need_review=0,
    has_leave=0,
    is_holiday=0,
    leave_unpaid=0,
    employee_name="NV",
    department="IT",
    branch="HQ",
    company="ACME",
):
    return {
        "employee": employee,
        "employee_name": employee_name,
        "work_date": work_date,
        "department": department,
        "branch": branch,
        "company": company,
        "regular_hours": regular,
        "regular_night_hours": night,
        "overtime_normal_hours": ot_normal,
        "overtime_night_hours": ot_night,
        "overtime_holiday_hours": ot_holiday,
        "raw_overtime_hours": raw_ot,
        "late_minutes": late,
        "early_leave_minutes": early,
        "payable_day": payable_day,
        "absent": absent,
        "need_review": need_review,
        "has_leave": has_leave,
        "is_holiday": is_holiday,
        "leave_unpaid": leave_unpaid,
    }


# --------------------------------------------------------------------------- #
# aggregate_period
# --------------------------------------------------------------------------- #
def test_aggregate_single_employee_full_month():
    rows = [_ws("E1", work_date=f"2026-06-{d:02d}", ot_normal=2.0, late=15) for d in range(1, 27)]
    lines = ap.aggregate_period(rows)
    assert set(lines) == {"E1"}
    ln = lines["E1"]
    assert ln["working_days"] == 26
    assert ln["regular_hours"] == 208.0  # 26 × 8
    assert ln["overtime_hours"] == 52.0  # 26 × 2
    assert ln["late_count"] == 26
    assert ln["late_minutes"] == 390
    assert ln["present_days"] == 26.0
    assert ln["absent_days"] == 0.0
    assert ln["employee_name"] == "NV"


def test_aggregate_multiple_employees_grouped():
    rows = [
        _ws("E1"),
        _ws("E1", work_date="2026-06-02"),
        _ws("E2", work_date="2026-06-01", regular=8),
    ]
    lines = ap.aggregate_period(rows)
    assert lines["E1"]["working_days"] == 2
    assert lines["E2"]["working_days"] == 1


def test_aggregate_absent_day():
    rows = [_ws("E1"), _ws("E1", work_date="2026-06-02", absent=1, payable_day=0)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["absent_days"] == 1.0
    assert ln["present_days"] == 1.0  # only the present day counts


def test_aggregate_leave_split_paid_unpaid():
    rows = [
        _ws("E1", has_leave=1),
        _ws("E1", work_date="2026-06-02", has_leave=1, leave_unpaid=1),
    ]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["paid_leave_days"] == 1.0
    assert ln["unpaid_leave_days"] == 1.0


def test_aggregate_holiday_day_counts():
    rows = [_ws("E1", is_holiday=1, payable_day=1.0)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["holiday_days"] == 1.0


def test_aggregate_ot_falls_back_to_raw_when_no_split():
    rows = [_ws("E1", raw_ot=3.0)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["overtime_hours"] == 3.0
    assert ln["overtime_night_hours"] == 0.0


def test_aggregate_ot_night_and_holiday_split():
    rows = [_ws("E1", ot_normal=1.0, ot_night=2.0, ot_holiday=4.0)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["overtime_hours"] == 1.0
    assert ln["overtime_night_hours"] == 2.0
    assert ln["overtime_holiday_hours"] == 4.0


def test_aggregate_early_leave():
    rows = [_ws("E1", early=20)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["early_leave_count"] == 1
    assert ln["early_leave_minutes"] == 20


def test_aggregate_need_review():
    rows = [_ws("E1", need_review=1), _ws("E1", work_date="2026-06-02", need_review=1)]
    ln = ap.aggregate_period(rows)["E1"]
    assert ln["need_review_count"] == 2


def test_aggregate_ignores_rows_without_employee():
    rows = [{"employee": None, "regular_hours": 8}, _ws("E1")]
    lines = ap.aggregate_period(rows)
    assert set(lines) == {"E1"}


def test_aggregate_empty_returns_empty():
    assert ap.aggregate_period([]) == {}
    assert ap.aggregate_period(None) == {}


def test_round2_helper():
    assert ap.round2(1.005) == 1.0 or ap.round2(1.005) == 1.01  # banker's rounding tolerant
    assert ap.round2(None) == 0.0
    assert ap.round2("3.2") == 3.2


# --------------------------------------------------------------------------- #
# rollup_period
# --------------------------------------------------------------------------- #
def test_rollup_sums_totals():
    lines = {
        "E1": {
            "present_days": 26.0,
            "absent_days": 0.0,
            "overtime_hours": 10.0,
            "late_minutes": 30,
            "need_review_count": 1,
        },
        "E2": {
            "present_days": 20.0,
            "absent_days": 6.0,
            "overtime_hours": 5.0,
            "late_minutes": 10,
            "need_review_count": 0,
        },
    }
    totals = ap.rollup_period(lines)
    assert totals["total_employees"] == 2
    assert totals["total_present_days"] == 46.0
    assert totals["total_absent_days"] == 6.0
    assert totals["total_overtime_hours"] == 15.0
    assert totals["total_late_minutes"] == 40
    assert totals["total_need_review"] == 1


def test_rollup_empty():
    totals = ap.rollup_period({})
    assert totals == {
        "total_employees": 0,
        "total_present_days": 0.0,
        "total_absent_days": 0.0,
        "total_overtime_hours": 0.0,
        "total_late_minutes": 0,
        "total_need_review": 0,
    }


def test_rollup_tolerant_of_missing_keys():
    totals = ap.rollup_period({"E1": {}})
    assert totals["total_employees"] == 1
    assert totals["total_present_days"] == 0.0


# --------------------------------------------------------------------------- #
# can_lock
# --------------------------------------------------------------------------- #
def test_can_lock_all_confirmed():
    lines = [{"status": "Confirmed"}, {"status": "Adjusted"}]
    assert ap.can_lock(lines) is True


def test_can_lock_blocked_by_draft():
    lines = [{"status": "Confirmed"}, {"status": "Draft"}]
    assert ap.can_lock(lines) is False


def test_can_lock_empty_is_false():
    assert ap.can_lock([]) is False


# --------------------------------------------------------------------------- #
# recalc_payable_days fallback
# --------------------------------------------------------------------------- #
def test_recalc_payable_days_falls_back_to_present():
    lines = {"E1": {"present_days": 22.0, "payable_days": 0.0}}
    ap.recalc_payable_days(lines)
    assert lines["E1"]["payable_days"] == 22.0


def test_recalc_payable_days_preserves_explicit():
    lines = {"E1": {"present_days": 22.0, "payable_days": 21.5}}
    ap.recalc_payable_days(lines)
    assert lines["E1"]["payable_days"] == 21.5
