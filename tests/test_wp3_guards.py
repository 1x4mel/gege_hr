"""WP3 (prod-readiness-plan) — guard tests: materialise isolation + Employee
lifecycle hooks.

  MH1  one broken Shift Assignment must NOT block the others (skipped=1,
       valid employee still materialised)
  MH2  recalc-loop isolation lives in attendance.recalculate_period (per-SI
       try/except) — covered indirectly by the SI guard here
  MH3  Employee delete BLOCKED while attendance data exists
  MH4  Active → Left ends Shift Assignments + cancels future SIs

Bench-free stub-frappe harness (same pattern as test_checkout_miss_api.py).
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class ADict(dict):
    """dict with attribute access — mimics frappe._dict rows from db.get_all."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e


def _make_frappe_mod(*, counts=None, sa_rows=None, si_future=None, prev_status=None):
    """Build a stub frappe module tailored to the lifecycle/materialise flows."""
    frappe_mod = types.ModuleType("frappe")
    frappe_mod._ = lambda s: s
    frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    frappe_mod.throw = lambda msg, exc=None: (_ for _ in ()).throw(FrappeError(msg))
    frappe_mod.log_error = lambda *a, **k: None
    frappe_mod.get_traceback = lambda: "tb"
    frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
    frappe_mod.ValidationError = FrappeError
    frappe_mod.only_for = lambda *a, **k: None

    utils = types.ModuleType("frappe.utils")
    utils.add_days = lambda d, n: d + dt.timedelta(days=n)
    utils.getdate = lambda v=None: v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])
    utils.today = lambda: dt.date(2026, 8, 18)
    utils.now = lambda: "2026-08-18 08:00:00"
    utils.get_datetime = lambda v=None: dt.datetime.now()
    frappe_mod.utils = utils

    class _DB:
        def __init__(self):
            self.counts = dict(counts or {})
            self.set_values = []
            self.deleted = []

        def count(self, doctype, filters=None, **_kw):
            return self.counts.get(doctype, 0)

        def get_value(self, doctype, name=None, *a, **_k):
            if doctype == "Employee" and name:
                return prev_status
            return None

        def get_all(self, doctype, filters=None, fields=None, pluck=None, **_kw):
            if doctype == "Shift Assignment":
                return list(sa_rows or [])
            if doctype == "VN Employee Shift Instance":
                return list(si_future or [])
            return []

        def set_value(self, doctype, name, updates=None, *more, **_kw):
            self.set_values.append((doctype, name, updates))

        def rollback(self):
            pass

    frappe_mod.db = _DB()

    class _Meta:
        def has_field(self, name):
            return False

    frappe_mod.get_meta = lambda doctype: _Meta()

    cancelled = []

    class _FakeSIDoc:
        def __init__(self, name):
            self.name = name

        def cancel(self):
            cancelled.append(self.name)

        def delete(self, **_kw):
            pass

    def get_doc(arg, name=None, **_kw):
        if isinstance(arg, str):
            return _FakeSIDoc(name)
        raise FrappeError("not used")

    frappe_mod.get_doc = get_doc
    frappe_mod.cancelled = cancelled
    return frappe_mod, utils


@pytest.fixture()
def lifecycle(monkeypatch):
    frappe_mod, utils = _make_frappe_mod()
    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.employee_lifecycle"))
    return frappe_mod, mod


# --------------------------------------------------------------------------- #
# MH3 — delete blocked while attendance data exists
# --------------------------------------------------------------------------- #
def test_mh3_delete_blocked_with_checkins(lifecycle):
    frappe_mod, mod = lifecycle
    frappe_mod.db.counts = {"Employee Checkin": 12}
    doc = types.SimpleNamespace(name="HR-EMP-001")
    with pytest.raises(FrappeError) as ei:
        mod.guard_employee_delete(doc)
    assert "Employee Checkin: 12" in str(ei.value)
    assert "Nghỉ việc" in str(ei.value)


