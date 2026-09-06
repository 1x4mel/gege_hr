"""Leave Policy desk-free lifecycle tests (plan ``leave-policy-frontend-crud`` §4.1).

Bench-free stub harness covering the G9 endpoints added for full-SPA parity:

  Policy    save (title fix G1) / get / submit / cancel / amend / delete /
            duplicate + duplicate-leave-type guard + docstatus guards
  Assignment assign (submitted-policy guard G2 + carry_forward + allocations) /
            cancel (reason + cascade count) / amend / bulk partial-safe
  Period    update (date-freeze guard) / delete (link guard)
  Lists     server-side q/docstatus filters, allocations list
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest


class _FrappeError(Exception):
    """Stand-in for frappe.exceptions.ValidationError used by frappe.throw."""


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils

    mod.db = None
    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.throw = lambda msg, exc=_FrappeError, *args, **kwargs: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None
    mod.copy_doc = None
    mod.delete_doc = None
    mod.get_traceback = lambda *a, **k: ""
    return mod


class _FakeDoc:
    """Minimal document stub with docstatus lifecycle + optional submit error."""

    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.docstatus = payload.get("docstatus", 0)
        self.title = payload.get("title", "")
        self.leave_policy_details = payload.get("leave_policy_details", [])
        self.inserted = False
        self.submitted = False
        self.saved = False
        self.cancelled = False
        self.cancel_calls = 0
        self.submit_error = None

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def submit(self):
        if self.submit_error:
            raise _FrappeError(self.submit_error)
        self.submitted = True
        self.docstatus = 1
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def cancel(self):
        if self.docstatus != 1:
            raise _FrappeError("cannot cancel non-submitted doc")
        self.cancel_calls += 1
        self.cancelled = True
        self.docstatus = 2
        return self

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeDB:
    def __init__(self):
        self.exists_map = {}
        self.values_map = {}
        self.commits = 0
        self.savepoints = []
        self.rollbacks = []

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)

    def get_value(self, doctype, name, fieldname=None, **_k):
        key = (doctype, name, fieldname)
        if key in self.values_map:
            return self.values_map[key]
        return 1 if fieldname == "docstatus" else None

    def commit(self):
        self.commits += 1

    def savepoint(self, name):
        self.savepoints.append(name)

    def rollback(self, save_point=None):
        self.rollbacks.append(save_point)


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()
        self.docs_created = []
        self.deleted = []
        self._next_id = 1
        self._existing = {}
        self._all_rows = {}
        self.all_calls = []  # [(doctype, kwargs)]
        self.fail_submit_for = {}  # {employee: hrms error message}

    def get_doc(self, payload_or_doctype, name=None):
        if name is not None:
            return self._existing[(payload_or_doctype, name)]
        doc = _FakeDoc(payload_or_doctype, name=f"NEW-{self._next_id:04d}")
        if getattr(doc, "employee", None) in self.fail_submit_for:
            doc.submit_error = self.fail_submit_for[doc.employee]
        self._next_id += 1
        self.docs_created.append(doc)
        return doc

    def copy_doc(self, doc):
        payload = {
            k: v
            for k, v in doc.__dict__.items()
            if k in ("doctype", "title", "leave_policy_details", "employee", "leave_policy")
        }
        new = _FakeDoc(payload, name=f"NEW-{self._next_id:04d}")
        self._next_id += 1
        self.docs_created.append(new)
        return new

    def delete_doc(self, doctype, name):
        self.deleted.append((doctype, name))
        self._existing.pop((doctype, name), None)
        return name

    def get_all(self, doctype, **kwargs):
        self.all_calls.append((doctype, kwargs))
        return self._all_rows.get(doctype, [])

    def throw(self, msg, exc=_FrappeError, *a, **k):
        raise exc(msg)


@pytest.fixture
def fake(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.payroll_master")
    monkeypatch.setattr(api, "frappe", stub)

    harness = _FakeFrappe()
    monkeypatch.setattr(stub, "db", harness.db)
    monkeypatch.setattr(stub, "get_doc", harness.get_doc)
    monkeypatch.setattr(stub, "get_all", harness.get_all)
    monkeypatch.setattr(stub, "throw", harness.throw)
    monkeypatch.setattr(stub, "copy_doc", harness.copy_doc)
    monkeypatch.setattr(stub, "delete_doc", harness.delete_doc)

    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")
    monkeypatch.setattr(api, "_company_for_employee", lambda e: "GEGE")

    harness.api = api
    harness.stub = stub
    harness.audit_calls = audit_calls
    harness.set_rows = lambda doctype, rows: harness._all_rows.__setitem__(doctype, rows)
    harness.policy = lambda **kw: harness._existing.__setitem__(
        ("Leave Policy", kw.get("name", "P1")),
        _FakeDoc(
            {
                "doctype": "Leave Policy",
                "title": kw.get("title", "Chính sách 1"),
                "docstatus": kw.get("docstatus", 0),
                "leave_policy_details": kw.get(
                    "details", [{"leave_type": "Annual", "annual_allocation": 12}]
                ),
            },
            name=kw.get("name", "P1"),
        ),
    )
    return harness


# --------------------------------------------------------------------------- #
# save_leave_policy (G1 fix)
# --------------------------------------------------------------------------- #
def test_b1_save_creates_with_title_field(fake):
    res = fake.api.save_leave_policy(
        title="Chính sách Chuẩn", details=[{"leave_type": "Annual", "annual_allocation": 12}]
    )
    doc = fake.docs_created[-1]
    assert doc.inserted is True
    assert doc.title == "Chính sách Chuẩn"
    assert "leave_policy" not in doc.__dict__  # G1: payload key must be `title`
    assert res["title"] == "Chính sách Chuẩn"
    assert res["docstatus"] == 0
    assert len(fake.audit_calls) == 1


def test_b2_save_requires_title(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_leave_policy(title="", details=[{"leave_type": "Annual", "annual_allocation": 12}])


def test_b3_save_rejects_duplicate_leave_type(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_leave_policy(
            title="P1",
            details=[
                {"leave_type": "Annual", "annual_allocation": 12},
                {"leave_type": "Annual", "annual_allocation": 5},
            ],
        )


def test_b4_save_refuses_submitted_policy(fake):
    fake.policy(name="P1", docstatus=1)
    with pytest.raises(_FrappeError) as err:
        fake.api.save_leave_policy(
            title="P1 mới", name="P1", details=[{"leave_type": "Annual", "annual_allocation": 15}]
        )
    assert "Nháp" in str(err.value)


def test_b5_save_updates_draft(fake):
    fake.policy(name="P1", docstatus=0, title="Cũ")
    res = fake.api.save_leave_policy(
        title="Mới", name="P1", details=[{"leave_type": "Annual", "annual_allocation": 15}]
    )
    existing = fake._existing[("Leave Policy", "P1")]
    assert existing.saved is True
    assert existing.title == "Mới"
    assert res["name"] == "P1"


# --------------------------------------------------------------------------- #
# submit / cancel / amend / delete / duplicate
# --------------------------------------------------------------------------- #
def test_b6_submit_draft(fake):
    fake.policy(name="P1", docstatus=0)
    res = fake.api.submit_leave_policy("P1")
    assert fake._existing[("Leave Policy", "P1")].submitted is True
    assert res["docstatus"] == 1
    assert len(fake.audit_calls) == 1


def test_b7_submit_non_draft_throws(fake):
    fake.policy(name="P1", docstatus=1)
    with pytest.raises(_FrappeError):
        fake.api.submit_leave_policy("P1")


def test_b8_cancel_requires_reason(fake):
    fake.policy(name="P1", docstatus=1)
    with pytest.raises(_FrappeError):
        fake.api.cancel_leave_policy("P1", reason="  ")


def test_b9_cancel_submitted(fake):
    fake.policy(name="P1", docstatus=1)
    res = fake.api.cancel_leave_policy("P1", reason="thay đổi số ngày")
    assert fake._existing[("Leave Policy", "P1")].cancelled is True
    assert res["docstatus"] == 2


def test_b10_cancel_draft_throws(fake):
    fake.policy(name="P1", docstatus=0)
    with pytest.raises(_FrappeError):
        fake.api.cancel_leave_policy("P1", reason="x")


def test_b10b_cancel_blocked_by_assignment_maps_vietnamese(fake):
    doc = _FakeDoc(
        {"doctype": "Leave Policy", "title": "P1", "docstatus": 1, "leave_policy_details": []},
        name="P1",
    )

    def _boom():
        raise Exception("Cannot cancel or delete: linked with Leave Policy Assignment LPA-1")

    doc.cancel = _boom
    fake._existing[("Leave Policy", "P1")] = doc
    with pytest.raises(_FrappeError) as err:
        fake.api.cancel_leave_policy("P1", reason="x")
    assert "đang được gán" in str(err.value)


def test_b11_amend_from_submitted_cancels_old(fake):
    fake.policy(name="P1", docstatus=1, title="Cũ")
    res = fake.api.amend_leave_policy(
        "P1", title="Mới", details=[{"leave_type": "Annual", "annual_allocation": 15}]
    )
    old = fake._existing[("Leave Policy", "P1")]
    assert old.cancelled is True
    new = fake.docs_created[-1]
    assert new.docstatus == 0
    assert new.amended_from == "P1"
    assert new.title == "Mới"
    assert res["amended_from"] == "P1"


def test_b12_amend_from_cancelled_skips_cancel(fake):
    fake.policy(name="P1", docstatus=2)
    fake.api.amend_leave_policy("P1")
    old = fake._existing[("Leave Policy", "P1")]
    assert old.cancel_calls == 0  # already cancelled — never cancelled twice


def test_b13_amend_from_draft_throws(fake):
    fake.policy(name="P1", docstatus=0)
    with pytest.raises(_FrappeError):
        fake.api.amend_leave_policy("P1")


def test_b14_delete_submitted_throws(fake):
    fake.policy(name="P1", docstatus=1)
    with pytest.raises(_FrappeError):
        fake.api.delete_leave_policy("P1")


def test_b15_delete_cancelled(fake):
    fake.policy(name="P1", docstatus=2)
    res = fake.api.delete_leave_policy("P1")
    assert ("Leave Policy", "P1") in fake.deleted
    assert res == {"name": "P1"}
    assert len(fake.audit_calls) == 1


def test_b16_delete_linkexists_maps_vietnamese(fake, monkeypatch):
    fake.policy(name="P1", docstatus=2)

    def _boom(doctype, name):
        raise Exception("Cannot delete: linked with Leave Policy Assignment LPA-1")

    monkeypatch.setattr(fake.stub, "delete_doc", _boom)
    with pytest.raises(_FrappeError) as err:
        fake.api.delete_leave_policy("P1")
    assert "tham chiếu" in str(err.value)


def test_b6b_duplicate_policy(fake):
    fake.policy(name="P1", docstatus=1, title="Gốc")
    res = fake.api.duplicate_leave_policy("P1", title="Bản sao")
    new = fake.docs_created[-1]
    assert new.docstatus == 0
    assert new.title == "Bản sao"
    assert new.amended_from == ""
    assert res["title"] == "Bản sao"


def test_b31_get_leave_policy_can_matrix(fake):
    fake.policy(name="P1", docstatus=1, title="Gốc")
    fake.set_rows("Leave Type", [
        {"name": "Annual", "max_leaves_allowed": 20, "is_carry_forward": 1, "is_earned_leave": 0,
         "is_lwp": 0, "is_compensatory": 0},
    ])
    fake.set_rows("Leave Policy Assignment", [{"name": "A1"}, {"name": "A2"}])
    res = fake.api.get_leave_policy("P1")
    assert res["assignment_count"] == 2
    assert res["can"]["cancel"] is True and res["can"]["rename"] is True
    assert res["can"]["edit"] is False and res["can"]["submit"] is False
    assert res["leave_policy_details"][0]["max_leaves_allowed"] == 20
    assert res["leave_policy_details"][0]["is_carry_forward"] is True


# --------------------------------------------------------------------------- #
# assign (G2 fix) / cancel / amend / bulk
# --------------------------------------------------------------------------- #
def test_b17_assign_requires_submitted_policy(fake):
    fake.db.values_map[("Leave Policy", "P1", "docstatus")] = 0
    with pytest.raises(_FrappeError) as err:
        fake.api.assign_leave_policy(
            employee="EMP-1", leave_policy="P1", leave_period="LP-2026"
        )
    assert "đã duyệt" in str(err.value)


def test_b18_assign_carries_carry_forward(fake):
    fake.api.assign_leave_policy(
        employee="EMP-1", leave_policy="P1", leave_period="LP-2026", carry_forward=1
    )
    doc = fake.docs_created[-1]
    assert doc.carry_forward is True
    assert doc.submitted is True


def test_b19_assign_returns_allocations(fake):
    fake.set_rows("Leave Allocation", [
        {"name": "LA-1", "leave_type": "Annual", "new_leaves_allocated": 12},
        {"name": "LA-2", "leave_type": "Sick", "new_leaves_allocated": 5},
    ])
    res = fake.api.assign_leave_policy(
        employee="EMP-1", leave_policy="P1", leave_period="LP-2026"
    )
    assert len(res["allocations"]) == 2


def test_b20_cancel_assignment_guards(fake):
    lpa = _FakeDoc(
        {"doctype": "Leave Policy Assignment", "employee": "EMP-1", "leave_policy": "P1",
         "docstatus": 1, "leave_period": "LP-2026", "assignment_based_on": "Leave Period"},
        name="LPA-1",
    )
    fake._existing[("Leave Policy Assignment", "LPA-1")] = lpa
    with pytest.raises(_FrappeError):
        fake.api.cancel_leave_policy_assignment("LPA-1", reason="")
    lpa.docstatus = 0
    with pytest.raises(_FrappeError):
        fake.api.cancel_leave_policy_assignment("LPA-1", reason="đủ")


def test_b21_cancel_assignment_cascades_allocations(fake):
    lpa = _FakeDoc(
        {"doctype": "Leave Policy Assignment", "employee": "EMP-1", "leave_policy": "P1",
         "docstatus": 1, "leave_period": "LP-2026", "assignment_based_on": "Leave Period"},
        name="LPA-1",
    )
    fake._existing[("Leave Policy Assignment", "LPA-1")] = lpa
    la1 = _FakeDoc({"doctype": "Leave Allocation", "docstatus": 1}, name="LA-1")
    la2 = _FakeDoc({"doctype": "Leave Allocation", "docstatus": 1}, name="LA-2")
    fake._existing[("Leave Allocation", "LA-1")] = la1
    fake._existing[("Leave Allocation", "LA-2")] = la2
    fake.set_rows("Leave Allocation", [{"name": "LA-1"}, {"name": "LA-2"}])
    res = fake.api.cancel_leave_policy_assignment("LPA-1", reason="thu hồi")
    assert lpa.cancelled is True
    # cascade: allocations submitted bị huỷ TRƯỚC khi huỷ lượt gán (smoke erp-hr.local)
    assert la1.cancelled is True and la2.cancelled is True
    assert res["cancelled_allocations"] == 2


def test_b22_amend_assignment(fake):
    old = _FakeDoc(
        {"doctype": "Leave Policy Assignment", "employee": "EMP-1", "leave_policy": "P1",
         "docstatus": 1, "leave_period": "LP-2026", "assignment_based_on": "Leave Period",
         "company": "GEGE", "carry_forward": 0, "effective_from": "2026-01-01",
         "effective_to": "2026-12-31"},
        name="LPA-1",
    )
    fake._existing[("Leave Policy Assignment", "LPA-1")] = old
    res = fake.api.amend_leave_policy_assignment("LPA-1", leave_policy="P2", carry_forward=1)
    assert old.cancelled is True
    new = fake.docs_created[-1]
    assert new.amended_from == "LPA-1"
    assert new.leave_policy == "P2"
    assert new.carry_forward is True
    assert new.submitted is True
    assert res["amended_from"] == "LPA-1"


def test_b23_bulk_partial_safe(fake):
    fake.fail_submit_for = {
        "EMP-2": "Leave Policy: P1 already assigned for Employee EMP-2 for period 2026-01-01 to 2026-12-31"
    }
    res = fake.api.bulk_assign_leave_policy(
        ["EMP-1", "EMP-2", "EMP-3"], leave_policy="P1", leave_period="LP-2026"
    )
    assert len(res["assigned"]) == 2
    assert [f["employee"] for f in res["failed"]] == ["EMP-2"]
    assert "chồng kỳ" in res["failed"][0]["reason"]
    assert res["total"] == 3


def test_b24_bulk_empty_throws(fake):
    with pytest.raises(_FrappeError):
        fake.api.bulk_assign_leave_policy([], leave_policy="P1", leave_period="LP-2026")


def test_friendly_error_maps_overlap(fake):
    msg = fake.api._friendly_leave_error(
        "Leave Policy: P1 already assigned for Employee EMP-2 for period"
    )
    assert "chồng kỳ" in msg
    assert "Value missing for Title" not in msg or "Thiếu" in fake.api._friendly_leave_error(
        "Error: Value missing for Title"
    )


# --------------------------------------------------------------------------- #
# lists (server-side search / filters / G3 fix)
# --------------------------------------------------------------------------- #
def test_b25_list_policies_filters(fake):
    fake.api.list_leave_policies(q="thường niên", docstatus=1)
    doctype, kwargs = fake.all_calls[-1]
    assert doctype == "Leave Policy"
    assert kwargs["filters"] == [["docstatus", "=", 1]]
    assert any(f[0] == "title" for f in kwargs["or_filters"])
    assert "title" in kwargs["fields"] and "amended_from" in kwargs["fields"]


def test_b26_list_assignments_no_hardcoded_docstatus(fake):
    fake.api.list_leave_policy_assignments()
    doctype, kwargs = fake.all_calls[-1]
    assert kwargs["filters"] == []  # G3: no forced docstatus=1 anymore
    for field in ("docstatus", "carry_forward", "leaves_allocated", "amended_from"):
        assert field in kwargs["fields"]


def test_b26b_list_assignments_docstatus_filter(fake):
    fake.api.list_leave_policy_assignments(docstatus=1, employee="EMP-1")
    doctype, kwargs = fake.all_calls[-1]
    assert ["docstatus", "=", 1] in kwargs["filters"]
    assert ["employee", "=", "EMP-1"] in kwargs["filters"]


def test_b27_list_allocations_filter(fake):
    fake.api.list_leave_allocations(assignment="LPA-1")
    doctype, kwargs = fake.all_calls[-1]
    assert doctype == "Leave Allocation"
    assert ["leave_policy_assignment", "=", "LPA-1"] in kwargs["filters"]


def test_b25b_list_periods_is_active_all(fake):
    fake.api.list_leave_periods(is_active="")
    doctype, kwargs = fake.all_calls[-1]
    assert not any(f[0] == "is_active" for f in kwargs["filters"])
    fake.api.list_leave_periods(is_active=1, company="GEGE")
    doctype, kwargs = fake.all_calls[-1]
    assert ["is_active", "=", 1] in kwargs["filters"]
    assert ["company", "=", "GEGE"] in kwargs["filters"]


# --------------------------------------------------------------------------- #
# Leave Period update / delete
# --------------------------------------------------------------------------- #
def _period(fake, **kw):
    doc = _FakeDoc(
        {
            "doctype": "Leave Period",
            "from_date": kw.get("from_date", datetime.date(2026, 1, 1)),
            "to_date": kw.get("to_date", datetime.date(2026, 12, 31)),
            "company": kw.get("company", "GEGE"),
            "is_active": kw.get("is_active", 1),
        },
        name=kw.get("name", "LP-2026"),
    )
    fake._existing[("Leave Period", doc.name)] = doc
    return doc


def test_b28_update_period_dates_frozen_with_assignment(fake):
    _period(fake)
    fake.set_rows("Leave Policy Assignment", [{"name": "LPA-1"}])
    with pytest.raises(_FrappeError) as err:
        fake.api.update_leave_period("LP-2026", from_date="2026-02-01")
    assert "lượt gán" in str(err.value)


def test_b29_update_period_toggle_active(fake):
    doc = _period(fake)
    res = fake.api.update_leave_period("LP-2026", is_active=0)
    assert doc.saved is True
    assert doc.is_active is False
    assert res["is_active"] is False


def test_b30_delete_period_guard_and_delete(fake):
    _period(fake)
    fake.set_rows("Leave Policy Assignment", [{"name": "LPA-1"}])
    with pytest.raises(_FrappeError):
        fake.api.delete_leave_period("LP-2026")
    fake.set_rows("Leave Policy Assignment", [])
    fake.set_rows("Leave Allocation", [])
    fake.api.delete_leave_period("LP-2026")
    assert ("Leave Period", "LP-2026") in fake.deleted


# --------------------------------------------------------------------------- #
# permission gate (mirror test_hardening fixture — spot checks)
# --------------------------------------------------------------------------- #
def test_b32_new_endpoints_require_hr_admin(fake, monkeypatch):
    def _denied(*a, **k):
        raise _FrappeError("permission denied")

    monkeypatch.setattr(fake.api, "_require_hr_admin", _denied)
    for call in (
        lambda: fake.api.submit_leave_policy("P1"),
        lambda: fake.api.cancel_leave_policy("P1", reason="x"),
        lambda: fake.api.bulk_assign_leave_policy(["EMP-1"], leave_policy="P1", leave_period="LP"),
        lambda: fake.api.delete_leave_period("LP-2026"),
        lambda: fake.api.list_leave_allocations(),
    ):
        with pytest.raises(_FrappeError):
            call()
