"""Bench-free unit tests for ``api/employee_services.py`` (NEW-4, hr-gap-audit 🟥)."""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Open"

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
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
    m = importlib.import_module("gege_hr.gege_hr.api.employee_services")
    importlib.reload(m)
    return m, stub


# ── grievance ───────────────────────────────────────────────────────────────
def test_my_grievances_filters_own(mod):
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "GR-1", "employee": "HR-EMP-1", "employee_name": "An", "subject": "Lương"},
        {"name": "GR-2", "employee": "HR-EMP-2", "employee_name": "Binh", "subject": "Khac"},
    ]
    res = m.my_grievances()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "GR-1"


def test_submit_grievance_requires_subject(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_grievance(employee="HR-EMP-1", subject="   ")
    res = m.submit_grievance(employee="HR-EMP-1", grievance_type="Lương", subject="Sai lương")
    assert res["subject"] == "Sai lương"


def test_resolve_grievance_sets_status(mod):
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.resolve_grievance(created["name"], resolution="Đã giải quyết")
    assert res["status"] == "Resolved"
    assert stub.store[("Employee Grievance", created["name"])].status == "Resolved"


def test_all_grievances_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_grievances()


# ── travel ──────────────────────────────────────────────────────────────────
def test_submit_travel_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_travel_request(employee="HR-EMP-1", purpose_of_travel="KH", from_date=None, to_date=None)
    res = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-10", to_date="2026-08-12", estimated_cost=2000000
    )
    assert res["from_date"] == "2026-08-10"


def test_approve_travel_sets_status(mod):
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-10", to_date="2026-08-12"
    )
    res = m.approve_travel_request(created["name"])
    assert res["status"] == "Approved"
    assert stub.store[("Travel Request", created["name"])].status == "Approved"
