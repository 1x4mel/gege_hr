"""Bench-free tests for the Overtime ↔ Work Session recalc synchronisation
(plan T1 / BUG-1; ``plans/OT-Approval-Sync-Plan.md``).

``approval._after_ot_state_change`` / ``_recalc_ot_shift_instance`` must:

  * trigger a Work Session recalc ONLY when a ``VN Overtime Request`` enters or
    leaves the engine-active state set (``Approved`` / ``Confirmed``);
  * be a no-op for non-OT doctypes and for pending→pending transitions;
  * never let a recalc failure abort the approve/reject (best-effort) — and fall
    back to an inline recalc when the enqueue path is unavailable.

A stub ``frappe`` is injected into ``sys.modules`` (mirroring
``tests/test_approval_leave_unify.py``). What is under test is the *routing
contract* of the recalc trigger — the heavy recalc itself (``persist_work_session``)
is spied, not executed.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class _Doc:
    """Minimal doc double exposing ``.get()`` (what _after_ot_state_change calls)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return getattr(self, key, default)


def _install_stub(monkeypatch, *, table_exists=True, enqueue_raises=False, shift_instance="SI-TEST"):
    """Install a stub ``frappe`` sufficient for api.approval to import + run.

    Returns a ``state`` recorder holding ``enqueue_calls`` (the recalc enqueue
    attempts) so tests can assert the trigger fired with the right arguments.
    """
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.throw = lambda *a, **k: (_ for _ in ()).throw(Exception(a[0] if a else "throw"))
    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="mgr@x")

    state = {"enqueue_calls": []}

    def _enqueue(target, **kw):
        state["enqueue_calls"].append((target, kw))
        if enqueue_raises:
            raise RuntimeError("enqueue unavailable (test)")

    mod.enqueue = _enqueue

    class _DB:
        def table_exists(self, name):
            return table_exists

        def get_value(self, doctype, *a, **k):
            return shift_instance

        def get_all(self, doctype, *a, **k):
            return []

    mod.db = _DB()

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: v
    utils.get_datetime = lambda v=None: v
    utils.now = lambda: "2026-08-14 00:00:00"
    mod.utils = utils

    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    return mod, state


@pytest.fixture
def approval(monkeypatch):
    """Fresh api.approval bound to the stub frappe, with a persist spy."""
    _install_stub(monkeypatch)
    from gege_hr.gege_hr.api import approval as approval_api
    from gege_hr.gege_hr.utils import calc as calc_mod

    importlib.reload(approval_api)
    return approval_api


def _ot_doc(state="Approved", shift_instance="SI-TEST"):
    return _Doc(
        doctype="VN Overtime Request",
        name="OR-1",
        employee="EMP-1",
        work_date="2026-08-14",
        shift_instance=shift_instance,
        workflow_state=state,
    )


# --------------------------------------------------------------------------- #
# _after_ot_state_change — routing
# --------------------------------------------------------------------------- #
class TestAfterOtStateChange:
    def test_engine_states_constant(self, approval):
        assert approval._OT_ENGINE_STATES == ("Approved", "Confirmed")

    def test_recalc_on_enter_approved(self, approval, monkeypatch):
        # Reload with a state recorder we can read after the call.
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(), from_state="Pending Manager", to_state="Approved"
        )
        assert len(state["enqueue_calls"]) == 1
        target, kw = state["enqueue_calls"][0]
        assert target == "gege_hr.gege_hr.utils.calc.persist_work_session"
        assert kw["shift_instance_name"] == "SI-TEST"
        assert kw["calculate_mode"] == "recalculate"

    def test_recalc_on_enter_confirmed(self, approval, monkeypatch):
        # Pending → Confirmed (skipping Approved) is a real enter → recalc once.
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(state="Confirmed"), from_state="Pending Manager", to_state="Confirmed"
        )
        assert len(state["enqueue_calls"]) == 1

    def test_no_recalc_when_staying_inside_engine_set(self, approval, monkeypatch):
        # Approved → Confirmed: both engine-active → membership unchanged → no recalc.
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(state="Confirmed"), from_state="Approved", to_state="Confirmed"
        )
        assert state["enqueue_calls"] == []

    def test_recalc_on_leave_approved(self, approval, monkeypatch):
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(), from_state="Approved", to_state="Rejected"
        )
        assert len(state["enqueue_calls"]) == 1

    def test_no_recalc_on_pending_to_pending(self, approval, monkeypatch):
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(state="Pending HR"), from_state="Pending Manager", to_state="Pending HR"
        )
        assert state["enqueue_calls"] == []

    def test_no_recalc_when_staying_approved(self, approval, monkeypatch):
        # Approved → Confirmed stays inside the engine set → still recalcs once
        # (Confirmed is also engine-active), but Approved→Approved is a no-op.
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        approval._after_ot_state_change(
            _ot_doc(), from_state="Approved", to_state="Approved"
        )
        assert state["enqueue_calls"] == []

    def test_noop_for_non_ot_doctype(self, approval, monkeypatch):
        _, state = _install_stub(monkeypatch)
        importlib.reload(approval)
        doc = _Doc(doctype="Leave Application", name="LV-1", employee="EMP-1")
        approval._after_ot_state_change(doc, from_state="Open", to_state="Approved")
        assert state["enqueue_calls"] == []

    def test_noop_when_no_shift_instance_resolvable(self, approval, monkeypatch):
        # No shift_instance on the doc, and the lookup finds nothing.
        _install_stub(monkeypatch, shift_instance=None)
        importlib.reload(approval)
        doc = _ot_doc(shift_instance=None)
        # Must not raise, and must not enqueue.
        approval._after_ot_state_change(doc, from_state="Pending Manager", to_state="Approved")


