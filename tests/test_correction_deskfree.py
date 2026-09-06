"""Bench-free tests for the /hr/correction desk-free endpoints
(plans/correction-deskfree-complete.md — CD1..CD22).

Covers the NEW surface of ``api/correction.py`` (``get_correction_request`` /
``update_correction_request`` / ``all_correction_requests`` /
``_publish_correction``), the realtime wires in ``attendance`` (submit/cancel)
and ``approval._after_correction_state_change`` (fires on every CR state
change), the workflow-seed regression (Employee may Reject from Pending HR —
shared blueprint with the OT B-1 fix) and the CR-specific link metas
(Work Session "before" punches, Checkout-Miss ticket, generated results).

Stub-frappe pattern of ``tests/test_overtime_deskfree.py`` (extended with a
``utils.__getattr__`` fallback so ``api.attendance`` also reloads cleanly —
proved by ``tests/test_monthly_self.py``): a stub ``frappe`` is injected into
``sys.modules``; heavy collaborators (audit log, send_for_approval,
publish_realtime, emp_utils) are spied, not executed.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

DOCTYPE = "VN Attendance Correction Request"


# --------------------------------------------------------------------------- #
# Stub infrastructure
# --------------------------------------------------------------------------- #
class FakeDoc:
    """Minimal Document double: get/setattr, reload (from store truth), save."""

    def __init__(self, store, data):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_saved_kwargs", [])
        for k, v in data.items():
            setattr(self, k, v)
        if not getattr(self, "name", None):
            self.name = "CR-NEW"
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def update(self, d):
        for k, v in (d or {}).items():
            setattr(self, k, v)

    def set(self, key, value):
        setattr(self, key, value)

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

    def cancel(self, *a, **kw):
        self.docstatus = 2
        return self.save(*a, **kw)

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
    """Install a stub ``frappe`` sufficient for api.correction / api.approval /
    api.attendance (the utils.__getattr__ fallback keeps the latter reloadable)."""
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
    mod.only_for = lambda roles: None
    mod.session = types.SimpleNamespace(user="emp@x")
    # Default identity = plain Employee (tests that need HR patch cr._is_hr_manager).
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

            def set(self, k, v):
                holder[k] = v

            def insert(self, *a, **kw):
                holder.setdefault("name", "CR-NEW")
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
    utils.flt = lambda v=None, precision=None, *a, **kw: v
    utils.add_days = lambda v=None, days=None, *a, **kw: v

    def _utils_getattr(name):
        def _generic(*_a, **_k):
            return None

        return _generic

    utils.__getattr__ = _utils_getattr  # PEP 562 — attendance's broad imports
    mod.utils = utils

    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    return mod


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def cr(monkeypatch, store):
    """Fresh api.correction bound to the stub frappe with spied collaborators."""
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import correction as cr_mod
    from gege_hr.gege_hr.utils import employee as emp_utils

    importlib.reload(cr_mod)
    # Deterministic identity / role helpers (module-level, restored by monkeypatch).
    monkeypatch.setattr(emp_utils, "get_employee_for_user", lambda: "EMP-1")
    monkeypatch.setattr(emp_utils, "emp_name", lambda x: x)
    monkeypatch.setattr(cr_mod.audit_api, "log", lambda kind, **kw: store.audit_calls.append((kind, kw)))
    return cr_mod


@pytest.fixture
def approval(monkeypatch, store):
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import approval as approval_api

    importlib.reload(approval_api)
    return approval_api


@pytest.fixture
def att(monkeypatch, store):
    """Fresh api.attendance (legacy CR endpoints + the new publish wire)."""
    _install_stub(monkeypatch, store)
    from gege_hr.gege_hr.api import attendance as att_mod
    from gege_hr.gege_hr.api import correction as cr_mod
    from gege_hr.gege_hr.utils import employee as emp_utils

    # correction FIRST: attendance lazy-imports it at call time; reloading it
    # here binds its module-level frappe to THIS stub (no stale store).
    importlib.reload(cr_mod)
    importlib.reload(att_mod)
    monkeypatch.setattr(emp_utils, "get_employee_for_user", lambda: "EMP-1")
    monkeypatch.setattr(emp_utils, "emp_name", lambda x: x)
    monkeypatch.setattr(emp_utils, "get_user_roles", lambda: [])
    monkeypatch.setattr(att_mod, "send_for_approval", lambda doc: store.send_calls.append(doc))
    monkeypatch.setattr(att_mod.audit_api, "log", lambda kind, **kw: store.audit_calls.append((kind, kw)))
    return att_mod


def _seed_doc(store, name="CR-1", employee="EMP-1", state="Draft", docstatus=0, **extra):
    row = {
        "doctype": DOCTYPE,
        "name": name,
        "employee": employee,
        "employee_name": "Nhân viên Một",
        "work_date": "2026-09-01",
        "shift_instance": "SI-1",
        "work_session": "WS-1",
        "company": "GEGE",
        "correction_type": "Wrong Time",
        "current_checkin_time": "2026-09-01 08:30:00",
        "current_checkout_time": "2026-09-01 17:30:00",
        "requested_checkin_time": "2026-09-01 08:02:00",
        "requested_checkout_time": None,
        "reason": "máy lỗi quét muộn",
        "attachment": None,
        "workflow_state": state,
        "approver": None,
        "approved_at": None,
        "generated_checkin": None,
        "generated_attendance": None,
        "vn_checkout_miss": None,
        "docstatus": docstatus,
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
# CD1–CD4, CD22 — get_correction_request
# --------------------------------------------------------------------------- #
class TestGetCorrectionRequest:
    def _run(self, cr, store, monkeypatch, *, is_hr, caller, name="CR-1"):
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: is_hr)
        monkeypatch.setattr(cr.emp_utils, "get_employee_for_user", lambda: caller)
        return cr.get_correction_request(name=name)

    def test_cd1_shape_and_links(self, cr, store, monkeypatch):
        _seed_doc(store)
        store.values["VN Employee Shift Instance"] = {"SI-1": {
            "name": "SI-1", "shift_type": "Ca A", "planned_start": "2026-09-01 08:00:00",
            "planned_end": "2026-09-01 17:00:00",
        }}
        store.values["VN Attendance Work Session"] = {"WS-1": {
            "name": "WS-1", "actual_checkin": "2026-09-01 08:30:00",
            "actual_checkout": "2026-09-01 17:30:00", "total_actual_hours": 9.0,
            "calculation_status": "Computed",
        }}
        store.queries["File"] = [
            {"name": "F1", "file_name": "may.jpg", "file_url": "/private/files/may.jpg",
             "is_private": 1, "file_size": 10,
             "attached_to_doctype": DOCTYPE, "attached_to_name": "CR-1"},
        ]
        store.queries["VN Approval Log"] = [
            {"name": "L1", "action": "Reject", "from_state": "Pending Manager",
             "to_state": "Rejected", "actor": "mgr@x", "comment": "thiếu bằng chứng",
             "creation": "2026-09-02",
             "reference_doctype": DOCTYPE, "reference_name": "CR-1"},
        ]
        out = self._run(cr, store, monkeypatch, is_hr=False, caller="EMP-1")
        assert out["doc"]["name"] == "CR-1"
        assert out["doc"]["owner"] == "emp@x"
        assert out["doc"]["correction_type"] == "Wrong Time"
        assert out["employee_name"] == "Nhân viên Một"
        assert out["links"]["shift_instance"]["shift_type"] == "Ca A"
        assert out["links"]["work_session"]["actual_checkin"] == "2026-09-01 08:30:00"
        assert out["attachments"][0]["file_name"] == "may.jpg"
        assert out["activity"][0]["comment"] == "thiếu bằng chứng"
        assert out["can"]["print"] is True

    def test_cd1_can_matrix_branches(self, cr, store, monkeypatch):
        cases = [
            # (state, docstatus, is_hr, caller) -> expected can dict (no confirm in CR)
            ("Draft", 0, False, "EMP-1", dict(edit=True, cancel=True, resend=False,
                                              upload=True, remove_file=True)),
            ("Pending Manager", 0, False, "EMP-1", dict(edit=False, cancel=True, resend=False,
                                                        upload=False, remove_file=False)),
            ("Pending HR", 0, False, "EMP-1", dict(edit=False, cancel=True, resend=False,
                                                   upload=False, remove_file=False)),
            ("Rejected", 0, False, "EMP-1", dict(edit=False, cancel=False, resend=True,
                                                 upload=False, remove_file=False)),
            ("Draft", 0, True, None, dict(edit=True, cancel=True, resend=False,
                                          upload=True, remove_file=True)),
            ("Approved", 0, True, None, dict(edit=False, cancel=False, resend=False,
                                             upload=False, remove_file=False)),
            ("Approved", 1, True, None, dict(edit=False, cancel=False, resend=False,
                                             upload=False, remove_file=False)),
        ]
        for i, (state, docstatus, is_hr, caller, expected) in enumerate(cases):
            _seed_doc(store, name=f"CR-C{i}", state=state, docstatus=docstatus)
            out = self._run(cr, store, monkeypatch, is_hr=is_hr, caller=caller, name=f"CR-C{i}")
            can = out["can"]
            assert set(can.keys()) == {"edit", "cancel", "resend", "upload", "remove_file", "print"}
            for key, val in expected.items():
                assert can[key] is val, f"case {i} key {key}: {can}"
            assert can["print"] is True

    def test_cd2_non_owner_permission_error(self, cr, store, monkeypatch):
        _seed_doc(store, employee="EMP-2")
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: False)
        monkeypatch.setattr(cr.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        with pytest.raises(cr.frappe.PermissionError):
            cr.get_correction_request(name="CR-1")

    def test_cd3_missing(self, cr, store, monkeypatch):
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        with pytest.raises(Exception):
            cr.get_correction_request(name=None)
        with pytest.raises(Exception):
            cr.get_correction_request(name="CR-404")

    def test_cd4_activity_reads_reject_comment(self, cr, store, monkeypatch):
        """Gap-4 pin: the comment the manager typed when rejecting surfaces in
        the employee's drawer activity (VN Approval Log projection)."""
        _seed_doc(store, state="Rejected")
        store.queries["VN Approval Log"] = [
            {"name": "L1", "action": "Reject", "from_state": "Pending HR",
             "to_state": "Rejected", "actor": "hr@x", "comment": "ảnh không rõ",
             "creation": "2026-09-02",
             "reference_doctype": DOCTYPE, "reference_name": "CR-1"},
        ]
        out = self._run(cr, store, monkeypatch, is_hr=False, caller="EMP-1")
        rows = out["activity"]
        assert rows and rows[0]["action"] == "Reject" and rows[0]["comment"] == "ảnh không rõ"

    def test_cd22_approved_with_ticket_and_results(self, cr, store, monkeypatch):
        _seed_doc(
            store, state="Approved", vn_checkout_miss="CM-1",
            generated_checkin="EC-1", generated_attendance="ATT-1",
            approver="hr@x", approved_at="2026-09-02 09:00:00",
        )
        store.values["VN Checkout Miss"] = {"CM-1": {
            "name": "CM-1", "status": "Waived", "penalty_amount": 0,
            "grace_deadline": "2026-09-02 12:00:00", "work_date": "2026-09-01",
        }}
        store.values["Employee Checkin"] = {"EC-1": {
            "name": "EC-1", "time": "2026-09-01 22:00:00", "log_type": "OUT",
        }}
        store.values["Attendance"] = {"ATT-1": {
            "name": "ATT-1", "status": "Present", "attendance_date": "2026-09-01",
        }}
        out = self._run(cr, store, monkeypatch, is_hr=True, caller=None)
        assert out["links"]["vn_checkout_miss"]["status"] == "Waived"
        assert out["links"]["generated_checkin"]["log_type"] == "OUT"
        assert out["links"]["generated_attendance"]["status"] == "Present"
        assert out["doc"]["vn_checkout_miss"] == "CM-1"

    def test_cd1_links_none_when_absent(self, cr, store, monkeypatch):
        row = _seed_doc(store)
        row["shift_instance"] = None
        row["work_session"] = None
        store.docs["CR-1"] = row
        out = self._run(cr, store, monkeypatch, is_hr=True, caller=None)
        for key in ("shift_instance", "work_session", "vn_checkout_miss",
                    "generated_checkin", "generated_attendance"):
            assert out["links"][key] is None
        assert out["activity"] == []


