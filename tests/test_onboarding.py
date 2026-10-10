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


# ── desk-free extension (plan-onboarding-desk-free.md §4.1, B1–B27) ──────────
import datetime as _dt  # noqa: E402


def _seed_doc(
    stub, name="OB-X", status="Open", tasks=None, employee="E1", boarding_date="2026-08-08", progress=0
):
    doc = _Doc(
        "VN Employee Onboarding",
        {
            "name": name,
            "status": status,
            "progress": progress,
            "employee": employee,
            "employee_name": "An",
            "company": "Gege",
            "template": None,
            "boarding_date": boarding_date,
            "tasks": tasks or [],
        },
        store=stub.store,
    )
    stub.store[("VN Employee Onboarding", name)] = doc
    return doc


def _with_delete(stub):
    deleted = []
    stub.delete_doc = lambda doctype, name: deleted.append((doctype, name))
    return deleted


def _fake_assign_to(monkeypatch):
    """Inject a fake ``frappe.desk.form.assign_to`` recording add/remove calls."""
    calls = []
    mod = types.ModuleType("frappe.desk.form")
    mod.assign_to = types.SimpleNamespace(
        add=lambda payload: calls.append(("add", tuple(payload.get("assign") or []), payload.get("name"))),
        remove=lambda dt, name, user: calls.append(("remove", user, name)),
    )
    monkeypatch.setitem(sys.modules, "frappe.desk.form", mod)
    return calls


def test_is_overdue_pure_matrix(mod):
    m, _ = mod
    assert m.is_overdue("2026-08-01", "Open", today="2026-08-08") is True
    assert m.is_overdue("2026-08-09", "Open", today="2026-08-08") is False
    assert m.is_overdue(None, "Open", today="2026-08-08") is False
    assert m.is_overdue("2026-08-01", "Done", today="2026-08-08") is False


def test_derive_can_matrix(mod):
    m, _ = mod
    assert m.derive_can("Open") == {"edit_tasks": True, "cancel": True, "delete": True}
    assert m.derive_can("In Progress")["delete"] is False
    assert m.derive_can("Completed") == {"edit_tasks": False, "cancel": False, "delete": False}
    assert m.derive_can("Cancelled")["delete"] is True


def test_complete_task_by_row_name_with_duplicate_names(mod):
    # B1 — two tasks sharing one name: row PK picks the right one.
    m, stub = mod
    doc = _seed_doc(
        stub,
        tasks=[
            {"name": "row-a", "task_name": "Setup", "status": "Open"},
            {"name": "row-b", "task_name": "Setup", "status": "Open"},
        ],
    )
    res = m.complete_task(doc.name, row_name="row-b")
    assert res["progress"] == 50.0
    assert doc.tasks[0]["status"] == "Open" and doc.tasks[1]["status"] == "Done"


def test_complete_task_skipped_with_note(mod):
    # B3 — the SPA "Bỏ qua" flow stores status + note + completed_by.
    m, stub = mod
    doc = _seed_doc(stub, tasks=[{"name": "row-a", "task_name": "A", "status": "Open"}])
    res = m.complete_task(doc.name, task_name="A", status="Skipped", note="không cần")
    assert res["status"] == "Completed"
    assert doc.tasks[0]["status"] == "Skipped"
    assert doc.tasks[0]["note"] == "không cần"
    assert doc.tasks[0]["completed_by"] == "hr@gege.local"


def test_reopen_task_recomputes_doc_status(mod):
    # B4 — undo: Completed doc drops back to In Progress, stamps cleared.
    m, stub = mod
    doc = _seed_doc(
        stub,
        status="Completed",
        progress=100.0,
        tasks=[
            {"name": "row-a", "task_name": "A", "status": "Done", "completed_at": "x", "completed_by": "u"},
            {"name": "row-b", "task_name": "B", "status": "Done", "completed_at": "y", "completed_by": "u"},
        ],
    )
    res = m.reopen_task(doc.name, "row-a")
    assert res["status"] == "In Progress" and res["progress"] == 50.0
    assert doc.tasks[0]["status"] == "Open"
    assert doc.tasks[0]["completed_at"] is None and doc.tasks[0]["completed_by"] is None


def test_reopen_task_on_cancelled_doc_throws(mod):
    # B5
    m, stub = mod
    doc = _seed_doc(
        stub,
        status="Cancelled",
        tasks=[{"name": "row-a", "task_name": "A", "status": "Done"}],
    )
    with pytest.raises(Exception):
        m.reopen_task(doc.name, "row-a")


