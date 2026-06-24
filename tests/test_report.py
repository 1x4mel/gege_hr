"""Bench-free tests for gege_hr.gege_hr.utils.report.

Exercises the pure aggregation/rollup core (no bench import needed) the same
way test_attendance_period.py / test_payroll.py do, plus the month-window
resolver used by the ``get_attendance_report`` endpoint.
"""

from gege_hr.gege_hr.utils import report as rpt


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


def _leave(
    employee,
    *,
    total_leave_days=1.0,
    is_lwp=0,
    from_date=None,
    to_date=None,
    employee_name="NV",
    department="IT",
    branch="HQ",
    company="ACME",
    leave_type="Annual",
):
    return {
        "name": f"L-{employee}-{from_date}",
        "employee": employee,
        "employee_name": employee_name,
        "department": department,
        "branch": branch,
        "company": company,
        "from_date": from_date,
        "to_date": to_date,
        "total_leave_days": total_leave_days,
        "is_lwp": is_lwp,
        "leave_type": leave_type,
    }


# --------------------------------------------------------------------------- #
# month_window
# --------------------------------------------------------------------------- #
def test_month_window_basic():
    win = rpt.month_window("06", "2026")
    assert win is not None
    f, t = win
    assert (f.year, f.month, f.day) == (2026, 6, 1)
    assert (t.year, t.month, t.day) == (2026, 6, 30)


def test_month_window_february_leap():
    win = rpt.month_window(2, 2024)  # 2024 is a leap year
    assert win is not None
    f, t = win
    assert t.day == 29


def test_month_window_february_nonleap():
    win = rpt.month_window(2, 2023)
    assert win is not None
    _, t = win
    assert t.day == 28


def test_month_window_int_inputs():
    win = rpt.month_window(12, 2026)
    assert win is not None
    _, t = win
    assert t.day == 31


def test_month_window_invalid_month():
    assert rpt.month_window(0, 2026) is None
    assert rpt.month_window(13, 2026) is None
    assert rpt.month_window("", 2026) is None


def test_month_window_invalid_year():
    assert rpt.month_window(6, 1899) is None
    assert rpt.month_window(6, 3000) is None
    assert rpt.month_window(6, "") is None


# --------------------------------------------------------------------------- #
# build_employee_report
# --------------------------------------------------------------------------- #
def test_report_empty():
    rows = rpt.build_employee_report([], [])
    assert rows == []
    totals = rpt.report_totals(rows)
    assert totals["total_employees"] == 0
    assert totals["total_present_days"] == 0.0


def test_report_none_inputs():
    # None must not crash (tolerant of missing leave ledger).
    rows = rpt.build_employee_report(None, None)
    assert rows == []


def test_report_single_employee_attendance():
    ws = [
        _ws("E1", work_date="2026-06-01", regular=8.0, late=15),
        _ws("E1", work_date="2026-06-02", regular=8.0, ot_normal=2.0),
        _ws("E1", work_date="2026-06-03", regular=0.0, absent=1, payable_day=0),
    ]
    rows = rpt.build_employee_report(ws, [])
    assert len(rows) == 1
    r = rows[0]
    assert r["employee"] == "E1"
    # present_days sums payable_day of non-absent days (1 + 1 = 2).
    assert r["present_days"] == 2.0
    assert r["absent_days"] == 1.0
    assert r["overtime_hours"] == 2.0
    assert r["late_minutes"] == 15
    assert r["leave_days"] == 0.0


def test_report_multiple_employees_sorted_by_name():
    ws = [
        _ws("E2", employee_name="Zara", work_date="2026-06-01", regular=8.0),
        _ws("E1", employee_name="Anna", work_date="2026-06-01", regular=8.0),
    ]
    rows = rpt.build_employee_report(ws, [])
    assert [r["employee_name"] for r in rows] == ["Anna", "Zara"]


def test_report_leave_from_ledger_paid_and_unpaid():
    # No work sessions — employee was on leave the whole month.
    leaves = [
        _leave("E1", total_leave_days=3.0, is_lwp=0),  # paid
        _leave("E1", total_leave_days=1.0, is_lwp=1, leave_type="Unpaid"),  # unpaid
    ]
    rows = rpt.build_employee_report([], leaves)
    assert len(rows) == 1
    r = rows[0]
    assert r["leave_days"] == 4.0
    assert r["paid_leave_days"] == 3.0
    assert r["unpaid_leave_days"] == 1.0
    assert r["present_days"] == 0.0


def test_report_leave_augments_existing_employee():
    ws = [_ws("E1", work_date="2026-06-01", regular=8.0)]
    leaves = [_leave("E1", total_leave_days=2.0, is_lwp=0)]
    rows = rpt.build_employee_report(ws, leaves)
    assert len(rows) == 1
    r = rows[0]
    assert r["present_days"] == 1.0
    assert r["leave_days"] == 2.0
    assert r["paid_leave_days"] == 2.0


def test_report_leave_day_span_fallback_without_total_leave_days():
    # total_leave_days absent → compute inclusive day span from dates.
    leaves = [
        _leave("E1", total_leave_days=None, from_date="2026-06-10", to_date="2026-06-12"),
    ]
    rows = rpt.build_employee_report([], leaves)
    assert rows[0]["leave_days"] == 3.0  # 10, 11, 12 inclusive


