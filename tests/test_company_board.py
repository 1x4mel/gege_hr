"""Bench-free unit tests for ``api/company_board`` (board + shift swap).

Mirrors the stub-frappe harness of ``test_holiday_master``: a stub ``frappe``
module is injected into ``sys.modules`` for the duration of each test and the
admin helpers imported by company_board are monkeypatched on the module.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest

TODAY = dt.date.today()
TOMORROW = (TODAY + dt.timedelta(days=1)).isoformat()
DAY_AFTER = (TODAY + dt.timedelta(days=2)).isoformat()
YESTERDAY = (TODAY - dt.timedelta(days=1)).isoformat()


class _FrappeError(Exception):
    pass


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: dt.date.today() if v in (None, "") else dt.date.fromisoformat(str(v)[:10])
    utils.now_datetime = lambda: dt.datetime(2026, 9, 11, 8, 0, 0)
    mod.utils = utils

    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.get_roles = lambda user=None: ["Employee"]
    mod.db = None
    mod.throw = lambda msg, exc=_FrappeError, *a, **k: (_ for _ in ()).throw(exc(msg))
    mod.flags = types.SimpleNamespace()

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "emp.demo@gege.demo"
    return mod


class _D(dict):
    """frappe._dict stand-in — attribute AND item access."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


class _FakeDoc:
    """Insert() mirrors Frappe autoname: SW-##### is minted on insert."""

    _seq = [0]

    def __init__(self, payload):
        self.__dict__.update(payload)
        self.doctype = payload.get("doctype")
        if not getattr(self, "status", None):
            self.status = "Open"  # Select default applied by Frappe on insert
        self.inserted = False
        self.saved = False

    def insert(self, ignore_permissions=False):
        type(self)._seq[0] += 1
        if not getattr(self, "name", None):
            self.name = f"SW-{type(self)._seq[0]:05d}"
        self.inserted = True
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self


@pytest.fixture
def fake(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.company_board")
    monkeypatch.setattr(api, "frappe", stub)

    # admin helpers isolated
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")
    audits = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audits.append((a, k)))

    class _H:
        pass

    h = _H()
    h.api = api
    h.stub = stub
    h.audits = audits
    h.created_docs = []
    h.employees = [
        {"name": "HR-EMP-00001", "employee_name": "An", "department": "IT", "holiday_list": "HL"},
        {"name": "HR-EMP-00002", "employee_name": "Bình", "department": "IT", "holiday_list": "HL"},
    ]
    h.instances = []
    h.leaves = []
    h.holidays = []
    h.values = {}  # db.get_value lookup {(doctype, name): {..}}
    h.exists_map = {}

    def _wrap(rows):
        return [_D(r) if isinstance(r, dict) else r for r in (rows or [])]

    def fake_get_all(doctype, filters=None, fields=None, **kw):
        if doctype == "Employee":
            return _wrap(h.employees)
        if doctype == "VN Employee Shift Instance":
            return _wrap(h.instances)
        if doctype == "Leave Application":
            return _wrap(h.leaves)
        if doctype == "Holiday":
            return _wrap(h.holidays)
        if doctype == "VN Shift Swap Request":
            return []
        return []

    class _DB:
        @staticmethod
        def get_value(doctype, name, fields=None, as_dict=False):
            if isinstance(name, dict):
                # viewer lookup: Employee by user_id → attribute-style object
                if doctype == "Employee" and name.get("user_id") == "emp.demo@gege.demo":
                    return types.SimpleNamespace(name="HR-EMP-00001", employee_name="An")
                return None
            v = h.values.get((doctype, name))
            if as_dict:
                return types.SimpleNamespace(**(v or {}))
            return v

        @staticmethod
        def exists(doctype, name):
            return h.exists_map.get((doctype, name))

        @staticmethod
        def commit():
            pass

    monkeypatch.setattr(stub, "get_all", fake_get_all)
    monkeypatch.setattr(stub, "db", _DB)

    def fake_get_doc(payload_or_name, name=None):
        if isinstance(payload_or_name, dict):
            doc = _FakeDoc(payload_or_name)
            h.created_docs.append(doc)
            return doc
        return h._loaded_docs[(payload_or_name, name)]

    h._loaded_docs = {}
    monkeypatch.setattr(stub, "get_doc", fake_get_doc)
    return h


def _inst(name, emp, date, shift):
    return {"name": name, "employee": emp, "work_date": date, "shift_type": shift, "shift_name": shift}


# --------------------------------------------------------------------------- #
# board()
# --------------------------------------------------------------------------- #
def test_board_composes_shifts_leaves_and_counts(fake):
    fake.instances = [
        _inst("SI-1", "HR-EMP-00001", TOMORROW, "Ca sáng"),
        _inst("SI-2", "HR-EMP-00002", TOMORROW, "Ca tối"),
    ]
    fake.leaves = [
        {
            "employee": "HR-EMP-00002",
            "from_date": TOMORROW,
            "to_date": DAY_AFTER,
            "leave_type": "Phép ốm",
            "status": "Approved",
        },
    ]
    fake.holidays = [{"holiday_date": DAY_AFTER, "description": "Lễ"}]

    out = fake.api.board(TOMORROW, DAY_AFTER)
    days = {e["employee"]: e["days"] for e in out["employees"]}
    assert days["HR-EMP-00001"][TOMORROW]["shift"] == "Ca sáng"
    # E2 duoc len lich ca TOMORROW (shift wins) — leave chi an DAY_AFTER.
    assert days["HR-EMP-00002"][TOMORROW]["shift"] == "Ca tối"
    assert days["HR-EMP-00002"][DAY_AFTER]["leave"] == "Phép ốm"
    assert out["leave_counts"].get(TOMORROW, 0) == 0
    assert out["leave_counts"][DAY_AFTER] == 1
    assert out["holidays"][DAY_AFTER] == "Lễ"
    assert out["working_counts"][TOMORROW] == 2


