"""Bench-free tests for the /hr/overtime desk-free endpoints
(plans/overtime-deskfree-complete.md — OD1..OD22).

Covers the NEW surface of ``api/overtime.py`` (``get_overtime_request`` /
``update_overtime_request`` / ``all_overtime_requests`` / ``_publish_overtime``),
the realtime wire in ``approval._after_ot_state_change`` (fires even on
pending→pending moves), the B-1 workflow-seed fix (Employee may Reject from
Pending HR) and the ``_ensure_transition_rows`` upsert, plus contract pins for
the reject ``comment`` path (OT3).

Stub-frappe pattern of ``tests/test_overtime_approval_sync.py``: a stub
``frappe`` is injected into ``sys.modules``; heavy collaborators (audit log,
send_for_approval, publish_realtime, emp_utils) are spied, not executed.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types

import pytest

DOCTYPE = "VN Overtime Request"


# --------------------------------------------------------------------------- #
# Stub infrastructure
# --------------------------------------------------------------------------- #
class _Thrown(Exception):
    """Legacy marker (unused) — the stub throw now raises the requested class."""

    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


class FakeDoc:
    """Minimal Document double: get/setattr, reload (from store truth), save."""

    def __init__(self, store, data):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_saved_kwargs", [])
        for k, v in data.items():
            setattr(self, k, v)
        if not getattr(self, "name", None):
            self.name = "OR-NEW"
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def update(self, d):
        for k, v in (d or {}).items():
            setattr(self, k, v)

    def reload(self):
        fresh = self._store.docs.get(self.name)
        if fresh is not None:
            self.workflow_state = fresh.get("workflow_state", self.workflow_state)
            self.docstatus = fresh.get("docstatus", self.docstatus)

    def save(self, *a, **kw):
        self._saved_kwargs.append(kw.get("ignore_permissions"))
        self._store.docs[self.name] = {
            k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "flags"
        }
        return self

    def insert(self, *a, **kw):
        self._store.inserted.append(self.name)
        self._store.docs[self.name] = {
            k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "flags"
        }
        return self

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class FakeStore:
    def __init__(self):
        self.docs: dict[str, dict] = {}          # DOCTYPE docs by name (DB truth)
        self.queries: dict[str, list[dict]] = {} # get_all rows per doctype
        self.values: dict[str, dict] = {}        # get_value maps per doctype
        self.exists: dict[str, str] = {}         # exists() truths
        self.inserted: list[str] = []
        self.publish_calls: list[tuple] = []
        self.audit_calls: list[tuple] = []
        self.send_calls: list[object] = []


class FakeDB:
    def __init__(self, store: FakeStore):
        self._store = store

    def _match(self, row, filters):
        for k, v in (filters or {}).items():
            rv = row.get(k)
            if isinstance(v, list) and v and v[0] == "in":
                if rv not in v[1]:
                    return False
            elif isinstance(v, list) and v and v[0] == "between":
                lo, hi = v[1][0], v[1][1]
                s = str(rv or "")
                if lo is not None and s < str(lo):
                    return False
                if hi is not None and s > str(hi):
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

    def exists(self, doctype, name=None, *a, **kw):
        if name is not None:
            return name in self._store.values.get(doctype, {}) or name in self._store.exists.get(doctype, "")
        return bool(self._store.exists.get(doctype))

    def table_exists(self, name):
        return True

    def set_value(self, *a, **kw):
        return None


def _install_stub(monkeypatch, store: FakeStore):
    """Install a stub ``frappe`` sufficient for api.overtime / api.approval."""
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    recorded = set()

    def _whitelist(fn=None, **kw):
        def deco(f):
            recorded.add(getattr(f, "__name__", ""))
            return f
        return deco(fn) if fn is not None else deco

    mod.whitelist = _whitelist
    mod.recorded_whitelist = recorded
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.ValidationError = type("ValidationError", (Exception,), {})
    mod.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    def _throw(msg, exc=None):
        # Raise an instance of the REQUESTED class so pytest.raises(frappe.X)
        # matches exactly like the real frappe.throw.
        err = (exc or Exception)(msg)
        err.kind = exc
        raise err

    mod.throw = _throw

    def _publish(event, payload=None, *a, **kw):
        store.publish_calls.append((event, payload))

    mod.publish_realtime = _publish
    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="emp@x")
    # Default identity = plain Employee (tests that need HR patch ot._is_manager).
    mod.get_roles = lambda user=None: ["Employee"]

    def _get_doc(doctype, name=None):
        if doctype == DOCTYPE:
            row = store.docs.get(name)
            if row is None:
                raise mod.DoesNotExistError(f"{doctype} {name} not found")
            return FakeDoc(store, dict(row))
        row = store.docs.get(f"{doctype}::{name}", {"doctype": doctype, "name": name})
        return FakeDoc(store, dict(row))

    mod.get_doc = _get_doc

    def _new_doc(doctype):
        holder = {}

        class _New:
            def update(self, d):
                holder.update(d)

            def insert(self, *a, **kw):
                holder.setdefault("name", "OR-NEW")
                store.inserted.append(holder["name"])
                return self

            def as_dict(self):
                return dict(holder)

            def __getattr__(self, key):
                return holder.get(key)

        return _New()

    mod.new_doc = _new_doc
    mod.enqueue = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("enqueue unavailable (test)"))
    mod.db = FakeDB(store)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: v
    utils.get_datetime = lambda v=None: v
    utils.now = lambda: "2026-09-03 00:00:00"
    mod.utils = utils

    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    return mod


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def ot(monkeypatch, store):
    """Fresh api.overtime bound to the stub frappe with spied collaborators."""
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import overtime as ot_mod
    from gege_hr.gege_hr.utils import employee as emp_utils

    importlib.reload(ot_mod)
    # Deterministic identity / role helpers (module-level, restored by monkeypatch).
    monkeypatch.setattr(emp_utils, "get_employee_for_user", lambda: "EMP-1")
    monkeypatch.setattr(emp_utils, "emp_name", lambda x: x)
    monkeypatch.setattr(ot_mod.audit_api, "log", lambda kind, **kw: store.audit_calls.append((kind, kw)))
    return ot_mod


@pytest.fixture
def approval(monkeypatch, store):
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import approval as approval_api

    importlib.reload(approval_api)
    return approval_api


def _seed_doc(store, name="OR-1", employee="EMP-1", state="Draft", docstatus=0, **extra):
    row = {
        "doctype": DOCTYPE,
        "name": name,
        "employee": employee,
        "employee_name": "Nhân viên Một",
        "work_date": "2026-09-01",
        "shift_instance": "SI-1",
        "work_session": "WS-1",
        "company": "GEGE",
        "overtime_type": "Post-shift",
        "from_datetime": "2026-09-01 17:00:00",
        "to_datetime": "2026-09-01 19:00:00",
        "requested_hours": 2.0,
        "actual_hours": 1.5,
        "approved_hours": 0,
        "workflow_state": state,
        "docstatus": docstatus,
        "reason": "deadline",
        "owner": "emp@x",
    }
    row.update(extra)
    store.docs[name] = row
    store.queries.setdefault(DOCTYPE, []).append(row)
    store.values.setdefault("Employee", {})[employee] = {
        "employee_name": row["employee_name"], "department": "IT",
    }
    return row


# --------------------------------------------------------------------------- #
# OD1–OD3, OD22 — get_overtime_request
# --------------------------------------------------------------------------- #
class TestGetOvertimeRequest:
    def _run(self, ot, store, monkeypatch, *, is_hr, caller, name="OR-1"):
        monkeypatch.setattr(ot, "_is_manager", lambda: is_hr)
        monkeypatch.setattr(ot.emp_utils, "get_employee_for_user", lambda: caller)
        return ot.get_overtime_request(name=name)

    def test_od1_shape_and_links(self, ot, store, monkeypatch):
        _seed_doc(store)
        store.values["VN Employee Shift Instance"] = {"SI-1": {
            "name": "SI-1", "shift_type": "Ca A", "planned_start": "2026-09-01 08:00:00",
            "planned_end": "2026-09-01 17:00:00",
        }}
        store.values["VN Attendance Work Session"] = {"WS-1": {
            "name": "WS-1", "total_actual_hours": 9.5, "raw_overtime_hours": 2.5,
            "approved_overtime_hours": 0, "calculation_status": "Computed",
        }}
        store.queries["File"] = [
            {"name": "F1", "file_name": "a.pdf", "file_url": "/private/files/a.pdf",
             "is_private": 1, "file_size": 10,
             "attached_to_doctype": DOCTYPE, "attached_to_name": "OR-1"},
        ]
        store.queries["VN Approval Log"] = [
            {"name": "L1", "action": "Reject", "from_state": "Pending Manager",
             "to_state": "Rejected", "actor": "mgr@x", "comment": "thiếu lý do",
             "creation": "2026-09-02",
             "reference_doctype": DOCTYPE, "reference_name": "OR-1"},
        ]
        out = self._run(ot, store, monkeypatch, is_hr=False, caller="EMP-1")
        assert out["doc"]["name"] == "OR-1"
        assert out["doc"]["owner"] == "emp@x"
        assert out["employee_name"] == "Nhân viên Một"
        assert out["links"]["shift_instance"]["shift_type"] == "Ca A"
        assert out["links"]["work_session"]["raw_overtime_hours"] == 2.5
        assert out["attachments"][0]["file_name"] == "a.pdf"
        assert out["activity"][0]["comment"] == "thiếu lý do"
        assert out["can"]["print"] is True

    def test_od1_can_matrix_six_branches(self, ot, store, monkeypatch):
        cases = [
            # (state, docstatus, is_hr, caller) -> expected can dict
            ("Draft", 0, False, "EMP-1", dict(edit=True, cancel=True, resend=False, confirm=False,
                                              upload=True, remove_file=True)),
            ("Pending Manager", 0, False, "EMP-1", dict(edit=False, cancel=True, resend=False,
                                                        confirm=False, upload=False, remove_file=False)),
            ("Rejected", 0, False, "EMP-1", dict(edit=False, cancel=False, resend=True,
                                                confirm=False, upload=False, remove_file=False)),
            ("Draft", 0, True, None, dict(edit=True, cancel=True, resend=False, confirm=False,
                                          upload=True, remove_file=True)),
            ("Approved", 0, True, None, dict(edit=False, cancel=False, resend=False, confirm=True,
                                             upload=False, remove_file=False)),
            ("Confirmed", 1, True, None, dict(edit=False, cancel=False, resend=False, confirm=False,
                                              upload=False, remove_file=False)),
        ]
        for i, (state, docstatus, is_hr, caller, expected) in enumerate(cases):
            _seed_doc(store, name=f"OR-C{i}", state=state, docstatus=docstatus)
            out = self._run(ot, store, monkeypatch, is_hr=is_hr, caller=caller, name=f"OR-C{i}")
            can = out["can"]
            for key, val in expected.items():
                assert can[key] is val, f"case {i} key {key}: {can}"
            assert can["print"] is True

    def test_od2_non_owner_permission_error(self, ot, store, monkeypatch):
        _seed_doc(store, employee="EMP-2")
        monkeypatch.setattr(ot, "_is_manager", lambda: False)
        monkeypatch.setattr(ot.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        with pytest.raises(ot.frappe.PermissionError):
            ot.get_overtime_request(name="OR-1")

    def test_od3_missing(self, ot, store, monkeypatch):
        monkeypatch.setattr(ot, "_is_manager", lambda: True)
        with pytest.raises(Exception):
            ot.get_overtime_request(name=None)
        with pytest.raises(Exception):
            ot.get_overtime_request(name="OR-404")

    def test_od22_links_none_when_absent(self, ot, store, monkeypatch):
        row = _seed_doc(store)
        row["shift_instance"] = None
        row["work_session"] = None
        store.docs["OR-1"] = row
        out = self._run(ot, store, monkeypatch, is_hr=True, caller=None)
        assert out["links"] == {"shift_instance": None, "work_session": None}
        assert out["activity"] == []


# --------------------------------------------------------------------------- #
# OD4–OD10, OD21 — update_overtime_request
# --------------------------------------------------------------------------- #
class TestUpdateOvertimeRequest:
    def _patch_actor(self, ot, monkeypatch, *, is_hr=False, caller="EMP-1"):
        monkeypatch.setattr(ot, "_is_manager", lambda: is_hr)
        monkeypatch.setattr(ot.emp_utils, "get_employee_for_user", lambda: caller)
        monkeypatch.setattr(ot, "send_for_approval", lambda doc: None)

    def test_od4_happy_path(self, ot, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(ot, monkeypatch)
        out = ot.update_overtime_request(
            name="OR-1",
            work_date="2026-09-02",
            overtime_type="Pre-shift",
            from_datetime="2026-09-02 06:00:00",
            to_datetime="2026-09-02 08:00:00",
            requested_hours=2.5,
            reason="sửa giờ",
            shift_instance="SI-2",
            work_session="WS-2",
        )
        assert out["name"] == "OR-1"
        saved = store.docs["OR-1"]
        assert saved["work_date"] == "2026-09-02"
        assert saved["overtime_type"] == "Pre-shift"
        assert saved["requested_hours"] == 2.5
        assert saved["reason"] == "sửa giờ"
        assert store.audit_calls and store.audit_calls[0][0] == "OT Update Draft"
        assert store.publish_calls and store.publish_calls[0][0] == "overtime_updated"

    def test_od5_non_draft_state(self, ot, store, monkeypatch):
        _seed_doc(store, state="Pending Manager")
        self._patch_actor(ot, monkeypatch)
        with pytest.raises(Exception) as e:
            ot.update_overtime_request(name="OR-1", reason="x")
        assert "không thể sửa" in str(e.value)

    def test_od6_non_owner(self, ot, store, monkeypatch):
        _seed_doc(store, employee="EMP-2")
        self._patch_actor(ot, monkeypatch, caller="EMP-1")
        with pytest.raises(ot.frappe.PermissionError):
            ot.update_overtime_request(name="OR-1", reason="x")

    def test_od7_race_detected_on_reload(self, ot, store, monkeypatch):
        _seed_doc(store)
        # DB truth flips to Pending Manager while the owner was editing —
        # reload() inside the endpoint must surface it and abort without save.
        row = store.docs["OR-1"]

        class _Racer(FakeDoc):
            def reload(self):
                self.workflow_state = "Pending Manager"

        monkeypatch.setattr(ot, "_is_manager", lambda: False)
        monkeypatch.setattr(ot.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        monkeypatch.setattr(ot.frappe, "get_doc", lambda dt, name=None: _Racer(store, dict(row)))
        monkeypatch.setattr(ot, "send_for_approval", lambda doc: None)
        with pytest.raises(Exception) as e:
            ot.update_overtime_request(name="OR-1", reason="muộn")
        assert "người khác cập nhật" in str(e.value)
        assert not any(c[0] == "OR-1" for c in store.audit_calls if c and c[0] == "OT Update Draft")

    def test_od8_unknown_fields_ignored(self, ot, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(ot, monkeypatch)
        ot.update_overtime_request(
            name="OR-1", reason="ok", name_alt=None, employee="EMP-9", company="HACK",
            docstatus=1, workflow_state="Approved",
        )
        saved = store.docs["OR-1"]
        assert saved["employee"] == "EMP-1"
        assert saved["company"] == "GEGE"
        assert saved["workflow_state"] == "Draft"
        assert saved["docstatus"] == 0

    def test_od9_empty_reason(self, ot, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(ot, monkeypatch)
        with pytest.raises(Exception) as e:
            ot.update_overtime_request(name="OR-1", reason="  ")
        assert "không được để trống" in str(e.value)

    def test_od10_submitted_doc(self, ot, store, monkeypatch):
        _seed_doc(store, docstatus=1, state="Approved")
        self._patch_actor(ot, monkeypatch)
        with pytest.raises(Exception) as e:
            ot.update_overtime_request(name="OR-1", reason="x")
        assert "chưa duyệt" in str(e.value)

    def test_od21_pushes_stuck_draft(self, ot, store, monkeypatch):
        """A Draft that never reached Pending Manager is pushed again after edit."""
        _seed_doc(store)
        sent = []
        monkeypatch.setattr(ot, "_is_manager", lambda: False)
        monkeypatch.setattr(ot.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        monkeypatch.setattr(ot, "send_for_approval", lambda doc: sent.append(doc.name))
        ot.update_overtime_request(name="OR-1", reason="lần 2")
        assert sent == ["OR-1"]


# --------------------------------------------------------------------------- #
# OD11–OD14 — all_overtime_requests
# --------------------------------------------------------------------------- #
class TestAllOvertimeRequests:
    def _seed(self, store):
        _seed_doc(store, name="OR-A", employee="EMP-1", state="Pending Manager")
        _seed_doc(
            store, name="OR-B", employee="EMP-2", state="Approved",
            employee_name="Nhân viên Hai", approved_hours=3.0,
        )
        store.queries["Employee"] = [
            {"name": "EMP-1", "employee_name": "Nhân viên Một", "department": "IT"},
            {"name": "EMP-2", "employee_name": "Nhân viên Hai", "department": "Sales"},
        ]
        store.queries["VN Employee Shift Instance"] = [
            {"name": "SI-1", "shift_type": "Ca A"},
            {"name": "WS-SI", "shift_type": "Ca B"},
        ]

    def test_od11_hr_scoped_department_filter_and_enrich(self, ot, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(ot, "_is_manager", lambda: True)
        rows = ot.all_overtime_requests(department="IT")
        assert [r["name"] for r in rows] == ["OR-A"]
        assert rows[0]["department"] == "IT"
        assert rows[0]["shift_type"] == "Ca A"
        assert rows[0]["employee_name"] == "Nhân viên Một"

    def test_od12_non_hr_forbidden(self, ot, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(ot, "_is_manager", lambda: False)
        with pytest.raises(ot.frappe.PermissionError):
            ot.all_overtime_requests()

    def test_od13_pagination_envelope_and_bare_list(self, ot, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(ot, "_is_manager", lambda: True)
        env = ot.all_overtime_requests(page=1, page_size=1)
        assert set(env.keys()) == {"data", "total", "summary"}
        assert env["total"] == 2 and len(env["data"]) == 1
        assert env["summary"]["pending"] == 1
        assert env["summary"]["approved_hours"] == 3.0
        assert env["summary"]["requested_hours"] == 4.0
        bare = ot.all_overtime_requests()
        assert isinstance(bare, list) and len(bare) == 2

    def test_od14_search_matches_name_and_department(self, ot, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(ot, "_is_manager", lambda: True)
        assert [r["name"] for r in ot.all_overtime_requests(search="sales")] == ["OR-B"]
        assert [r["name"] for r in ot.all_overtime_requests(search="hai")] == ["OR-B"]
        assert len(ot.all_overtime_requests(search="xyz")) == 0

    def test_empty_department_short_circuits(self, ot, store, monkeypatch):
        store.queries["Employee"] = []
        monkeypatch.setattr(ot, "_is_manager", lambda: True)
        env = ot.all_overtime_requests(department="Nope", page_size=10)
        assert env["total"] == 0 and env["summary"]["pending"] == 0


# --------------------------------------------------------------------------- #
# OD15, OD18 — realtime wire + reject comment contract
# --------------------------------------------------------------------------- #
class TestRealtimeAndComment:
    def test_od15_publish_helper(self, ot, store):
        ot._publish_overtime(types.SimpleNamespace(name="OR-1"))
        ot._publish_overtime(None)
        assert store.publish_calls[0] == (
            "overtime_updated", {"doctype": DOCTYPE, "name": "OR-1"}
        )
        assert store.publish_calls[1][1]["name"] is None

    def test_od15_publish_never_raises(self, ot, store, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("socket down")

        monkeypatch.setattr(ot.frappe, "publish_realtime", _boom)
        ot._publish_overtime()  # must not raise

    def test_od15_wire_fires_on_pending_move(self, approval, monkeypatch, store):
        calls = []
        fake_ot = types.ModuleType("gege_hr.gege_hr.api.overtime")
        fake_ot._publish_overtime = lambda doc=None: calls.append(getattr(doc, "name", None))
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.overtime", fake_ot)

        class _D:
            doctype = DOCTYPE
            name = "OR-9"
            employee = "EMP-1"
            work_date = "2026-09-01"
            shift_instance = "SI-1"

            def get(self, k, d=None):
                return getattr(self, k, d)

        approval._after_ot_state_change(_D(), from_state="Pending Manager", to_state="Pending HR")
        assert calls == ["OR-9"]  # pending→pending: no recalc, but publish fired

    def test_od15_wire_skips_non_ot(self, approval, monkeypatch):
        calls = []
        fake_ot = types.ModuleType("gege_hr.gege_hr.api.overtime")
        fake_ot._publish_overtime = lambda doc=None: calls.append(doc)
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.overtime", fake_ot)

        class _D:
            doctype = "Leave Application"
            name = "LA-1"

            def get(self, k, d=None):
                return getattr(self, k, d)

        approval._after_ot_state_change(_D(), from_state="Open", to_state="Approved")
        assert calls == []

    def test_od18_reject_request_accepts_comment(self, approval):
        params = inspect.signature(approval.reject_request).parameters
        assert "comment" in params

    def test_od18_write_log_stores_comment(self, approval, store, monkeypatch):
        created = {}

        class _Log:
            def update(self, d):
                created.update(d)

            def insert(self, *a, **kw):
                created["inserted"] = True

        monkeypatch.setattr(approval.frappe, "new_doc", lambda dt: _Log())
        approval._write_log(
            transaction_type="Overtime Request", name="OR-1", action="Reject",
            from_state="Pending HR", to_state="Rejected",
            comment="ngoài giờ cho phép", actor="mgr@x",
        )
        assert created["inserted"] is True
        assert created["reference_doctype"] == DOCTYPE
        assert created["comment"] == "ngoài giờ cho phép"


# --------------------------------------------------------------------------- #
# OD16–OD17, OD19–OD20 — seed fix, submit publish, whitelist contract
# --------------------------------------------------------------------------- #
class TestSeedFixAndContracts:
    def test_od16_seed_contains_employee_reject_from_pending_hr(self, monkeypatch, store):
        _install_stub(monkeypatch, store)
        from gege_hr.gege_hr import setup_workflows as sw

        importlib.reload(sw)
        rows = sw._common_transitions()
        assert ("Pending HR", "Reject", "Rejected", "Employee") in rows
        # Shared blueprint → the fix lands on Correction + Salary Advance too.
        for spec in sw._WORKFLOWS:
            keys = {
                (t["state"], t["action"], t["next_state"], t["allowed"])
                for t in spec["transitions"]
            }
            assert ("Pending HR", "Reject", "Rejected", "Employee") in keys, spec["name"]

    def test_od17_employee_can_cancel_pending_hr_with_fixed_seed(self, monkeypatch, store):
        """Mini validate_workflow over the seeded blueprint: the owner Employee
        must hold a Pending HR → Rejected transition (they did not before B-1)."""
        _install_stub(monkeypatch, store)
        from gege_hr.gege_hr import setup_workflows as sw

        importlib.reload(sw)
        seed = sw._transition_rows(sw._common_transitions())

        def can_move(from_state, to_state, role):
            return any(
                t["state"] == from_state and t["next_state"] == to_state and t["allowed"] == role
                for t in seed
            )

        assert can_move("Pending HR", "Rejected", "Employee")
        # The cancel endpoint path itself: state write + save + WS sync.
        _seed_doc(store, state="Pending HR")
        from gege_hr.gege_hr.api import overtime as ot_mod

        importlib.reload(ot_mod)  # bind to THIS test's stub/store
        monkeypatch.setattr(ot_mod, "_is_manager", lambda: False)
        monkeypatch.setattr(ot_mod.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        out = ot_mod.cancel_overtime_request(name="OR-1")
        assert out["status"] == "Rejected"

    def test_od17_ensure_transition_rows_upsert(self, monkeypatch, store):
        _install_stub(monkeypatch, store)
        from gege_hr.gege_hr import setup_workflows as sw

        importlib.reload(sw)
        existing = [
            {"state": "Pending HR", "action": "Reject", "next_state": "Rejected",
             "allowed": "HR User", "allow_self_approval": 1},
        ]
        appended = []
        saved = []

        class _WF:
            doctype = "Workflow"
            name = "VN Overtime Request Workflow"
            flags = types.SimpleNamespace(ignore_permissions=False)
            transitions = [dict(t) for t in existing]

            def append(self, key, row):
                appended.append((key, dict(row)))

            def save(self, *a, **kw):
                saved.append(kw.get("ignore_permissions"))

        wf_holder = {}

        def _get_doc(dt, name=None):
            # Same instance across calls — reload()/second upsert must see the
            # transitions the first run appended.
            wf_holder.setdefault("doc", _WF())
            return wf_holder["doc"]

        monkeypatch.setattr(sw.frappe, "get_doc", _get_doc)
        wanted = sw._transition_rows(sw._common_transitions())
        added = sw._ensure_transition_rows("VN Overtime Request Workflow", wanted)
        assert added == len(wanted) - len(existing)
        # Only the missing Employee row for this key family was appended.
        keys = [(r["state"], r["action"], r["next_state"], r["allowed"]) for _, r in appended]
        assert ("Pending HR", "Reject", "Rejected", "Employee") in keys
        assert saved == [True]
        # Idempotent second run appends nothing.
        wf_holder["doc"].transitions = [dict(t) for t in wanted]
        appended.clear()
        assert sw._ensure_transition_rows("VN Overtime Request Workflow", wanted) == 0
        assert appended == []

    def test_od19_submit_publishes_realtime(self, ot, store, monkeypatch):
        monkeypatch.setattr(ot, "send_for_approval", lambda doc: None)
        out = ot.submit_overtime_request(
            employee="EMP-1", work_date="2026-09-03", overtime_type="Post-shift",
            from_datetime="2026-09-03 17:00:00", to_datetime="2026-09-03 19:00:00",
            requested_hours=2, reason="gấp",
        )
        assert out["name"] == "OR-NEW"
        assert any(e == "overtime_updated" for e, _ in store.publish_calls)
        assert store.audit_calls and store.audit_calls[0][0] == "OT Submit"

    def test_od20_whitelist_contract(self, ot):
        recorded = ot.frappe.recorded_whitelist
        for fn in (
            "my_overtime_requests",
            "submit_overtime_request",
            "cancel_overtime_request",
            "get_overtime_request",
            "update_overtime_request",
            "all_overtime_requests",
        ):
            assert fn in recorded, fn

    def test_od23_normalize_dt_strips_offset_keeps_wall_clock(self, ot):
        # EC-3 SPA payload "2026-10-06 17:00+07:00" → naive wall clock, seconds
        # padded (MySQL 1292 fix; storage convention = portal wall clock).
        assert ot._normalize_input_dt("2026-10-06 17:00+07:00") == "2026-10-06 17:00:00"
        assert ot._normalize_input_dt("2026-10-06T17:00+00:00") == "2026-10-06 17:00:00"
        assert ot._normalize_input_dt("2026-10-06 9:05+07:00") == "2026-10-06 09:05:00"

    def test_od24_normalize_dt_passes_naive_and_garbage_through(self, ot):
        assert ot._normalize_input_dt("2026-10-06 17:00:00") == "2026-10-06 17:00:00"
        assert ot._normalize_input_dt("") == ""
        assert ot._normalize_input_dt(None) is None
        assert ot._normalize_input_dt("garbage") == "garbage"
