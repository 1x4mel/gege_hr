"""Bench-free tests for the batched punch → Work Session lookup behind
``utils.calc.heal_stale_sessions`` / ``recalc_stale_sessions``.

PERF 2026-10-04: the heal-on-read did one query per punch (1 009 queries,
4.6 s for September in /hr/team/attendance, also on every today_status). The
lookup now loads every candidate session in ONE query and applies the same
rules in memory.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime

import pytest

WS_DT = "VN Attendance Work Session"


class _Row(dict):
    def __getattr__(self, key):
        return self.get(key)


def _ws(name, emp, wd, ps, pe, calc="2026-09-30 00:00:00", **kw):
    return _Row(
        name=name,
        employee=emp,
        work_date=wd,
        shift_instance=f"SI-{name}",
        planned_start=ps,
        planned_end=pe,
        calculated_at=calc,
        actual_checkin=kw.get("actual_checkin", ps),
        actual_checkout=kw.get("actual_checkout", pe),
    )


@pytest.fixture
def calc_env(monkeypatch):
    calls = {"get_all": [], "persist": []}
    store = {"Employee Checkin": [], WS_DT: []}

    mod = types.ModuleType("frappe")

    def get_all(doctype, filters=None, fields=None, **kw):
        calls["get_all"].append(doctype)
        rows = store[doctype]
        if doctype == WS_DT:
            emps = set(filters["employee"][1])
            lo, hi = filters["work_date"][1]
            rows = [r for r in rows if r["employee"] in emps and lo <= r["work_date"] <= hi]
        return [_Row(r) for r in rows]

    mod.get_all = get_all
    mod.log_error = lambda *a, **k: None
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v: v if isinstance(v, date) else datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    utils.add_to_date = lambda *a, **k: "2026-09-01 00:00:00"
    mod.utils = utils
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    from gege_hr.gege_hr.utils import calc

    monkeypatch.setattr(
        calc, "persist_work_session", lambda si, calculate_mode=None: calls["persist"].append(si) or si
    )
    return calc, store, calls


def test_finder_same_rules_one_query(calc_env):
    calc, store, calls = calc_env
    store[WS_DT] = [
        _ws("D22", "E1", "2026-09-22", "2026-09-22 08:00:00", "2026-09-22 20:00:00"),
        _ws("N27", "E2", "2026-09-27", "2026-09-27 20:00:00", "2026-09-28 08:00:00"),
        _ws("N28", "E2", "2026-09-28", "2026-09-28 20:00:00", "2026-09-29 08:00:00"),
    ]
    find = calc._ws_finder({("E1", "2026-09-22"), ("E2", "2026-09-28")})
    assert calls["get_all"] == [WS_DT]  # ONE query for every pair
    assert find("E1", "2026-09-22", "2026-09-22 20:13:00").name == "D22"
    # night OUT 08:00 on 28/09: outside 28/09's window → the session of 27/09
    assert find("E2", "2026-09-28", "2026-09-28 08:00:00").name == "N27"
    assert find("E2", "2026-09-28", "2026-09-28 20:05:00").name == "N28"
    assert find("E9", "2026-09-28", "2026-09-28 08:00:00") is None
    # the single-punch helper keeps its contract
    assert calc._find_ws_for_punch("E2", "2026-09-28", "2026-09-28 08:00:00").name == "N27"


def test_heal_one_ws_query_and_each_session_once(calc_env):
    """1 punch query + 1 session query, whatever the punch count; a stale
    session reached by punches of two days is recalculated once."""
    calc, store, calls = calc_env
    store[WS_DT] = [
        _ws(
            "N27",
            "E2",
            "2026-09-27",
            "2026-09-27 20:00:00",
            "2026-09-28 08:00:00",
            calc="2026-09-27 00:00:00",
        ),
        _ws("D22", "E1", "2026-09-22", "2026-09-22 08:00:00", "2026-09-22 20:00:00"),
    ]
    store["Employee Checkin"] = [
        {"name": "P1", "employee": "E2", "time": "2026-09-27 20:01:00", "creation": "2026-09-27 20:01:00"},
        {"name": "P2", "employee": "E2", "time": "2026-09-28 08:00:00", "creation": "2026-09-28 08:00:00"},
        {"name": "P3", "employee": "E1", "time": "2026-09-22 08:03:00", "creation": "2026-09-22 08:03:00"},
    ]
    assert calc.heal_stale_sessions("2026-09-22", "2026-09-28") == 1
    assert calls["persist"] == ["SI-N27"]
    assert calls["get_all"] == ["Employee Checkin", WS_DT]
