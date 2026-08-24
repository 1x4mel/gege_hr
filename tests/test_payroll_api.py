"""Regression tests for ``api/payroll._employee_work_sessions``.

Guards the bug that made EVERY payroll ``gross_pay = 0``: the loader called
``frappe.db.get_all(..., as_dict=True)``, which this Frappe version rejects with
``TypeError: DatabaseQuery.execute() got an unexpected keyword argument
'as_dict'``. The surrounding ``except: return []`` swallowed it, so the
aggregator always received an empty list.

Bench-free: a stub ``frappe`` is injected into ``sys.modules`` (auto-restored on
teardown via ``monkeypatch.setitem``) whose ``db.get_all`` mimics the real
behaviour — it returns plain dict rows and **raises TypeError if ``as_dict`` is
passed**. If anyone re-introduces ``as_dict=True``, ``_employee_work_sessions``
returns ``[]`` and these tests fail.
"""

import importlib
import sys
import types

import pytest


def _build_stub_frappe(rows=None, reject_as_dict=True):
    mod = types.ModuleType("frappe")
    mod.__path__ = []  # mark as a package so `from frappe.utils import ...` resolves
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.log_error = lambda *a, **k: None
    mod.throw = lambda *a, **k: (_ for _ in ()).throw(Exception(str(a)))
    mod.new_doc = lambda *a, **k: None
    mod.get_doc = lambda *a, **k: None
    mod.get_cached_doc = lambda *a, **k: None
    mod.flags = types.SimpleNamespace()

    # frappe.utils submodule (registered so transitive `from frappe.utils import X`
    # in api/audit, utils/employee, utils/notify, utils/pagination resolves).
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: __import__("datetime").date.today()
    utils.today = lambda: __import__("datetime").date.today()
    utils.add_days = lambda d, n: d
    utils.flt = lambda v, p=None: float(v or 0)
    utils.cint = lambda v, p=None: int(v or 0)
    utils.now_datetime = lambda: __import__("datetime").datetime.now()
    utils.get_datetime = lambda v=None: __import__("datetime").datetime.now()
    mod.utils = utils

    class _DB:
        def __init__(self):
            self.rows = list(rows or [])
            self.last_kwargs = None
            self.last_fields = None

        def get_all(self, doctype, filters=None, fields=None, **kwargs):
            self.last_kwargs = kwargs
            self.last_fields = fields
            if reject_as_dict and "as_dict" in kwargs:
                raise TypeError("DatabaseQuery.execute() got an unexpected keyword argument 'as_dict'")
            return [dict(r) for r in self.rows]

        def get_value(self, *a, **k):
            return None

        def exists(self, *a, **k):
            return False

        def count(self, *a, **k):
            return 0

    mod.db = _DB()
    return mod, utils


@pytest.fixture
def payroll_api(monkeypatch):
    # rows mimic a real VN Attendance Work Session projection (no
    # `overtime_normal_hours` column — that key is remapped from
    # `approved_overtime_hours` by the loader).
    rows = [
        {
            "payable_day": 1.0,
            "regular_hours": 9.0,
            "regular_night_hours": 0.0,
            "raw_overtime_hours": 0.0,
            "approved_overtime_hours": 2.0,
            "overtime_night_hours": 0.0,
            "overtime_holiday_hours": 0.0,
            "late_minutes": 0,
            "early_leave_minutes": 0,
            "absent": 0,
        },
        {"payable_day": 0.5, "regular_hours": 4.0, "approved_overtime_hours": 0.0},
    ]
    stub, utils = _build_stub_frappe(rows=rows)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    # Reload so the module binds to THIS stub even when another test file has
    # already imported (and reload-bound) api.payroll with a different stub.
    api = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll"))
    return api, stub


def test_employee_work_sessions_returns_rows_not_empty(payroll_api):
    """The as_dict bug returned [] (swallowed TypeError) → gross always 0."""
    api, _ = payroll_api
    rows = api._employee_work_sessions("HR-EMP-00001", "2026-08-01", "2026-08-31")
    assert len(rows) == 2
    assert sum(r["payable_day"] for r in rows) == 1.5


def test_employee_work_sessions_does_not_pass_as_dict(payroll_api):
    """Re-introducing ``as_dict=True`` must fail (the real Frappe rejects it)."""
    api, stub = payroll_api
    api._employee_work_sessions("HR-EMP-00001", "2026-08-01", "2026-08-31")
    assert "as_dict" not in (stub.db.last_kwargs or {})


def test_employee_work_sessions_remaps_overtime_normal_hours(payroll_api):
    """The doctype has no overtime_normal_hours column; the loader remaps it from
    approved_overtime_hours so aggregate_work_sessions reads the right key."""
    api, _ = payroll_api
    rows = api._employee_work_sessions("HR-EMP-00001", "2026-08-01", "2026-08-31")
    assert rows[0]["overtime_normal_hours"] == 2.0
    assert rows[1]["overtime_normal_hours"] == 0.0


