"""Bench-free unit tests for ``utils/pagination.py`` (DNA §6.6 A).

Covers the pure helpers (``as_int`` / ``bucket_counts`` / ``distinct_count``)
and the frappe-dependent readers (``count_all`` / ``all_rows`` / ``page_slice``)
using the same stub-frappe pattern as ``test_blackout_api.py`` /
``test_audit_api.py`` (``monkeypatch.setitem(sys.modules, "frappe", stub)`` so it
never leaks into the sibling bench-free tests that assert frappe is unimportable).

The key invariant (DNA §6.6 A): ``count_all`` must count via
``get_all(fields=["name"], limit_page_length=0).len`` — **not** ``db.count``,
which silently ignores ``or_filters`` and would under-count a broad-search list.
"""

import importlib
import sys
import types

import pytest


def _build_stub_frappe(rows=None):
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.log_error = lambda *a, **k: None

    class _DB:
        def __init__(self):
            self.rows = list(rows or [])

        def get_all(
            self,
            doctype,
            filters=None,
            or_filters=None,
            fields=None,
            order_by=None,
            limit_start=0,
            limit_page_length=0,
        ):
            data = list(self.rows)
            if limit_page_length:
                start = int(limit_start or 0)
                return data[start : start + int(limit_page_length)]
            return data

    mod.db = _DB()
    return mod


@pytest.fixture
def pagination_mod(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    # pagination.py imports frappe lazily inside its functions, so the sys.modules
    # stub above is enough — no module-level attr to patch.
    mod = importlib.import_module("gege_hr.gege_hr.utils.pagination")
    return mod, stub


# ── pure helpers ────────────────────────────────────────────────────────────
def test_as_int(pagination_mod):
    mod, _ = pagination_mod
    assert mod.as_int("3", 0) == 3
    assert mod.as_int(None, 7) == 7
    assert mod.as_int("x", 9) == 9
    assert mod.as_int(5, 0) == 5


def test_bucket_counts(pagination_mod):
    mod, _ = pagination_mod
    rows = [{"status": "Open"}, {"status": "Open"}, {"status": "Resolved"}, {"status": None}]
    assert mod.bucket_counts(rows, "status") == {"Open": 2, "Resolved": 1, None: 1}
    assert mod.bucket_counts([], "status") == {}


def test_distinct_count_ignores_empty(pagination_mod):
    mod, _ = pagination_mod
    rows = [
        {"employee": "A"},
        {"employee": "B"},
        {"employee": "A"},
        {"employee": ""},
        {"employee": None},
    ]
    assert mod.distinct_count(rows, "employee") == 2


# ── frappe-dependent readers ────────────────────────────────────────────────
def test_count_all_uses_get_all_len_with_or_filters(pagination_mod):
    mod, stub = pagination_mod
    stub.db.rows = [{"name": f"R{i}"} for i in range(7)]
    # count_all must honour or_filters (db.count would ignore it) — DNA §6.6 A.
    assert mod.count_all("VN Audit Event", or_filters=[["a", "like", "%x%"]]) == 7


def test_count_all_swallows_db_error(pagination_mod, monkeypatch):
    mod, stub = pagination_mod

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(stub.db, "get_all", boom)
    assert mod.count_all("VN Audit Event") == 0


def test_page_slice_total_and_window(pagination_mod):
    mod, stub = pagination_mod
    stub.db.rows = [{"name": f"R{i}", "v": i} for i in range(25)]
    res = mod.page_slice("VN Audit Event", fields=["name", "v"], page=2, page_size=10)
    assert res["total"] == 25
    assert [r["v"] for r in res["data"]] == list(range(10, 20))


def test_page_slice_transform_and_out_of_range(pagination_mod):
    mod, stub = pagination_mod
    stub.db.rows = [{"name": "R1"}]
    # transform applies to each row of the page.
    res = mod.page_slice(
        "X",
        fields=["name"],
        page=1,
        page_size=10,
        transform=lambda r: {"id": r["name"]},
    )
    assert res["total"] == 1
    assert res["data"] == [{"id": "R1"}]
    # An out-of-range page yields an empty page; the total is still the full set.
    res2 = mod.page_slice("X", fields=["name"], page=99, page_size=10)
    assert res2["total"] == 1
    assert res2["data"] == []


def test_all_rows_returns_everything(pagination_mod):
    mod, stub = pagination_mod
    stub.db.rows = [{"name": f"R{i}"} for i in range(3)]
    rows = mod.all_rows("X", fields=["name"])
    assert len(rows) == 3
