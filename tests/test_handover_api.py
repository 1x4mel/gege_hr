"""Bench-free unit tests for ``api/handover.py``.

The pure ranking helper (``utils/handover.suggest_receivers``) is covered by
``test_handover.py``. This file targets the bench-dependent wrapper
:func:`suggest_handover_receivers` — specifically the **company scoping** of the
receiver-suggestion pool introduced for multi-company correctness (the pool must
not leak headcount across company boundaries). A stub ``frappe`` is injected
into ``sys.modules`` for the duration of each test via ``monkeypatch.setitem``
(auto-restored on teardown) so it never leaks into the sibling bench-free tests
that assert ``frappe`` is unimportable.
"""

import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/handover`` to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.now = lambda: "2026-06-22 10:00:00"
    utils.getdate = lambda s=None: __import__("datetime").date.today()
    mod.utils = utils

    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="administrator@example.com")
    # Runtime db is swapped per-test.
    mod.db = None
    return mod


class _FakeDB:
    """Records ``get_list`` filters + serves a configurable Employee meta row."""

    def __init__(self):
        self.table_exists_flag = True
        # meta returned by get_value("Employee", emp, [...], as_dict=True)
        self.meta = {"reports_to": "MGR", "department": "Eng", "company": "Gege Demo"}
        self.list_rows: list[dict] = []
        self.calls: list[dict] = []

    def table_exists(self, doctype):
        return self.table_exists_flag

    def get_value(self, doctype, name, field, as_dict=False):
        return dict(self.meta)

    def get_list(self, doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        self.calls.append({"filters": [list(c) for c in (filters or [])]})
        return [dict(r) for r in self.list_rows]


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import ``api/handover``, wire a fake db."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.handover")
    monkeypatch.setattr(api, "frappe", stub)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)
    return types.SimpleNamespace(api=api, db=db)


def _has_clause(filters, field, value):
    return [field, "=", value] in filters


# --------------------------------------------------------------------------- #
# Company scoping of the receiver-suggestion pool
# --------------------------------------------------------------------------- #
def test_suggest_scopes_pool_by_departing_employee_company(fake):
    fake.db.meta = {
        "reports_to": "MGR",
        "department": "Eng",
        "company": "Gege Demo",
    }
    # A same-company peer + a direct report both survive.
    fake.db.list_rows = [
        {"name": "ALICE", "employee_name": "Alice", "reports_to": "ME", "department": "Eng"},
        {"name": "BOB", "employee_name": "Bob", "reports_to": "MGR", "department": "Eng"},
    ]
    out = fake.api.suggest_handover_receivers(employee="ME")
    # Both pool queries carry the company clause.
    assert fake.db.calls, "expected at least one get_list call"
    for call in fake.db.calls:
        assert _has_clause(call["filters"], "company", "Gege Demo")
    # Suggestion list is well-formed (helper excludes self).
    assert all(r["name"] != "ME" for r in out)


def test_suggest_direct_reports_query_also_company_scoped(fake):
    fake.db.meta = {"reports_to": None, "department": None, "company": "Acme Corp"}
    fake.db.list_rows = []
    fake.api.suggest_handover_receivers(employee="ME")
    # Two get_list calls: department pool + direct-reports pool. Both scoped.
    assert len(fake.db.calls) == 2
    for call in fake.db.calls:
        assert _has_clause(call["filters"], "company", "Acme Corp")
        # direct-reports query always carries the reports_to==emp clause
        assert _has_clause(call["filters"], "reports_to", "ME") or _has_clause(
            call["filters"], "status", "Active"
        )


def test_suggest_no_company_clause_when_company_blank(fake):
    # Older bench / Employee without company → fall back to unscoped pool.
    fake.db.meta = {"reports_to": "MGR", "department": "Eng", "company": None}
    fake.db.list_rows = [
        {"name": "ALICE", "employee_name": "Alice", "reports_to": "ME", "department": "Eng"},
    ]
    out = fake.api.suggest_handover_receivers(employee="ME")
    assert fake.db.calls
    for call in fake.db.calls:
        assert not any(c[0] == "company" for c in call["filters"])
    assert any(r["name"] == "ALICE" for r in out)


def test_suggest_returns_empty_when_table_missing(fake):
    fake.db.table_exists_flag = False
    assert fake.api.suggest_handover_receivers(employee="ME") == []
    assert fake.db.calls == []


def test_suggest_degrades_to_empty_on_load_failure(fake):
    fake.db.meta = {"reports_to": "MGR", "department": "Eng", "company": "Gege Demo"}

    def boom(*a, **k):
        raise RuntimeError("db down")

    fake.db.get_list = boom
    assert fake.api.suggest_handover_receivers(employee="ME") == []
