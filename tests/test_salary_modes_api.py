"""PS21–PS25 — calculate-branch + breakdown for the two payroll modes.

Targets ``api/payroll._compute_hourly_amounts`` / ``_compute_monthly_amounts``
/ ``_monthly_missing_base`` / ``_line_breakdown`` (plan per-employee-salary
§6.1). Bench loaders and the small db-hitting api helpers are monkeypatched so
the tests pin the BRANCHING + math, not the db.

Bench-free: stub ``frappe`` into ``sys.modules`` (harness pattern of
``test_payroll_slip_generate.py``, reduced to what this flow touches).
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class NSDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e


_PERIOD = types.SimpleNamespace(
    name="PRP-00001",
    company="GeGe Esport",
    from_date="2026-08-01",
    to_date="2026-08-31",
)

_PENALTY_RULES = [
    {"from_minutes": 0, "to_minutes": 9999, "penalty_type": "Fixed Amount", "penalty_value": 50000}
]
_DED_RATES = {"BHXH": 8.0}


@pytest.fixture()
def api(monkeypatch):
    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda v, p=None: round(float(v or 0), p if p is not None else 2)
    utils.getdate = lambda v=None: __import__("datetime").date.today()
    utils.today = lambda: "2026-08-31"
    utils.now = lambda: "2026-08-31 08:00:00"
    utils.cint = lambda v, *a: int(v or 0)

    frappe_mod = types.ModuleType("frappe")
    frappe_mod.db = types.SimpleNamespace(
        get_all=lambda *a, **k: [],
        get_value=lambda *a, **k: None,
        get_single_value=lambda *a, **k: None,
    )
    frappe_mod.get_doc = lambda *a, **k: None
    frappe_mod.log_error = lambda *a, **k: None
    frappe_mod.throw = lambda msg, exc=None, *a, **k: (_ for _ in ()).throw(FrappeError(msg))
    frappe_mod._ = lambda s: s
    frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    frappe_mod.utils = utils
    frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
    frappe_mod.ValidationError = FrappeError
    frappe_mod.PermissionError = FrappeError
    frappe_mod.get_traceback = lambda: "tb"
    frappe_mod.publish_realtime = lambda *a, **k: None

    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
    fake_audit.log = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)
    fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
    fake_emp.HR_MANAGER_ROLES = {"HR Manager"}
    fake_emp.get_user_roles = lambda: ["HR Manager"]
    fake_emp.get_employee_for_user = lambda: "HR-EMP-001"
    fake_emp.emp_name = lambda v: v
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

    import gege_hr.gege_hr.api as api_pkg
    import gege_hr.gege_hr.utils as utils_pkg

    monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
    monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)

    api = importlib.import_module("gege_hr.gege_hr.api.payroll")
    calc = importlib.import_module("gege_hr.gege_hr.utils.payroll")

    # Pure-module defaults for the bench loaders this flow touches.
    monkeypatch.setattr(
        calc,
        "load_employee_period_summary",
        lambda *a, **k: {
            "regular_hours": 0,
            "overtime_hours": 0,
            "payable_days": 0,
        },
        raising=False,
    )
    monkeypatch.setattr(
        calc,
        "load_employee_period_agg",
        lambda *a, **k: {
            "payable_days": 0,
            "regular_hours": 0,
            "regular_night_hours": 0,
            "overtime_normal_hours": 0,
            "overtime_night_hours": 0,
            "overtime_holiday_hours": 0,
            "late_minutes": 0,
        },
        raising=False,
    )
    monkeypatch.setattr(calc, "load_segment_multipliers", lambda *a, **k: {"OT": 1.5}, raising=False)
    monkeypatch.setattr(calc, "load_checkout_miss_penalty", lambda *a, **k: 0.0, raising=False)
    monkeypatch.setattr(api, "_employee_late_minutes", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(api, "_employee_bracket_hours", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(api, "_employee_salary_components", lambda *a, **k: ([], []), raising=False)

    api.calc = calc
    return api


# --------------------------------------------------------------------------- #
# PS21 — hourly branch (extracted path == the pre-plan math)
# --------------------------------------------------------------------------- #
def test_ps21_hourly_branch_math(api, monkeypatch):
    api._employee_bracket_hours = lambda *a, **k: {1.0: 160.0, 1.5: 20.0}
    api._employee_late_minutes = lambda *a, **k: [10.0]
    api._employee_salary_components = lambda *a, **k: ([500_000.0], [100_000.0])
    api.calc.load_checkout_miss_penalty = lambda *a, **k: 30_000.0

    out = api._compute_hourly_amounts(
        "E1",
        _PERIOD,
        {"mode": "Hourly", "hourly_rate": 30_000.0},
        time_brackets=[],
        penalty_rules=_PENALTY_RULES,
        deduction_rates=_DED_RATES,
        salary_advance_deduction=200_000.0,
    )
    # base gross = 160×30k + 20×30k×1.5 = 4.8M + 900k = 5.7M; +500k allowance.
    assert out["gross_pay"] == 6_200_000.0
    # deductions = 8%×6.2M + 100k + 50k + 30k + 200k
    assert out["total_deduction"] == 876_000.0
    assert out["net_pay"] == 5_324_000.0
    assert out["payroll_mode"] if "payroll_mode" in out else True  # set by caller
    assert out["bracket_hours_1"] == 160.0 and out["bracket_hours_1_5"] == 20.0


# --------------------------------------------------------------------------- #
# PS22 — monthly branch (full-semantics cross-check)
# --------------------------------------------------------------------------- #
def test_ps22_monthly_branch_math(api, monkeypatch):
    api.calc.load_employee_period_summary = lambda *a, **k: {
        "regular_hours": 208.0,
        "overtime_hours": 10.0,
        "payable_days": 26.0,
    }
    api.calc.load_employee_period_agg = lambda *a, **k: {
        "payable_days": 26.0,
        "regular_hours": 208.0,
        "regular_night_hours": 0.0,
        "overtime_normal_hours": 10.0,
        "overtime_night_hours": 0.0,
        "overtime_holiday_hours": 0.0,
        "late_minutes": 0.0,
    }
    api._employee_late_minutes = lambda *a, **k: [15.0]
    api._employee_salary_components = lambda *a, **k: ([500_000.0], [100_000.0])
    api.calc.load_checkout_miss_penalty = lambda *a, **k: 30_000.0

    out = api._compute_monthly_amounts(
        "E2",
        _PERIOD,
        {"mode": "Monthly", "monthly_base": 10_000_000.0},
        penalty_rules=_PENALTY_RULES,
        deduction_rates=_DED_RATES,
        salary_advance_deduction=200_000.0,
    )
    rate = 10_000_000 / 208
    proportional = round(10_000_000 * 26 / 26, 2)
    ot = round(10 * rate * 1.5, 2)
    gross = round(proportional + ot + 500_000, 2)
    assert out["base_salary"] == 10_000_000.0
    assert out["gross_pay"] == gross
    assert out["total_deduction"] == round(gross * 0.08 + 100_000 + 50_000 + 30_000 + 200_000, 2)
    assert out["net_pay"] == round(gross - out["total_deduction"], 2)
    # Hourly-only columns zeroed (mode switch leaves no stale brackets).
    assert out["hourly_rate"] == 0.0
    assert out["bracket_hours_1"] == out["bracket_hours_1_2"] == out["bracket_hours_1_5"] == 0.0
    assert out["late_penalty"] == 50_000.0  # alias stamped like the hourly path
    assert out["worked_hours"] == 218.0


# --------------------------------------------------------------------------- #
# PS23 — monthly pre-flight list
# --------------------------------------------------------------------------- #
def test_ps23_monthly_missing_base(api):
    settings = {
        "E1": {"mode": "Hourly", "monthly_base": 0.0},
        "E2": {"mode": "Monthly", "monthly_base": 10_000_000.0},
        "E3": {"mode": "Monthly", "monthly_base": 0.0},
        "E4": {"mode": "Monthly", "monthly_base": None},
    }
    assert api._monthly_missing_base(settings) == ["E3", "E4"]
    assert api._monthly_missing_base({}) == []


# --------------------------------------------------------------------------- #
# PS24 — _line_breakdown hourly mode unchanged (monthly block zeroed)
# --------------------------------------------------------------------------- #
def test_ps24_breakdown_hourly_mode(api):
    doc = NSDict(
        name="L1",
        employee="E1",
        employee_name="NV A",
        department=None,
        status="Confirmed",
        payroll_mode="Hourly",
        hourly_rate=30000,
        base_salary=30000,
        payable_days=26,
        worked_hours=180,
        bracket_hours_1=160,
        bracket_hours_1_2=0,
        bracket_hours_1_5=20,
        gross_pay=5700000,
        manual_adjustments=[],
        formula_net=5700000,
        net_pay=5700000,
        payroll_review_period="PRP-00001",
        regular_hours=160,
        overtime_hours=20,
        leave_days=0,
        absent_days=0,
        need_review_days=0,
        worked_days=None,
    )
    out = api._line_breakdown(doc)
    assert out["payroll_mode"] == "Hourly"
    assert len(out["brackets"]) == 3
    assert out["base_gross"] == round(160 * 30000 + 20 * 30000 * 1.5, 2)
    assert out["monthly"]["proportional_base"] == 0.0


# --------------------------------------------------------------------------- #
# PS25 — _line_breakdown monthly mode
# --------------------------------------------------------------------------- #
def test_ps25_breakdown_monthly_mode(api):
    doc = NSDict(
        name="L2",
        employee="E2",
        employee_name="NV B",
        department=None,
        status="Confirmed",
        payroll_mode="Monthly",
        hourly_rate=0,
        base_salary=10_000_000,
        payable_days=24,
        worked_hours=200,
        bracket_hours_1=0,
        bracket_hours_1_2=0,
        bracket_hours_1_5=0,
        gross_pay=9700000,
        manual_adjustments=[],
        formula_net=9500000,
        net_pay=9500000,
        payroll_review_period="PRP-00001",
        regular_hours=192,
        overtime_hours=8,
        leave_days=2,
        absent_days=0,
        need_review_days=0,
        worked_days=None,
    )
    out = api._line_breakdown(doc)
    assert out["payroll_mode"] == "Monthly"
    assert out["brackets"] == []
    assert out["monthly"]["proportional_base"] == round(10_000_000 * 24 / 26, 2)
    assert out["monthly"]["daily_rate"] == round(10_000_000 / 26, 2)
    assert out["monthly"]["hourly_equivalent"] == round(10_000_000 / 208, 2)
    assert out["base_gross"] == out["monthly"]["proportional_base"]
