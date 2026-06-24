"""Unit tests for the Payroll Review pure helpers (utils/payroll.py).

Bench-free: the pure functions take plain dicts and need no ``frappe``. The
bench loaders are exercised via a lightweight fake ``frappe`` injected into
``sys.modules`` (same pattern as ``test_payroll_mapping.py``).
"""

import sys
import types

import pytest

from gege_hr.gege_hr.utils import payroll as P


# --------------------------------------------------------------------------- #
# aggregate_work_sessions
# --------------------------------------------------------------------------- #
def test_aggregate_sums_per_type_hours():
    rows = [
        {
            "payable_day": 1.0,
            "regular_hours": 8.0,
            "overtime_normal_hours": 2.0,
            "overtime_night_hours": 1.0,
            "late_minutes": 15,
        },
        {"payable_day": 0.5, "regular_hours": 4.0, "overtime_holiday_hours": 3.0, "regular_night_hours": 2.0},
    ]
    agg = P.aggregate_work_sessions(rows)
    assert agg["payable_days"] == 1.5
    assert agg["regular_hours"] == 12.0
    assert agg["overtime_normal_hours"] == 2.0
    assert agg["overtime_night_hours"] == 1.0
    assert agg["overtime_holiday_hours"] == 3.0
    assert agg["overtime_hours"] == 6.0
    assert agg["regular_night_hours"] == 2.0
    assert agg["late_minutes"] == 15.0
    assert agg["session_count"] == 2


def test_aggregate_falls_back_to_raw_overtime():
    rows = [{"payable_day": 1.0, "raw_overtime_hours": 5.0}]
    agg = P.aggregate_work_sessions(rows)
    assert agg["overtime_normal_hours"] == 5.0
    assert agg["overtime_hours"] == 5.0


def test_aggregate_empty():
    agg = P.aggregate_work_sessions([])
    assert agg["payable_days"] == 0.0
    assert agg["overtime_hours"] == 0.0
    assert agg["session_count"] == 0


def test_aggregate_counts_absent():
    rows = [{"absent": 1}, {"absent": 0}, {"absent": 1}]
    agg = P.aggregate_work_sessions(rows)
    assert agg["absent_days"] == 2


# --------------------------------------------------------------------------- #
# compute_line
# --------------------------------------------------------------------------- #
def test_compute_line_full_month_no_ot():
    agg = P.aggregate_work_sessions([{"payable_day": 26.0, "regular_hours": 208.0}])
    line = P.compute_line(agg, base_salary=10_000_000)
    assert line["base_salary"] == 10_000_000
    # Full month → proportional base == base salary, nothing deducted.
    assert line["gross_pay"] == 10_000_000
    assert line["overtime_amount"] == 0
    assert line["net_pay"] == 10_000_000
    assert line["total_deduction"] == 0
    assert line["unpaid_leave_deduction"] == 0


def test_compute_line_overtime_amount():
    # 2 normal OT hours, base 10M / 208h = ~48076.92/h × 1.5 = ~72115.38
    agg = P.aggregate_work_sessions(
        [{"payable_day": 26.0, "regular_hours": 208.0, "overtime_normal_hours": 2.0}]
    )
    line = P.compute_line(agg, base_salary=10_400_000)  # 50k/h for clean math
    # 2 × 50000 × 1.5 = 150000
    assert line["overtime_amount"] == 150000
    assert line["gross_pay"] == 10_400_000 + 150000


def test_compute_line_segment_multiplier_override():
    agg = P.aggregate_work_sessions(
        [{"payable_day": 26.0, "regular_hours": 208.0, "overtime_night_hours": 4.0}]
    )
    cfg = P.default_config()
    cfg["segment_multipliers"] = {"OT Night": 3.0}
    line = P.compute_line(agg, base_salary=10_400_000, config=cfg)
    # 4 × 50000 × 3.0 = 600000
    assert line["overtime_amount"] == 600000


def test_compute_line_night_allowance():
    agg = P.aggregate_work_sessions(
        [{"payable_day": 26.0, "regular_hours": 208.0, "regular_night_hours": 10.0}]
    )
    cfg = P.default_config()
    cfg["night_allowance_rate"] = 20_000  # per night hour
    line = P.compute_line(agg, base_salary=10_400_000, config=cfg)
    assert line["night_allowance_amount"] == 200000


