"""Bench-free unit tests for the pure helpers in ``gege_hr.gege_hr.api.dashboard``.

Covers the rate / aggregation logic that drives the company KPI overview (plan
v5 §13). The bench-dependent endpoint (``get_employee_dashboard``) is exercised
on the bench.
"""

from gege_hr.gege_hr.api import dashboard as dash


# --------------------------------------------------------------------------- #
# compute_rate
# --------------------------------------------------------------------------- #
def test_rate_simple():
    assert dash.compute_rate(8, 2) == 80.0


def test_rate_all_present():
    assert dash.compute_rate(10, 0) == 100.0


def test_rate_all_absent():
    assert dash.compute_rate(0, 10) == 0.0


def test_rate_zero_denominator_is_zero():
    assert dash.compute_rate(0, 0) == 0.0


def test_rate_negative_safe():
    # Defensive: negative inputs should never raise / produce NaN.
    assert dash.compute_rate(-1, 0) == 0.0


def test_rate_rounds_two_decimals():
    assert dash.compute_rate(1, 3) == 25.0
    assert dash.compute_rate(2, 3) == 40.0
    # 1/7 ≈ 14.285714 → 14.29
    assert dash.compute_rate(1, 6) == 14.29


# --------------------------------------------------------------------------- #
# aggregate_today
# --------------------------------------------------------------------------- #
def _ws(absent=0, missing_checkin=0, late_minutes=0):
    return {
        "absent": absent,
        "missing_checkin": missing_checkin,
        "late_minutes": late_minutes,
    }


def test_aggregate_today_present_late_absent():
    rows = [
        _ws(),  # present, on time
        _ws(late_minutes=15),  # present, late
        _ws(absent=1),  # absent
        _ws(missing_checkin=1),  # no checkin, not flagged → counts absent
    ]
    out = dash.aggregate_today(rows)
    assert out == {"present_today": 2, "absent_today": 2, "late_today": 1}


def test_aggregate_today_empty():
    assert dash.aggregate_today([]) == {
        "present_today": 0,
        "absent_today": 0,
        "late_today": 0,
    }


def test_aggregate_today_none():
    assert dash.aggregate_today(None) == {
        "present_today": 0,
        "absent_today": 0,
        "late_today": 0,
    }


def test_aggregate_today_absent_ignores_late():
    # An absent row is not also counted as late even if late_minutes set.
    rows = [_ws(absent=1, late_minutes=99)]
    out = dash.aggregate_today(rows)
    assert out["late_today"] == 0
    assert out["absent_today"] == 1


def test_aggregate_today_string_values_coerced():
    # Frappe may return "0"/"1" strings.
    rows = [
        {"absent": "0", "missing_checkin": "0", "late_minutes": "5"},
        {"absent": "1", "missing_checkin": "0", "late_minutes": "0"},
    ]
    out = dash.aggregate_today(rows)
    assert out == {"present_today": 1, "absent_today": 1, "late_today": 1}


# --------------------------------------------------------------------------- #
# aggregate_dashboard
# --------------------------------------------------------------------------- #
def test_aggregate_dashboard_full():
    rows = [_ws(), _ws(late_minutes=10), _ws(absent=1)]
    payload = dash.aggregate_dashboard(
        ws_rows=rows,
        total_employees=25,
        on_leave_today=3,
        total_exceptions=7,
        locked_periods=1,
    )
    assert payload["total_employees"] == 25
    assert payload["present_today"] == 2
    assert payload["absent_today"] == 1
    assert payload["late_today"] == 1
    assert payload["on_leave_today"] == 3
    assert payload["total_exceptions"] == 7
    assert payload["locked_periods"] == 1
    # rate = 2/(2+1)*100 = 66.67
    assert payload["company_attendance_rate"] == 66.67


def test_aggregate_dashboard_empty_company():
    payload = dash.aggregate_dashboard(
        ws_rows=[],
        total_employees=0,
        on_leave_today=0,
        total_exceptions=0,
        locked_periods=0,
    )
    assert payload["company_attendance_rate"] == 0.0
    assert payload["present_today"] == 0


def test_aggregate_dashboard_coerces_string_counts():
    payload = dash.aggregate_dashboard(
        ws_rows=[_ws()],
        total_employees="40",
        on_leave_today="2",
        total_exceptions="9",
        locked_periods="3",
    )
    assert payload["total_employees"] == 40
    assert payload["on_leave_today"] == 2
    assert payload["total_exceptions"] == 9
    assert payload["locked_periods"] == 3


def test_open_exception_statuses():
    assert "Open" in dash.OPEN_EXCEPTION_STATUSES
    assert "Resolved" not in dash.OPEN_EXCEPTION_STATUSES


def test_module_imports_outside_bench():
    # The whitelist shim must not require frappe at import time.
    assert callable(dash.get_employee_dashboard)
    assert getattr(dash.get_employee_dashboard, "whitelisted", False) is True