def test_delete_allowed_when_clean(lifecycle):
    frappe_mod, mod = lifecycle
    frappe_mod.db.counts = {}
    doc = types.SimpleNamespace(name="HR-EMP-001")
    mod.guard_employee_delete(doc)  # no raise


# --------------------------------------------------------------------------- #
# MH4 — Active → Left ends SAs + cancels future SIs
# --------------------------------------------------------------------------- #
def test_mh4_left_ends_assignments_and_cancels_future_si(lifecycle):
    frappe_mod, mod = lifecycle
    frappe_mod.db.counts = {}
    frappe_mod.db.__dict__.get  # noqa: B018
    # db fixtures
    frappe_mod.db.counts = {}
    sa_rows = [{"employee": "E1", "docstatus": 1, "status": "Active", "name": "SA-1"}]
    si_future = ["SI-F1", "SI-F2"]
    frappe_mod.db.get_all = lambda doctype, filters=None, **_kw: (
        sa_rows if doctype == "Shift Assignment" else si_future
    )
    doc = types.SimpleNamespace(name="HR-EMP-001", status="Left", relieving_date="2026-08-15")
    mod.handle_employee_status_change(doc)
    sa_sets = [sv for sv in frappe_mod.db.set_values if sv[0] == "Shift Assignment"]
    assert sa_sets and sa_sets[0][2].get("end_date") == "2026-08-15"
    assert frappe_mod.cancelled == ["SI-F1", "SI-F2"]


def test_left_twice_is_noop(lifecycle):
    frappe_mod, mod = lifecycle
    frappe_mod.db.counts = {}
    doc = types.SimpleNamespace(name="HR-EMP-001", status="Left")
    mod.handle_employee_status_change(doc)  # prev status Left → no-op, no raise
    assert frappe_mod.db.set_values == []


# --------------------------------------------------------------------------- #
# MH1 — one broken assignment doesn't block the rest (materialise guard)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def shift_mod(monkeypatch):
    frappe_mod, utils = _make_frappe_mod()
    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    tz = types.ModuleType("gege_hr.gege_hr.utils.tz")
    tz.now_in_portal = lambda *a, **k: dt.datetime(2026, 8, 18, 8)
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.tz", tz)
    import gege_hr.gege_hr.utils as utils_pkg

    monkeypatch.setattr(utils_pkg, "tz", tz, raising=False)

    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.shift"))

    assignments = [
        ADict(
            {  # broken employee A (duplicate/corrupt SA)
                "name": "SA-A",
                "employee": "EMP-A",
                "shift_type": "ST",
                "start_date": "2026-08-18",
                "end_date": "2026-08-18",
                "company": "C",
            }
        ),
        ADict(
            {
                "name": "SA-B",
                "employee": "EMP-B",
                "shift_type": "ST",
                "start_date": "2026-08-18",
                "end_date": "2026-08-18",
                "company": "C",
            }
        ),
    ]
    frappe_mod.db.get_all = lambda doctype, filters=None, **_kw: assignments

    def fake_ensure(a, day):
        if a["employee"] == "EMP-A":
            raise FrappeError("duplicate shift window")
        return True

    monkeypatch.setattr(mod, "_ensure_shift_instance", fake_ensure)
    return frappe_mod, mod


def test_mh1_broken_assignment_skipped_others_created(shift_mod):
    frappe_mod, mod = shift_mod
    res = mod._materialise_shift_instances(from_date="2026-08-18", to_date="2026-08-18")
    assert res["created"] == 1  # EMP-B got its instance
    assert res["skipped"] == 1  # EMP-A skipped, counted
    assert res["errors"] == ["EMP-A"]


def test_materialise_idempotent_run_twice(shift_mod):
    frappe_mod, mod = shift_mod
    # Second run: _ensure returns False (already exists) → created=0, no dupes
    mod._ensure_shift_instance = lambda a, day: False
    res = mod._materialise_shift_instances(from_date="2026-08-18", to_date="2026-08-18")
    assert res["created"] == 0
    assert res["skipped"] == 0
