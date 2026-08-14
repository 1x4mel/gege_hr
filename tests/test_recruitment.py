"""Bench-free unit tests for ``api/recruitment.py`` (NEW-5, hr-gap-audit 🟥)."""

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


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.opening_designation = "Sale Lead"
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

        def get_value(self, doctype, name, field=None, as_dict=False):
            if doctype == "Job Opening" and name:
                return self.fr.opening_designation
            if doctype == "Employee" and isinstance(name, dict):
                return self.fr.employee_for_user
            return None

    @property
    def db(self):
        return self._DB(self)

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

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
    m = importlib.import_module("gege_hr.gege_hr.api.recruitment")
    importlib.reload(m)
    return m, stub


# ── recruitment ──────────────────────────────────────────────────────────────
def test_list_job_openings(mod):
    m, stub = mod
    stub.list_rows["Job Opening"] = [{"name": "JO-1", "designation": "Sale Lead", "status": "Open"}]
    res = m.list_job_openings()
    assert res["total"] == 1


def test_submit_job_application_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_job_application(applicant_name="", email_id="x@y.z")
    res = m.submit_job_application(job_opening="JO-1", applicant_name="Nguyen A", email_id="a@b.c", phone_number="090")
    assert res["applicant_name"] == "Nguyen A"
    doc = m.frappe_new_doc if hasattr(m, "frappe_new_doc") else None
    # the applicant doc is stored by the stub
    assert any(k[0] == "Job Applicant" for k in _.store)


def test_all_applicants_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_applicants()


# ── training ─────────────────────────────────────────────────────────────────
def test_list_training_events(mod):
    m, stub = mod
    stub.list_rows["Training Event"] = [{"name": "TE-1", "event_name": "Sales 101", "status": "Scheduled"}]
    res = m.list_training_events()
    assert res["total"] == 1


def test_enroll_training_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.enroll_training(employee=None, training_event=None)
    res = m.enroll_training(employee="HR-EMP-1", training_event="TE-1")
    assert res["training_event"] == "TE-1"


def test_my_training_filters_own(mod):
    m, stub = mod
    stub.list_rows["Employee Training"] = [
        {"name": "ET-1", "employee": "HR-EMP-1", "employee_name": "An"},
        {"name": "ET-2", "employee": "HR-EMP-2", "employee_name": "Binh"},
    ]
    res = m.my_training()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "ET-1"