def test_compute_line_late_penalty_and_unpaid():
    # 5 unpaid days (26-21) + 60 late minutes.
    agg = P.aggregate_work_sessions([{"payable_day": 21.0, "regular_hours": 168.0, "late_minutes": 60}])
    cfg = P.default_config()
    cfg["late_penalty_rate"] = 5_000  # per minute
    line = P.compute_line(agg, base_salary=10_400_000, config=cfg)
    daily = 10_400_000 / 26
    assert line["unpaid_leave_deduction"] == round(5 * daily, 2)
    assert line["late_penalty_amount"] == 300_000
    assert line["total_deduction"] == round(5 * daily, 2) + 300_000
    assert line["net_pay"] == round((10_400_000 / 26 * 21) - (5 * daily + 300_000), 2)


def test_compute_line_salary_advance_deduction():
    agg = P.aggregate_work_sessions([{"payable_day": 26.0, "regular_hours": 208.0}])
    cfg = P.default_config()
    cfg["salary_advance_deduction"] = 2_000_000
    line = P.compute_line(agg, base_salary=10_000_000, config=cfg)
    assert line["salary_advance_deduction"] == 2_000_000
    assert line["total_deduction"] == 2_000_000
    assert line["net_pay"] == 8_000_000


def test_compute_line_net_never_negative_clamped():
    # Huge deduction beyond gross → net goes negative (we do NOT clamp; the
    # caller decides policy). Verify the arithmetic is honest.
    agg = P.aggregate_work_sessions([{"payable_day": 5.0, "regular_hours": 40.0}])
    cfg = P.default_config()
    cfg["other_deduction"] = 99_999_999
    line = P.compute_line(agg, base_salary=1_000_000, config=cfg)
    assert line["net_pay"] < 0


def test_hourly_rate_zero_standard_hours():
    assert P.hourly_rate(10_000_000, 0) == 0.0


# --------------------------------------------------------------------------- #
# rollup_lines
# --------------------------------------------------------------------------- #
def test_rollup_sums_totals():
    lines = [
        {"gross_pay": 10_000, "total_deduction": 1_000, "net_pay": 9_000},
        {"gross_pay": 5_000, "total_deduction": 500, "net_pay": 4_500},
    ]
    totals = P.rollup_lines(lines)
    assert totals == {
        "total_employees": 2,
        "total_gross_pay": 15_000,
        "total_deductions": 1_500,
        "total_net_pay": 13_500,
    }


def test_rollup_empty():
    totals = P.rollup_lines([])
    assert totals["total_employees"] == 0
    assert totals["total_gross_pay"] == 0


# --------------------------------------------------------------------------- #
# Bench loaders — fake frappe
# --------------------------------------------------------------------------- #
class _FakeDB:
    def __init__(self):
        self._store = {}
        self._exists = {}

    def register(self, doctype, rows):
        self._store[doctype] = list(rows)

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        rows = self._store.get(doctype, [])
        out = []
        for r in rows:
            ok = True
            for k, v in (filters or {}).items():
                if isinstance(v, list):
                    op, val = v[0], v[1]
                    rv = r.get(k)
                    if op == "in" and rv not in val:
                        ok = False
                else:
                    if r.get(k) != v:
                        ok = False
                if not ok:
                    break
            if ok:
                out.append({f: r.get(f) for f in (fields or r.keys())})
        return out

    def _matches(self, r, filters):
        for k, v in (filters or {}).items():
            rv = r.get(k)
            if isinstance(v, list):
                op, val = v[0], v[1]
                # Coerce to strings so dates (str vs date) compare cleanly.
                sval = str(val) if not isinstance(val, str) else val
                srv = str(rv) if rv is not None and not isinstance(rv, str) else rv
                if op == "<=" and not (srv is not None and srv <= sval):
                    return False
                if op == ">=" and not (srv is not None and srv >= sval):
                    return False
                if op == "in" and rv not in val:
                    return False
            else:
                if rv != v:
                    return False
        return True

    def get_value(self, doctype, name, *args, **kwargs):
        rows = self._store.get(doctype, [])
        # ``name`` may be a filter dict (dict-based get_value) or a pk string.
        if isinstance(name, dict):
            rows = [r for r in rows if self._matches(r, name)]
        # Apply order_by when present (e.g. "from_date desc").
        order_by = kwargs.get("order_by")
        if order_by:
            field = order_by.split()[0]
            rows = sorted(rows, key=lambda r: r.get(field) or "", reverse="desc" in order_by)
        for r in rows:
            if isinstance(name, dict) or r.get("name") == name or r.get("employee") == name:
                if not args:
                    return r
                if isinstance(args[0], str):
                    return r.get(args[0])
                return {f: r.get(f) for f in args[0]}
        return None


