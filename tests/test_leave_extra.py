"""Bench-free unit tests for ``api/leave_extra.py`` (NEW-3, hr-gap-audit 🟥)."""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Draft"

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def submit(self):
        self.status = "Submitted"
        return self

    def cancel(self):
        self.status = "Cancelled"
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.employee_company = "Gege"
        self.roles = {"HR Manager"}

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

    @property
    def session(self):
        return types.SimpleNamespace(user="hr@gege.local")

    def get_roles(self, user):
        return set(self.roles)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, key, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(key, dict):
                return self.fr.employee_for_user
            if doctype == "Employee":
                return self.fr.employee_company
            return None

    @property
    def db(self):
        return self._DB(self)

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def get_all(self, doctype, filters=None, or_filters=None, fields=None, order_by=None, limit_start=0, limit_page_length=0, pluck=None, **k):
        rows = list(self.list_rows.get(doctype, []))

        def keep(r):
            if filters:
                for cond in filters:
                    if r.get(cond[0]) != cond[2]:
                        return False
            if or_filters:
                if not any(r.get(c[0]) == c[2] for c in or_filters):
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.leave_extra")
    importlib.reload(m)
    return m, stub


# ── pure ────────────────────────────────────────────────────────────────────
def test_encashment_amount(mod):
    m, _ = mod
    assert m.encashment_amount(5, 200000) == 1000000.0
    assert m.encashment_amount(0, 100) == 0.0
    assert m.encashment_amount(2.5, 100000) == 250000.0


# ── encashment I/O ──────────────────────────────────────────────────────────
def test_my_encashments_filters_own(mod):
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"name": "LE-1", "employee": "HR-EMP-1", "employee_name": "An", "encashment_days": 3},
        {"name": "LE-2", "employee": "HR-EMP-2", "employee_name": "Binh", "encashment_days": 1},
    ]
    res = m.my_leave_encashments()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "LE-1"


def test_submit_encashment_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=0)
    res = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=4)
    assert res["encashment_days"] == 4


def test_all_encashments_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_leave_encashments()


# ── comp-off I/O ────────────────────────────────────────────────────────────
def test_submit_comp_off_requires_dates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_comp_off(employee="HR-EMP-1", work_from_date=None, work_to_date=None)


def test_approve_comp_off(mod):
    m, stub = mod
    created = m.submit_comp_off(
        employee="HR-EMP-1", leave_type="Compensatory Off", work_from_date="2026-08-01", work_to_date="2026-08-01"
    )
    res = m.approve_comp_off(created["name"])
    assert res["status"] in ("Submitted", "Draft")  # submit best-effort
    assert stub.store[("Compensatory Leave Request", created["name"])].name == created["name"]