# --------------------------------------------------------------------------- #
# CD5–CD12 — update_correction_request
# --------------------------------------------------------------------------- #
class TestUpdateCorrectionRequest:
    def _patch_actor(self, cr, monkeypatch, *, is_hr=False, caller="EMP-1"):
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: is_hr)
        monkeypatch.setattr(cr.emp_utils, "get_employee_for_user", lambda: caller)
        monkeypatch.setattr(cr, "send_for_approval", lambda doc: None)

    def test_cd5_happy_path(self, cr, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(cr, monkeypatch)
        out = cr.update_correction_request(
            name="CR-1",
            work_date="2026-09-02",
            correction_type="Missing IN",
            requested_checkin_time="2026-09-02 07:58:00",
            requested_checkout_time="2026-09-02 17:35:00",
            reason="thiếu giờ vào do máy hỏng",
            shift_instance="SI-2",
            work_session="WS-2",
        )
        assert out["name"] == "CR-1"
        saved = store.docs["CR-1"]
        assert saved["work_date"] == "2026-09-02"
        assert saved["correction_type"] == "Missing IN"
        assert saved["requested_checkin_time"] == "2026-09-02 07:58:00"
        assert saved["reason"] == "thiếu giờ vào do máy hỏng"
        assert store.audit_calls and store.audit_calls[0][0] == "Correction Update Draft"
        assert store.publish_calls and store.publish_calls[0][0] == "correction_updated"

    def test_cd6_non_draft_state(self, cr, store, monkeypatch):
        _seed_doc(store, state="Pending Manager")
        self._patch_actor(cr, monkeypatch)
        with pytest.raises(Exception) as e:
            cr.update_correction_request(name="CR-1", reason="x")
        assert "không thể sửa" in str(e.value)

    def test_cd7_non_owner(self, cr, store, monkeypatch):
        _seed_doc(store, employee="EMP-2")
        self._patch_actor(cr, monkeypatch, caller="EMP-1")
        with pytest.raises(cr.frappe.PermissionError):
            cr.update_correction_request(name="CR-1", reason="x")

    def test_cd8_race_detected_on_reload(self, cr, store, monkeypatch):
        _seed_doc(store)
        # DB truth flips to Pending Manager while the owner was editing —
        # reload() inside the endpoint must surface it and abort without save.
        row = store.docs["CR-1"]

        class _Racer(FakeDoc):
            def reload(self):
                self.workflow_state = "Pending Manager"

        monkeypatch.setattr(cr, "_is_hr_manager", lambda: False)
        monkeypatch.setattr(cr.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        monkeypatch.setattr(cr.frappe, "get_doc", lambda dt, name=None: _Racer(store, dict(row)))
        monkeypatch.setattr(cr, "send_for_approval", lambda doc: None)
        with pytest.raises(Exception) as e:
            cr.update_correction_request(name="CR-1", reason="muộn")
        assert "người khác cập nhật" in str(e.value)
        # No audit / publish for the aborted edit.
        assert not any(k == "Correction Update Draft" for k, _ in store.audit_calls)
        assert not any(evt == "correction_updated" for evt, _ in store.publish_calls)

    def test_cd9_unknown_fields_ignored(self, cr, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(cr, monkeypatch)
        cr.update_correction_request(
            name="CR-1", reason="đủ lý do ok", employee="EMP-9", company="HACK",
            docstatus=1, workflow_state="Approved",
        )
        saved = store.docs["CR-1"]
        assert saved["employee"] == "EMP-1"
        assert saved["company"] == "GEGE"
        assert saved["workflow_state"] == "Draft"
        assert saved["docstatus"] == 0

    def test_cd10_empty_reason(self, cr, store, monkeypatch):
        _seed_doc(store)
        self._patch_actor(cr, monkeypatch)
        with pytest.raises(Exception) as e:
            cr.update_correction_request(name="CR-1", reason="  ")
        assert "không được để trống" in str(e.value)

    def test_cd11_datetime_offset_normalized(self, cr, store, monkeypatch):
        """V4: an SPA payload like '08:02+07:00' must land as a naive
        wall-clock string with seconds padded (MySQL 1292 guard)."""
        _seed_doc(store)
        self._patch_actor(cr, monkeypatch)
        cr.update_correction_request(
            name="CR-1",
            requested_checkin_time="2026-09-01 08:02+07:00",
            reason="đủ lý do ok",
        )
        saved = store.docs["CR-1"]
        assert saved["requested_checkin_time"] == "2026-09-01 08:02:00"

    def test_cd12_pushes_stuck_draft_and_tolerates_failure(self, cr, store, monkeypatch):
        """A Draft that never reached Pending Manager is pushed again after the
        edit. The real send_for_approval never raises (it swallows internally
        and keeps the doc Draft — F17) — the mock mirrors that contract."""
        _seed_doc(store)
        sent = []

        def _swallowing_send(doc):
            sent.append(doc.name)
            doc.workflow_state = "Draft"  # internal failure path: stays Draft
            return doc.workflow_state

        monkeypatch.setattr(cr, "_is_hr_manager", lambda: False)
        monkeypatch.setattr(cr.emp_utils, "get_employee_for_user", lambda: "EMP-1")
        monkeypatch.setattr(cr, "send_for_approval", _swallowing_send)
        out = cr.update_correction_request(name="CR-1", reason="lần hai")
        assert sent == ["CR-1"]
        assert out["name"] == "CR-1"
        assert out["status"] == "Draft"


# --------------------------------------------------------------------------- #
# CD13–CD16 — all_correction_requests
# --------------------------------------------------------------------------- #
class TestAllCorrectionRequests:
    def _seed(self, store):
        _seed_doc(store, name="CR-A", employee="EMP-1", state="Pending Manager")
        _seed_doc(
            store, name="CR-B", employee="EMP-2", state="Approved",
            employee_name="Nhân viên Hai",
        )
        _seed_doc(
            store, name="CR-C", employee="EMP-2", state="Rejected",
            employee_name="Nhân viên Hai",
        )
        store.queries["Employee"] = [
            {"name": "EMP-1", "employee_name": "Nhân viên Một", "department": "IT"},
            {"name": "EMP-2", "employee_name": "Nhân viên Hai", "department": "Sales"},
        ]

    def test_cd13_hr_scoped_department_filter_and_enrich(self, cr, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        rows = cr.all_correction_requests(department="IT")
        assert [r["name"] for r in rows] == ["CR-A"]
        assert rows[0]["department"] == "IT"
        assert rows[0]["employee_name"] == "Nhân viên Một"

    def test_cd14_non_hr_forbidden(self, cr, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: False)
        with pytest.raises(cr.frappe.PermissionError):
            cr.all_correction_requests()

    def test_cd15_pagination_envelope_and_bare_list(self, cr, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        env = cr.all_correction_requests(page=1, page_size=2)
        assert set(env.keys()) == {"data", "total", "summary"}
        assert env["total"] == 3 and len(env["data"]) == 2
        assert env["summary"] == {"total": 3, "pending": 1, "approved": 1, "rejected": 1}
        bare = cr.all_correction_requests()
        assert isinstance(bare, list) and len(bare) == 3

    def test_cd16_search_matches_employee_and_department(self, cr, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        assert [r["name"] for r in cr.all_correction_requests(search="sales")] == ["CR-B", "CR-C"]
        assert [r["name"] for r in cr.all_correction_requests(search="hai")] == ["CR-B", "CR-C"]
        assert len(cr.all_correction_requests(search="xyz")) == 0

    def test_empty_department_short_circuits(self, cr, store, monkeypatch):
        store.queries["Employee"] = []
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        env = cr.all_correction_requests(department="Nope", page_size=10)
        assert env["total"] == 0 and env["summary"]["pending"] == 0

    def test_status_and_correction_type_filters(self, cr, store, monkeypatch):
        self._seed(store)
        monkeypatch.setattr(cr, "_is_hr_manager", lambda: True)
        rows = cr.all_correction_requests(status="Approved", correction_type="Wrong Time")
        assert [r["name"] for r in rows] == ["CR-B"]


# --------------------------------------------------------------------------- #
# CD17 — realtime wire (helper + approval hook + attendance submit/cancel)
# --------------------------------------------------------------------------- #
class TestRealtimeWire:
    def test_cd17_publish_helper(self, cr, store):
        cr._publish_correction(types.SimpleNamespace(name="CR-1"))
        cr._publish_correction(None)
        assert store.publish_calls[0] == (
            "correction_updated", {"doctype": DOCTYPE, "name": "CR-1"}
        )
        assert store.publish_calls[1][1]["name"] is None

    def test_cd17_publish_never_raises(self, cr, store, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("socket down")

        monkeypatch.setattr(cr.frappe, "publish_realtime", _boom)
        cr._publish_correction()  # must not raise

    def test_cd17_approval_hook_fires_on_any_state_change(self, approval, monkeypatch, store):
        """approve_request AND reject_request both funnel through
        _after_correction_state_change — publish fires for every CR move,
        not just Approved (pending→pending included)."""
        calls = []
        fake_cr = types.ModuleType("gege_hr.gege_hr.api.correction")
        fake_cr._publish_correction = lambda doc=None: calls.append(getattr(doc, "name", None))
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.correction", fake_cr)

        class _D:
            doctype = DOCTYPE
            name = "CR-9"
            employee = "EMP-1"
            work_date = "2026-09-01"

            def get(self, k, d=None):
                return getattr(self, k, d)

        approval._after_correction_state_change(_D(), from_state="Pending Manager", to_state="Pending HR")
        assert calls == ["CR-9"]

    def test_cd17_approval_hook_skips_non_cr(self, approval, monkeypatch):
        calls = []
        fake_cr = types.ModuleType("gege_hr.gege_hr.api.correction")
        fake_cr._publish_correction = lambda doc=None: calls.append(doc)
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.correction", fake_cr)

        class _D:
            doctype = "VN Overtime Request"
            name = "OR-1"

            def get(self, k, d=None):
                return getattr(self, k, d)

        approval._after_correction_state_change(_D(), from_state="Pending HR", to_state="Approved")
        assert calls == []

    def test_cd17_attendance_submit_publishes(self, att, store):
        out = att.submit_correction_request(
            employee="EMP-1", work_date="2026-09-03", correction_type="Wrong Time",
            requested_checkin_time="2026-09-03 08:02:00", reason="máy lỗi quét muộn",
        )
        assert out["name"] == "CR-NEW"
        assert any(e == "correction_updated" for e, _ in store.publish_calls)

    def test_cd17_attendance_cancel_publishes(self, att, store):
        _seed_doc(store, state="Pending Manager")
        out = att.cancel_correction_request(name="CR-1")
        assert out["status"] == "Rejected"
        assert any(e == "correction_updated" for e, _ in store.publish_calls)


# --------------------------------------------------------------------------- #
# CD18–CD21 — seed regression, cancel Pending HR, import cycle, whitelist
# --------------------------------------------------------------------------- #
class TestSeedRegressionAndContracts:
    def test_cd18_cr_workflow_contains_employee_reject_from_pending_hr(self, monkeypatch, store):
        """V1 pin: the shared B-1 fix lands on the Correction workflow — the
        owner Employee may cancel (Reject) their own request at Pending HR."""
        _install_stub(monkeypatch, store)
        from gege_hr.gege_hr import setup_workflows as sw

        importlib.reload(sw)
        cr_spec = next(
            w for w in sw._WORKFLOWS if w["doctype"] == DOCTYPE
        )
        keys = {
            (t["state"], t["action"], t["next_state"], t["allowed"])
            for t in cr_spec["transitions"]
        }
        assert ("Pending HR", "Reject", "Rejected", "Employee") in keys
        assert ("Pending Manager", "Reject", "Rejected", "Employee") in keys
        assert ("Draft", "Send for Approval", "Pending Manager", "Employee") in keys

    def test_cd19_employee_can_cancel_pending_hr(self, att, store):
        """The cancel endpoint path end-to-end under the stub: owner (plain
        Employee) cancels a Pending HR request → Rejected, no permission error."""
        _seed_doc(store, state="Pending HR")
        out = att.cancel_correction_request(name="CR-1")
        assert out["status"] == "Rejected"
        assert store.docs["CR-1"]["workflow_state"] == "Rejected"

    def test_cd20_import_cycle_attendance_correction(self, monkeypatch, store):
        """CD20: attendance lazy-imports correction (publish wire) while
        correction avoids importing attendance at module level — importing and
        reloading both under one stub must never recurse."""
        _install_stub(monkeypatch, store)
        import gege_hr.gege_hr.api.attendance as a1
        import gege_hr.gege_hr.api.correction as c1

        importlib.reload(c1)
        importlib.reload(a1)
        assert a1.submit_correction_request is not None
        assert c1.get_correction_request is not None

    def test_cd21_whitelist_contract(self, cr):
        recorded = cr.frappe.recorded_whitelist
        for fn in (
            "get_correction_request",
            "update_correction_request",
            "all_correction_requests",
        ):
            assert fn in recorded, fn
