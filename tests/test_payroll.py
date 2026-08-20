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


def test_compute_hourly_line_exposes_advance_deduction_key():
    """E2E bug 2026-08-20 — compute_hourly_line subtracted the advance from
    net/total_deduction but never RETURNED the key, so the review-line /
    payslip field stayed 0 while the money was deducted (invisible advance)."""
    line = P.compute_hourly_line(
        bracket_hours={1.0: 208.0},
        hourly_rate=50_000,
        deduction_rates={},
        late_penalty=100_000,
        checkout_miss_penalty=0,
        salary_advance_deduction=2_000_000,
    )
    assert line["salary_advance_deduction"] == 2_000_000
    assert line["total_deduction"] == 2_100_000
    assert line["net_pay"] == line["gross_pay"] - 2_100_000  # 10.4M − 2.1M


# --------------------------------------------------------------------------- #
# Checkout-miss penalty (GĐ2 — folds into total_deduction / net_pay)
# --------------------------------------------------------------------------- #
def test_compute_line_checkout_miss_penalty_added_to_deduction():
    # B9: config checkout_miss_penalty increases total_deduction + reduces net.
    agg = P.aggregate_work_sessions([{"payable_day": 26.0, "regular_hours": 208.0}])
    line_base = P.compute_line(agg, base_salary=10_000_000, config=P.default_config())
    cfg = P.default_config()
    cfg["checkout_miss_penalty"] = 50_000
    line = P.compute_line(agg, base_salary=10_000_000, config=cfg)
    assert line["checkout_miss_penalty"] == 50_000
    assert line["total_deduction"] == line_base["total_deduction"] + 50_000
    assert line["net_pay"] == line_base["net_pay"] - 50_000


def test_compute_line_checkout_miss_penalty_default_zero():
    # B10: absent config → 0, total_deduction unchanged.
    agg = P.aggregate_work_sessions([{"payable_day": 26.0, "regular_hours": 208.0}])
    line = P.compute_line(agg, base_salary=10_000_000)
    assert line["checkout_miss_penalty"] == 0
    assert line["total_deduction"] == 0


def test_compute_hourly_line_checkout_miss_penalty():
    # Active payroll path: penalty folded into total_deduction + net.
    base = P.compute_hourly_line({1.0: 160.0}, hourly_rate=50_000, deduction_rates={"BHXH": 8})
    out = P.compute_hourly_line(
        {1.0: 160.0},
        hourly_rate=50_000,
        deduction_rates={"BHXH": 8},
        checkout_miss_penalty=50_000,
    )
    assert out["checkout_miss_penalty"] == 50_000
    assert out["total_deduction"] == base["total_deduction"] + 50_000
    assert out["net_pay"] == base["net_pay"] - 50_000


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
        out = [r for r in rows if self._matches(r, filters)]
        return [{f: r.get(f) for f in (fields or r.keys())} for r in out]

    def count(self, doctype, filters=None, **_kw):
        return len(self.get_all(doctype, filters=filters))

    @staticmethod
    def _cmp(v):
        """Comparable key: numeric when possible, else string (ISO dates sort)."""
        try:
            return (0, float(v))
        except (TypeError, ValueError):
            return (1, str(v))

    def _matches(self, r, filters):
        for k, v in (filters or {}).items():
            rv = r.get(k)
            if isinstance(v, list):
                op, val = v[0], v[1]
                if op == "between":
                    lo, hi = val
                    if not (self._cmp(lo) <= self._cmp(rv) <= self._cmp(hi)):
                        return False
                    continue
                if op in ("<", "<=", ">", ">="):
                    a, b = self._cmp(rv), self._cmp(val)
                    if op == "<" and not a < b:
                        return False
                    if op == "<=" and not a <= b:
                        return False
                    if op == ">" and not a > b:
                        return False
                    if op == ">=" and not a >= b:
                        return False
                    continue
                # Coerce to strings so dates (str vs date) compare cleanly.
                sval = str(val) if not isinstance(val, str) else val
                srv = str(rv) if rv is not None and not isinstance(rv, str) else rv
                if op == "in" and rv not in val:
                    return False
                if op == "not in" and rv in val:
                    return False
                if op == "!=" and srv == sval:
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


