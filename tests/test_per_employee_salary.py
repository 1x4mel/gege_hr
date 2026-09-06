"""PS1–PS13 — per-employee salary plan (plan per-employee-salary §6.1).

Covers the pure-engine layer of the two payroll modes:

* ``resolve_hourly_rate_detail`` chain  Employee → Department → portal default
* ``resolve_payroll_setting``           mode + rate + monthly base
* ``build_monthly_agg``                 F-LC13 summary × WS OT-split merge
* ``compute_line`` keyword extensions   late_penalty override / % deductions /
                                        allowances / extra deductions — with a
  regression lock (PS11) proving the legacy formula is untouched when the new
  keyword params are omitted.

Bench-free: same fake-``frappe``-into-``sys.modules`` pattern as
``test_payroll.py``.
"""

import sys
import types

import pytest

from gege_hr.gege_hr.utils import payroll as P


# --------------------------------------------------------------------------- #
# Fake frappe (compact — only what the resolvers/loaders touch)
# --------------------------------------------------------------------------- #
class _FakeDB:
    def __init__(self):
        self._store: dict[str, list[dict]] = {}
        self._singles: dict[str, object] = {}

    def register(self, doctype, rows):
        self._store.setdefault(doctype, []).extend(rows)

    def set_single(self, key, value):
        self._singles[key] = value

    @staticmethod
    def _matches(row, filters):
        import datetime as _dt

        for k, v in filters.items():
            rv = row.get(k)
            if isinstance(v, list):
                op, val = v[0], v[1]
                sval = str(val) if not isinstance(val, str) else val
                srv = str(rv) if rv is not None and not isinstance(rv, str) else rv
                if op == "<=":
                    # ISO strings compare lexicographically — fine for dates.
                    if srv is None or srv > sval:
                        return False
                if op == "in" and rv not in val:
                    return False
            elif rv != v:
                return False
        return True

    def get_value(self, doctype, name, *args, **kwargs):
        rows = self._store.get(doctype, [])
        if isinstance(name, dict):
            rows = [r for r in rows if self._matches(r, name)]
        else:
            rows = [r for r in rows if r.get("name") == name or r.get("employee") == name]
        order_by = kwargs.get("order_by")
        if order_by:
            field = order_by.split()[0]
            rows = sorted(rows, key=lambda r: str(r.get(field) or ""), reverse="desc" in order_by)
        for r in rows:
            if not args:
                return r
            if isinstance(args[0], str):
                return r.get(args[0])
            return {f: r.get(f) for f in args[0]}
        return None

    def get_all(self, doctype, filters=None, fields=None, **_kw):
        rows = self._store.get(doctype, [])
        if filters:
            rows = [r for r in rows if self._matches(r, filters)]
        return [{f: r.get(f) for f in (fields or [])} for r in rows]

    def get_single_value(self, _doctype, field):
        return self._singles.get(field)


@pytest.fixture
def fake_frappe(monkeypatch):
    ff = types.SimpleNamespace(db=_FakeDB())
    ff.utils = types.SimpleNamespace(
        getdate=lambda d=None: __import__("datetime").date(2026, 6, 1),
        today=lambda: __import__("datetime").date(2026, 6, 1),
    )
    monkeypatch.setitem(sys.modules, "frappe", ff)
    P.frappe = ff
    yield ff
    P.frappe = None


# --------------------------------------------------------------------------- #
# PS1–PS5 — resolver chain
# --------------------------------------------------------------------------- #
def test_ps1_employee_rate_wins(fake_frappe):
    fake_frappe.db.register(
        "Employee", [{"name": "EMP-1", "department": "D1", "vn_hourly_rate": 30000}]
    )
    fake_frappe.db.register("Department", [{"name": "D1", "vn_hourly_rate": 25000}])
    rate, source = P.resolve_hourly_rate_detail("EMP-1")
    assert rate == 30000
    assert source == "employee"
    assert P.resolve_hourly_rate("EMP-1") == 30000  # float wrapper


def test_ps2_department_fallback(fake_frappe):
    fake_frappe.db.register(
        "Employee", [{"name": "EMP-1", "department": "D1", "vn_hourly_rate": 0}]
    )
    fake_frappe.db.register("Department", [{"name": "D1", "vn_hourly_rate": 25000}])
    rate, source = P.resolve_hourly_rate_detail("EMP-1")
    assert rate == 25000
    assert source == "department"


def test_ps3_default_fallback(fake_frappe):
    fake_frappe.db.register("Employee", [{"name": "EMP-1", "department": "D1"}])
    fake_frappe.db.register("Department", [{"name": "D1", "vn_hourly_rate": 0}])
    fake_frappe.db.set_single("vn_default_hourly_rate", 22000)
    rate, source = P.resolve_hourly_rate_detail("EMP-1")
    assert rate == 22000
    assert source == "default"
    # And the hard default when the portal setting is missing too.
    fake_frappe.db._singles.clear()
    assert P.resolve_hourly_rate_detail("EMP-1") == (20000.0, "default")


def test_ps4_mode_normalisation(fake_frappe):
    for mode, expected in (("Monthly", "Monthly"), ("", "Hourly"), ("Junk", "Hourly")):
        fake_frappe.db._store["Employee"] = [{"name": "EMP-1", "vn_payroll_mode": mode}]
        assert P.resolve_payroll_setting("EMP-1")["mode"] == expected