def test_add_task_appends_and_recomputes(mod):
    # B6 — ad-hoc task on a running onboarding.
    m, stub = mod
    res = m.start_onboarding("HR-EMP-1", "2026-08-08", tasks=[{"task_name": "A", "due_in_days": 1}])
    out = m.add_task(res["name"], "Bổ sung", due_date="2026-09-01")
    assert out["task_count"] == 2
    doc = stub.store[("VN Employee Onboarding", res["name"])]
    assert doc.tasks[-1]["task_name"] == "Bổ sung"
    assert doc.tasks[-1]["due_date"] == "2026-09-01"


def test_add_task_on_cancelled_throws(mod):
    # B7
    m, stub = mod
    doc = _seed_doc(stub, status="Cancelled", tasks=[])
    with pytest.raises(Exception):
        m.add_task(doc.name, "Bổ sung")


def test_update_task_reassign_resyncs_todo(mod, monkeypatch):
    # B8 — rename / reschedule / reassign; ToDo moves with the assignee.
    m, stub = mod
    calls = _fake_assign_to(monkeypatch)
    doc = _seed_doc(
        stub,
        tasks=[
            {
                "name": "row-a",
                "task_name": "A",
                "assignee": "old@x",
                "status": "Open",
                "due_date": "2026-08-01",
            }
        ],
    )
    res = m.update_task(doc.name, "row-a", due_date="2026-08-20", assignee="new@x")
    assert res["name"] == doc.name
    assert doc.tasks[0]["due_date"] == "2026-08-20"
    assert ("remove", "old@x", doc.name) in calls
    assert ("add", ("new@x",), doc.name) in calls


def test_update_task_on_completed_doc_throws(mod):
    # B9
    m, stub = mod
    doc = _seed_doc(stub, status="Completed", tasks=[{"name": "row-a", "task_name": "A", "status": "Done"}])
    with pytest.raises(Exception):
        m.update_task(doc.name, "row-a", due_date="2026-08-20")


def test_delete_onboarding_in_progress_throws(mod):
    # B10 — guard: only Open/Cancelled may be deleted.
    m, stub = mod
    _with_delete(stub)
    doc = _seed_doc(stub, status="In Progress", tasks=[])
    with pytest.raises(Exception):
        m.delete_onboarding(doc.name)


def test_delete_onboarding_open_deletes(mod):
    # B11
    m, stub = mod
    deleted = _with_delete(stub)
    doc = _seed_doc(stub, status="Open", tasks=[])
    res = m.delete_onboarding(doc.name)
    assert res == {"name": doc.name, "deleted": True}
    assert deleted == [("VN Employee Onboarding", doc.name)]


def test_delete_onboarding_hr_user_denied(mod):
    # B12 — destructive gate is HR Manager only.
    m, stub = mod
    _with_delete(stub)
    stub.roles = ["HR User"]
    with pytest.raises(_PermissionError):
        m.delete_onboarding("OB-X")


def test_onboarding_summary_counts_and_overdue(mod):
    # B13 — server-side truth for the SPA cards.
    m, stub = mod
    today = _dt.date.today()
    yday = (today - _dt.timedelta(days=1)).isoformat()
    tmrw = (today + _dt.timedelta(days=1)).isoformat()
    stub.rows["VN Employee Onboarding"] = [
        {"name": "OB-1", "status": "Open"},
        {"name": "OB-2", "status": "Completed"},
        {"name": "OB-3", "status": "Cancelled"},
    ]
    stub.rows["VN Onboarding Task"] = [
        {
            "name": "t1",
            "parenttype": "VN Employee Onboarding",
            "parent": "OB-1",
            "status": "Open",
            "due_date": yday,
        },
        {
            "name": "t2",
            "parenttype": "VN Employee Onboarding",
            "parent": "OB-1",
            "status": "Open",
            "due_date": tmrw,
        },
        {
            "name": "t3",
            "parenttype": "VN Employee Onboarding",
            "parent": "OB-2",
            "status": "Open",
            "due_date": yday,
        },
    ]
    res = m.onboarding_summary()
    assert res["total"] == 3
    assert res["by_status"]["Open"] == 1 and res["by_status"]["Cancelled"] == 1
    assert res["overdue_tasks"] == 1  # t3 overdue but its parent is Completed
    assert res["overdue_processes"] == 1


def test_get_onboarding_payload_and_can(mod):
    # B15/B16 — one-call drawer payload.
    m, stub = mod
    doc = _seed_doc(
        stub, tasks=[{"name": "row-a", "task_name": "A", "status": "Open", "due_date": "2026-08-01"}]
    )
    res = m.get_onboarding(doc.name)
    assert res["can"] == {"edit_tasks": True, "cancel": True, "delete": True}
    assert res["tasks"][0]["row_name"] == "row-a"
    assert res["payroll"]["complete"] is False
    doc.status = "Completed"
    assert m.get_onboarding(doc.name)["can"]["edit_tasks"] is False