def test_employee_advance_deductions_period_boundaries(fake_frappe):
    """TC-E3/E4/E5 — advance posted in period P is deducted from P only.

    Requests on the window edges (01/07 and 31/07) hit the July period; a
    request posted 01/08 belongs to August — July must NOT pick it up (no
    re-deduction, and no leakage across periods).
    """
    fake_frappe.db.register(
        "VN Salary Advance Request",
        [
            {
                "employee": "EMP-1",
                "approved_amount": 500_000,
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-07-01",
                "docstatus": 1,
            },
            {
                "employee": "EMP-1",
                "approved_amount": 700_000,
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-07-31",
                "docstatus": 1,
            },
            {
                "employee": "EMP-2",
                "approved_amount": 900_000,
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-08-01",
                "docstatus": 1,
            },
        ],
    )
    P.frappe = fake_frappe
    try:
        assert P.employee_advance_deductions("C1", ["EMP-1", "EMP-2"], "2026-07-01", "2026-07-31") == {
            "EMP-1": 1_200_000
        }
        assert P.employee_advance_deductions("C1", ["EMP-1", "EMP-2"], "2026-08-01", "2026-08-31") == {
            "EMP-2": 900_000
        }
    finally:
        P.frappe = None


def test_employee_advance_deductions_excludes_closed_states(fake_frappe):
    """TC-E7 — only Approved/Paid in-window rows count; Rejected / Cancelled /
    doc-cancelled (docstatus 2) never reach the payslip."""
    fake_frappe.db.register(
        "VN Salary Advance Request",
        [
            {
                "employee": "EMP-1",
                "approved_amount": 1_000_000,
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-07-10",
                "docstatus": 1,
            },
            {
                "employee": "EMP-1",
                "approved_amount": 300_000,
                "company": "C1",
                "workflow_state": "Rejected",
                "posting_date": "2026-07-12",
                "docstatus": 0,
            },
            {
                "employee": "EMP-1",
                "approved_amount": 250_000,
                "company": "C1",
                "workflow_state": "Cancelled",
                "posting_date": "2026-07-14",
                "docstatus": 2,
            },
            {
                "employee": "EMP-2",
                "approved_amount": 400_000,
                "company": "C2",
                "workflow_state": "Paid",
                "posting_date": "2026-07-15",
                "docstatus": 1,
            },
        ],
    )
    P.frappe = fake_frappe
    try:
        assert P.employee_advance_deductions("C1", ["EMP-1"], "2026-07-01", "2026-07-31") == {"EMP-1": 1_000_000}
    finally:
        P.frappe = None


def test_pending_advance_requests_blocks_only_approved_in_window(fake_frappe):
    """TC-G1/G2/G3 — payroll calc blocks on Approved requests in the period:
    out-of-window, Paid and doc-cancelled rows never block; other companies
    are not counted."""
    fake_frappe.db.register(
        "VN Salary Advance Request",
        [
            {
                "company": "C1",
                "workflow_state": "Approved",
                "posting_date": "2026-07-05",
                "docstatus": 0,
            },
            {
                "company": "C1",
                "workflow_state": "Approved",
                "posting_date": "2026-06-20",
                "docstatus": 0,
            },
            {
                "company": "C1",
                "workflow_state": "Paid",
                "posting_date": "2026-07-10",
                "docstatus": 1,
            },
            {
                "company": "C1",
                "workflow_state": "Approved",
                "posting_date": "2026-07-15",
                "docstatus": 2,
            },
            {
                "company": "C2",
                "workflow_state": "Approved",
                "posting_date": "2026-07-15",
                "docstatus": 0,
            },
        ],
    )
    P.frappe = fake_frappe
    try:
        assert P.pending_advance_requests("C1", "2026-07-01", "2026-07-31") == 1
        assert P.pending_advance_requests("C1", "2026-08-01", "2026-08-31") == 0
    finally:
        P.frappe = None


def test_period_has_adjustment_requests_reopen_signal(fake_frappe):
    """L5 — Requested slips are the reopen signal for a Published period."""
    fake_frappe.db.register(
        "Salary Slip",
        [
            {"vn_payroll_review_period": "PRP-P", "vn_ack_status": "Requested"},
            {"vn_payroll_review_period": "PRP-P", "vn_ack_status": "Requested"},
            {"vn_payroll_review_period": "PRP-Q", "vn_ack_status": "Awaiting Payment"},
            {"vn_payroll_review_period": "PRP-R", "vn_ack_status": ""},
        ],
    )
    P.frappe = fake_frappe
    try:
        assert P.period_has_adjustment_requests("PRP-P") == 2
        assert P.period_has_adjustment_requests("PRP-Q") == 0  # locks ≠ adjustment
        assert P.period_has_adjustment_requests("PRP-R") == 0
    finally:
        P.frappe = None


