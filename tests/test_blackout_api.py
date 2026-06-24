"""Bench-free unit tests for ``api/leave_blackout.py``.

The pure decision engine (``utils/leave_blackout.evaluate_blackout``) is covered
by ``test_leave_blackout.py``. This file targets the bench-dependent wrapper
:func:`evaluate_leave_blackout` — specifically the new ``employee`` → ``company``
resolution branch (the SPA leave form knows the employee, not the company) —
using a stub ``frappe`` injected into ``sys.modules``.

The stub is registered only for the duration of each test via
``monkeypatch.setitem`` (auto-restored on teardown) so it never leaks into the
sibling bench-free tests that assert ``frappe`` is unimportable
(``test_dashboard`` / ``test_device`` / ``test_notification`` /
``test_payroll_mapping``).
"""

import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/leave_blackout`` to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    # Runtime db bits are swapped per-test.
    mod.db = None
    mod.log_error = lambda *a, **k: None
    return mod


class _FakeDB:
    """Captures ``get_value`` calls; ``get_all`` returns whatever is configured."""

    def __init__(self):
        self.table_exists_flag = True
        self.company_for = "Gege Demo"  # what get_value("Employee", ..., "company") returns
        self.get_value_calls = []
        self.get_all_rows = []  # rules returned by blackout_periods via get_all
        self.raise_on_get_value = False

    def table_exists(self, doctype):
        return self.table_exists_flag

    def get_value(self, doctype, name, field):
        self.get_value_calls.append((doctype, name, field))
        if self.raise_on_get_value:
            raise RuntimeError("db down")
        return self.company_for

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        return list(self.get_all_rows)


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import ``api/leave_blackout``, wire a fake db.

    Replaces the module-level ``blackout_periods`` reader with a spy so we can
    assert the company that ``evaluate_leave_blackout`` resolved + control the
    rule set the engine receives, without depending on the real DB.
    """
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)

    api = importlib.import_module("gege_hr.gege_hr.api.leave_blackout")
    monkeypatch.setattr(api, "frappe", stub)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)

    captured = {"calls": []}

    def spy_blackout_periods(company=None, applies_to_leave_type=None, is_active=None):
        captured["calls"].append(
            {"company": company, "applies_to_leave_type": applies_to_leave_type, "is_active": is_active}
        )
        return list(db.get_all_rows)

    monkeypatch.setattr(api, "blackout_periods", spy_blackout_periods)

    return types.SimpleNamespace(api=api, db=db, captured=captured)


# --------------------------------------------------------------------------- #
# evaluate_leave_blackout — employee → company resolution
# --------------------------------------------------------------------------- #
def test_evaluate_resolves_company_from_employee(fake):
    fake.db.company_for = "Gege Demo"
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    # company resolved from Employee record then forwarded to the rule loader
    assert fake.db.get_value_calls == [("Employee", "HR-EMP-0001", "company")]
    assert fake.captured["calls"] and fake.captured["calls"][0]["company"] == "Gege Demo"
    # well-formed decision even with no rules
    assert decision["blocked"] is False
    assert decision["warnings"] == []


def test_evaluate_explicit_company_skips_employee_lookup(fake):
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        company="Acme Co",
        employee="HR-EMP-0001",
    )
    # explicit company wins → no Employee lookup at all
    assert fake.db.get_value_calls == []
    assert fake.captured["calls"][0]["company"] == "Acme Co"
    assert decision["blocked"] is False


def test_evaluate_swallows_get_value_error(fake):
    fake.db.raise_on_get_value = True
    # must not raise; company stays None and the engine still returns a decision
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    assert fake.captured["calls"][0]["company"] is None
    assert decision["blocked"] is False


def test_evaluate_delegates_block_rule_to_engine(fake):
    fake.db.get_all_rows = [
        {
            "name": "BO-1",
            "blackout_name": "Tết cấm nghỉ",
            "company": "Gege Demo",
            "branch": "",
            "department": "",
            "from_date": "2026-06-20",
            "to_date": "2026-06-25",
            "applies_to_leave_type": "",
            "is_active": True,
            "action": "Block",
            "reason": " cao điểm",
            "modified": "2026-06-01",
        }
    ]
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    assert decision["blocked"] is True
    assert any("cấm nghỉ" in w for w in decision["warnings"])
