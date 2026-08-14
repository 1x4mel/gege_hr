"""Bench-free unit tests for ``api/appraisal.py`` (NEW-2, hr-gap-audit 🟥 Appraisal).

Pure helpers (``status_for_progress`` / ``goal_completion``) need no frappe. The
Goal I/O is exercised with a stub-frappe: own-goal filter, submit creates a Goal,
update_goal_progress clamps + auto-status, and the manager-only gate on all_goals.
"""

import importlib
import sys
import types

import pytest


class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.progress = 0
        self.status = "Pending"

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
        self.employee_company = "Gege"
        self.goal_employee = "HR-EMP-1"
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
            if doctype == "Goal":
                # by name → employee (for update_goal_progress ownership)
                row = next((r for r in self.fr.list_rows.get("Goal", []) if r.get("name") == key), None)
                return row.get("employee") if row else self.fr.goal_employee
            return None

        def set_value(self, doctype, name, fields, *a, **k):
            for r in self.fr.list_rows.get(doctype, []):
                if r.get("name") == name:
                    r.update(fields or {})

    @property
    def db(self):
        return self._DB(self)

    @property
    def utils(self):
        return types.SimpleNamespace(today=lambda: "2026-08-08")

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def get_all(self, doctype, filters=None, or_filters=None, fields=None, order_by=None, limit_start=0, limit_page_length=0, pluck=None, **k):
        rows = list(self.list_rows.get(doctype, []))

        def keep(r):
            if filters:
                for cond in filters:
                    op = cond[1] if len(cond) > 2 else "="
                    if op == "=" and r.get(cond[0]) != cond[2]:
                        return False
                    if op == "!=" and r.get(cond[0]) == cond[2]:
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
    m = importlib.import_module("gege_hr.gege_hr.api.appraisal")
    importlib.reload(m)
    return m, stub


# ── pure ────────────────────────────────────────────────────────────────────
def test_status_for_progress(mod):
    m, _ = mod
    assert m.status_for_progress(0) == "Pending"
    assert m.status_for_progress(40) == "In Progress"
    assert m.status_for_progress(100) == "Completed"
    assert m.status_for_progress(150) == "Completed"  # clamp handled by caller; status just >=100


def test_goal_completion_average(mod):
    m, _ = mod
    goals = [{"progress": 0}, {"progress": 100}, {"progress": 50}]
    assert m.goal_completion(goals) == 50.0
    assert m.goal_completion([]) == 0.0
    # attr-access rows too
    rows = [_AttrDict({"progress": 80}), _AttrDict({"progress": 20})]
    assert m.goal_completion(rows) == 50.0


# ── I/O ─────────────────────────────────────────────────────────────────────
def test_my_goals_filters_own(mod):
    m, stub = mod
    stub.list_rows["Goal"] = [
        {"name": "G-1", "employee": "HR-EMP-1", "employee_name": "An", "goal_name": "Doanh so", "progress": 40},
        {"name": "G-2", "employee": "HR-EMP-2", "employee_name": "Binh", "goal_name": "Khac", "progress": 10},
    ]
    res = m.my_goals()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "G-1"
    assert res["completion"] == 40.0


def test_submit_goal_creates_doc(mod):
    m, stub = mod
    res = m.submit_goal(employee="HR-EMP-1", goal_name="Dat 100 KPI", kra="Sale", end_date="2026-12-31")
    doc = stub.store[("Goal", res["name"])]
    assert doc.goal_name == "Dat 100 KPI"
    assert res["status"] == "Pending"


def test_submit_goal_requires_name(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_goal(employee="HR-EMP-1", goal_name="   ")


def test_update_goal_progress_clamps_and_status(mod):
    m, stub = mod
    stub.list_rows["Goal"] = [{"name": "G-1", "employee": "HR-EMP-1", "progress": 0, "status": "Pending"}]
    res = m.update_goal_progress("G-1", 150)  # clamped to 100
    assert res["progress"] == 100.0
    assert res["status"] == "Completed"
    # the default stub set_value persists the update on the seeded row
    row = stub.list_rows["Goal"][0]
    assert row["progress"] == 100.0
    assert row["status"] == "Completed"


def test_all_goals_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_goals()
