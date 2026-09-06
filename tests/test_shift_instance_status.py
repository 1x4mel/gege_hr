"""Bench-free unit tests for ``shift.set_shift_instance_status`` — the Phase-3
day-level instance ops endpoint (plans/hr-shifts-frontend-parity.md §8.4).

Mirrors the stub-frappe harness of ``test_admin_shift_types.py`` /
``test_admin_custom_checkin.py``: ``monkeypatch.setitem(sys.modules, …)``
including the ``gege_hr.utils.employee`` / ``.tz`` submodules ``api/shift.py``
imports at module top.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

SHIFT_API = "gege_hr.gege_hr.api.shift"


class _DotDict(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _FakeDoc:
    def __init__(self, name, status):
        self.name = name
        self.status = status
        self.db_sets: list[tuple] = []

    def db_set(self, field, value, **kw):
        self.db_sets.append((field, value, kw))
        setattr(self, field, value)


class _FakeDB:
    def __init__(self):
        self.docs: dict[str, _FakeDoc] = {}
        self.ws_status: dict[str, str] = {}  # shift_instance → calculation_status

    def get_value(self, doctype, filters, field=None):
        if doctype == "VN Attendance Work Session" and isinstance(filters, dict):
            return self.ws_status.get(filters.get("shift_instance"))
        return None


class _Stub:
    def __init__(self, db):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.deny = False
        self.PermissionError = type("PermissionError", (Exception,), {})

        def _only_for(roles=None):
            if self.deny:
                raise self.PermissionError("not permitted")

        self.only_for = _only_for
        self.log_error = lambda *a, **k: None
        self.db = db

        def _get_doc(doctype, name=None):
            doc = self.db.docs.get(name)
            if doc is None:
                frappe_missing.append(name)
            return doc

        self.get_doc = _get_doc

        def _throw(msg, exc=Exception, *a, **k):
            raise exc(msg)

        self.throw = _throw


frappe_missing: list[str] = []


@pytest.fixture
def shift(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: v
    utils.add_days = lambda v, d: v
    emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
    tz = types.ModuleType("gege_hr.gege_hr.utils.tz")
    tz.now_in_portal = lambda: None
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", emp)
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.tz", tz)
    sys.modules.pop(SHIFT_API, None)
    mod = importlib.import_module(SHIFT_API)
    monkeypatch.setattr(mod, "frappe", stub)
    return mod, stub, db


def test_si_s1_scheduled_to_cancelled(shift):
    mod, _stub, db = shift
    si = _FakeDoc("SI-1", "Scheduled")
    db.docs["SI-1"] = si
    out = mod.set_shift_instance_status("SI-1", "Cancelled")
    assert out == {"name": "SI-1", "status": "Cancelled", "warning": ""}
    assert si.db_sets == [("status", "Cancelled", {"update_modified": True})]


def test_si_s2_active_or_completed_refused(shift):
    mod, _stub, db = shift
    for busy in ("Active", "Completed"):
        db.docs["SI-2"] = _FakeDoc("SI-2", busy)
        with pytest.raises(Exception, match="không đổi trạng thái"):
            mod.set_shift_instance_status("SI-2", "Cancelled")


def test_si_s3_invalid_status_throws(shift):
    mod, _stub, db = shift
    db.docs["SI-3"] = _FakeDoc("SI-3", "Scheduled")
    with pytest.raises(Exception, match="Trạng thái phải là"):
        mod.set_shift_instance_status("SI-3", "Zzz")


def test_si_s4_same_status_is_noop(shift):
    mod, _stub, db = shift
    si = _FakeDoc("SI-4", "Cancelled")
    db.docs["SI-4"] = si
    out = mod.set_shift_instance_status("SI-4", "Cancelled")
    assert out["status"] == "Cancelled"
    assert si.db_sets == []


def test_si_s5_linked_work_session_surfaces_warning(shift):
    mod, _stub, db = shift
    db.docs["SI-5"] = _FakeDoc("SI-5", "Scheduled")
    db.ws_status["SI-5"] = "Need Review"
    out = mod.set_shift_instance_status("SI-5", "Skipped")
    assert out["status"] == "Skipped"
    assert "Need Review" in out["warning"]


def test_si_s6_requires_manager_role(shift):
    mod, stub, db = shift
    db.docs["SI-6"] = _FakeDoc("SI-6", "Scheduled")
    stub.deny = True
    with pytest.raises(stub.PermissionError):
        mod.set_shift_instance_status("SI-6", "Cancelled")


def test_si_s7_undo_back_to_scheduled(shift):
    mod, _stub, db = shift
    si = _FakeDoc("SI-7", "Cancelled")
    db.docs["SI-7"] = si
    out = mod.set_shift_instance_status("SI-7", "Scheduled")
    assert out["status"] == "Scheduled"
    assert si.status == "Scheduled"