def test_add_onboarding_comment(mod):
    # B17
    m, stub = mod
    doc = _seed_doc(stub, tasks=[])
    assert m.add_onboarding_comment(doc.name, "ghi chú") == {"name": doc.name, "ok": True}


def test_delete_onboarding_attachment_wrong_owner_throws(mod):
    # B18 — File attached to another doctype is rejected.
    m, stub = mod
    _with_delete(stub)
    stub.store[("File", "F-1")] = _Doc(
        "File", {"name": "F-1", "attached_to_doctype": "Employee", "attached_to_name": "E1"}
    )
    with pytest.raises(Exception):
        m.delete_onboarding_attachment("F-1")


def test_delete_template_referenced_throws(mod):
    # B19 — template in use by any onboarding cannot be deleted.
    m, stub = mod
    _with_delete(stub)
    _seed_template(stub, "Default")
    stub.rows["VN Employee Onboarding"] = [{"name": "OB-1", "template": "Default", "status": "Open"}]
    with pytest.raises(Exception):
        m.delete_template("Default")


def test_duplicate_template_copies_tasks_inactive(mod):
    # B20
    m, stub = mod
    _seed_template(
        stub, "Default", [{"task_name": "A", "due_in_days": 1}, {"task_name": "B", "due_in_days": 2}]
    )
    res = m.duplicate_template("Default")
    assert res["task_count"] == 2
    dup = stub.store[("VN Onboarding Template", res["name"])]
    assert dup.template_name == "Default (copy)"
    assert dup.is_active == 0


def test_get_template_returns_tasks(mod):
    # B21
    m, stub = mod
    _seed_template(stub, "Default", [{"task_name": "A", "due_in_days": 1, "assignee": "it@x"}])
    res = m.get_template("Default")
    assert res["tasks"] == [{"task_name": "A", "assignee": "it@x", "due_in_days": 1}]


def test_list_assignees_filters_enabled_system_users(mod):
    # B22
    m, stub = mod
    stub.rows["User"] = [
        {"name": "a@x", "full_name": "A", "enabled": 1, "user_type": "System User"},
        {"name": "b@x", "full_name": "B", "enabled": 0, "user_type": "System User"},
        {"name": "c@x", "full_name": "C", "enabled": 1, "user_type": "Website User"},
    ]
    res = m.list_assignees()
    assert res == [{"value": "a@x", "label": "A"}]


def test_start_onboarding_creates_todo_per_assignee(mod, monkeypatch):
    # B23 — Frappe-standard assignment; only tasks WITH an assignee.
    m, _ = mod
    calls = _fake_assign_to(monkeypatch)
    res = m.start_onboarding(
        "HR-EMP-1",
        "2026-08-08",
        tasks=[{"task_name": "A", "assignee": "it@x"}, {"task_name": "B"}],
    )
    assert calls == [("add", ("it@x",), res["name"])]


def test_complete_task_closes_todo(mod, monkeypatch):
    # B24
    m, stub = mod
    calls = _fake_assign_to(monkeypatch)
    doc = _seed_doc(stub, tasks=[{"name": "row-a", "task_name": "A", "assignee": "it@x", "status": "Open"}])
    m.complete_task(doc.name, row_name="row-a")
    assert ("remove", "it@x", doc.name) in calls


def test_cancel_onboarding_closes_all_todos(mod, monkeypatch):
    # B25 — cancel sweeps every open assignment.
    m, stub = mod
    calls = _fake_assign_to(monkeypatch)
    doc = _seed_doc(
        stub,
        tasks=[
            {"name": "row-a", "task_name": "A", "assignee": "it@x", "status": "Open"},
            {"name": "row-b", "task_name": "B", "assignee": "hr2@x", "status": "Open"},
        ],
    )
    m.cancel_onboarding(doc.name, "nhầm")
    assert ("remove", "it@x", doc.name) in calls and ("remove", "hr2@x", doc.name) in calls


def test_onboarding_list_company_filter(mod):
    # B26
    m, stub = mod
    stub.rows["VN Employee Onboarding"] = [
        {"name": "OB-1", "employee": "E1", "company": "C1", "status": "Open", "progress": 0},
        {"name": "OB-2", "employee": "E2", "company": "C2", "status": "Open", "progress": 0},
    ]
    res = m.onboarding_list(company="C1")
    assert [r["name"] for r in res["data"]] == ["OB-1"]


def test_permission_denied_outside_hr(mod):
    # B27 — HR User may operate tasks but NOT delete; Employee sees nothing.
    m, stub = mod
    stub.roles = ["HR User"]
    m.onboarding_summary()  # allowed
    _with_delete(stub)
    with pytest.raises(_PermissionError):
        m.delete_onboarding("OB-X")
    stub.roles = ["Employee"]
    with pytest.raises(_PermissionError):
        m.get_onboarding("OB-X")