def test_ps5_monthly_base_from_ssa(fake_frappe):
    fake_frappe.db.register(
        "Salary Structure Assignment",
        [
            {"employee": "EMP-1", "docstatus": 1, "from_date": "2026-01-01", "base": 10_000_000},
            {"employee": "EMP-1", "docstatus": 1, "from_date": "2026-07-01", "base": 20_000_000},
        ],
    )
    # As of 2026-06-01 the future SSA must NOT apply — 10M wins.
    assert P.resolve_base_salary("EMP-1", "2026-06-01") == 10_000_000
    fake_frappe.db._store["Employee"] = [{"name": "EMP-1", "vn_payroll_mode": "Monthly"}]
    setting = P.resolve_payroll_setting("EMP-1", "2026-06-01")
    assert setting["monthly_base"] == 10_000_000
    # No SSA at all → 0.0 (pre-flight treats ≤0 as "missing base").
    assert P.resolve_payroll_setting("EMP-2")["monthly_base"] == 0.0


# --------------------------------------------------------------------------- #
# PS6–PS7 — build_monthly_agg
# --------------------------------------------------------------------------- #
def _sample_agg():
    return {
        "payable_days": 24.0,
        "regular_hours": 180.0,
        "regular_night_hours": 6.0,
        "overtime_normal_hours": 10.0,
        "overtime_night_hours": 4.0,
        "overtime_holiday_hours": 2.0,
        "late_minutes": 25.0,
    }


def test_ps6_merge_prefers_real_summary():
    out = P.build_monthly_agg(_sample_agg(), {"payable_days": 26.0, "regular_hours": 208.0})
    assert out["payable_days"] == 26.0  # F-LC13 REAL days (incl. leave-only)
    assert out["regular_hours"] == 208.0
    assert out["overtime_normal_hours"] == 10.0  # OT split kept from the WS agg
    assert out["overtime_night_hours"] == 4.0
    assert out["overtime_holiday_hours"] == 2.0
    assert out["regular_night_hours"] == 6.0
    assert out["late_minutes"] == 25.0


def test_ps7_empty_summary_falls_back_to_agg():
    out = P.build_monthly_agg(_sample_agg(), None)
    assert out["payable_days"] == 24.0
    assert out["regular_hours"] == 180.0
    assert out["overtime_normal_hours"] == 10.0


# --------------------------------------------------------------------------- #
# PS8–PS13 — compute_line keyword extensions
# --------------------------------------------------------------------------- #
def _agg(**over):
    base = {
        "payable_days": 26.0,
        "regular_hours": 208.0,
        "regular_night_hours": 0.0,
        "overtime_normal_hours": 10.0,
        "overtime_night_hours": 0.0,
        "overtime_holiday_hours": 0.0,
        "late_minutes": 0.0,
    }
    base.update(over)
    return base


_GROSS_10M = 10_000_000 + 10 * (10_000_000 / 208) * 1.5  # proportional + OT×1.5


def test_ps11_regression_lock_legacy_formula_unchanged():
    """THE regression lock: no keyword params ⇒ EXACT legacy numbers."""
    out = P.compute_line(_agg(), 10_000_000)
    assert out["base_salary"] == 10_000_000
    assert out["allowance_amount"] == 0.0
    assert out["gross_pay"] == round(_GROSS_10M, 2)
    assert out["total_deduction"] == 0.0
    assert out["net_pay"] == round(_GROSS_10M, 2)
    assert out["unpaid_leave_deduction"] == 0.0


def test_ps8_late_penalty_override():
    # late_minutes=30 with the default 0/min rate would contribute 0 — the
    # explicit override must win (tiered engine computed it upstream).
    out = P.compute_line(_agg(late_minutes=30), 10_000_000, late_penalty=50000)
    assert out["late_penalty_amount"] == 50000
    assert out["total_deduction"] == 50000
    assert out["net_pay"] == round(_GROSS_10M - 50000, 2)


def test_ps9_percentage_deductions():
    out = P.compute_line(_agg(), 10_000_000, deduction_rates={"BHXH": 8.0})
    assert out["total_deduction"] == round(_GROSS_10M * 0.08, 2)
    assert out["net_pay"] == round(_GROSS_10M * 0.92, 2)


def test_ps10_allowances_and_extra_deductions():
    out = P.compute_line(
        _agg(),
        10_000_000,
        allowances=[500_000, 200_000],
        extra_deductions=[100_000],
    )
    assert out["allowance_amount"] == 700_000
    assert out["gross_pay"] == round(_GROSS_10M + 700_000, 2)
    assert out["other_deduction"] == 100_000
    assert out["net_pay"] == round(_GROSS_10M + 700_000 - 100_000, 2)


def test_ps12_payable_days_proration():
    out = P.compute_line(_agg(payable_days=20.0), 10_000_000)
    daily = 10_000_000 / 26
    assert out["base_salary"] == 10_000_000
    assert abs(out["gross_pay"] - (daily * 20 + 10 * (10_000_000 / 208) * 1.5)) < 1
    assert abs(out["unpaid_leave_deduction"] - 6 * daily) < 1


def test_ps13_full_monthly_flow_cross_check():
    """Hand-computed cross-check: base 10M, 26/26 days, 10h OT×1.5, 8% BHXH."""
    out = P.compute_line(
        _agg(),
        10_000_000,
        deduction_rates={"BHXH": 8.0},
        late_penalty=100_000,
    )
    gross = round(_GROSS_10M, 2)
    assert out["gross_pay"] == gross
    assert out["total_deduction"] == round(gross * 0.08 + 100_000, 2)
    assert out["net_pay"] == round(gross - gross * 0.08 - 100_000, 2)