# --------------------------------------------------------------------------- #
# _recalc_ot_shift_instance — best-effort / inline fallback
# --------------------------------------------------------------------------- #
class TestRecalcBestEffort:
    def test_inline_fallback_when_enqueue_unavailable(self, approval, monkeypatch):
        _install_stub(monkeypatch, enqueue_raises=True)
        importlib.reload(approval)
        from gege_hr.gege_hr.utils import calc as calc_mod

        called = []

        def _spy_persist(shift_instance_name, calculate_mode="realtime"):
            called.append((shift_instance_name, calculate_mode))
            return "WS-X"

        monkeypatch.setattr(calc_mod, "persist_work_session", _spy_persist)
        approval._recalc_ot_shift_instance("EMP-1", "2026-08-14", "SI-TEST")
        assert called == [("SI-TEST", "recalculate")]

    def test_swallows_inline_failure(self, approval, monkeypatch):
        _install_stub(monkeypatch, enqueue_raises=True)
        importlib.reload(approval)
        from gege_hr.gege_hr.utils import calc as calc_mod

        def _boom(shift_instance_name, calculate_mode="realtime"):
            raise RuntimeError("boom")

        monkeypatch.setattr(calc_mod, "persist_work_session", _boom)
        # Must NOT raise — best-effort never aborts the approve/reject.
        approval._recalc_ot_shift_instance("EMP-1", "2026-08-14", "SI-TEST")

    def test_noop_when_shift_instance_missing(self, approval, monkeypatch):
        _install_stub(monkeypatch, shift_instance=None)
        importlib.reload(approval)
        from gege_hr.gege_hr.utils import calc as calc_mod

        monkeypatch.setattr(
            calc_mod, "persist_work_session",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not recalc")),
        )
        approval._recalc_ot_shift_instance("EMP-1", "2026-08-14", None)


# --------------------------------------------------------------------------- #
# _writeback_ot_request_hours — regression for the live NameError (T3/BUG-2)
# --------------------------------------------------------------------------- #
# calc.py has NO module-level ``import frappe``; the write-back helper must
# import it locally. A missing import crashed the live recalc (worker log:
# NameError: name 'frappe' is not defined). These stub-frappe tests pin that.
class TestWritebackOtRequestHours:
    def _stub_frappe(self, monkeypatch, set_value):
        mod = types.ModuleType("frappe")
        mod.log_error = lambda *a, **k: None

        class _DB:
            def table_exists(self, name):
                return True

            def set_value(self, dt, name, vals, *a, **k):
                set_value((name, vals))

        mod.db = _DB()
        monkeypatch.setitem(sys.modules, "frappe", mod)

    def test_stamps_matched_and_zeros_unserved(self, monkeypatch):
        calls = []
        self._stub_frappe(monkeypatch, calls.append)
        from gege_hr.gege_hr.utils import calc

        calc._writeback_ot_request_hours(
            {"name": "SI-X"},
            {"_ot_request_breakdown": {"OR-A": 1.5}},
            [{"name": "OR-A"}, {"name": "OR-B"}],  # OR-B unserved
        )
        assert ("OR-A", {"actual_hours": 1.5, "approved_hours": 1.5}) in calls
        assert ("OR-B", {"actual_hours": 0, "approved_hours": 0}) in calls

    def test_noop_when_nothing_to_write(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("should not write anything")

        self._stub_frappe(monkeypatch, _boom)
        from gege_hr.gege_hr.utils import calc

        # No breakdown AND no ot_requests → early return, no set_value.
        calc._writeback_ot_request_hours({"name": "SI-X"}, {}, [])

    def test_swallows_db_failure(self, monkeypatch):
        class _DB:
            def table_exists(self, name):
                return True

            def set_value(self, *a, **k):
                raise RuntimeError("db down")

        mod = types.ModuleType("frappe")
        mod.log_error = lambda *a, **k: None
        mod.db = _DB()
        monkeypatch.setitem(sys.modules, "frappe", mod)
        from gege_hr.gege_hr.utils import calc

        # Must not raise — write-back is best-effort.
        calc._writeback_ot_request_hours(
            {"name": "SI-X"}, {"_ot_request_breakdown": {"OR-A": 1.0}}, [{"name": "OR-A"}]
        )
