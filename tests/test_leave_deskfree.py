"""Bench-free unit tests for the /hr/leave desk-free endpoints (api/leave.py).

plans/plan-leave-deskfree-complete.md §5.1 — covers the new self-service
lifecycle surface:

  * ``update_draft``      (edit Open / resubmit Rejected + race guard)
  * ``delete_draft``      (hard-delete own docstatus-0)
  * ``get_leave_application`` (detail + attachments + cancellation + can.*)
  * ``my_applications`` v2   (q/status/leave_type/limit/offset + {data,total})
  * ``my_leave_balance``  G0 fix (no stock ``disabled`` filter) + own-gate

Same stub-frappe harness style as ``test_leave_approval_api.py`` — the stub is
registered per-test via ``monkeypatch.setitem`` so it never leaks.
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/leave`` to import + run
# --------------------------------------------------------------------------- #
class _ValidationError(Exception):
    pass


class _PermissionDenied(Exception):
    pass


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.today = lambda: datetime.date.today()
    utils.cint = lambda v, d=0: int(v) if v not in (None, "") else d
    mod.utils = utils

    mod.PermissionError = _PermissionDenied
    mod.ValidationError = _ValidationError

    def _throw(msg, exc=None):
        if exc is None:
            raise Exception(str(msg))
        raise exc(str(msg))

    mod.throw = _throw
    mod.log_error = lambda *a, **k: None
    mod.session = types.SimpleNamespace(user="emp.demo@gege.demo")

    class _Meta:
        def has_field(self, name):
            return True

    mod.get_meta = lambda doctype: _Meta()

    mod.db = None
    mod.get_doc = None
    return mod


class _FakeDoc:
    """Leave Application stand-in: attribute settable + no-op persist."""

    def __init__(self, name, status="Open", docstatus=0, employee="EMP-1", **extra):
        self.name = name
        self.status = status
        self.docstatus = docstatus
        self.employee = employee
        self.flags = types.SimpleNamespace(ignore_permissions=False)
        self.saved_with_ip = None
        self.deleted = False
        self.reload_side = {}  # applied by reload() — race simulation
        for k, v in extra.items():
            setattr(self, k, v)

    def save(self, ignore_permissions=False):
        # Real Document reads doc.flags.ignore_permissions (the scoped-bypass
        # mechanism used by update_draft/_save_or_submit) — honour it here too.
        self.saved_with_ip = bool(ignore_permissions or self.flags.ignore_permissions)
        return self

    def delete(self):
        self.deleted = True

    def reload(self):
        for k, v in self.reload_side.items():
            setattr(self, k, v)
        return self

    def as_dict(self):
        return dict(self.__dict__)


class _FakeDB:
    """Stub DB with per-doctype rows + full get_all kwarg capture."""

    def __init__(self):
        self.rows_by_doctype = {}
        self.calls = []  # (doctype, filters, kwargs)

    def configure(self, doctype, rows):
        self.rows_by_doctype[doctype] = rows

    def get_all(
        self,
        doctype,
        filters=None,
        or_filters=None,
        fields=None,
        order_by=None,
        limit_page_length=None,
        limit=None,
        start=None,
        pluck=None,
    ):
        self.calls.append(
            {
                "doctype": doctype,
                "filters": dict(filters or {}),
                "or_filters": or_filters,
                "limit": limit,
                "start": start,
                "limit_page_length": limit_page_length,
            }
        )
        rows = self.rows_by_doctype.get(doctype, [])
        if start:
            rows = rows[start:]
        if limit is not None:
            rows = rows[:limit]
        if limit_page_length == 0:
            rows = self.rows_by_doctype.get(doctype, [])
        return [_Row(r) for r in rows]

    def sql(self, query, params=None, **_kw):
        return []


class _Row(dict):
    """frappe._dict stand-in — attribute AND key access (my_leave_balance's
    ``lt.name`` etc.)."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


