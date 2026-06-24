"""Unit tests for VN Payroll Component Mapping wiring + VN Overtime Request loader.

Bench-free: the loaders in :mod:`calc` import ``frappe`` lazily and are guarded,
so we inject a lightweight fake ``frappe`` into ``sys.modules`` to exercise the
company-resolution and table-exists paths.
"""

import sys
import types

import pytest

from gege_hr.gege_hr.utils import calc


# --------------------------------------------------------------------------- #
# Fake frappe plumbing
# --------------------------------------------------------------------------- #
class _FakeDB:
    """Minimal stand-in for the bits of ``frappe.db`` the loaders touch."""

    def __init__(self):
        self._tables = set()
        self._store = {}  # doctype -> list[dict]

    def table_exists(self, name):
        # Frappe passes "tab<DocType>"; normalise.
        return name.replace("tab", "", 1) in self._tables or name in self._tables

    def register(self, doctype, rows):
        self._tables.add(doctype)
        self._store[doctype] = list(rows)

    def get_all(self, doctype, filters=None, fields=None, pluck=None):
        rows = self._store.get(doctype, [])
        out = []
        for r in rows:
            ok = True
            if filters:
                for k, v in filters.items():
                    if isinstance(v, list):
                        op, val = v[0], v[1]
                        rv = r.get(k)
                        if op == "in" and rv not in val:
                            ok = False
                        elif op == "not in" and rv in val:
                            ok = False
                        elif op == "<" and not (rv is not None and rv < val):
                            ok = False
                        elif op == "!=" and rv == val:
                            ok = False
                    else:
                        if r.get(k) != v:
                            ok = False
                    if not ok:
                        break
            if ok:
                out.append(r)
        if pluck:
            return [r.get(pluck) for r in out]
        if fields and fields != ["*"]:
            return [{f: r.get(f) for f in fields} for r in out]
        return out


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()


@pytest.fixture
def fake_frappe(monkeypatch):
    ff = _FakeFrappe()
    mod = types.SimpleNamespace(db=ff.db)
    monkeypatch.setitem(sys.modules, "frappe", mod)
    return ff


class _Doc:
    """Tiny attribute container mimicking a Frappe document."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# --------------------------------------------------------------------------- #
# _load_holiday_multipliers
# --------------------------------------------------------------------------- #
def test_multipliers_explicit_child_attr():
    """Forward-compat: an explicit child table on the policy wins."""
    doc = _Doc(
        company="Acme",
        holiday_multipliers=[
            _Doc(segment_type="OT", multiplier="1.75"),
            _Doc(segment_type="Regular", multiplier="1.0"),
        ],
    )
    assert calc._load_holiday_multipliers(doc) == {"OT": 1.75, "Regular": 1.0}


def test_multipliers_no_company_returns_empty():
    assert calc._load_holiday_multipliers(_Doc(company=None)) == {}


def test_multipliers_resolve_from_payroll_component_mapping(fake_frappe):
    fake_frappe.db.register(
        "VN Payroll Component Mapping",
        [
            {"segment_type": "OT", "multiplier": 1.75, "day_type": "All", "company": "Acme"},
            {"segment_type": "OT Holiday", "multiplier": 3.5, "day_type": "All", "company": "Acme"},
            # Non-All day types must be ignored by the loader.
            {"segment_type": "Regular", "multiplier": 9.0, "day_type": "Holiday", "company": "Acme"},
            # Inactive rule ignored.
            {"segment_type": "Regular", "multiplier": 2.0, "day_type": "All", "company": "Acme"},
        ],
    )
    # Mark the last row inactive for the filter (is_active=1).
    fake_frappe.db._store["VN Payroll Component Mapping"][-1]["is_active"] = 0
    for r in fake_frappe.db._store["VN Payroll Component Mapping"][:-1]:
        r["is_active"] = 1

    out = calc._load_holiday_multipliers(_Doc(company="Acme"))
    assert out == {"OT": 1.75, "OT Holiday": 3.5}


def test_get_holiday_multiplier_uses_mapping_override():
    """An explicit override in the policy dict beats the built-in default."""
    policy = {"holiday_multipliers": {"OT": 1.75}}
    # Default for plain OT is 1.5; override must win.
    assert calc.get_holiday_multiplier("OT", is_holiday=False, is_night=False, policy=policy) == 1.75


def test_get_holiday_multiplier_default_when_no_override():
    policy = {"holiday_multipliers": {}}
    assert calc.get_holiday_multiplier("OT", is_holiday=False, is_night=False, policy=policy) == 1.5
    assert calc.get_holiday_multiplier("Regular", is_holiday=True, is_night=False, policy=policy) == 2.0


def test_multipliers_table_missing_returns_empty(fake_frappe):
    """When the DocType isn't installed yet, resolution degrades to {}."""
    assert calc._load_holiday_multipliers(_Doc(company="Acme")) == {}


# --------------------------------------------------------------------------- #
# get_approved_ot_requests
# --------------------------------------------------------------------------- #
def test_ot_requests_returns_empty_without_frappe():
    # Outside a bench frappe import fails -> guarded [].
    assert calc.get_approved_ot_requests("EMP-1", "2026-06-21") == []
    assert calc.get_approved_ot_requests(None, None) == []


def test_ot_requests_table_missing_returns_empty(fake_frappe):
    assert calc.get_approved_ot_requests("EMP-1", "2026-06-21") == []


def test_ot_requests_filters_by_workflow_state(fake_frappe):
    fake_frappe.db.register(
        "VN Overtime Request",
        [
            {
                "name": "OR-1",
                "employee": "E1",
                "work_date": "2026-06-21",
                "workflow_state": "Approved",
                "docstatus": 1,
                "from_datetime": "2026-06-21 17:00:00",
                "to_datetime": "2026-06-21 19:00:00",
            },
            {
                "name": "OR-2",
                "employee": "E1",
                "work_date": "2026-06-21",
                "workflow_state": "Confirmed",
                "docstatus": 1,
                "from_datetime": "2026-06-21 19:00:00",
                "to_datetime": "2026-06-21 20:00:00",
            },
            {
                "name": "OR-3",
                "employee": "E1",
                "work_date": "2026-06-21",
                "workflow_state": "Draft",
                "docstatus": 0,
                "from_datetime": "2026-06-21 17:00:00",
                "to_datetime": "2026-06-21 18:00:00",
            },
            {
                "name": "OR-4",
                "employee": "E1",
                "work_date": "2026-06-21",
                "workflow_state": "Rejected",
                "docstatus": 1,
                "from_datetime": "2026-06-21 17:00:00",
                "to_datetime": "2026-06-21 18:00:00",
            },
        ],
    )
    rows = calc.get_approved_ot_requests("E1", "2026-06-21")
    names = sorted(r["name"] for r in rows)
    # Only Approved + Confirmed survive; Draft / Rejected excluded.
    assert names == ["OR-1", "OR-2"]
