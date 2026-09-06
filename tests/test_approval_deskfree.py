"""Bench-free tests for the approval-inbox desk-free Phase A endpoints
(plans/approvals-deskfree-complete.md §5.1 — AD1..AD8, AD24, AD25).

Covers the NEW surface of ``api/approval.py``:
``filter_options`` (AD1-2), the ``bucket="processed"`` decision history on
``get_pending_approvals`` (AD3-5) + the pending-contract regression pin
(AD25), ``get_request_detail`` (AD6-8) and ``export_pending_csv`` (AD24).

Stub-frappe pattern of ``tests/test_overtime_deskfree.py``: a stub ``frappe``
is injected into ``sys.modules``; heavy collaborators (matrices via get_all,
comments/files via a store-backed ``frappe.get_all``) are fed through a
``FakeStore``. The FakeDB here ALSO understands LIST-form filters — the
pending loader and the processed bucket filter with
``[["field", "in", [...]]]`` tuples.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

OT_DT = "VN Overtime Request"
LEAVE_DT = "Leave Application"
LOG_DT = "VN Approval Log"

OT_TYPE = "Overtime Request"
LEAVE_TYPE = "Leave Application"


# --------------------------------------------------------------------------- #
# Stub infrastructure
# --------------------------------------------------------------------------- #
class FakeDoc:
    """Minimal Document double: get/setattr, as_dict, save."""

    def __init__(self, store, data):
        object.__setattr__(self, "_store", store)
        for k, v in data.items():
            setattr(self, k, v)
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def update(self, d):
        for k, v in (d or {}).items():
            setattr(self, k, v)

    def save(self, *a, **kw):
        self._store.docs[self.name] = {
            k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "flags"
        }
        return self

    def insert(self, *a, **kw):
        if not getattr(self, "name", None):
            self.name = f"NEW-{len(self._store.docs) + 1}"
        self._store.inserted.append(self.name)
        return self.save()

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "flags"}


class FakeStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}          # get_doc truth by name
        self.queries: dict[str, list[dict]] = {} # db.get_all rows per doctype
        self.mod_queries: dict[str, list[dict]] = {}  # frappe.get_all rows (Comment/File)
        self.values: dict[str, dict] = {}        # db.get_value maps per doctype
        self.inserted: list[str] = []            # names inserted via FakeDoc.insert
        self.deleted: list[str] = []             # names removed via frappe.delete_doc
        self.mails: list[tuple] = []             # (recipients, subject) via frappe.sendmail
        self.single: dict = {}                   # db.get_single_value map (field → value)


class FakeDB:
    """db double matching BOTH dict and LIST filter forms."""

    def __init__(self, store: FakeStore):
        self._store = store

    @staticmethod
    def _match(row, filters) -> bool:
        if isinstance(filters, dict):
            items = [(k, "==", v) for k, v in filters.items()]
        else:
            items = []
            for f in filters or []:
                if isinstance(f, (list, tuple)) and len(f) == 3:
                    items.append((f[0], f[1], f[2]))
        for k, op, v in items:
            rv = row.get(k)
            if op == "in":
                if rv not in (v or []):
                    return False
            elif op == ">=":
                if str(rv or "") < str(v):
                    return False
            elif op == "<=":
                if str(rv or "") > str(v):
                    return False
            else:  # "=="
                if isinstance(v, list) and v:
                    op0 = v[0]
                    if op0 == "in":
                        if rv not in (v[1] if len(v) > 1 else []):
                            return False
                    elif op0 == "<=":
                        if str(rv or "") > str(v[1]):
                            return False
                    elif op0 == ">=":
                        if str(rv or "") < str(v[1]):
                            return False
                    elif op0 == "<":
                        if str(rv or "") >= str(v[1]):
                            return False
                    elif op0 == ">":
                        if str(rv or "") <= str(v[1]):
                            return False
                    else:
                        return False
                elif rv != v:
                    return False
        return True

    def get_all(self, doctype, filters=None, fields=None, order_by=None,
                limit_page_length=None, pluck=None, **kw):
        rows = [dict(r) for r in self._store.queries.get(doctype, [])
                if self._match(r, filters)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows

    def get_value(self, doctype, name, fields=None, as_dict=False, *a, **kw):
        val = self._store.values.get(doctype, {}).get(name)
        if val is None:
            return None
        if isinstance(fields, (list, tuple)):
            picked = {f: val.get(f) for f in fields}
            return picked if as_dict else (list(picked.values())[0] if picked else None)
        return val.get(fields) if isinstance(val, dict) else val

    def table_exists(self, name):
        return True

    def set_value(self, *a, **kw):
        return None

    def sql(self, *a, **kw):
        return None


def _install_stub(monkeypatch, store: FakeStore):
    """Install a stub ``frappe`` sufficient for api.approval Phase A."""
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s

    def _whitelist(fn=None, **kw):
        def deco(f):
            return f
        return deco(fn) if fn is not None else deco

    mod.whitelist = _whitelist
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.ValidationError = type("ValidationError", (Exception,), {})
    mod.DoesNotExistError = type("DoesNotExistError", (Exception,), {})

    def _throw(msg, exc=None):
        err = (exc or Exception)(msg)
        err.kind = exc
        raise err

    mod.throw = _throw
    mod.publish_realtime = lambda *a, **k: None
    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="hr@x")
    mod.get_roles = lambda user=None: ["HR Manager"]

    def _get_doc(doctype, name=None):
        # New-doc style: frappe.get_doc({...}) → a fresh FakeDoc (insert/save
        # persist it into the store — used by return_request's Comment row,
        # delegate_request's VN Approval Delegation and _write_log payloads).
        if isinstance(doctype, dict):
            return FakeDoc(store, dict(doctype))
        row = store.docs.get(name)
        if row is None:
            raise mod.DoesNotExistError(f"{doctype} {name} not found")
        data = dict(row)
        data.setdefault("doctype", doctype)
        return FakeDoc(store, data)

    mod.get_doc = _get_doc

    def _new_doc(doctype):
        return FakeDoc(store, {"doctype": doctype, "name": None})

    mod.new_doc = _new_doc
    mod.delete_doc = lambda dt, name, **kw: store.deleted.append(name)
    mod.parse_json = lambda v: v

    def _mod_get_all(doctype, filters=None, fields=None, order_by=None, limit=None, pluck=None, **kw):
        rows = [dict(r) for r in store.mod_queries.get(doctype, [])
                if FakeDB._match(r, filters)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows

    mod.get_all = _mod_get_all
    mod.sendmail = lambda recipients, subject, message, **kw: store.mails.append(
        (list(recipients or []), subject)
    )

    def _get_single_value(doctype, field, *a, **kw):
        return store.single.get(field)

    mod.db = FakeDB(store)
    mod.db.get_single_value = _get_single_value

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: v
    utils.get_datetime = lambda v=None: v
    utils.now = lambda: "2026-09-05 00:00:00"
    utils.today = lambda: "2026-09-05"
    utils.add_days = lambda d, n: d
    import datetime as _dtmod

    utils.now_datetime = lambda: _dtmod.datetime(2026, 9, 5, 12, 0, 0)
    utils.get_datetime = lambda v=None: (
        _dtmod.datetime.fromisoformat(str(v).replace(" ", "T")) if v else None
    )
    mod.utils = utils

    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    return mod


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def approval(monkeypatch, store):
    """Fresh api.approval bound to the stub frappe (HR Manager by default)."""
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import approval as approval_api
    from gege_hr.gege_hr.utils import employee as emp_utils

    importlib.reload(approval_api)
    monkeypatch.setattr(emp_utils, "get_current_user", lambda: "hr@x")
    monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["HR Manager"])
    monkeypatch.setattr(emp_utils, "get_employee_for_user", lambda: "EMP-9")
    return approval_api


def _seed_ot_pending(store, employee="EMP-1", employee_name="An Nguyen", name="OR-1"):
    store.queries.setdefault(OT_DT, []).append({
        "name": name,
        "employee": employee,
        "employee_name": employee_name,
        "docstatus": 0,
        "creation": "2026-09-01 08:00:00",
        "workflow_state": "Pending Manager",
        "company": "GEGE",
        "work_date": "2026-09-01",
        "overtime_type": "Post Shift",
        "requested_hours": 2.0,
        "reason": "gấp deadline",
    })


def _seed_leave_pending(store, name="LAP-1", employee="EMP-2", employee_name="Binh Tran"):
    store.queries.setdefault(LEAVE_DT, []).append({
        "name": name,
        "employee": employee,
        "employee_name": employee_name,
        "docstatus": 0,
        "creation": "2026-09-02 09:00:00",
        "status": "Open",
        "from_date": "2026-09-10",
        "to_date": "2026-09-11",
        "leave_type": "Casual Leave",
        "total_leave_days": 2,
        "description": "việc gia đình",
    })


# --------------------------------------------------------------------------- #
# AD1-2 — filter_options
# --------------------------------------------------------------------------- #
class TestFilterOptions:
    def test_ad1_distinct_employees_departments_branches(self, approval, store):
        _seed_ot_pending(store, employee="EMP-1", employee_name="An Nguyen", name="OR-1")
        _seed_ot_pending(store, employee="EMP-2", employee_name="Binh Tran", name="OR-2")
        _seed_leave_pending(store, name="LAP-1", employee="EMP-1", employee_name="An Nguyen")
        store.queries.setdefault("Employee", []).extend([
            {"name": "EMP-1", "employee_name": "An Nguyen", "department": "Eng", "branch": "HN"},
            {"name": "EMP-2", "employee_name": "Binh Tran", "department": "Sales", "branch": "HCM"},
        ])

        out = approval.filter_options(approver="hr@x")

        assert [e["value"] for e in out["employees"]] == ["EMP-1", "EMP-2"]
        assert [e["label"] for e in out["employees"]] == ["An Nguyen", "Binh Tran"]
        assert [d["value"] for d in out["departments"]] == ["Eng", "Sales"]
        assert [b["value"] for b in out["branches"]] == ["HCM", "HN"]

    def test_ad2_plain_employee_gets_empty_lists(self, approval, store, monkeypatch):
        from gege_hr.gege_hr.utils import employee as emp_utils

        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])
        _seed_ot_pending(store)

        out = approval.filter_options(approver="emp@x")

        assert out == {"employees": [], "departments": [], "branches": []}


# --------------------------------------------------------------------------- #
# AD3-5, AD25 — bucket=processed + pending regression pin
# --------------------------------------------------------------------------- #
class TestProcessedBucket:
    def _seed_logs(self, store):
        store.queries.setdefault(LOG_DT, []).extend([
            {"reference_doctype": OT_DT, "reference_name": "OR-1", "action": "Approve",
             "actor": "hr@x", "action_at": "2026-09-02 10:00:00"},
            {"reference_doctype": LEAVE_DT, "reference_name": "LAP-1", "action": "Reject",
             "actor": "hr@x", "action_at": "2026-09-03 11:00:00"},
            # Another approver's log — must be filtered out by actor.
            {"reference_doctype": OT_DT, "reference_name": "OR-2", "action": "Approve",
             "actor": "other@x", "action_at": "2026-09-03 12:00:00"},
        ])

    def test_ad3_groups_by_type_with_last_action(self, approval, store):
        self._seed_logs(store)
        store.queries.setdefault(OT_DT, []).append({
            "name": "OR-1", "employee": "EMP-1", "employee_name": "An", "docstatus": 1,
            "creation": "2026-09-01 08:00:00", "workflow_state": "Approved",
            "company": "GEGE", "work_date": "2026-09-01", "reason": "ok",
        })
        store.queries.setdefault(LEAVE_DT, []).append({
            "name": "LAP-1", "employee": "EMP-2", "employee_name": "Binh", "docstatus": 0,
            "creation": "2026-09-02 09:00:00", "status": "Rejected",
            "from_date": "2026-09-10", "to_date": "2026-09-11", "description": "x",
        })

        out = approval.get_pending_approvals(approver="hr@x", bucket="processed")
        by_type = {g["request_type"]: g for g in out["groups"]}

        assert set(by_type) == {OT_TYPE, LEAVE_TYPE}  # OR-2 (other actor) excluded
        ot_row = by_type[OT_TYPE]["requests"][0]
        assert ot_row["last_action"] == "Approve"
        assert ot_row["last_action_at"] == "2026-09-02 10:00:00"
        assert by_type[LEAVE_TYPE]["requests"][0]["last_action"] == "Reject"

    def test_ad4_window_filters_on_action_at(self, approval, store):
        self._seed_logs(store)
        store.queries.setdefault(OT_DT, []).append({
            "name": "OR-1", "employee": "EMP-1", "employee_name": "An", "docstatus": 1,
            "creation": "2026-09-01 08:00:00", "workflow_state": "Approved",
        })
        store.queries.setdefault(LEAVE_DT, []).append({
            "name": "LAP-1", "employee": "EMP-2", "employee_name": "Binh", "docstatus": 0,
            "creation": "2026-09-02 09:00:00", "status": "Rejected",
        })

        # from 2026-09-03 → only the 09-03 Reject log survives.
        out = approval.get_pending_approvals(
            approver="hr@x", bucket="processed", from_date="2026-09-03"
        )
        types_seen = {g["request_type"] for g in out["groups"]}
        assert types_seen == {LEAVE_TYPE}

    def test_ad5_deleted_doc_skipped_silently(self, approval, store):
        self._seed_logs(store)
        # No rows seeded for OT_DT / LEAVE_DT → the referenced docs are "deleted".

        out = approval.get_pending_approvals(approver="hr@x", bucket="processed")

        assert out["groups"] == []

    def test_ad25_pending_contract_unchanged_without_bucket(self, approval, store):
        _seed_ot_pending(store)
        _seed_leave_pending(store)

        out = approval.get_pending_approvals(approver="hr@x")

        by_type = {g["request_type"]: g for g in out["groups"]}
        assert set(by_type) == {OT_TYPE, LEAVE_TYPE}
        ot_row = by_type[OT_TYPE]["requests"][0]
        # Legacy rows carry NO processed-only keys…
        assert "last_action" not in ot_row
        # …but DO carry the normalised inbox stamps.
        assert ot_row["request_type"] == OT_TYPE
        assert ot_row["approver"] == "hr@x"
        assert ot_row["reason"] == "gấp deadline"


# --------------------------------------------------------------------------- #
# AD6-8 — get_request_detail
# --------------------------------------------------------------------------- #
class TestGetRequestDetail:
    def _seed_ot_doc(self, store):
        store.docs["OR-1"] = {
            "doctype": OT_DT, "name": "OR-1", "employee": "EMP-1",
            "employee_name": "An Nguyen", "docstatus": 0,
            "workflow_state": "Pending Manager", "company": "GEGE",
            "work_date": "2026-09-01", "overtime_type": "Post Shift",
            "requested_hours": 2.0, "reason": "gấp deadline",
        }
        store.queries.setdefault(LOG_DT, []).append({
            "reference_doctype": OT_DT, "reference_name": "OR-1", "action": "Submit",
            "from_state": "Draft", "to_state": "Pending Manager", "actor": "emp@x",
            "comment": "", "action_at": "2026-09-01 08:00:00",
        })
        store.mod_queries.setdefault("Comment", []).append({
            "reference_doctype": OT_DT, "reference_name": "OR-1",
            "comment_type": "Comment", "name": "C-1", "owner": "emp@x",
            "content": "đính kèm thêm giờ", "creation": "2026-09-01 09:00:00",
        })
        store.mod_queries.setdefault("File", []).append({
            "attached_to_doctype": OT_DT, "attached_to_name": "OR-1",
            "name": "F-1", "file_name": "proof.png", "file_url": "/files/proof.png",
            "file_size": 1024,
        })

    def test_ad6_ot_pending_full_payload(self, approval, store):
        self._seed_ot_doc(store)

        out = approval.get_request_detail(name="OR-1", request_type=OT_TYPE)

        assert out["name"] == "OR-1"
        assert out["label"] == "Tăng ca"
        assert out["doc"]["request_type"] == OT_TYPE
        assert out["matrix"]["current_state"] == "Pending Manager"
        assert out["history"] and out["history"][0]["action"] == "Submit"
        assert out["comments"] and out["comments"][0]["content"] == "đính kèm thêm giờ"
        assert out["attachments"] and out["attachments"][0]["file_name"] == "proof.png"
        assert isinstance(out["context"], dict)
        assert out["can"] == {"approve": True, "reject": True, "return": True, "edit_amount": False}

    def test_ad7_leave_not_returnable(self, approval, store):
        store.docs["LAP-1"] = {
            "doctype": LEAVE_DT, "name": "LAP-1", "employee": "EMP-2",
            "employee_name": "Binh Tran", "docstatus": 0, "status": "Open",
            "from_date": "2026-09-10", "to_date": "2026-09-11",
            "leave_type": "Casual Leave", "total_leave_days": 2,
            "description": "việc gia đình",
        }

        out = approval.get_request_detail(name="LAP-1", request_type=LEAVE_TYPE)

        assert out["label"] == "Nghỉ phép"
        assert out["can"]["approve"] is True
        assert out["can"]["return"] is False  # Leave is HRMS-managed → reject-only

    def test_ad8_non_approver_non_hr_non_owner_denied(self, approval, store, monkeypatch):
        from gege_hr.gege_hr.utils import employee as emp_utils

        self._seed_ot_doc(store)
        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])
        monkeypatch.setattr(approval, "_user_can_act", lambda *a, **k: False)

        with pytest.raises(approval.frappe.PermissionError):
            approval.get_request_detail(name="OR-1", request_type=OT_TYPE)


# --------------------------------------------------------------------------- #
# AD24 — export_pending_csv
# --------------------------------------------------------------------------- #
class TestExportCsv:
    def test_ad24_csv_shape_and_department_enrichment(self, approval, store):
        _seed_ot_pending(store, employee="EMP-1", employee_name="An Nguyen", name="OR-1")
        _seed_ot_pending(store, employee="EMP-2", employee_name="Binh Tran", name="OR-2")
        store.queries.setdefault("Employee", []).append(
            {"name": "EMP-1", "employee_name": "An Nguyen", "department": "Eng"},
        )

        out = approval.export_pending_csv(approver="hr@x")

        assert out["filename"].startswith("duyet-yeu-cau-")
        assert out["csv"].startswith("\ufeff")  # UTF-8 BOM for Excel
        lines = out["csv"].strip("\ufeff\r\n").split("\r\n")
        assert lines[0] == "Loại,Mã,Nhân viên,Mã NV,Phòng ban,Trạng thái,Ngày yêu cầu,Gửi lúc,Lý do"
        assert len(lines) == 3  # header + 2 rows
        assert "OR-1" in lines[1] and "Eng" in lines[1]
        assert "Tăng ca" in lines[1]
        # EMP-2 has no Employee row → empty department cell, row still exported.
        assert lines[2].count(",,") >= 1


# --------------------------------------------------------------------------- #
# AD9-13 — return_request (Phase B1)
# --------------------------------------------------------------------------- #
def _seed_ot_doc_pending(store, name="OR-1", state="Pending Manager"):
    store.docs[name] = {
        "doctype": OT_DT, "name": name, "employee": "EMP-1",
        "employee_name": "An Nguyen", "docstatus": 0,
        "workflow_state": state, "company": "GEGE",
        "work_date": "2026-09-01", "overtime_type": "Post Shift",
        "requested_hours": 2.0, "reason": "gấp deadline",
    }


class TestReturnRequest:
    def test_ad9_pending_ot_returns_to_draft_with_log_and_comment(self, approval, store):
        _seed_ot_doc_pending(store)

        out = approval.return_request(name="OR-1", request_type=OT_TYPE, comment="thiếu CCDD")

        assert out["status"] == "Draft"
        assert store.docs["OR-1"]["workflow_state"] == "Draft"
        logs = [d for d in store.docs.values() if d.get("action") == "Return"]
        assert logs and logs[0]["from_state"] == "Pending Manager" and logs[0]["to_state"] == "Draft"
        assert logs[0]["comment"] == "thiếu CCDD"
        comments = [d for d in store.docs.values() if d.get("doctype") == "Comment"]
        assert comments and "thiếu CCDD" in comments[0]["content"]

    def test_ad10_non_pending_state_throws(self, approval, store):
        _seed_ot_doc_pending(store, state="Approved")

        with pytest.raises(approval.frappe.PermissionError):
            approval.return_request(name="OR-1", request_type=OT_TYPE)

    def test_ad11_leave_type_not_returnable(self, approval, store):
        store.docs["LAP-1"] = {
            "doctype": LEAVE_DT, "name": "LAP-1", "employee": "EMP-2",
            "employee_name": "Binh", "docstatus": 0, "status": "Open",
        }

        with pytest.raises(Exception, match="không hỗ trợ trả lại"):
            approval.return_request(name="LAP-1", request_type=LEAVE_TYPE)

    def test_ad12_non_approver_denied(self, approval, store, monkeypatch):
        from gege_hr.gege_hr.utils import employee as emp_utils

        _seed_ot_doc_pending(store)
        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])
        monkeypatch.setattr(approval, "_user_can_act", lambda *a, **k: False)

        with pytest.raises(approval.frappe.PermissionError):
            approval.return_request(name="OR-1", request_type=OT_TYPE)

    def test_ad13_seed_contains_return_transitions(self, approval):
        """Pin the B-1 style regression: Return transitions + action master."""
        from gege_hr.gege_hr import setup_workflows as sw

        specs = sw._common_transitions()
        assert "Return" in sw._ACTIONS
        for role in (sw._EMP, sw._HRU, sw._HRM):
            assert ("Pending Manager", "Return", "Draft", role) in specs
            assert ("Pending HR", "Return", "Draft", role) in specs


# --------------------------------------------------------------------------- #
# AD14-19 — delegation (Phase B2)
# --------------------------------------------------------------------------- #
DEL_DT = "VN Approval Delegation"


def _seed_delegation(store, **kw):
    row = {
        "name": kw.get("name", "DEL-1"),
        "from_user": kw.get("from_user", "mgr@x"),
        "to_user": kw.get("to_user", "del@x"),
        "transaction_type": kw.get("transaction_type", "All"),
        "request_name": kw.get("request_name", ""),
        "from_date": kw.get("from_date", "2026-09-01"),
        "to_date": kw.get("to_date", "2026-09-30"),
        "is_active": kw.get("is_active", 1),
    }
    store.queries.setdefault(DEL_DT, []).append(row)
    return row


def _seed_line_manager_matrix(store):
    store.queries.setdefault("VN Approval Matrix", []).append({
        "name": "M1", "transaction_type": "Overtime Request",
        "is_active": 1, "company": "GEGE",
    })
    store.docs["M1"] = {
        "doctype": "VN Approval Matrix", "name": "M1", "apply_to": "All",
        "branch": None, "department": None, "employee_grade": None,
        "modified": "2026-01-01",
        "steps": [types.SimpleNamespace(
            step_no=1, approver_type="Line Manager",
            approver_user=None, approver_role=None,
        )],
    }
    store.values.setdefault("Employee", {}).update({
        "EMP-1": {
            "name": "EMP-1", "company": "GEGE", "branch": None,
            "department": None, "grade": None, "reports_to": "EMP-M",
        },
        "EMP-M": {"user_id": "mgr@x"},
    })


class TestDelegation:
    def test_ad14_active_matching_delegation_resolves(self, approval, store):
        _seed_delegation(store)

        out = approval._delegated_users("mgr@x", OT_TYPE, "OR-1")

        assert out == ["del@x"]

    def test_ad15_expired_wrong_type_inactive_dont_resolve(self, approval, store):
        _seed_delegation(store, name="DEL-old", to_date="2026-08-31")   # expired
        _seed_delegation(store, name="DEL-type", transaction_type="Leave Application")
        _seed_delegation(store, name="DEL-off", is_active=0)
        _seed_delegation(store, name="DEL-scope", request_name="OR-OTHER")  # different request

        assert approval._delegated_users("mgr@x", OT_TYPE, "OR-1") == []

    def test_ad16_delegate_can_act_via_user_can_act(self, approval, store, monkeypatch):
        from gege_hr.gege_hr.utils import employee as emp_utils

        _seed_line_manager_matrix(store)
        _seed_delegation(store)

        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])
        doc_dict = {
            "name": "OR-1", "employee": "EMP-1", "company": "GEGE",
            "workflow_state": "Pending Manager",
        }
        # The delegate (del@x) is a plain Employee — only the delegation lets
        # them hold the Line Manager step.
        assert approval._user_can_act(doc_dict, OT_TYPE, "del@x") is True
        # A random employee still cannot.
        assert approval._user_can_act(doc_dict, OT_TYPE, "rand@x") is False

    def test_ad17_delegate_request_creates_scoped_delegation_and_log(
        self, approval, store, monkeypatch
    ):
        from gege_hr.gege_hr.utils import employee as emp_utils

        _seed_ot_doc_pending(store, name="OR-2")
        _seed_line_manager_matrix(store)  # mgr@x holds the step (Line Manager)
        monkeypatch.setattr(emp_utils, "get_current_user", lambda: "mgr@x")
        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])

        out = approval.delegate_request(
            name="OR-2", request_type=OT_TYPE, to_user="del@x", comment="tôi nghỉ phép"
        )

        assert out["to_user"] == "del@x"
        assert out["status"] == "Pending Manager"  # state unchanged
        # Scoped delegation row created for exactly this request.
        dels = [d for d in store.docs.values() if d.get("doctype") == DEL_DT]
        assert dels and dels[0]["request_name"] == "OR-2"
        assert dels[0]["from_user"] == "mgr@x" and dels[0]["to_user"] == "del@x"
        # Delegate action logged; the request stayed pending.
        logs = [d for d in store.docs.values() if d.get("action") == "Delegate"]
        assert logs and logs[0]["actor"] == "mgr@x"
        assert store.docs["OR-2"]["workflow_state"] == "Pending Manager"

    def test_ad18_self_delegation_throws(self, approval, store, monkeypatch):
        from gege_hr.gege_hr.utils import employee as emp_utils

        _seed_ot_doc_pending(store)
        monkeypatch.setattr(emp_utils, "get_current_user", lambda: "hr@x")

        with pytest.raises(Exception, match="chính mình"):
            approval.delegate_request(name="OR-1", request_type=OT_TYPE, to_user="hr@x")

    def test_ad19_delete_delegation_by_non_owner_denied(self, monkeypatch, store):
        _install_stub(monkeypatch, store)
        from gege_hr.gege_hr.api import delegation as del_api
        from gege_hr.gege_hr.utils import employee as emp_utils

        importlib.reload(del_api)
        store.docs["DEL-9"] = {
            "doctype": DEL_DT, "name": "DEL-9", "from_user": "mgr@x",
            "to_user": "del@x", "transaction_type": "All", "request_name": "",
            "from_date": "2026-09-01", "to_date": "2026-09-30", "is_active": 1,
        }
        monkeypatch.setattr(emp_utils, "get_current_user", lambda: "other@x")
        monkeypatch.setattr(emp_utils, "get_user_roles", lambda: ["Employee"])

        with pytest.raises(del_api.frappe.PermissionError):
            del_api.delete_delegation(name="DEL-9")
        # …but the delegator themself may delete it.
        monkeypatch.setattr(emp_utils, "get_current_user", lambda: "mgr@x")
        out = del_api.delete_delegation(name="DEL-9")
        assert out["name"] == "DEL-9"
        assert "DEL-9" in store.deleted


# --------------------------------------------------------------------------- #
# AD20-23 — approval follow-up jobs (Phase B3)
# --------------------------------------------------------------------------- #
@pytest.fixture
def followup(monkeypatch, store):
    """Fresh api.approval_followup bound to the stub frappe.

    api.approval must ALSO be reloaded: followup reaches into its loaders
    (_load_matrices / _employee_attrs) and a stale module-level ``frappe``
    binding from an earlier test would point at that test's store.
    """
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import approval as approval_api
    from gege_hr.gege_hr.api import approval_followup as fu

    importlib.reload(approval_api)
    importlib.reload(fu)
    return fu


def _seed_pending_ot_query(store, name, creation, employee="EMP-1"):
    store.queries.setdefault(OT_DT, []).append({
        "name": name,
        "employee": employee,
        "employee_name": "An Nguyen",
        "docstatus": 0,
        "creation": creation,
        "workflow_state": "Pending Manager",
        "company": "GEGE",
    })


class TestFollowup:
    def _seed_holder_queue(self, store):
        _seed_line_manager_matrix(store)  # step holder = mgr@x (Line Manager)

    def test_ad20_digest_one_mail_per_holder_with_count_and_oldest(self, followup, store):
        self._seed_holder_queue(store)
        _seed_pending_ot_query(store, "OR-1", "2026-09-04 08:00:00")
        _seed_pending_ot_query(store, "OR-2", "2026-09-05 09:00:00")

        out = followup.send_pending_digests()

        assert out["approvers"] == ["mgr@x"]
        assert out["sent"] == 1
        assert len(store.mails) == 1
        recipients, subject = store.mails[0]
        assert recipients == ["mgr@x"]
        assert "2" in subject  # pending count in the subject line

    def test_ad23_digest_disabled_is_a_noop(self, followup, store):
        self._seed_holder_queue(store)
        _seed_pending_ot_query(store, "OR-1", "2026-09-04 08:00:00")
        store.single["vn_approval_digest_enabled"] = 0

        out = followup.send_pending_digests()

        assert out.get("skipped")
        assert store.mails == []

    def test_ad21_fresh_requests_below_sla_get_nothing(self, followup, store):
        self._seed_holder_queue(store)
        _seed_pending_ot_query(store, "OR-1", "2026-09-05 08:00:00")  # ~4h old

        out = followup.escalate_stale_requests()

        assert out == {"reminded": 0, "escalated": 0}
        assert store.mails == []

    def test_ad22_stale_reminds_holder_and_escalates_hr(self, followup, store):
        self._seed_holder_queue(store)
        _seed_pending_ot_query(store, "OR-stale", "2026-09-01 08:00:00")   # ~100h → reminder
        _seed_pending_ot_query(store, "OR-urgent", "2026-08-25 08:00:00")  # ~260h → reminder + HR
        store.mod_queries.setdefault("Has Role", []).append({
            "role": "HR Manager", "parenttype": "User", "parent": "hr2@x",
        })

        out = followup.escalate_stale_requests()

        assert out["reminded"] == 2  # both rows reminded mgr@x
        assert out["escalated"] == 1  # only the ≥120h row escalated to HR
        hr_mails = [m for m in store.mails if "hr2@x" in m[0]]
        assert hr_mails and any("Escalate" in subj for _, subj in hr_mails)
        holder_mails = [m for m in store.mails if "mgr@x" in m[0]]
        assert len(holder_mails) == 2