def test_period_ack_progress_counts_visible_only(fake_frappe):
    """Plan v2 badge source: visible slips only; confirmed==total ⇒ auto-lock."""
    fake_frappe.db.register(
        "Salary Slip",
        [
            {"vn_payroll_review_period": "PRP-B", "vn_employee_visible": 1, "vn_ack_status": "Awaiting Payment", "docstatus": 0},
            {"vn_payroll_review_period": "PRP-B", "vn_employee_visible": 1, "vn_ack_status": "Paid", "docstatus": 0},
            {"vn_payroll_review_period": "PRP-B", "vn_employee_visible": 1, "vn_ack_status": "", "docstatus": 0},
            {"vn_payroll_review_period": "PRP-B", "vn_employee_visible": 1, "vn_ack_status": "Requested", "docstatus": 0},
            {"vn_payroll_review_period": "PRP-B", "vn_employee_visible": 0, "vn_ack_status": "Paid", "docstatus": 0},  # withheld → ignored
        ],
    )
    P.frappe = fake_frappe
    try:
        out = P.period_ack_progress("PRP-B")
        assert out == {"total": 4, "confirmed": 2, "requested": 1}
    finally:
        P.frappe = None


def test_period_ack_progress_safe_without_frappe(monkeypatch):
    monkeypatch.setattr(P, "frappe", None)
    assert P.period_ack_progress("PRP-X") == {"total": 0, "confirmed": 0, "requested": 0}


def test_period_has_confirmed_slips_lock_rule(fake_frappe):
    """L1/L3 — decision #2: Awaiting Payment / Paid lock the period;
    Requested / empty do NOT (that's the adjustment loop)."""
    fake_frappe.db.register(
        "Salary Slip",
        [
            {"vn_payroll_review_period": "PRP-1", "vn_ack_status": "Awaiting Payment"},
            {"vn_payroll_review_period": "PRP-1", "vn_ack_status": ""},
            {"vn_payroll_review_period": "PRP-1", "vn_ack_status": "Requested"},
            {"vn_payroll_review_period": "PRP-2", "vn_ack_status": ""},
            {"vn_payroll_review_period": "PRP-2", "vn_ack_status": "Paid"},
        ],
    )
    P.frappe = fake_frappe
    try:
        assert P.period_has_confirmed_slips("PRP-1") == 1  # only Awaiting locks
        assert P.period_has_confirmed_slips("PRP-2") == 1  # Paid locks
        assert P.period_has_confirmed_slips("PRP-3") == 0  # unknown period
    finally:
        P.frappe = None


def test_period_lock_safe_without_frappe(monkeypatch):
    monkeypatch.setattr(P, "frappe", None)
    assert P.period_has_confirmed_slips("PRP-1") == 0


def test_pending_advance_requests_safe_without_frappe(monkeypatch):
    monkeypatch.setattr(P, "frappe", None)
    assert P.pending_advance_requests("C1", "2026-07-01", "2026-07-31") == 0


def test_loaders_safe_without_frappe(monkeypatch):
    monkeypatch.setattr(P, "frappe", None)
    assert P.load_segment_multipliers("C1") == {}
    assert P.load_component_map("C1") == {}
    assert P.resolve_base_salary("EMP-1") == 0.0
    assert P.employee_advance_deductions("C1", ["EMP-1"], "2026-01-01", "2026-01-31") == {}
    assert P.load_checkout_miss_penalty("EMP-1", "2026-01-01", "2026-01-31") == 0.0


def test_load_checkout_miss_penalty_sums_penalised(fake_frappe):
    fake_frappe.db.register(
        "VN Checkout Miss",
        [
            {"employee": "EMP-1", "status": "Penalised", "penalty_waived": 0, "docstatus": 1,
             "work_date": "2026-06-05", "penalty_amount": 100_000},
            {"employee": "EMP-1", "status": "Penalised", "penalty_waived": 0, "docstatus": 1,
             "work_date": "2026-06-20", "penalty_amount": 100_000},
            # excluded: waived
            {"employee": "EMP-1", "status": "Penalised", "penalty_waived": 1, "docstatus": 1,
             "work_date": "2026-06-10", "penalty_amount": 100_000},
            # excluded: still Pending
            {"employee": "EMP-1", "status": "Pending", "penalty_waived": 0, "docstatus": 1,
             "work_date": "2026-06-12", "penalty_amount": 100_000},
            # excluded: other employee
            {"employee": "EMP-2", "status": "Penalised", "penalty_waived": 0, "docstatus": 1,
             "work_date": "2026-06-12", "penalty_amount": 100_000},
        ],
    )
    P.frappe = fake_frappe
    try:
        total = P.load_checkout_miss_penalty("EMP-1", "2026-06-01", "2026-06-30")
        assert total == 200_000.0
    finally:
        P.frappe = None