def test_report_row_shape_complete():
    ws = [_ws("E1", work_date="2026-06-01", regular=8.0, ot_normal=1.5, late=10, early=5)]
    rows = rpt.build_employee_report(ws, [])
    r = rows[0]
    expected_keys = {
        "employee",
        "employee_name",
        "department",
        "branch",
        "company",
        "present_days",
        "absent_days",
        "paid_leave_days",
        "unpaid_leave_days",
        "holiday_days",
        "leave_days",
        "regular_hours",
        "overtime_hours",
        "overtime_night_hours",
        "late_count",
        "late_minutes",
        "early_leave_minutes",
        "payable_days",
        "need_review_count",
    }
    assert expected_keys.issubset(set(r.keys()))
    assert r["overtime_hours"] == 1.5
    assert r["late_count"] == 1


# --------------------------------------------------------------------------- #
# report_totals
# --------------------------------------------------------------------------- #
def test_report_totals_sum():
    rows = [
        {
            "present_days": 20.0,
            "absent_days": 1.0,
            "leave_days": 2.0,
            "paid_leave_days": 2.0,
            "unpaid_leave_days": 0.0,
            "overtime_hours": 5.0,
            "late_minutes": 30,
            "need_review_count": 1,
            "payable_days": 20.0,
        },
        {
            "present_days": 18.0,
            "absent_days": 3.0,
            "leave_days": 1.0,
            "paid_leave_days": 1.0,
            "unpaid_leave_days": 1.0,
            "overtime_hours": 3.0,
            "late_minutes": 15,
            "need_review_count": 0,
            "payable_days": 18.0,
        },
    ]
    totals = rpt.report_totals(rows)
    assert totals["total_employees"] == 2
    assert totals["total_present_days"] == 38.0
    assert totals["total_absent_days"] == 4.0
    assert totals["total_leave_days"] == 3.0
    assert totals["total_paid_leave_days"] == 3.0
    assert totals["total_unpaid_leave_days"] == 1.0
    assert totals["total_overtime_hours"] == 8.0
    assert totals["total_late_minutes"] == 45
    assert totals["total_need_review"] == 1
    assert totals["total_payable_days"] == 38.0


def test_report_totals_empty():
    totals = rpt.report_totals([])
    assert totals["total_employees"] == 0
    assert totals["total_overtime_hours"] == 0.0


def test_report_totals_none_safe():
    # Missing keys must default to 0, not raise.
    rows = [{"employee": "E1"}]
    totals = rpt.report_totals(rows)
    assert totals["total_employees"] == 1
    assert totals["total_present_days"] == 0.0
    assert totals["total_late_minutes"] == 0


# --------------------------------------------------------------------------- #
# End-to-end shape (helpers compose → endpoint payload shape)
# --------------------------------------------------------------------------- #
def test_build_then_totals_end_to_end():
    ws = [
        _ws("E1", employee_name="Anna", work_date="2026-06-01", regular=8.0, late=10),
        _ws("E1", employee_name="Anna", work_date="2026-06-02", regular=8.0, ot_normal=2.0),
        _ws("E2", employee_name="Ben", work_date="2026-06-01", regular=8.0, absent=1, payable_day=0),
    ]
    leaves = [_leave("E2", total_leave_days=2.0, is_lwp=1, employee_name="Ben")]
    rows = rpt.build_employee_report(ws, leaves)
    totals = rpt.report_totals(rows)
    assert totals["total_employees"] == 2
    # E1 present 2, E2 absent (0 present) → 2.
    assert totals["total_present_days"] == 2.0
    assert totals["total_unpaid_leave_days"] == 2.0
    assert totals["total_overtime_hours"] == 2.0
    assert totals["total_late_minutes"] == 10


# --------------------------------------------------------------------------- #
# Bench-loader field guard (_existing_fields) — avoids OperationalError when
# a DocType lacks a column the loader would otherwise SELECT (e.g. stock
# Leave Application has no ``branch`` column).
# --------------------------------------------------------------------------- #
def test_existing_fields_no_bench_returns_all():
    # Outside a bench report.frappe is None → every candidate is kept so the
    # pure tests still exercise the full field set.
    candidates = ["name", "employee", "branch", "leave_type"]
    assert rpt._existing_fields("Leave Application", candidates) == candidates


def test_existing_fields_filters_to_meta(monkeypatch):
    class _Meta:
        _present = {"name", "employee", "employee_name", "leave_type"}

        def has_field(self, fieldname):
            return fieldname in self._present

    class _FakeFrappe:
        @staticmethod
        def get_meta(doctype):
            return _Meta()

    monkeypatch.setattr(rpt, "frappe", _FakeFrappe())
    result = rpt._existing_fields(
        "Leave Application",
        ["name", "employee", "employee_name", "department", "branch", "leave_type"],
    )
    # department / branch are dropped (not on meta); name always kept.
    assert "name" in result
    assert "employee" in result
    assert "leave_type" in result
    assert "branch" not in result
    assert "department" not in result


def test_existing_fields_meta_failure_returns_all(monkeypatch):
    # If the meta cannot be loaded, fall back to the full list (defensive).
    class _FakeFrappe:
        @staticmethod
        def get_meta(doctype):
            raise RuntimeError("boom")

    monkeypatch.setattr(rpt, "frappe", _FakeFrappe())
    candidates = ["name", "employee", "branch"]
    assert rpt._existing_fields("Leave Application", candidates) == candidates


def test_existing_fields_meta_none_returns_all(monkeypatch):
    class _FakeFrappe:
        @staticmethod
        def get_meta(doctype):
            return None

    monkeypatch.setattr(rpt, "frappe", _FakeFrappe())
    candidates = ["name", "employee", "branch"]
    assert rpt._existing_fields("Leave Application", candidates) == candidates
