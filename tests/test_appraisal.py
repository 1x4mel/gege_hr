"""Bench-free unit tests for ``api/appraisal.py`` (NEW-2, hr-gap-audit 🟥 Appraisal).

Pure helpers (``status_for_progress`` / ``goal_completion`` / ``goal_can``) need
no frappe. The Goal I/O is exercised with a stub-frappe: own-goal filter, submit
creates a Goal, the desk-free CRUD surface (plans/goals-frontend-crud.md
G1–G36): get_goal can-matrix, update_goal (doc.save + friendly errors),
delete_goal (children guard), set_goal_status (partial-safe bulk) and the
update_goal_progress FIX that now saves via doc.save() instead of db.set_value.
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
    """Doc returned by stub ``frappe.new_doc`` (used by submit_goal)."""

    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.progress = 0
        self.status = "Pending"
        self.flags = types.SimpleNamespace()

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self


class _GoalDoc:
    """Seeded Goal doc: attribute bag + save()/delete() bookkeeping."""

    def __init__(self, **kw):
        self.doctype = "Goal"
        self.name = None
        self.progress = 0
        self.status = "Pending"
        self.is_group = 0
        self.parent_goal = None
        self.appraisal_cycle = None
        self.kra = None
        self.employee = "HR-EMP-1"
        self.goal_name = ""
        self.start_date = None
        self.end_date = None
        self.description = ""
        for k, v in kw.items():
            setattr(self, k, v)
        self._saved = 0
        self._save_raises = None
        self.flags = types.SimpleNamespace()

    def get(self, key, default=None):
        return getattr(self, key, default)

    def save(self, *a, **k):
        if self._save_raises is not None:
            raise self._save_raises
        self._saved += 1
        return self


def _seed_goal(stub, **kw) -> _GoalDoc:
    """Seed a Goal into BOTH the doc store (get_doc) and list_rows (get_all/exists)."""
    doc = _GoalDoc(**kw)
    if not doc.name:
        doc.name = f"G-{len(stub.store) + 1:03d}"
    doc.goal_name = doc.goal_name or f"Goal {doc.name}"
    stub.store[("Goal", doc.name)] = doc
    rows = stub.list_rows.setdefault("Goal", [])
    rows.append(
        {
            "name": doc.name,
            "employee": doc.employee,
            "employee_name": kw.get("employee_name", "An"),
            "goal_name": doc.goal_name,
            "progress": doc.progress,
            "status": doc.status,
            "start_date": doc.start_date,
            "end_date": doc.end_date,
            "appraisal_cycle": doc.appraisal_cycle,
            "kra": doc.kra,
            "is_group": doc.is_group,
            "parent_goal": doc.parent_goal,
        }
    )
    return doc


def _sync(doc, stub):
    """Copy doc attributes back onto its list_rows mirror."""
    for row in stub.list_rows.get("Goal", []):
        if row.get("name") == doc.name:
            for key in ("progress", "status", "goal_name", "kra", "start_date", "end_date", "parent_goal"):
                row[key] = getattr(doc, key, None)


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.employee_company = "Gege"
        self.goal_employee = "HR-EMP-1"
        self.roles = {"HR Manager"}
        self.cycle_rows = {"CY-1": {"status": "Active", "start_date": "2026-01-05"}}

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

    def delete_doc(self, doctype, name, *a, **k):
        self.store.pop((doctype, name), None)
        self.list_rows[doctype] = [r for r in self.list_rows.get(doctype, []) if r.get("name") != name]

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def exists(self, doctype, name=None):
            if doctype == "Goal":
                return any(r.get("name") == name for r in self.fr.list_rows.get("Goal", []))
            return True

        def get_value(self, doctype, key, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(key, dict):
                return self.fr.employee_for_user
            if doctype == "Employee":
                return self.fr.employee_company
            if doctype == "Goal":
                row = next((r for r in self.fr.list_rows.get("Goal", []) if r.get("name") == key), None)
                if isinstance(field, (list, tuple)):
                    # legacy branch: [employee, cycle] (kept for compatibility)
                    return (row or {}).get("employee", self.fr.goal_employee), None
                if isinstance(field, str):
                    return (row or {}).get(field)
                return row.get("employee") if row else self.fr.goal_employee
            if doctype == "Appraisal Cycle":
                cyc = self.fr.cycle_rows.get(key, {})
                if isinstance(field, str):
                    if field == "status":
                        return cyc.get("status", "Active")
                    return cyc.get(field)
                return cyc.get("status", "Active")
            return None

        def set_value(self, doctype, name, fields, *a, **k):
            for r in self.fr.list_rows.get(doctype, []):
                if r.get("name") == name:
                    r.update(fields or {})

        def count(self, doctype, filters=None):
            return len(self.fr.get_all(doctype, filters=filters))

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


def test_goal_can_matrix(mod):
    m, _ = mod
    can = m.goal_can("Pending", 0, 0)
    assert can["edit"] and can["delete"] and can["set_progress"] and can["archive"] and can["close"]
    assert not can["unarchive"] and not can["reopen"]
    # group with children: no manual progress, no delete
    can = m.goal_can("In Progress", 1, 2)
    assert not can["set_progress"] and not can["delete"]
    # sticky states
    assert m.goal_can("Archived", 0, 0)["unarchive"] is True
    assert m.goal_can("Archived", 0, 0)["archive"] is False
    assert m.goal_can("Closed", 0, 0)["reopen"] is True
    assert m.goal_can("Closed", 0, 0)["close"] is False


# ── I/O (legacy) ────────────────────────────────────────────────────────────
def test_my_goals_filters_own(mod):
    m, stub = mod
    stub.list_rows["Goal"] = [
        {
            "name": "G-1",
            "employee": "HR-EMP-1",
            "employee_name": "An",
            "goal_name": "Doanh so",
            "progress": 40,
        },
        {"name": "G-2", "employee": "HR-EMP-2", "employee_name": "Binh", "goal_name": "Khac", "progress": 10},
    ]
    res = m.my_goals()
    assert res["total"] == 1


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


def test_all_goals_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_goals()


# ── G1–G7: get_goal ─────────────────────────────────────────────────────────
def test_g1_get_goal_own_full_shape(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="G-100", employee="HR-EMP-1", kra="Sale", start_date="2026-01-01", end_date="2026-06-30")
    res = m.get_goal("G-100")
    assert res["name"] == "G-100"
    assert res["goal_name"] == doc.goal_name
    assert res["kra"] == "Sale"
    assert res["children"] == {"total": 0, "completed": 0, "in_progress": 0, "pending": 0}
    can = res["can"]
    assert can["edit"] and can["delete"] and can["set_progress"] and can["archive"] and can["close"]
    assert not can["unarchive"] and not can["reopen"]


def test_g2_get_goal_group_children_stats(mod):
    m, stub = mod
    _seed_goal(stub, name="G-GROUP", is_group=1)
    _seed_goal(stub, name="G-C1", parent_goal="G-GROUP", status="Completed")
    _seed_goal(stub, name="G-C2", parent_goal="G-GROUP", status="In Progress")
    res = m.get_goal("G-GROUP")
    assert res["children"] == {"total": 2, "completed": 1, "in_progress": 1, "pending": 0}
    assert res["completion_count"] == "1/2 hoàn thành"
    assert res["can"]["set_progress"] is False
    assert res["can"]["delete"] is False  # G5: group with children cannot be deleted


def test_g3_get_goal_closed_can_matrix(mod):
    m, stub = mod
    _seed_goal(stub, name="G-CL", status="Closed", progress=40)
    res = m.get_goal("G-CL")
    assert res["can"]["set_progress"] is False
    assert res["can"]["reopen"] is True
    assert res["can"]["close"] is False


def test_g4_get_goal_archived_can_matrix(mod):
    m, stub = mod
    _seed_goal(stub, name="G-AR", status="Archived", progress=40)
    res = m.get_goal("G-AR")
    assert res["can"]["unarchive"] is True
    assert res["can"]["archive"] is False


def test_g6_get_goal_other_employee_denied(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="G-OTHER", employee="HR-EMP-2")
    with pytest.raises(Exception, match="chính mình"):
        m.get_goal("G-OTHER")


def test_g7_get_goal_missing_throws(mod):
    m, _ = mod
    with pytest.raises(Exception, match="không tồn tại"):
        m.get_goal("G-NOPE")


# ── G8–G14: update_goal ─────────────────────────────────────────────────────
def test_g8_update_goal_saves_fields(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="G-200", status="In Progress", progress=40)
    res = m.update_goal("G-200", goal_name="Tên mới", end_date="2026-09-30", description="Ghi chú")
    assert doc._saved == 1
    assert doc.goal_name == "Tên mới"
    assert doc.end_date == "2026-09-30"
    assert doc.description == "Ghi chú"
    assert doc.progress == 40 and doc.status == "In Progress"  # untouched by edit
    assert res["name"] == "G-200"


def test_g9_update_goal_never_touches_locked_state(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="G-201", status="In Progress", progress=40)
    m.update_goal("G-201", goal_name="Đổi tên")
    # progress/status/is_group/employee/cycle are NOT accepted params → unchanged
    assert doc.progress == 40
    assert doc.status == "In Progress"
    assert doc.is_group == 0
    assert doc.employee == "HR-EMP-1"


def test_g10_update_goal_native_error_mapped_to_vietnamese(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="G-202")
    doc._save_raises = Exception("From Date must be before To Date")
    with pytest.raises(Exception, match="ngày bắt đầu"):
        m.update_goal("G-202", goal_name="x")


def test_g11_update_goal_closed_cycle_throws(mod):
    m, stub = mod
    stub.cycle_rows["CY-DONE"] = {"status": "Completed"}
    _seed_goal(stub, name="G-203", appraisal_cycle="CY-DONE", kra="Sale")
    with pytest.raises(Exception, match="đã đóng"):
        m.update_goal("G-203", goal_name="x")


def test_g12_update_goal_parent_move_sets_old_parent(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="G-204", parent_goal="G-OLD")
    m.update_goal("G-204", parent_goal="G-NEW")
    assert doc.old_parent == "G-OLD"  # DB truth before the move (NestedSet contract)
    assert doc.parent_goal == "G-NEW"
    # detaching: old_parent mirrors the previous DB value ("" when it was root)
    doc2 = _seed_goal(stub, name="G-205", parent_goal=None)
    m.update_goal("G-205", parent_goal="G-NEW")
    assert doc2.old_parent == ""


def test_g13_update_goal_kra_required_with_cycle(mod):
    m, stub = mod
    _seed_goal(stub, name="G-206", appraisal_cycle="CY-1", kra=None)
    with pytest.raises(Exception, match="KRA"):
        m.update_goal("G-206", goal_name="x")


def test_g14_update_goal_other_employee_denied(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="G-207", employee="HR-EMP-2")
    with pytest.raises(Exception, match="chính mình"):
        m.update_goal("G-207", goal_name="x")


# ── G15–G17: delete_goal ────────────────────────────────────────────────────
def test_g15_delete_goal_group_with_children_throws(mod):
    m, stub = mod
    _seed_goal(stub, name="G-GR2", is_group=1)
    _seed_goal(stub, name="G-KID", parent_goal="G-GR2")
    with pytest.raises(Exception, match="mục tiêu con"):
        m.delete_goal("G-GR2")


def test_g16_delete_goal_happy(mod):
    m, stub = mod
    _seed_goal(stub, name="G-300")
    res = m.delete_goal("G-300")
    assert res == {"name": "G-300"}
    assert ("Goal", "G-300") not in stub.store
    assert all(r["name"] != "G-300" for r in stub.list_rows.get("Goal", []))


def test_g17_delete_goal_other_employee_denied(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="G-301", employee="HR-EMP-2")
    with pytest.raises(Exception, match="chính mình"):
        m.delete_goal("G-301")


# ── G18–G24: set_goal_status (bulk) ─────────────────────────────────────────
def test_g18_bulk_archive(mod):
    m, stub = mod
    a = _seed_goal(stub, name="A-1", status="In Progress", progress=40)
    b = _seed_goal(stub, name="A-2", status="Pending")
    res = m.set_goal_status(["A-1", "A-2"], "Archived")
    assert res["updated"] == ["A-1", "A-2"] and res["failed"] == []
    assert a.status == "Archived" and b.status == "Archived"
    assert a.progress == 40  # archive never touches progress


def test_g19_bulk_completed_forces_progress(mod):
    m, stub = mod
    a = _seed_goal(stub, name="A-3", progress=20, status="In Progress")
    m.set_goal_status(["A-3"], "Completed")
    assert a.status == "Completed" and a.progress == 100


def test_g20_bulk_unarchive_recomputes_from_progress(mod):
    m, stub = mod
    a = _seed_goal(stub, name="A-4", status="Archived", progress=40)
    m.set_goal_status(["A-4"], "Unarchive")
    assert a.status == "In Progress"  # recomputed from progress


def test_g21_bulk_reopen_leaves_closed(mod):
    m, stub = mod
    a = _seed_goal(stub, name="A-5", status="Closed", progress=0)
    m.set_goal_status(["A-5"], "Reopen")
    assert a.status == "Pending"


def test_g22_bulk_partial_safe(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    a = _seed_goal(stub, name="A-6", employee="HR-EMP-1")
    _seed_goal(stub, name="A-7", employee="HR-EMP-2")  # not ours → fails
    res = m.set_goal_status(["A-6", "A-7"], "Closed")
    assert res["updated"] == ["A-6"]
    assert res["failed"][0]["name"] == "A-7"
    assert res["failed"][0]["reason"]


def test_g23_bulk_invalid_status_throws(mod):
    m, _ = mod
    with pytest.raises(Exception, match="Trạng thái"):
        m.set_goal_status(["X"], "Banana")


def test_g24_bulk_closed_cycle_goes_to_failed_bucket(mod):
    m, stub = mod
    stub.cycle_rows["CY-DONE"] = {"status": "Completed"}
    _seed_goal(stub, name="A-8", appraisal_cycle="CY-DONE", kra="Sale")
    res = m.set_goal_status(["A-8"], "Archived")
    assert res["updated"] == []
    assert "đã đóng" in res["failed"][0]["reason"]


# ── G25–G28: update_goal_progress FIX (doc.save, not db.set_value) ──────────
def test_g25_progress_saves_via_doc_save(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="P-1", progress=0, status="Pending")
    res = m.update_goal_progress("P-1", 60)
    assert doc._saved == 1  # the doc itself was saved → native hooks run in prod
    assert doc.progress == 60
    assert res == {"name": "P-1", "progress": 60, "status": "In Progress"}


def test_g26_progress_clamps_and_auto_status(mod):
    m, stub = mod
    doc = _seed_goal(stub, name="P-2", progress=0, status="Pending")
    m.update_goal_progress("P-2", 150)
    assert doc.progress == 100 and doc.status == "Completed"


def test_g27_progress_group_throws(mod):
    m, stub = mod
    _seed_goal(stub, name="P-3", is_group=1)
    with pytest.raises(Exception, match="Mục tiêu nhóm"):
        m.update_goal_progress("P-3", 50)


def test_g28_progress_sticky_status_throws(mod):
    m, stub = mod
    _seed_goal(stub, name="P-4", status="Closed")
    with pytest.raises(Exception, match="lưu trữ/đóng"):
        m.update_goal_progress("P-4", 50)


# ── G29–G33: submit_goal extensions ─────────────────────────────────────────
def test_g29_submit_group_zero_progress_no_parent(mod):
    m, stub = mod
    res = m.submit_goal(employee="HR-EMP-1", goal_name="Nhóm mục tiêu", is_group=1, parent_goal="G-X")
    doc = stub.store[("Goal", res["name"])]
    assert doc.is_group == 1
    assert doc.progress == 0
    assert doc.parent_goal is None  # a group is always a root
    assert res["is_group"] == 1


def test_g30_submit_child_inherits_kra_cycle(mod):
    m, stub = mod
    _seed_goal(stub, name="PAR-1", is_group=1, kra="Sale", appraisal_cycle="CY-1", employee="HR-EMP-1")
    res = m.submit_goal(employee="HR-EMP-1", goal_name="Con", parent_goal="PAR-1")
    doc = stub.store[("Goal", res["name"])]
    assert doc.kra == "Sale" and doc.appraisal_cycle == "CY-1"
    assert doc.parent_goal == "PAR-1"


def test_g30b_submit_child_conflicting_kra_throws(mod):
    m, stub = mod
    _seed_goal(stub, name="PAR-2", is_group=1, kra="Sale", employee="HR-EMP-1")
    with pytest.raises(Exception, match="cùng KRA"):
        m.submit_goal(employee="HR-EMP-1", goal_name="Con", parent_goal="PAR-2", kra="Marketing")


def test_g30c_submit_child_other_employee_throws(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="PAR-3", is_group=1, employee="HR-EMP-2", kra="Sale")
    with pytest.raises(Exception, match="cùng nhân viên"):
        m.submit_goal(employee="HR-EMP-1", goal_name="Con", parent_goal="PAR-3")


def test_g31_submit_cycle_requires_kra(mod):
    m, _ = mod
    with pytest.raises(Exception, match="KRA"):
        m.submit_goal(employee="HR-EMP-1", goal_name="Gắn kỳ", cycle="CY-1")


def test_g32_submit_manager_on_behalf(mod):
    m, stub = mod
    res = m.submit_goal(employee="HR-EMP-2", goal_name="Hộ nhân viên", kra="Sale")
    doc = stub.store[("Goal", res["name"])]
    assert doc.employee == "HR-EMP-2"  # manager (default roles) may create for others


def test_g33_submit_start_date_from_cycle(mod):
    m, stub = mod
    res = m.submit_goal(employee="HR-EMP-1", goal_name="Theo kỳ", cycle="CY-1", kra="Sale")
    doc = stub.store[("Goal", res["name"])]
    assert doc.start_date == "2026-01-05"  # CY-1.start_date fallback


# ── G34–G35: appraisal_options employees ────────────────────────────────────
def test_g34_options_employees_for_manager(mod):
    m, stub = mod
    stub.list_rows["Employee"] = [
        {"name": "HR-EMP-1", "employee_name": "An", "status": "Active"},
        {"name": "HR-EMP-2", "employee_name": "Bình", "status": "Active"},
        {"name": "HR-EMP-3", "employee_name": "Cũ", "status": "Left"},
    ]
    res = m.appraisal_options()
    assert {e["name"] for e in res["employees"]} == {"HR-EMP-1", "HR-EMP-2"}  # Active only


def test_g35_options_no_employees_for_plain_employee(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    stub.list_rows["Employee"] = [{"name": "HR-EMP-1", "employee_name": "An", "status": "Active"}]
    res = m.appraisal_options()
    assert res["employees"] == []


# ── G36: list-field contract ────────────────────────────────────────────────
def test_g36_goal_fields_contract(mod):
    m, _ = mod
    assert {"is_group", "parent_goal"} <= set(m._GOAL_FIELDS)
    assert "description" in m._GOAL_DETAIL_FIELDS


# ── G37–G40: P1 — children listing + manager bulk delete ────────────────────
def test_g37_list_goal_children_hides_archived_and_flags_grandchildren(mod):
    m, stub = mod
    _seed_goal(stub, name="ROOT-1", is_group=1)
    _seed_goal(stub, name="KID-A", parent_goal="ROOT-1", status="Completed")
    _seed_goal(stub, name="KID-B", parent_goal="ROOT-1", status="Archived")  # hidden (hrms tree)
    _seed_goal(stub, name="KID-C", parent_goal="ROOT-1", is_group=1)
    _seed_goal(stub, name="GKID", parent_goal="KID-C")  # makes KID-C expandable
    res = m.list_goal_children("ROOT-1")
    names = [c["name"] for c in res["children"]]
    assert names == ["KID-A", "KID-C"]  # Archived excluded, creation order kept
    assert res["children"][0]["status"] == "Completed"
    assert res["children"][0]["has_children"] is False
    assert res["children"][1]["has_children"] is True  # KID-C itself has a child


def test_g38_list_goal_children_other_employee_denied(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="ROOT-2", is_group=1, employee="HR-EMP-2")
    with pytest.raises(Exception, match="chính mình"):
        m.list_goal_children("ROOT-2")


def test_g39_bulk_delete_manager_partial_safe(mod):
    m, stub = mod
    _seed_goal(stub, name="BD-1")
    _seed_goal(stub, name="BD-G", is_group=1)
    _seed_goal(stub, name="BD-K", parent_goal="BD-G")  # BD-G still has children → fails
    res = m.bulk_delete_goals(["BD-1", "BD-G"])
    assert res["deleted"] == ["BD-1"]
    assert res["failed"][0]["name"] == "BD-G"
    assert "mục tiêu con" in res["failed"][0]["reason"]
    assert ("Goal", "BD-1") not in stub.store
    assert ("Goal", "BD-G") in stub.store  # untouched


def test_g40_bulk_delete_denied_for_plain_employee(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    _seed_goal(stub, name="BD-2")
    with pytest.raises(Exception, match="HR/Manager"):
        m.bulk_delete_goals(["BD-2"])
