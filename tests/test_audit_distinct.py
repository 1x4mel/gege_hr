"""Bench-free unit tests for ``audit_distinct`` (plan audit-center B2, prefix AD).

Same stub-frappe pattern as ``test_audit_csv.py`` / ``test_audit_stats.py``.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class StubFrappe:
    def __init__(self, roles=("HR Manager",), rows=None, table_exists=True):
        self.roles = list(roles)
        self.rows = rows if rows is not None else []
        self.get_all_calls = []
        self.last_error = None
        self.table_exists_flag = table_exists
        outer = self

        class _DB:
            def table_exists(inner, doctype):
                return outer.table_exists_flag

            def get_all(inner, doctype, filters=None, or_filters=None, fields=None, **kw):
                outer.get_all_calls.append({"doctype": doctype, "filters": filters, "kw": kw})
                return list(outer.rows)

        self.db = _DB()

    def throw(self, msg, exc=None):
        raise (exc or FrappeError)(msg)

    def get_roles(self, user):
        return list(self.roles)

    def log_error(self, title=None, message=None):
        self.last_error = title


@pytest.fixture()
def distinct_mod(monkeypatch):
    def _make(roles=("HR Manager",), rows=None, table_exists=True):
        stub = StubFrappe(roles=roles, rows=rows, table_exists=table_exists)

        frappe_mod = types.ModuleType("frappe")
        frappe_mod._ = lambda s: s
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.throw = stub.throw
        frappe_mod.get_roles = stub.get_roles
        frappe_mod.db = stub.db
        frappe_mod.log_error = stub.log_error
        frappe_mod.PermissionError = FrappeError
        frappe_mod.session = types.SimpleNamespace(user="hr@example.com")

        utils = types.ModuleType("frappe.utils")
        utils.getdate = lambda v=None: __import__("datetime").date(2026, 8, 28)
        frappe_mod.utils = utils

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.audit"))
        return mod, stub

    return _make


# AD1 — dedup + frequency-ranked + shape {"value", "count"}
def test_ad1_dedup_ranked_by_frequency(distinct_mod):
    rows = [{"actor": "a@x.vn"} for _ in range(3)] + [{"actor": "b@x.vn"}, {"actor": "c@x.vn"}]
    mod, _ = distinct_mod(rows=rows)
    res = mod.audit_distinct(field="actor")
    assert res[0] == {"value": "a@x.vn", "count": 3}
    assert [r["value"] for r in res] == ["a@x.vn", "b@x.vn", "c@x.vn"]


# AD1b — client search narrows the options
def test_ad1b_search_narrows(distinct_mod):
    rows = [{"actor": "hr.manager@x.vn"}, {"actor": "emp@x.vn"}]
    mod, _ = distinct_mod(rows=rows)
    res = mod.audit_distinct(field="actor", search="manager")
    assert [r["value"] for r in res] == ["hr.manager@x.vn"]


# AD2 — invalid field is thrown out (whitelist)
def test_ad2_invalid_field_throws(distinct_mod):
    mod, _ = distinct_mod(rows=[{"actor": "a@x.vn"}])
    with pytest.raises(FrappeError):
        mod.audit_distinct(field="name")


# AD3 — role gate: non-HR is thrown out
def test_ad3_non_hr_rejected(distinct_mod):
    mod, _ = distinct_mod(roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.audit_distinct(field="actor")


# AD4 — limit clamped by MAX_PAGE_SIZE (200); default call caps at 100
def test_ad4_limit_clamp(distinct_mod):
    rows = [{"actor": f"u{i:03d}@x.vn"} for i in range(250)]
    mod, _ = distinct_mod(rows=rows)
    assert len(mod.audit_distinct(field="actor", limit=99999)) == 200
    assert len(mod.audit_distinct(field="actor")) == 100