@pytest.fixture
def fake(monkeypatch):
    """Stub frappe + import api/leave with recorders for audit/touch/blackout."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.leave")
    monkeypatch.setattr(api, "frappe", stub)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)

    # Record side effects (audit / calendar refresh / blackout stamp).
    log = []
    touch = []
    stamp = []
    monkeypatch.setattr(api.audit_api, "log", lambda *a, **k: log.append({"args": a, "kwargs": k}))
    monkeypatch.setattr(api, "_touch_calendar", lambda doc=None: touch.append(doc))
    monkeypatch.setattr(
        api, "_stamp_blackout_decision", lambda doc, **k: stamp.append({"doc": doc, "kwargs": k})
    )
    monkeypatch.setattr(api, "_open_cancellation_request", lambda name: None)

    # Persona control: own employee id + manager flag.
    state = {"own": "EMP-1", "manager": False}

    def _own(user=None):
        return state["own"]

    monkeypatch.setattr(api, "_is_manager", lambda: state["manager"])
    emp_utils = importlib.import_module("gege_hr.gege_hr.utils.employee")
    monkeypatch.setattr(emp_utils, "get_employee_for_user", _own)
    monkeypatch.setattr(emp_utils, "emp_name", lambda v: v)

    def wire(docs):
        """Route frappe.get_doc("Leave Application", name) to a fake doc."""
        table = {d.name: d for d in docs}

        def get_doc(doctype, name):
            return table[name]

        stub.get_doc = get_doc

    return types.SimpleNamespace(
        api=api,
        stub=stub,
        db=db,
        wire=wire,
        log=log,
        touch=touch,
        stamp=stamp,
        state=state,
    )


def _open_doc(name="L1", status="Open", **extra):
    return _FakeDoc(name, status=status, docstatus=0, **extra)


# --------------------------------------------------------------------------- #
# LV1-LV7 — update_draft
# --------------------------------------------------------------------------- #
def test_lv1_update_draft_open_saves_fields_and_touches_calendar(fake):
    doc = _open_doc(leave_type="AN", from_date="2026-09-01", to_date="2026-09-02", description="")
    fake.wire([doc])
    res = fake.api.update_draft(
        "L1",
        leave_type="AN",
        from_date="2026-09-03",
        to_date="2026-09-05",
        description="việc gia đình",
    )
    assert doc.from_date == datetime.date(2026, 9, 3)
    assert doc.to_date == datetime.date(2026, 9, 5)
    assert doc.description == "việc gia đình"
    assert res == {
        "name": "L1",
        "status": "Open",
        "message": "Đã cập nhật đơn nghỉ phép L1.",
    }
    # Proper flow: Open branch saves WITHOUT the scoped bypass.
    assert doc.saved_with_ip is False
    assert fake.touch, "calendar cache must be refreshed after an edit"
    assert fake.log and fake.log[0]["args"][0] == "Leave Update Draft"


def test_lv2_update_draft_rejected_resubmits_with_scoped_bypass(fake):
    doc = _FakeDoc("L2", status="Rejected", docstatus=0, leave_type="AN")
    fake.wire([doc])
    res = fake.api.update_draft("L2", from_date="2026-09-10", to_date="2026-09-11")
    assert doc.status == "Open", "Rejected draft must re-enter the approval pipeline"
    assert doc.saved_with_ip is True, "status is permlevel-1 — save needs the scoped elevation"
    assert res["status"] == "Open"
    assert "từ chối" in fake.log[0]["kwargs"]["description"]


def test_lv3_update_draft_rejects_submitted_doc(fake):
    doc = _FakeDoc("L3", status="Approved", docstatus=1)
    fake.wire([doc])
    with pytest.raises(_ValidationError):
        fake.api.update_draft("L3", description="x")


def test_lv4_update_draft_denies_other_employee(fake):
    doc = _open_doc("L4", employee="EMP-9")
    fake.wire([doc])
    fake.state["own"] = "EMP-1"
    fake.state["manager"] = False
    with pytest.raises(_PermissionDenied):
        fake.api.update_draft("L4", description="x")


def test_lv5_update_draft_allows_hr_on_behalf(fake):
    doc = _open_doc("L5", employee="EMP-9")
    fake.wire([doc])
    fake.state["manager"] = True
    res = fake.api.update_draft("L5", description="HR sửa hộ")
    assert res["status"] == "Open"
    assert doc.employee == "EMP-9"


def test_lv6_update_draft_race_hr_approved_while_editing(fake):
    doc = _open_doc("L6")
    doc.reload_side = {"docstatus": 1, "status": "Approved"}
    fake.wire([doc])
    with pytest.raises(Exception, match="duyệt bởi người khác"):
        fake.api.update_draft("L6", description="x")


def test_lv7_update_draft_restamps_blackout(fake):
    doc = _open_doc("L7", leave_type="AN")
    fake.wire([doc])
    fake.api.update_draft("L7", from_date="2026-12-30", to_date="2027-01-02")
    assert fake.stamp, "blackout decision must be re-stamped with the new dates"
    assert fake.stamp[0]["kwargs"]["leave_type"] == "AN"


# --------------------------------------------------------------------------- #
# LV8-LV10 — delete_draft
# --------------------------------------------------------------------------- #
def test_lv8_delete_draft_removes_own_docstatus0(fake):
    doc = _open_doc("L8", status="Rejected")
    fake.wire([doc])
    res = fake.api.delete_draft("L8")
    assert doc.deleted is True
    assert res == {"name": "L8"}
    assert fake.log and fake.log[0]["args"][0] == "Leave Delete Draft"
    assert fake.touch


def test_lv9_delete_draft_rejects_submitted_doc(fake):
    doc = _FakeDoc("L9", status="Approved", docstatus=1)
    fake.wire([doc])
    with pytest.raises(_ValidationError):
        fake.api.delete_draft("L9")


def test_lv10_delete_draft_denies_other_employee(fake):
    doc = _open_doc("L10", employee="EMP-9")
    fake.wire([doc])
    with pytest.raises(_PermissionDenied):
        fake.api.delete_draft("L10")


# --------------------------------------------------------------------------- #
# LV11-LV12 — get_leave_application
# --------------------------------------------------------------------------- #
def test_lv11_get_leave_application_can_matrix(fake):
    cases = [
        (_FakeDoc("A", status="Open", docstatus=0), {"edit": True, "delete": True, "resubmit": False, "cancel_draft": True, "request_cancel": False}),
        (_FakeDoc("B", status="Rejected", docstatus=0), {"edit": True, "delete": True, "resubmit": True, "cancel_draft": False, "request_cancel": False}),
        (_FakeDoc("C", status="Approved", docstatus=1), {"edit": False, "delete": False, "resubmit": False, "cancel_draft": False, "request_cancel": True}),
        (_FakeDoc("D", status="Cancelled", docstatus=2), {"edit": False, "delete": False, "resubmit": False, "cancel_draft": False, "request_cancel": False}),
    ]
    for doc, expected in cases:
        fake.wire([doc])
        res = fake.api.get_leave_application(doc.name)
        assert res["can"] == expected, f"can-matrix wrong for status={doc.status}"
        assert res["doc"]["employee"] == "EMP-1"


def test_lv12_get_leave_application_attachments_and_cancellation(fake):
    doc = _open_doc("L12")
    fake.wire([doc])
    fake.db.configure(
        "File",
        [{"name": "F1", "file_name": "don-benh.jpg", "file_url": "/private/files/don-benh.jpg", "is_private": 1}],
    )
    fake.db.configure(
        "VN Leave Cancellation Request",
        [{"name": "CR-1", "status": "Pending Manager", "reason": "nhầm ngày", "rejection_reason": ""}],
    )
    res = fake.api.get_leave_application("L12")
    assert res["attachments"][0]["file_name"] == "don-benh.jpg"
    assert res["cancellation"]["reason"] == "nhầm ngày"


# --------------------------------------------------------------------------- #
# LV13 — my_applications v2
# --------------------------------------------------------------------------- #
def test_lv13_my_applications_v2_filters_and_envelope(fake):
    rows = [{"name": f"L{i}"} for i in range(3)]
    fake.db.configure("Leave Application", rows)
    res = fake.api.my_applications(
        employee="EMP-1", q="an", status="Approved", leave_type="Annual", limit=2, offset=1
    )
    assert res["total"] == 3
    assert [r["name"] for r in res["data"]] == ["L1", "L2"]
    leave_calls = [c for c in fake.db.calls if c["doctype"] == "Leave Application"]
    assert leave_calls, "list + count queries must hit Leave Application"
    first = leave_calls[0]
    assert first["filters"]["status"] == "Approved"
    assert first["filters"]["docstatus"] == ["<", 2]
    assert first["filters"]["leave_type"] == "Annual"
    assert first["or_filters"], "q must become a broad or_filters search"
    assert first["limit"] == 2
    assert first["start"] == 1


def test_lv13b_my_applications_cancelled_maps_to_docstatus2(fake):
    fake.db.configure("Leave Application", [])
    fake.api.my_applications(employee="EMP-1", status="Cancelled")
    first = [c for c in fake.db.calls if c["doctype"] == "Leave Application"][0]
    assert first["filters"]["docstatus"] == 2
    assert "status" not in first["filters"]


def test_lv13c_my_applications_legacy_params_keep_plain_filters(fake):
    fake.db.configure("Leave Application", [])
    res = fake.api.my_applications(employee="EMP-1")
    assert res == {"data": [], "total": 0}
    first = [c for c in fake.db.calls if c["doctype"] == "Leave Application"][0]
    assert first["filters"] == {"employee": "EMP-1"}
    assert first["or_filters"] is None
    assert first["limit"] == 50


# --------------------------------------------------------------------------- #
# LV14-LV15 — my_leave_balance
# --------------------------------------------------------------------------- #
def test_lv14_my_leave_balance_no_disabled_filter(fake):
    """G0: Leave Type has no stock ``disabled`` column — the filter must be gone."""
    fake.db.configure("Leave Type", [{"name": "Annual"}, {"name": "Sick"}])
    out = fake.api.my_leave_balance(employee="EMP-1")
    assert [r["leave_type"] for r in out] == ["Annual", "Sick"]
    type_calls = [c for c in fake.db.calls if c["doctype"] == "Leave Type"]
    assert type_calls and "disabled" not in type_calls[0]["filters"]


def test_lv15_my_leave_balance_own_gate(fake):
    fake.db.configure("Leave Type", [{"name": "Annual"}])
    with pytest.raises(_PermissionDenied):
        fake.api.my_leave_balance(employee="EMP-9")


# --------------------------------------------------------------------------- #
# LV16 — contract regression
# --------------------------------------------------------------------------- #
def test_lv16_new_endpoints_are_whitelisted_callables(fake):
    for fn_name in ("update_draft", "delete_draft", "get_leave_application"):
        fn = getattr(fake.api, fn_name, None)
        assert callable(fn), f"{fn_name} must exist on api/leave"


# --------------------------------------------------------------------------- #
# LV17-LV20 — P1: allocations / ledger / activity (plan §3.6)
# --------------------------------------------------------------------------- #
def test_lv17_my_leave_allocations_filters_and_own_gate(fake):
    fake.db.configure(
        "Leave Allocation",
        [
            {"name": "AL-1", "leave_type": "Annual", "total_leaves_allocated": 12},
            {"name": "AL-2", "leave_type": "Sick", "total_leaves_allocated": 5},
        ],
    )
    out = fake.api.my_leave_allocations(employee="EMP-1", leave_type="Annual")
    # Stub DB không lọc — chỉ assert server đã push filter đúng (pattern LV13).
    assert {r["name"] for r in out} == {"AL-1", "AL-2"}
    alloc_calls = [c for c in fake.db.calls if c["doctype"] == "Leave Allocation"]
    assert alloc_calls[0]["filters"]["leave_type"] == "Annual"
    assert alloc_calls[0]["filters"]["docstatus"] == 1
    assert alloc_calls[0]["filters"]["employee"] == "EMP-1"


def test_lv17b_my_leave_allocations_denies_other_employee(fake):
    with pytest.raises(_PermissionDenied):
        fake.api.my_leave_allocations(employee="EMP-9")


def test_lv18_my_leave_ledger_rows_and_limit_clamp(fake):
    rows = [{"name": f"LE-{i}"} for i in range(5)]
    fake.db.configure("Leave Ledger Entry", rows)
    out = fake.api.my_leave_ledger(employee="EMP-1", limit=999)
    assert [r["name"] for r in out] == [f"LE-{i}" for i in range(5)]
    ledger_calls = [c for c in fake.db.calls if c["doctype"] == "Leave Ledger Entry"]
    assert ledger_calls[0]["limit"] == 200, "limit must clamp to 1..200"


def test_lv18b_my_leave_ledger_own_gate(fake):
    with pytest.raises(_PermissionDenied):
        fake.api.my_leave_ledger(employee="EMP-9")


def test_lv19_get_leave_application_includes_activity(fake):
    doc = _open_doc("L19")
    fake.wire([doc])
    fake.db.configure(
        "VN Audit Event",
        [
            {
                "name": "AE-1",
                "audit_type": "Leave Approve",
                "employee": "EMP-1",
                "description": "HR duyệt đơn",
                "creation": "2026-09-02 10:00:00",
            }
        ],
    )
    res = fake.api.get_leave_application("L19")
    assert res["activity"][0]["audit_type"] == "Leave Approve"


def test_lv20_my_leave_ledger_date_window_filter(fake):
    fake.db.configure("Leave Ledger Entry", [])
    fake.api.my_leave_ledger(employee="EMP-1", from_date="2026-01-01", to_date="2026-06-30")
    ledger_calls = [c for c in fake.db.calls if c["doctype"] == "Leave Ledger Entry"]
    assert ledger_calls[0]["filters"]["from_date"] == ["between", ["2026-01-01", "2026-06-30"]]