def test_board_scheduled_day_wins_over_overlapping_leave(fake):
    fake.instances = [_inst("SI-1", "HR-EMP-00001", TOMORROW, "Ca sáng")]
    fake.leaves = [
        {
            "employee": "HR-EMP-00001",
            "from_date": TOMORROW,
            "to_date": TOMORROW,
            "leave_type": "Phép năm",
            "status": "Open",
        },
    ]
    out = fake.api.board(TOMORROW, TOMORROW)
    days = {e["employee"]: e["days"] for e in out["employees"]}
    assert days["HR-EMP-00001"][TOMORROW]["shift"] == "Ca sáng"
    assert out["leave_counts"].get(TOMORROW, 0) == 0


# --------------------------------------------------------------------------- #
# create_swap_request()
# --------------------------------------------------------------------------- #
def test_create_swap_rejects_foreign_instance(fake):
    fake.values = {
        ("VN Employee Shift Instance", "SI-OTHER"): {
            "employee": "HR-EMP-00002",
            "work_date": TOMORROW,
            "shift_type": "Ca tối",
        },
    }
    with pytest.raises(_FrappeError):
        fake.api.create_swap_request(
            from_instance="SI-OTHER", target_instance="SI-X", reason="Đổi ca đi khám"
        )


def test_create_swap_rejects_past_date(fake):
    fake.values = {
        ("VN Employee Shift Instance", "SI-A"): {
            "employee": "HR-EMP-00001",
            "work_date": YESTERDAY,
            "shift_type": "Ca sáng",
        },
        ("VN Employee Shift Instance", "SI-B"): {
            "employee": "HR-EMP-00002",
            "work_date": TOMORROW,
            "shift_type": "Ca tối",
        },
    }
    with pytest.raises(_FrappeError):
        fake.api.create_swap_request(
            from_instance="SI-A", target_instance="SI-B", reason="Đổi ca ngày đã qua"
        )


def test_create_swap_rejects_short_reason(fake):
    fake.values = {
        ("VN Employee Shift Instance", "SI-A"): {
            "employee": "HR-EMP-00001",
            "work_date": TOMORROW,
            "shift_type": "Ca sáng",
        },
        ("VN Employee Shift Instance", "SI-B"): {
            "employee": "HR-EMP-00002",
            "work_date": DAY_AFTER,
            "shift_type": "Ca tối",
        },
    }
    with pytest.raises(_FrappeError):
        fake.api.create_swap_request(from_instance="SI-A", target_instance="SI-B", reason="ok")


def test_create_swap_success(fake):
    fake.values = {
        ("VN Employee Shift Instance", "SI-A"): {
            "employee": "HR-EMP-00001",
            "work_date": TOMORROW,
            "shift_type": "Ca sáng",
        },
        ("VN Employee Shift Instance", "SI-B"): {
            "employee": "HR-EMP-00002",
            "work_date": DAY_AFTER,
            "shift_type": "Ca tối",
        },
    }
    out = fake.api.create_swap_request(
        from_instance="SI-A", target_instance="SI-B", reason="Có việc gia đình"
    )
    doc = fake.created_docs[0]
    assert doc.inserted is True
    assert doc.employee == "HR-EMP-00001"
    assert doc.target_employee == "HR-EMP-00002"
    assert doc.from_date == TOMORROW
    assert doc.target_date == DAY_AFTER
    assert out["status"] == "Open"
    assert fake.audits  # audit emitted


# --------------------------------------------------------------------------- #
# decide_swap_request()
# --------------------------------------------------------------------------- #
def test_decide_approve_swaps_and_marks(fake, monkeypatch):
    admin = importlib.import_module("gege_hr.gege_hr.api.admin")
    swapped = []
    monkeypatch.setattr(admin, "swap_shift_days", lambda a, b: swapped.append((a, b)) or {})

    doc = _FakeDoc(
        {
            "doctype": "VN Shift Swap Request",
            "name": "SW-00001",
            "status": "Open",
            "employee": "HR-EMP-00001",
            "from_instance": "SI-A",
            "target_instance": "SI-B",
            "from_date": TOMORROW,
            "target_date": DAY_AFTER,
        }
    )
    fake._loaded_docs[("VN Shift Swap Request", "SW-00001")] = doc

    out = fake.api.decide_swap_request("SW-00001", "approve", note="OK")
    assert swapped == [("SI-A", "SI-B")]
    assert doc.status == "Approved"
    assert doc.saved is True
    assert out["status"] == "Approved"


def test_decide_reject_no_swap(fake, monkeypatch):
    admin = importlib.import_module("gege_hr.gege_hr.api.admin")
    swapped = []
    monkeypatch.setattr(admin, "swap_shift_days", lambda a, b: swapped.append((a, b)) or {})

    doc = _FakeDoc(
        {
            "doctype": "VN Shift Swap Request",
            "name": "SW-00002",
            "status": "Open",
            "employee": "HR-EMP-00001",
            "from_instance": "SI-A",
            "target_instance": "SI-B",
            "from_date": TOMORROW,
            "target_date": DAY_AFTER,
        }
    )
    fake._loaded_docs[("VN Shift Swap Request", "SW-00002")] = doc
    fake.api.decide_swap_request("SW-00002", "reject", note="Không đủ người")
    assert swapped == []
    assert doc.status == "Rejected"
