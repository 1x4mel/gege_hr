"""Bench-free tests for the inbox-centric leave-approval unification (Phase 0).

``plans/leave_approval_inbox_centric_plan.md`` §1.2: a leave decision made
through the unified inbox (``approval._delegate_leave``, reached from
``approve_request`` / ``reject_request`` with ``request_type="Leave
Application"``) must delegate to the HRMS-aware ``leave._approve_one`` /
``leave._reject_one`` — the path that runs HRMS ``validate`` and creates the
**Leave Ledger Entry** (so the leave balance is actually deducted) — and must
NOT fall back to the legacy raw ``frappe.db.set_value`` that left the ledger
stale.

Bench-free: a stub ``frappe`` is injected into ``sys.modules`` and the leave
handlers + ``_write_log`` + ``_user`` are monkeypatched as spies. What is under
test here is the *routing contract* (delegate → HRMS path; never raw status
write; matrix log preserved; idempotent), not the HRMS ledger itself — the
latter lives behind ``doc.submit()`` on a real bench and is covered by the
manual E2E checklist (plan §5).
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for api/approval to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe(status="Open"):
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.throw = lambda *a, **k: (_ for _ in ()).throw(Exception(a[0] if a else "frappe.throw"))
    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="hr.demo@gege.demo")

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.today = lambda: datetime.date.today()
    utils.cint = lambda v, d=0: int(v) if v not in (None, "") else d
    utils.nowdate = lambda: datetime.date.today().isoformat()
    mod.utils = utils

    class _DB:
        """Records db.set_value calls so the regression guard can assert none happen.

        ``status`` backs Leave Application; ``cancel_state`` backs the VN Leave
        Cancellation Request workflow_state (Phase 2A)."""

        def __init__(self):
            self.status = status
            self.cancel_state = "Pending Manager"
            self.set_value_calls = []

        def get_value(self, doctype, name, field, *a, **k):
            if field == "status":
                return self.status
            if field == "workflow_state":
                return self.cancel_state
            return None

        def set_value(self, doctype, name, values, *a, **k):
            self.set_value_calls.append((doctype, name, values))
            return None

    mod.db = _DB()
    return mod


@pytest.fixture
def env(monkeypatch):
    """Register the stub frappe, import api/approval, spy on the leave handlers."""
    stub = _build_stub_frappe(status="Open")
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    approval = importlib.import_module("gege_hr.gege_hr.api.approval")
    # Point the module's `frappe` at our stub (in case it was bound to another).
    monkeypatch.setattr(approval, "frappe", stub)
    # The leave handlers are imported lazily inside _delegate_leave; patch them on
    # the real leave module so the late `from ... import leave as leave_api` sees them.
    leave = importlib.import_module("gege_hr.gege_hr.api.leave")

    calls = {
        "approve_one": [],
        "reject_one": [],
        "approve_cancel_one": [],
        "reject_cancel_one": [],
        "write_log": [],
    }

    def spy_approve_one(name):
        calls["approve_one"].append(name)
        return {"name": name, "status": "Approved", "message": f"Đã duyệt đơn nghỉ phép {name}."}

    def spy_reject_one(name, reason=None):
        calls["reject_one"].append((name, reason))
        return {"name": name, "status": "Rejected", "message": f"Đã từ chối đơn nghỉ phép {name}."}

    def spy_approve_cancel_one(name):
        calls["approve_cancel_one"].append(name)
        return {"name": name, "status": "Approved", "message": f"Đã duyệt yêu cầu hủy {name}."}

    def spy_reject_cancel_one(name, reason=None):
        calls["reject_cancel_one"].append((name, reason))
        return {"name": name, "status": "Rejected", "message": f"Đã từ chối yêu cầu hủy {name}."}

    def spy_write_log(**kw):
        calls["write_log"].append(kw)

    monkeypatch.setattr(leave, "_approve_one", spy_approve_one)
    monkeypatch.setattr(leave, "_reject_one", spy_reject_one)
    monkeypatch.setattr(leave, "_approve_cancellation_one", spy_approve_cancel_one)
    monkeypatch.setattr(leave, "_reject_cancellation_one", spy_reject_cancel_one)
    monkeypatch.setattr(approval, "_write_log", spy_write_log)
    monkeypatch.setattr(approval, "_user", lambda: "hr.demo@gege.demo")

    return types.SimpleNamespace(approval=approval, leave=leave, calls=calls, stub=stub)


# --------------------------------------------------------------------------- #
# approve — delegates to the HRMS-aware leave handler
# --------------------------------------------------------------------------- #
def test_approve_delegates_to_leave_approve_one(env):
    out = env.approval._delegate_leave("LA-1", "Leave Application", None, approved=True)
    assert env.calls["approve_one"] == ["LA-1"]
    assert env.calls["reject_one"] == []
    assert out["status"] == "Approved"
    assert out["request_type"] == "Leave Application"


def test_approve_never_raw_writes_status(env):
    """Regression guard: the legacy raw db.set_value(status=...) must stay gone
    (it bypassed on_submit and left the leave ledger stale — plan §1.2)."""
    env.approval._delegate_leave("LA-1", "Leave Application", None, approved=True)
    assert env.stub.db.set_value_calls == []


def test_approve_still_records_approval_log(env):
    """Matrix history (VN Approval Log) is preserved after delegation."""
    env.approval._delegate_leave("LA-1", "Leave Application", None, approved=True)
    assert len(env.calls["write_log"]) == 1
    log = env.calls["write_log"][0]
    assert log["action"] == "Approve"
    assert log["from_state"] == "Open"
    assert log["to_state"] == "Approved"
    assert log["transaction_type"] == "Leave Application"
    assert log["actor"] == "hr.demo@gege.demo"


# --------------------------------------------------------------------------- #
# reject — delegates with the reason
# --------------------------------------------------------------------------- #
def test_reject_delegates_to_leave_reject_one_with_reason(env):
    out = env.approval._delegate_leave("LA-2", "Leave Application", "Thiếu CCCD", approved=False)
    assert env.calls["reject_one"] == [("LA-2", "Thiếu CCCD")]
    assert env.calls["approve_one"] == []
    assert out["status"] == "Rejected"


def test_reject_never_raw_writes_status(env):
    env.approval._delegate_leave("LA-2", "Leave Application", "x", approved=False)
    assert env.stub.db.set_value_calls == []


def test_reject_records_approval_log_with_comment(env):
    env.approval._delegate_leave("LA-2", "Leave Application", "Thiếu CCCD", approved=False)
    log = env.calls["write_log"][0]
    assert log["action"] == "Reject"
    assert log["from_state"] == "Open"
    assert log["to_state"] == "Rejected"
    assert log["comment"] == "Thiếu CCCD"


# --------------------------------------------------------------------------- #
# idempotency — already decided short-circuits
# --------------------------------------------------------------------------- #
def test_idempotent_when_already_approved(env):
    """Already-Approved leave must not call the handler / log / write again."""
    env.stub.db.status = "Approved"
    out = env.approval._delegate_leave("LA-3", "Leave Application", None, approved=True)
    assert env.calls["approve_one"] == []
    assert env.calls["write_log"] == []
    assert env.stub.db.set_value_calls == []
    assert out["status"] == "Approved"
    assert out["request_type"] == "Leave Application"


def test_idempotent_when_already_rejected(env):
    env.stub.db.status = "Rejected"
    out = env.approval._delegate_leave("LA-4", "Leave Application", None, approved=False)
    assert env.calls["reject_one"] == []
    assert env.calls["write_log"] == []
    assert out["status"] == "Rejected"


# --------------------------------------------------------------------------- #
# propagation — validation failures are not swallowed (plan R2)
# --------------------------------------------------------------------------- #
def test_validation_failure_propagates(env, monkeypatch):
    """HRMS validate errors (insufficient balance / blackout) must surface to the
    inbox — _delegate_leave must not swallow them like the old try/except did."""

    def boom(name):
        raise frappe_throw("insufficient leave balance")

    def frappe_throw(msg):
        raise Exception(msg)

    monkeypatch.setattr(env.leave, "_approve_one", boom)
    with pytest.raises(Exception, match="insufficient leave balance"):
        env.approval._delegate_leave("LA-5", "Leave Application", None, approved=True)


# --------------------------------------------------------------------------- #
# leave-cancellation delegation (Phase 2A — inbox now owns cancellation approvals)
# --------------------------------------------------------------------------- #
def test_cancel_approve_delegates_to_leave_handler(env):
    out = env.approval._delegate_leave_cancellation("CR-1", "Leave Cancellation Request", None, approved=True)
    assert env.calls["approve_cancel_one"] == ["CR-1"]
    assert env.calls["reject_cancel_one"] == []
    assert env.calls["approve_one"] == []  # must not touch the application path
    assert env.stub.db.set_value_calls == []
    assert out["status"] == "Approved"
    assert out["request_type"] == "Leave Cancellation Request"


def test_cancel_approve_records_approval_log(env):
    env.approval._delegate_leave_cancellation("CR-1", "Leave Cancellation Request", None, approved=True)
    log = env.calls["write_log"][0]
    assert log["action"] == "Approve"
    assert log["from_state"] == "Pending Manager"
    assert log["to_state"] == "Approved"
    assert log["transaction_type"] == "Leave Cancellation Request"


def test_cancel_reject_delegates_with_reason(env):
    out = env.approval._delegate_leave_cancellation(
        "CR-2", "Leave Cancellation Request", "Đơn gốc sai", approved=False
    )
    assert env.calls["reject_cancel_one"] == [("CR-2", "Đơn gốc sai")]
    assert env.calls["approve_cancel_one"] == []
    assert out["status"] == "Rejected"
    log = env.calls["write_log"][0]
    assert log["action"] == "Reject"
    assert log["comment"] == "Đơn gốc sai"


def test_cancel_idempotent_when_already_approved(env):
    env.stub.db.cancel_state = "Approved"
    out = env.approval._delegate_leave_cancellation("CR-3", "Leave Cancellation Request", None, approved=True)
    assert env.calls["approve_cancel_one"] == []
    assert env.calls["write_log"] == []
    assert out["status"] == "Approved"


def test_cancel_validation_failure_propagates(env, monkeypatch):
    def boom(name):
        raise Exception("linked leave already cancelled")

    monkeypatch.setattr(env.leave, "_approve_cancellation_one", boom)
    with pytest.raises(Exception, match="linked leave already cancelled"):
        env.approval._delegate_leave_cancellation("CR-4", "Leave Cancellation Request", None, approved=True)