# --------------------------------------------------------------------------- #
# Checkout-miss config (vn_cm_*) — get/save_payroll_settings.
# Bench-free: reuse _build_stub_frappe (complete frappe + frappe.utils) and
# augment it with only_for / get_single_value / a mock Portal Setting doc.
# --------------------------------------------------------------------------- #
class _MockSetting:
    """Stands in for ``frappe.get_doc('VN HR Portal Setting', ...)``."""

    def __init__(self):
        self.values = {}  # what .set(field, value) recorded
        self.saved = 0
        self.flags = types.SimpleNamespace()

    def set(self, field, value):
        self.values[field] = value

    def save(self, ignore_permissions=False):
        self.saved += 1


@pytest.fixture
def settings_api(monkeypatch):
    base, utils = _build_stub_frappe(rows=[])
    setting = _MockSetting()
    base.only_for = lambda *a, **k: None
    base.get_doc = lambda *a, **k: setting
    base.db.singles = {}
    base.db.get_single_value = lambda doctype, field: base.db.singles.get(field)
    base.db.commit = lambda *a, **k: None
    base.db.set_value = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "frappe", base)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    api = importlib.import_module("gege_hr.gege_hr.api.payroll")
    api.frappe = base  # force this stub even if the module was imported earlier
    return api, base.db, setting


# ---- B1: GET returns all 6 checkout_miss keys ----
def test_get_checkout_miss_returns_all_keys(settings_api):
    api, db, _ = settings_api
    db.singles.update(
        {
            "vn_cm_enabled": 1,
            "vn_cm_penalty_amount": 150000,
            "vn_cm_free_first_n": 3,
            "vn_cm_grace_hours": 24,
            "vn_cm_window_days": 90,
            "vn_cm_buffer_minutes": 360,
        }
    )
    cm = api.get_payroll_settings()["checkout_miss"]
    assert cm == {
        "enabled": True,
        "penalty_amount": 150000.0,
        "free_first_n": 3,
        "grace_hours": 24,
        "window_days": 90,
        "buffer_minutes": 360,
    }


# ---- B2: GET keeps a legit 0 (never replaced by default) ----
def test_get_checkout_miss_keeps_zero_valid(settings_api):
    api, db, _ = settings_api
    db.singles.update(
        {
            "vn_cm_enabled": 0,
            "vn_cm_penalty_amount": 0,
            "vn_cm_free_first_n": 0,
            "vn_cm_grace_hours": 0,
            "vn_cm_window_days": 0,
            "vn_cm_buffer_minutes": 0,
        }
    )
    cm = api.get_payroll_settings()["checkout_miss"]
    assert cm["penalty_amount"] == 0.0
    assert cm["free_first_n"] == 0
    assert cm["enabled"] is False


# ---- B3: SAVE persists numeric + enabled ----
def test_save_checkout_miss_persists(settings_api):
    api, _, setting = settings_api
    api.save_payroll_settings(
        checkout_miss={
            "enabled": True,
            "penalty_amount": 150000,
            "free_first_n": 3,
            "grace_hours": 24,
            "window_days": 90,
            "buffer_minutes": 360,
        }
    )
    assert setting.values["vn_cm_penalty_amount"] == 150000.0
    assert setting.values["vn_cm_free_first_n"] == 3
    assert setting.values["vn_cm_enabled"] == 1
    assert setting.saved == 1


# ---- B4: SAVE accepts 0 ----
def test_save_checkout_miss_accepts_zero(settings_api):
    api, _, setting = settings_api
    api.save_payroll_settings(checkout_miss={"penalty_amount": 0, "free_first_n": 0})
    assert setting.values["vn_cm_penalty_amount"] == 0.0
    assert setting.values["vn_cm_free_first_n"] == 0


# ---- B5: SAVE rejects negative ----
def test_save_checkout_miss_rejects_negative(settings_api):
    api, _, _ = settings_api
    with pytest.raises(Exception):
        api.save_payroll_settings(checkout_miss={"penalty_amount": -1})


# ---- B6: SAVE rejects non-numeric ----
def test_save_checkout_miss_rejects_non_number(settings_api):
    api, _, _ = settings_api
    with pytest.raises(Exception):
        api.save_payroll_settings(checkout_miss={"grace_hours": "abc"})


# ---- B7: SAVE skips None keys ----
def test_save_checkout_miss_skips_none(settings_api):
    api, _, setting = settings_api
    api.save_payroll_settings(checkout_miss={"penalty_amount": None, "free_first_n": 5})
    assert "vn_cm_penalty_amount" not in setting.values
    assert setting.values["vn_cm_free_first_n"] == 5


# ---- B8: SAVE coerces enabled bool ----
def test_save_checkout_miss_enabled_bool(settings_api):
    api, _, setting = settings_api
    api.save_payroll_settings(checkout_miss={"enabled": False})
    assert setting.values["vn_cm_enabled"] == 0
