"""Bench-free unit tests for ``api/onboarding.py`` (FIX-2, hr-gap-audit I-2).

Pure helpers (``compute_progress`` / ``derive_status`` / ``instantiate_task``) are
tested with no frappe stub. The lifecycle I/O (``start_onboarding`` /
``complete_task`` / ``list_templates`` / ``onboarding_list``) is exercised against
a stub-frappe (same ``monkeypatch.setitem(sys.modules, "frappe", stub)`` pattern).
"""

import importlib
import sys
import types

import pytest


class _AttrDict(dict):
    """dict with attribute get/set — mirrors a Frappe child-row field set."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class _PermissionError(Exception):
    pass


class _Doc:
    _counter = 0

    def __init__(self, doctype, payload=None, store=None):
        self.doctype = doctype
        self._store = store
        self.tasks = []
        for k, v in (payload or {}).items():
            if k == "tasks":
                self.tasks = [_AttrDict(t) if isinstance(t, dict) else t for t in (v or [])]
            else:
                setattr(self, k, v)
        self.name = (payload or {}).get("name")

    def append(self, field, row):
        lst = getattr(self, field, None)
        if not isinstance(lst, list):
            lst = []
            setattr(self, field, lst)
        lst.append(_AttrDict(row) if isinstance(row, dict) else row)

    def insert(self, ignore_permissions=False):
        _Doc._counter += 1
        if not self.name:
            self.name = f"{self.doctype.replace(' ', '-')}-{_Doc._counter:04d}"
        if self._store is not None:
            self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self

    def add_comment(self, *a, **k):
        return None


class _Frappe:
    def __init__(self):
        self.store = {}  # (doctype, name) -> _Doc
        self.rows = {}  # doctype -> [dict rows] for get_all/count
        self.roles = ["HR Manager"]
        utils = types.SimpleNamespace(now_datetime=lambda: "2026-08-08 10:00:00")
        self.utils = utils
        self.session = types.SimpleNamespace(user="hr@gege.local")
        self.db = types.SimpleNamespace(count=lambda doctype, filters=None: len(self.rows.get(doctype, [])))

    def whitelist(self, fn=None, **kw):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def only_for(self, roles):
        if not (set(roles) & set(self.roles)):
            raise _PermissionError("not allowed")

    def throw(self, msg, *_a, **_k):
        raise Exception(msg)

    def new_doc(self, doctype):
        return _Doc(doctype, store=self.store)

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            payload = doctype
            return _Doc(payload.get("doctype"), payload, store=self.store)
        doc = self.store.get((doctype, name))
        if doc is None:  # back-fill from list rows
            for r in self.rows.get(doctype, []):
                if r.get("name") == name:
                    doc = _Doc(doctype, r, store=self.store)
                    self.store[(doctype, name)] = doc
                    break
        return doc

    def get_all(
        self, doctype, filters=None, fields=None, order_by=None, limit_start=0, limit_page_length=0, **k
    ):
        rows = list(self.rows.get(doctype, []))

        def keep(r):
            if not filters:
                return True
            if isinstance(filters, dict):
                return all(r.get(k) == v for k, v in filters.items())
            for cond in filters:
                if r.get(cond[0]) != cond[2]:
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows] if isinstance(fields, list) else rows
        return rows


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.onboarding")
    importlib.reload(m)
    return m, stub


# ── pure helpers ────────────────────────────────────────────────────────────
def test_progress_empty_is_zero(mod):
    m, _ = mod
    assert m.compute_progress([]) == 0.0


def test_progress_partial(mod):
    m, _ = mod
    tasks = [{"status": "Done"}, {"status": "Open"}, {"status": "Skipped"}, {"status": "Open"}]
    assert m.compute_progress(tasks) == 50.0


def test_derive_status_transitions(mod):
    m, _ = mod
    assert m.derive_status([]) == "Open"
    assert m.derive_status([{"status": "Open"}]) == "Open"
    assert m.derive_status([{"status": "Done"}, {"status": "Open"}]) == "In Progress"
    assert m.derive_status([{"status": "Done"}, {"status": "Skipped"}]) == "Completed"
    # Cancelled is sticky
    assert m.derive_status([{"status": "Done"}], "Cancelled") == "Cancelled"


def test_instantiate_task_due_date(mod):
    m, _ = mod
    row = m.instantiate_task({"task_name": "Cấp email", "assignee": "it@x", "due_in_days": 3}, "2026-08-08")
    assert row["task_name"] == "Cấp email"
    assert row["assignee"] == "it@x"
    assert row["status"] == "Open"
    assert row["due_date"] == "2026-08-11"  # +3 days


def test_instantiate_task_no_due_in_days(mod):
    m, _ = mod
    row = m.instantiate_task({"task_name": "X", "due_in_days": 0}, "2026-08-08")
    assert row["due_date"] == "2026-08-08"


# ── lifecycle I/O ───────────────────────────────────────────────────────────
def _seed_template(stub, name="Default", tasks=None):
    stub.store[(stub and "VN Onboarding Template", name)] = _Doc(
        "VN Onboarding Template",
        {
            "name": name,
            "template_name": name,
            "tasks": tasks or [{"task_name": "Cấp email", "due_in_days": 1}],
        },
        store=stub.store,
    )


def test_start_onboarding_copies_template_tasks(mod):
    m, stub = mod
    _seed_template(stub, "Default", [{"task_name": "Cấp email", "due_in_days": 2, "assignee": "it@x"}])
    res = m.start_onboarding("HR-EMP-1", "2026-08-08", template="Default")
    doc = stub.store[("VN Employee Onboarding", res["name"])]
    assert res["task_count"] == 1
    assert doc.tasks[0]["task_name"] == "Cấp email"
    assert doc.tasks[0]["due_date"] == "2026-08-10"  # +2 days
    assert doc.status == "Open"
    assert doc.progress == 0.0


def test_complete_task_updates_progress_and_status(mod):
    m, stub = mod
    res = m.start_onboarding(
        "HR-EMP-1",
        "2026-08-08",
        tasks=[{"task_name": "A", "due_in_days": 1}, {"task_name": "B", "due_in_days": 2}],
    )
    r1 = m.complete_task(res["name"], "A")
    assert r1["progress"] == 50.0
    assert r1["status"] == "In Progress"
    # Completing the last task auto-completes the onboarding.
    r2 = m.complete_task(res["name"], "B")
    assert r2["progress"] == 100.0
    assert r2["status"] == "Completed"


def test_complete_task_not_found_throws(mod):
    m, stub = mod
    res = m.start_onboarding("HR-EMP-1", "2026-08-08", tasks=[{"task_name": "A", "due_in_days": 1}])
    with pytest.raises(Exception):
        m.complete_task(res["name"], "Khong-ton-tai")


def test_start_onboarding_requires_employee(mod):
    m, stub = mod
    with pytest.raises(Exception):
        m.start_onboarding("", "2026-08-08")


def test_onboarding_list_count(mod):
    m, stub = mod
    stub.rows["VN Employee Onboarding"] = [
        {"name": "OB-1", "employee": "E1", "employee_name": "An", "status": "Open", "progress": 0},
        {"name": "OB-2", "employee": "E2", "employee_name": "Bình", "status": "Completed", "progress": 100},
    ]
    res = m.onboarding_list()
    assert res["total"] == 2
    assert len(res["data"]) == 2
    done = m.onboarding_list(status="Completed")
    assert done["total"] == 2  # db.count ignores status in this stub; data filtered by list filter


def test_permission_denied_for_employee(mod):
    m, stub = mod
    stub.roles = ["Employee"]
    with pytest.raises(_PermissionError):
        m.list_templates()