@pytest.fixture
def fake_frappe(monkeypatch):
    ff = types.SimpleNamespace(db=_FakeDB())
    monkeypatch.setitem(sys.modules, "frappe", ff)
    return ff


def test_load_segment_multipliers(fake_frappe):
    fake_frappe.db.register(
        "VN Payroll Component Mapping",
        [
            {"segment_type": "OT", "day_type": "All", "multiplier": 1.5, "is_active": 1, "company": "C1"},
            {
                "segment_type": "OT Night",
                "day_type": "All",
                "multiplier": 2.7,
                "is_active": 1,
                "company": "C1",
            },
            {
                "segment_type": "OT Holiday",
                "day_type": "Weekday",
                "multiplier": 9,
                "is_active": 1,
                "company": "C1",
            },
            {"segment_type": "OT", "day_type": "All", "multiplier": 2.0, "is_active": 1, "company": "OTHER"},
        ],
    )
    P.frappe = fake_frappe
    try:
        out = P.load_segment_multipliers("C1")
        assert out == {"OT": 1.5, "OT Night": 2.7}
    finally:
        P.frappe = None


def test_load_component_map_specific_before_all(fake_frappe):
    fake_frappe.db.register(
        "VN Payroll Component Mapping",
        [
            {
                "segment_type": "OT",
                "day_type": "All",
                "salary_component": "OT-ALL",
                "is_active": 1,
                "company": "C1",
            },
            {
                "segment_type": "OT",
                "day_type": "Weekend",
                "salary_component": "OT-WE",
                "is_active": 1,
                "company": "C1",
            },
            {
                "segment_type": "Regular",
                "day_type": "All",
                "salary_component": "BASIC",
                "is_active": 1,
                "company": "C1",
            },
        ],
    )
    P.frappe = fake_frappe
    try:
        out = P.load_component_map("C1")
        # Weekend sorts before All (alphabetically), so OT-WE wins.
        assert out["OT"] == "OT-WE"
        assert out["Regular"] == "BASIC"
    finally:
        P.frappe = None


def test_resolve_base_salary(fake_frappe):
    fake_frappe.db.register(
        "Salary Structure Assignment",
        [{"employee": "EMP-1", "docstatus": 1, "from_date": "2026-01-01", "base": 12_000_000}],
    )
    # Patch getdate so the lookup matches.
    import frappe as fake  # the injected module

    fake.utils = types.SimpleNamespace(
        getdate=lambda d=None: __import__("datetime").date(2026, 6, 1),
        today=lambda: __import__("datetime").date(2026, 6, 1),
    )
    P.frappe = fake
    try:
        assert P.resolve_base_salary("EMP-1") == 12_000_000
    finally:
        P.frappe = None


def test_employee_advance_deductions(fake_frappe):
    fake_frappe.db.register(
        "VN Salary Advance Request",
        [
            {
                "employee": "EMP-1",
                "approved_amount": 1_000_000,
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-06-01",
                "docstatus": 1,
            },
            {
                "employee": "EMP-1",
                "approved_amount": 500_000,
                "company": "C1",
                "workflow_state": "Approved",
                "posting_date": "2026-06-10",
                "docstatus": 0,
            },
            {
                "employee": "EMP-2",
                "approved_amount": 2_000_000,
                "company": "C1",
                "workflow_state": "Rejected",
                "posting_date": "2026-06-05",
                "docstatus": 0,
            },
        ],
    )
    P.frappe = fake_frappe
    try:
        out = P.employee_advance_deductions("C1", ["EMP-1", "EMP-2"], "2026-06-01", "2026-06-30")
        assert out == {"EMP-1": 1_500_000}
    finally:
        P.frappe = None


def test_loaders_safe_without_frappe(monkeypatch):
    monkeypatch.setattr(P, "frappe", None)
    assert P.load_segment_multipliers("C1") == {}
    assert P.load_component_map("C1") == {}
    assert P.resolve_base_salary("EMP-1") == 0.0
    assert P.employee_advance_deductions("C1", ["EMP-1"], "2026-01-01", "2026-01-31") == {}
