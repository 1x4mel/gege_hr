"""Bench-free unit tests for the desk-free handover API
(plan-handover-deskfree-complete §5.1 — HO1-HO24).

Covers the P0 surface added on top of ``api/handover.py``:

  * ``update_handover``   — edit guards, field whitelist, race-guard reload
  * ``delete_handover``   — scoped bypass + audit + realtime
  * ``get_handover``      — detail shape + server-driven ``can`` matrix
  * ``update_handover_status`` — Completed ⇒ submit (docstatus 1), cancel of a
    submitted doc ⇒ docstatus 2, docstatus-aware ``can_transition``
  * ``_validate_leave_link`` / ``create_handover`` — LA Approved + ownership
  * ``handover_leave_options`` — non-HR pinned to own approved leaves
  * ``leave_handovers`` v2 — G1 or_filters fix, pagination + summary, names

The pure ``suggest_receivers`` ranking is covered by ``test_handover.py`` and
the IDOR pins by ``test_idor.py``. A stub ``frappe`` is injected via
``monkeypatch.setitem(sys.modules, ...)`` — the harness mirrors
``test_idor.py`` / ``test_leave_deskfree.py`` so everything runs bench-free.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from gege_hr.gege_hr.utils import handover as handover_utils


# --------------------------------------------------------------------------- #
# Exceptions carried by the stub frappe
# --------------------------------------------------------------------------- #
class _PermissionDenied(Exception):
    pass


class _ValidationError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Fake doc — models the submittable lifecycle (docstatus 0 → 1 → 2)
# --------------------------------------------------------------------------- #
class _Doc:
    def __init__(self, name="HT-1", **fields):
        self.name = name
        self.status = "Pending"
        self.from_employee = "FROM"
        self.to_employee = "TO"
        self.leave_application = "LA-OK"
        self.handover_date = "2026-06-22"
        self.description = "mô tả"
        self.note = ""
        self.docstatus = 0
        self.completed_at = None
        self.completed_by = None
        for k, v in fields.items():
            setattr(self, k, v)
        self.saved = 0
        self.submitted = 0
        self.cancelled = 0
        self.reloads = 0
        self.reload_hook = None  # callable(doc) applied inside reload()
        self.flags = types.SimpleNamespace()

    def get(self, key, default=None):
        return getattr(self, key, default)

    def insert(self, ignore_permissions=False):
        self.saved += 1
        return self

    def save(self, ignore_permissions=False):
        self.saved += 1
        return self

    def submit(self):
        self.submitted += 1
        self.docstatus = 1
        return self

    def cancel(self):
        self.cancelled += 1
        self.docstatus = 2
        return self

    def reload(self):
        self.reloads += 1
        if self.reload_hook:
            self.reload_hook(self)
        return self

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
# Fake DB — routes get_all by doctype, get_value by (doctype, name) row
# --------------------------------------------------------------------------- #
class _DB:
    def __init__(self):
        self.doc = _Doc()
        self.created: list[dict] = []  # payload dicts passed to get_doc({...})
        self.rows_by_doctype: dict[str, list[dict]] = {
            "VN Leave Handover Task": [],
            "Employee": [],
            "File": [],
            "Leave Application": [],
        }
        # (doctype, name) → row dict served by get_value (single field or list).
        self.row_values: dict[tuple, dict] = {
            ("Leave Application", "LA-OK"): {
                "docstatus": 1,
                "status": "Approved",
                "employee": "FROM",
            },
            ("Leave Application", "LA-DRAFT"): {
                "docstatus": 0,
                "status": "Open",
                "employee": "FROM",
            },
            ("Leave Application", "LA-OTHER"): {
                "docstatus": 1,
                "status": "Approved",
                "employee": "OTHER",
            },
            ("Employee", "FROM"): {"status": "Active", "employee_name": "Người Giao"},
            ("Employee", "TO"): {"status": "Active", "employee_name": "Người Nhận"},
            ("Employee", "TO2"): {"status": "Active", "employee_name": "Người Nhận 2"},
            ("Employee", "GONE"): {"status": "Left", "employee_name": "Đã Nghỉ"},
            # Policy lookups (P1 H9) — matched via dict-filter style get_value.
            ("VN Leave Policy Extension", "POL-AN"): {
                "leave_type": "AN",
                "is_active": 1,
                "require_handover": 1,
            },
            ("VN Leave Policy Extension", "POL-OFF"): {
                "leave_type": "OFF",
                "is_active": 1,
                "require_handover": 0,
            },
        }
        self.list_calls: list[dict] = []
        self.deleted: list[tuple] = []
        self.table_exists_flag = True

    def get_doc(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            payload = dict(args[0])
            self.created.append(payload)
            return _Doc(**payload)
        return self.doc

    def get_all(
        self,
        doctype,
        filters=None,
        fields=None,
        order_by=None,
        limit_page_length=None,
        or_filters=None,
        **_kw,
    ):
        self.list_calls.append({"doctype": doctype, "filters": filters, "or_filters": or_filters})
        return [dict(r) for r in self.rows_by_doctype.get(doctype, [])]

    def get_list(self, *a, **kw):
        return []

    def get_value(self, doctype, key, fields=None, *a, **kw):
        # Filter-style lookup (e.g. the policy extension by leave_type + active).
        if isinstance(key, dict):
            for (dt, _name), row in self.row_values.items():
                if dt == doctype and all(row.get(k) == v for k, v in key.items()):
                    if isinstance(fields, (list, tuple)):
                        return {f: row.get(f) for f in fields}
                    return row.get(fields) if fields else dict(row)
            return None
        row = self.row_values.get((str(doctype), str(key)))
        if row is None:
            return None
        if isinstance(fields, (list, tuple)):
            return {f: row.get(f) for f in fields}
        return row.get(fields) if fields else dict(row)

    def table_exists(self, doctype):
        return self.table_exists_flag

    def delete_doc(self, doctype, name, *a, **kw):
        self.deleted.append((doctype, name))


def _build_stub_frappe(db: _DB):
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = _PermissionDenied
    mod.ValidationError = _ValidationError

    def _throw(msg, exc=None):
        if exc is None:
            raise Exception(str(msg))
        raise exc(str(msg))

    mod.throw = _throw
    mod.log_error = lambda *a, **k: None
    mod.get_roles = lambda user: []
    mod.delete_doc = db.delete_doc

    published: list[tuple] = []

    def _publish(event, payload=None, **kw):
        published.append((event, payload))

    mod.publish_realtime = _publish

    utils = types.ModuleType("frappe.utils")
    utils.now = lambda: "2026-09-02 10:00:00"
    utils.today = lambda: "2026-09-02"
    utils.getdate = lambda v=None: v if isinstance(v, str) and v else "2026-09-02"
    utils.cint = lambda v, *a: int(v) if v not in (None, "") else 0
    mod.utils = utils

    mod.db = db
    mod.get_doc = db.get_doc
    mod.get_all = db.get_all
    mod.session = types.SimpleNamespace(user="caller@gege.demo")
    mod.local = types.SimpleNamespace(request_ip=None)
    mod.flags = types.SimpleNamespace()
    mod._published = published
    return mod


@pytest.fixture
def fake(monkeypatch):
    """Install the stub frappe + import api/handover with recorders."""
    db = _DB()
    stub = _build_stub_frappe(db)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.handover")
    monkeypatch.setattr(api, "frappe", stub)
    # The module binds ``now`` at import time — earlier test files may have
    # imported it under a different stub, so rebind to ours (suite-order safe).
    monkeypatch.setattr(api, "now", stub.utils.now)

    notified: list[dict] = []
    monkeypatch.setattr(
        api.notify, "push_notification", lambda *a, **k: notified.append({"args": a, "kwargs": k})
    )
    audits: list[dict] = []
    monkeypatch.setattr(api.audit_api, "log", lambda *a, **k: audits.append({"args": a, "kwargs": k}))

    emp_utils = importlib.import_module("gege_hr.gege_hr.utils.employee")
    state = {"roles": [], "employee": None}
    monkeypatch.setattr(emp_utils, "get_user_roles", lambda user=None: list(state["roles"]))
    monkeypatch.setattr(emp_utils, "get_employee_for_user", lambda user=None: state["employee"])

    def persona(roles=None, employee=None):
        state["roles"] = list(roles or [])
        state["employee"] = employee

    return types.SimpleNamespace(
        api=api,
        db=db,
        stub=stub,
        persona=persona,
        published=stub._published,
        notified=notified,
        audits=audits,
    )


def _handover_rows():
    return [
        {
            "name": "HT-1",
            "leave_application": "LA-OK",
            "from_employee": "FROM",
            "to_employee": "TO",
            "handover_date": "2026-09-01",
            "status": "Pending",
            "description": "d1",
            "attachment": None,
            "completed_at": None,
            "completed_by": None,
            "note": "",
            "docstatus": 0,
            "owner": "from@gege.demo",
            "modified": "2026-09-01 10:00:00",
        },
        {
            "name": "HT-2",
            "leave_application": "LA-OK",
            "from_employee": "TO",
            "to_employee": "FROM",
            "handover_date": "2026-09-02",
            "status": "In Progress",
            "description": "d2",
            "attachment": None,
            "completed_at": None,
            "completed_by": None,
            "note": "",
            "docstatus": 0,
            "owner": "to@gege.demo",
            "modified": "2026-09-02 10:00:00",
        },
        {
            "name": "HT-3",
            "leave_application": "LA-OK",
            "from_employee": "OTHER",
            "to_employee": "OTHER2",
            "handover_date": "2026-09-03",
            "status": "Completed",
            "description": "d3",
            "attachment": None,
            "completed_at": "2026-09-03 10:00:00",
            "completed_by": "x",
            "note": "",
            "docstatus": 0,
            "owner": "other@gege.demo",
            "modified": "2026-09-03 10:00:00",
        },
    ]


# --------------------------------------------------------------------------- #
# HO1-HO7 — update_handover
# --------------------------------------------------------------------------- #
def test_ho1_update_handover_applies_fields_saves_audits_publishes(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    res = fake.api.update_handover(
        "HT-1",
        to_employee="TO2",
        handover_date="2026-09-05T08:00:00",
        description="mô tả mới",
        note="ghi chú",
    )
    doc = fake.db.doc
    assert doc.to_employee == "TO2"
    assert doc.handover_date == "2026-09-05"  # datetime string coerced → date
    assert doc.description == "mô tả mới"
    assert doc.note == "ghi chú"
    assert doc.saved >= 1
    assert res["status"] == "Pending"
    assert any(a["args"][0] == "Handover Update" for a in fake.audits)
    assert any(e == "handover_updated" for e, _p in fake.published)


def test_ho2_update_handover_rejects_submitted(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.doc.docstatus = 1
    with pytest.raises(_ValidationError):
        fake.api.update_handover("HT-1", description="x")
    assert fake.db.doc.saved == 0


def test_ho3_update_handover_rejects_terminal_status(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.doc.status = "Cancelled"
    with pytest.raises(_ValidationError):
        fake.api.update_handover("HT-1", description="x")


def test_ho4_update_handover_denies_outsider(fake):
    fake.persona(roles=["Employee"], employee="STRANGER")
    with pytest.raises(_PermissionDenied):
        fake.api.update_handover("HT-1", description="x")
    assert fake.db.doc.saved == 0


def test_ho5_update_handover_race_detected_on_reload(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.doc.reload_hook = lambda d: setattr(d, "status", "Completed")
    with pytest.raises(_ValidationError):
        fake.api.update_handover("HT-1", description="x")
    assert fake.db.doc.saved == 0
    assert fake.db.doc.reloads == 1


def test_ho6_update_handover_identity_fields_hr_only(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.api.update_handover("HT-1", description="ok", from_employee="OTHER")
    assert fake.db.doc.from_employee == "FROM"  # silently dropped, not applied
    # HR *may* change identity fields.
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.api.update_handover("HT-1", description="ok", from_employee="OTHER")
    assert fake.db.doc.from_employee == "OTHER"


def test_ho7_update_handover_receiver_must_differ_from_giver(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    with pytest.raises(_ValidationError):
        fake.api.update_handover("HT-1", to_employee="FROM")


# --------------------------------------------------------------------------- #
# HO8-HO10 — delete_handover
# --------------------------------------------------------------------------- #
def test_ho8_delete_handover_scoped_bypass_audit_publish(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    res = fake.api.delete_handover("HT-1")
    assert fake.db.deleted == [("VN Leave Handover Task", "HT-1")]
    assert res["name"] == "HT-1"
    assert any(a["args"][0] == "Handover Delete" for a in fake.audits)
    assert any(e == "handover_updated" for e, _p in fake.published)
    # The scoped bypass flag must be reset after the call.
    assert not getattr(fake.stub.flags, "ignore_permissions", False)


def test_ho9_delete_handover_rejects_submitted(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.doc.docstatus = 1
    with pytest.raises(_ValidationError):
        fake.api.delete_handover("HT-1")
    assert fake.db.deleted == []


def test_ho10_delete_handover_denies_outsider(fake):
    fake.persona(roles=["Employee"], employee="STRANGER")
    with pytest.raises(_PermissionDenied):
        fake.api.delete_handover("HT-1")
    assert fake.db.deleted == []


# --------------------------------------------------------------------------- #
# HO11-HO12 — get_handover
# --------------------------------------------------------------------------- #
def _can(fake, **doc_fields):
    fake.db.doc = _Doc(**doc_fields)
    return fake.api.get_handover("HT-1")["can"]


def test_ho11_get_handover_can_matrix_six_branches(fake):
    # a) Pending + from-owner (non-HR): edit/delete/cancel yes; start/complete no.
    fake.persona(roles=["Employee"], employee="FROM")
    can = _can(fake)
    assert can["edit"] and can["delete"] and can["cancel"]
    assert not can["start"] and not can["complete"] and not can["recreate"]

    # b) Pending + receiver (non-HR): start/complete/upload yes; edit/delete/cancel no.
    can = _can(fake)
    fake.persona(roles=["Employee"], employee="TO")
    can = _can(fake)
    assert can["start"] and can["complete"] and can["upload"]
    assert not can["edit"] and not can["delete"] and not can["cancel"]

    # c) Pending + HR: everything actionable.
    fake.persona(roles=["HR Manager"], employee="HR")
    can = _can(fake)
    assert all(can[k] for k in ("edit", "delete", "start", "complete", "cancel", "upload"))

    # d) Completed + docstatus 1 + receiver: nothing left except view.
    fake.persona(roles=["Employee"], employee="TO")
    can = _can(fake, status="Completed", docstatus=1)
    assert not any(can[k] for k in ("edit", "delete", "start", "complete", "cancel", "reopen", "upload"))

    # e) Cancelled draft (docstatus 0) + receiver: reopen + recreate.
    can = _can(fake, status="Cancelled")
    assert can["reopen"] and can["recreate"]
    assert not can["edit"]

    # f) Cancelled submitted (docstatus 2): recreate only.
    can = _can(fake, status="Cancelled", docstatus=2)
    assert can["recreate"]
    assert not can["reopen"] and not can["edit"]


def test_ho12_get_handover_shape_names_leave_attachments(fake):
    fake.persona(roles=["Employee"], employee="TO")
    fake.db.rows_by_doctype["File"] = [
        {"name": "F1", "file_name": "a.pdf", "file_url": "/files/a.pdf", "is_private": 1, "file_size": 9}
    ]
    out = fake.api.get_handover("HT-1")
    assert out["from_employee_name"] == "Người Giao"
    assert out["to_employee_name"] == "Người Nhận"
    assert out["leave"]["employee"] == "FROM"
    assert out["leave"]["docstatus"] == 1
    assert out["attachments"][0]["file_name"] == "a.pdf"
    # A deleted Leave Application degrades to None — never throws.
    fake.db.doc.leave_application = "LA-GONE"
    assert fake.api.get_handover("HT-1")["leave"] is None


# --------------------------------------------------------------------------- #
# HO13-HO15 — update_handover_status submit/cancel
# --------------------------------------------------------------------------- #
def test_ho13_complete_submits_and_stamps(fake):
    fake.persona(roles=["Employee"], employee="TO")
    res = fake.api.update_handover_status("HT-1", "Completed", note="xong")
    doc = fake.db.doc
    assert doc.submitted == 1 and doc.docstatus == 1
    assert doc.completed_at == "2026-09-02 10:00:00"
    assert doc.completed_by == "caller@gege.demo"
    assert doc.note == "xong"
    assert res["docstatus"] == 1 and res["status"] == "Completed"
    assert any(a["args"][0] == "Handover Submit" for a in fake.audits)
    # The departing employee is notified their handover completed.
    assert any(n["kwargs"].get("employee") == "FROM" for n in fake.notified)


def test_ho14_cancel_submitted_runs_cancel_docstatus2(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.doc.status = "Completed"
    fake.db.doc.docstatus = 1
    res = fake.api.update_handover_status("HT-1", "Cancelled", note="làm lại")
    doc = fake.db.doc
    assert doc.cancelled == 1 and doc.docstatus == 2
    assert doc.status == "Cancelled"
    assert res["docstatus"] == 2
    assert any(a["args"][0] == "Handover Cancel" for a in fake.audits)
    # Receiver learns the submitted task was pulled back.
    assert any(n["kwargs"].get("employee") == "TO" for n in fake.notified)


def test_ho15_completed_doc1_cannot_reopen(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.doc.status = "Completed"
    fake.db.doc.docstatus = 1
    with pytest.raises(_ValidationError):
        fake.api.update_handover_status("HT-1", "Pending")
    assert fake.db.doc.docstatus == 1  # untouched


def test_ho15b_non_hr_cannot_cancel_submitted(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.doc.status = "Completed"
    fake.db.doc.docstatus = 1
    with pytest.raises(_PermissionDenied):
        fake.api.update_handover_status("HT-1", "Cancelled")
    assert fake.db.doc.cancelled == 0


# --------------------------------------------------------------------------- #
# HO16-HO17 — leave link validation
# --------------------------------------------------------------------------- #
def test_ho16_validate_leave_link_three_messages(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    with pytest.raises(_ValidationError, match="chưa được duyệt"):
        fake.api.create_handover(
            leave_application="LA-DRAFT",
            from_employee="FROM",
            to_employee="TO",
            handover_date="2026-09-05",
            description="d",
        )
    with pytest.raises(_ValidationError, match="không phải của người bàn giao"):
        fake.api.create_handover(
            leave_application="LA-OTHER",
            from_employee="FROM",
            to_employee="TO",
            handover_date="2026-09-05",
            description="d",
        )
    with pytest.raises(_ValidationError, match="không tồn tại"):
        fake.api.create_handover(
            leave_application="LA-GONE",
            from_employee="FROM",
            to_employee="TO",
            handover_date="2026-09-05",
            description="d",
        )


def test_ho17_create_handover_rejects_inactive_receiver(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    with pytest.raises(_ValidationError, match="không còn làm việc"):
        fake.api.create_handover(
            leave_application="LA-OK",
            from_employee="FROM",
            to_employee="GONE",
            handover_date="2026-09-05",
            description="d",
        )


def test_ho17b_create_handover_publishes_and_notifies(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    res = fake.api.create_handover(
        leave_application="LA-OK",
        from_employee="FROM",
        to_employee="TO",
        handover_date="2026-09-05",
        description="bàn giao dự án",
    )
    assert res["status"] == "Pending"
    assert any(e == "handover_updated" for e, _p in fake.published)
    assert any(n["kwargs"].get("employee") == "TO" for n in fake.notified)


# --------------------------------------------------------------------------- #
# HO18 — handover_leave_options
# --------------------------------------------------------------------------- #
def test_ho18_leave_options_non_hr_pinned_to_own_approved(fake):
    fake.persona(roles=["Employee"], employee="FROM")
    fake.db.rows_by_doctype["Leave Application"] = [
        {"name": "LA-OK", "employee": "FROM", "leave_type": "Nghỉ phép", "posting_date": "2026-09-01"}
    ]
    out = fake.api.handover_leave_options(search="nghỉ")
    assert [r["name"] for r in out] == ["LA-OK"]
    call = fake.db.list_calls[-1]
    assert call["doctype"] == "Leave Application"
    assert call["filters"] == {"docstatus": 1, "employee": "FROM"}
    # An explicit from_employee is ignored for non-HR (G3 scope pin).
    fake.api.handover_leave_options(from_employee="OTHER")
    assert fake.db.list_calls[-1]["filters"]["employee"] == "FROM"
    # HR may query anyone; no employee record → [].
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.api.handover_leave_options(from_employee="OTHER")
    assert fake.db.list_calls[-1]["filters"]["employee"] == "OTHER"
    fake.persona(roles=["Employee"], employee=None)
    assert fake.api.handover_leave_options() == []


# --------------------------------------------------------------------------- #
# HO19 — can_transition docstatus matrix (utils pin)
# --------------------------------------------------------------------------- #
def test_ho19_can_transition_docstatus_matrix():
    # Legacy two-arg behaviour is unchanged.
    assert handover_utils.can_transition("Completed", "Pending") is True
    assert handover_utils.can_transition("Pending", "Bogus") is False
    # docstatus 1 (submitted): same-state or Cancelled only.
    assert handover_utils.can_transition("Completed", "Completed", docstatus=1) is True
    assert handover_utils.can_transition("Completed", "Cancelled", docstatus=1) is True
    assert handover_utils.can_transition("Completed", "Pending", docstatus=1) is False
    assert handover_utils.can_transition("Completed", "In Progress", docstatus=1) is False
    # docstatus 2 (cancelled): idempotent same-state only.
    assert handover_utils.can_transition("Cancelled", "Cancelled", docstatus=2) is True
    assert handover_utils.can_transition("Cancelled", "Pending", docstatus=2) is False
    # Draft rows keep the permissive table (Cancelled → Pending reopen).
    assert handover_utils.can_transition("Cancelled", "Pending", docstatus=0) is True
    assert handover_utils.can_transition(None, "Completed", docstatus=0) is True


# --------------------------------------------------------------------------- #
# HO20-HO22 — leave_handovers v2
# --------------------------------------------------------------------------- #
def test_ho20_leave_handovers_employee_or_filters_fix(fake):
    """G1: `employee` must OR-match from/to — recorded via or_filters."""
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.rows_by_doctype["VN Leave Handover Task"] = _handover_rows()
    out = fake.api.leave_handovers(employee="FROM")
    # The handover query is the first call — later ones are the Employee name
    # enrichment lookups.
    call = next(c for c in fake.db.list_calls if c["doctype"] == "VN Leave Handover Task")
    assert call["or_filters"] == [["from_employee", "=", "FROM"], ["to_employee", "=", "FROM"]]
    # Stub returns rows verbatim — FROM appears on both sides across rows.
    assert {r["name"] for r in out} >= {"HT-1", "HT-2"}


def test_ho21_leave_handovers_pagination_and_summary(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.rows_by_doctype["VN Leave Handover Task"] = _handover_rows()
    fake.db.rows_by_doctype["Employee"] = [
        {"name": "FROM", "employee_name": "Người Giao"},
        {"name": "TO", "employee_name": "Người Nhận"},
        {"name": "OTHER", "employee_name": "Khác"},
        {"name": "OTHER2", "employee_name": "Khác 2"},
    ]
    out = fake.api.leave_handovers(page=1, page_size=2)
    assert isinstance(out, dict)
    assert out["total"] == 3
    assert len(out["data"]) == 2
    assert out["summary"] == {"pending": 1, "in_progress": 1}
    # Legacy bare-list contract survives when page_size is omitted.
    legacy = fake.api.leave_handovers()
    assert isinstance(legacy, list) and len(legacy) == 3


def test_ho22_rows_carry_employee_names(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.rows_by_doctype["VN Leave Handover Task"] = _handover_rows()
    fake.db.rows_by_doctype["Employee"] = [
        {"name": "FROM", "employee_name": "Người Giao"},
        {"name": "TO", "employee_name": "Người Nhận"},
    ]
    out = fake.api.leave_handovers()
    row = next(r for r in out if r["name"] == "HT-1")
    assert row["from_employee_name"] == "Người Giao"
    assert row["to_employee_name"] == "Người Nhận"
    # Unresolvable ids degrade to None (FE falls back to the raw id).
    row2 = next(r for r in out if r["name"] == "HT-3")
    assert row2["from_employee_name"] is None


# --------------------------------------------------------------------------- #
# HO23 — P1 auto-mint (policy require_handover) after leave approval
# --------------------------------------------------------------------------- #
def _la_doc(**fields):
    base = {
        "name": "LA-OK",
        "employee": "FROM",
        "leave_type": "AN",
        "from_date": "2026-09-05",
        "status": "Approved",
    }
    base.update(fields)
    return _Doc(**base)


def test_ho23_auto_mint_creates_task_and_notifies(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    la = _la_doc(vn_handover_employee="TO", vn_handover_note="dự án X")
    fake.api._maybe_mint_handover(la)
    assert len(fake.db.created) == 1
    payload = fake.db.created[0]
    assert payload["doctype"] == "VN Leave Handover Task"
    assert payload["leave_application"] == "LA-OK"
    assert payload["from_employee"] == "FROM"
    assert payload["to_employee"] == "TO"
    assert payload["handover_date"] == "2026-09-05"  # LA from_date
    assert payload["status"] == "Pending"
    assert "dự án X" in payload["description"]
    # Receiver notified + audit + realtime ping.
    assert any(n["kwargs"].get("employee") == "TO" for n in fake.notified)
    assert any(a["args"][0] == "Handover Auto Mint" for a in fake.audits)
    assert any(e == "handover_updated" for e, _p in fake.published)


def test_ho23b_auto_mint_skips_when_policy_off(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    la = _la_doc(leave_type="OFF", vn_handover_employee="TO")
    fake.api._maybe_mint_handover(la)
    assert fake.notified == []


def test_ho23c_auto_mint_skips_when_open_task_exists(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    fake.db.rows_by_doctype["VN Leave Handover Task"] = [
        {"name": "HT-X", "leave_application": "LA-OK", "status": "Pending"}
    ]
    fake.api._maybe_mint_handover(_la_doc(vn_handover_employee="TO"))
    assert fake.notified == []


def test_ho23d_auto_mint_without_receiver_notifies_employee(fake):
    fake.persona(roles=["HR Manager"], employee="HR")
    la = _la_doc()  # no vn_handover_employee
    fake.api._maybe_mint_handover(la)
    assert any(n["kwargs"].get("employee") == "FROM" for n in fake.notified)
    # Reminder only — no task minted.
    assert fake.db.created == []


# --------------------------------------------------------------------------- #
# HO24 — endpoint contract regression
# --------------------------------------------------------------------------- #
def test_ho24_new_endpoints_are_whitelisted_callables(fake):
    for name in (
        "get_handover",
        "update_handover",
        "delete_handover",
        "handover_leave_options",
        "my_handovers",
        "leave_handovers",
        "create_handover",
        "update_handover_status",
        "suggest_handover_receivers",
    ):
        fn = getattr(fake.api, name, None)
        assert callable(fn), f"{name} phải tồn tại và callable trên api/handover"
