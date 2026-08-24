"""Bench-free unit tests for ``api/benefits_admin.py`` (NEW-6, hr-gap-audit 🟥)."""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
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

    @property
    def utils(self):
        return types.SimpleNamespace(today=lambda: "2026-08-08")

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_all(
        self,
        doctype,
        filters=None,
        or_filters=None,
        fields=None,
        order_by=None,
        limit_start=0,
        limit_page_length=0,
        pluck=None,
        **k,
    ):
        rows = list(self.list_rows.get(doctype, []))

        def keep(r):
            if filters:
                for cond in filters:
                    if r.get(cond[0]) != cond[2]:
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
    m = importlib.import_module("gege_hr.gege_hr.api.benefits_admin")
    importlib.reload(m)
    return m, stub


def test_my_benefit_applications_filters_own(mod):
    m, stub = mod
    stub.list_rows["Employee Benefit Application"] = [
        {"name": "BA-1", "employee": "HR-EMP-1"},
        {"name": "BA-2", "employee": "HR-EMP-2"},
    ]
    res = m.my_benefit_applications()
    assert res["total"] == 1


def test_submit_benefit_creates(mod):
    m, _ = mod
    res = m.submit_benefit_application(employee="HR-EMP-1", max_benefits=500000)
    assert res["name"]


def test_all_benefit_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_benefit_applications()


def test_list_gratuities_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.list_gratuities()


def test_create_promotion_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.create_promotion(employee=None)
    res = m.create_promotion(employee="HR-EMP-1")
    assert res["name"]


def test_create_promotion_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.create_promotion(employee="HR-EMP-1")
