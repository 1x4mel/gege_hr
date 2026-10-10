"""Bench-free unit tests for ``audit_stats`` (plan audit-center B3, prefix AS).

Same stub-frappe pattern as ``test_audit_csv.py``. ``getdate`` is REAL (the
per-day series buckets depend on today's clock), so the tests are written
clock-agnostic: they seed rows relative to ``date.today()``.
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


def _row(audit_type="Leave Submit", actor="hr@example.com", created=None, **kw):
    base = {
        "audit_type": audit_type,
        "actor": actor,
        "employee": "HR-EMP-001",
        "company": "GeGe Esport",
        "created_at": created or (datetime.date.today().isoformat() + " 08:00:00"),
    }
    base.update(kw)
    return base


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
def stats_mod(monkeypatch):
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
        utils.getdate = lambda v=None: (
            datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
        )
        frappe_mod.utils = utils

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.audit"))
        return mod, stub

    return _make


# AS1 — group counts, top-20 cap, sorted count desc
def test_as1_groups_top20_sorted_desc(stats_mod):
    rows = [_row(audit_type=f"T{i}") for i in range(25)]
    rows += [_row(audit_type="T0") for _ in range(5)]  # T0 wins with 6
    mod, _ = stats_mod(rows=rows)
    res = mod.audit_stats(group_by="audit_type")
    assert res["total"] == 30
    assert len(res["groups"]) == 20
    assert res["groups"][0] == {"label": "T0", "count": 6}
    counts = [g["count"] for g in res["groups"]]
    assert counts == sorted(counts, reverse=True)


# AS2 — invalid group_by is thrown out (whitelist, never interpolated)
def test_as2_invalid_group_by_throws(stats_mod):
    mod, _ = stats_mod(rows=[_row()])
    with pytest.raises(FrappeError):
        mod.audit_stats(group_by="name; drop table")


# AS3 — days clamped to [7, 90]
def test_as3_days_clamped(stats_mod):
    mod, _ = stats_mod(rows=[])
    assert len(mod.audit_stats(days=365)["series"]) == 90
    assert len(mod.audit_stats(days=1)["series"]) == 7


# AS4 — dense per-day series: zero-filled, today bucket counted
def test_as4_dense_series(stats_mod):
    today = datetime.date.today()
    rows = [
        _row(created=today.isoformat() + " 09:00:00"),
        _row(created=today.isoformat() + " 15:00:00"),
        _row(created=(today - datetime.timedelta(days=2)).isoformat() + " 10:00:00"),
    ]
    mod, _ = stats_mod(rows=rows)
    res = mod.audit_stats(days=7)
    assert len(res["series"]) == 7
    assert res["series"][-1]["date"] == today.isoformat()
    assert res["series"][-1]["count"] == 2
    assert res["series"][-3]["count"] == 1  # two days ago
    assert sum(s["count"] for s in res["series"]) == 3


# AS5 — role gate: non-HR is thrown out
def test_as5_non_hr_rejected(stats_mod):
    mod, _ = stats_mod(roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.audit_stats()


# AS6 — table not migrated → empty shape, no crash
def test_as6_table_missing_empty_shape(stats_mod):
    mod, _ = stats_mod(rows=[_row()], table_exists=False)
    assert mod.audit_stats() == {"total": 0, "groups": [], "series": []}
