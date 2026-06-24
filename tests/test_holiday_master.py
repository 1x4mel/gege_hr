"""Bench-free unit tests for the Holiday List Master API (``api/holiday_master.py``).

Targets the dedicated Holiday List wrapper layer using a stub ``frappe``
injected into ``sys.modules`` for the duration of each test only, exactly like
``test_payroll_master``.

The permission/audit helpers imported from ``api/admin`` are stubbed out so the
tests stay focused on holiday_master's own validation, coercion and branching
logic rather than re-testing admin.py.
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/holiday_master`` + ``api/admin`` to
# import (module-level decorators + ``from frappe import _``) and run.
# --------------------------------------------------------------------------- #
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

    # Runtime bits swapped per-test; inert defaults here.
    mod.db = None
    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.delete_doc = lambda *a, **k: None
    mod.throw = lambda msg, exc=_FrappeError, *args, **kwargs: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    mod.local = _S()
    mod.local.request_ip = None
    return mod


# --------------------------------------------------------------------------- #
# Fake frappe harness (per-test)
# --------------------------------------------------------------------------- #
class _FakeDoc:
    """A minimal Frappe document stub supporting insert/save/set/get."""

    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.holidays = payload.get("holidays", [])
        self.inserted = False
        self.saved = False
        self.deleted = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeDB:
    def __init__(self):
        self.exists_map = {}  # {(doctype, name): bool}

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()
        self.docs_created = []
        self.deleted = []
        self._next_id = 1
        self._existing = {}  # {(doctype,name): _FakeDoc} for get_doc(name)

    def get_doc(self, payload_or_doctype, name=None):
        if name is not None:
            return self._existing[(payload_or_doctype, name)]
        doc = _FakeDoc(payload_or_doctype, name=f"NEW-{self._next_id:04d}")
        self._next_id += 1
        self.docs_created.append(doc)
        return doc

    def get_all(self, doctype, **kwargs):
        return self._all_rows.get(doctype, [])

    def delete_doc(self, doctype, name, **kwargs):
        self.deleted.append((doctype, name))

    def throw(self, msg, exc=_FrappeError, *a, **k):
        raise exc(msg)


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import holiday_master, wire a fake + stub admin helpers."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.holiday_master")
    monkeypatch.setattr(api, "frappe", stub)

    harness = _FakeFrappe()
    harness._all_rows = {}
    monkeypatch.setattr(stub, "db", harness.db)
    monkeypatch.setattr(stub, "get_doc", harness.get_doc)
    monkeypatch.setattr(stub, "get_all", harness.get_all)
    monkeypatch.setattr(stub, "delete_doc", harness.delete_doc)
    monkeypatch.setattr(stub, "throw", harness.throw)

    # Stub the admin helpers so holiday_master logic is isolated.
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")

    harness.api = api
    harness.stub = stub
    harness.audit_calls = audit_calls
    harness.set_rows = lambda doctype, rows: harness._all_rows.__setitem__(doctype, rows)
    return harness


# --------------------------------------------------------------------------- #
# _coerce_bool / _clean_holidays helpers
# --------------------------------------------------------------------------- #
def test_coerce_bool_accepts_js_and_python_truthy(fake):
    api = fake.api
    assert api._coerce_bool(True) is True
    assert api._coerce_bool("true") is True
    assert api._coerce_bool("True") is True
    assert api._coerce_bool(1) is True
    assert api._coerce_bool("1") is True
    assert api._coerce_bool(False) is False
    assert api._coerce_bool(0) is False
    assert api._coerce_bool("no") is False


def test_clean_holidays_drops_blank_rows_and_meta(fake):
    api = fake.api
    rows = [
        {"holiday_date": "2026-01-01", "description": "Tết Dương"},
        {"holiday_date": "", "description": "blank date"},
        {"holiday_date": "   ", "description": "whitespace date"},
        {"description": "missing date"},
        "not-a-dict",
        {"holiday_date": "2026-04-30", "description": "Giải phóng"},
    ]
    out = api._clean_holidays(rows)
    assert len(out) == 2
    assert out[0]["holiday_date"] == "2026-01-01"
    assert out[1]["holiday_date"] == "2026-04-30"
    # meta keys absent in payload must not appear
    assert set(out[0].keys()) == {"holiday_date", "description", "weekly_off"}


def test_clean_holidays_coerces_bool_and_truncates_date(fake):
    api = fake.api
    rows = [
        {"holiday_date": "2026-09-02T00:00:00.000Z", "description": " Quốc khánh ", "weekly_off": "true"},
    ]
    out = api._clean_holidays(rows)
    assert out[0]["holiday_date"] == "2026-09-02"
    assert out[0]["description"] == "Quốc khánh"
    assert out[0]["weekly_off"] is True


# --------------------------------------------------------------------------- #
# list / get
# --------------------------------------------------------------------------- #
def test_list_holiday_lists_returns_rows(fake):
    api = fake.api
    fake.set_rows(
        "Holiday List",
        [
            {"name": "HL-2026", "from_date": "2026-01-01", "to_date": "2026-12-31"},
        ],
    )
    rows = api.list_holiday_lists()
    assert rows == [{"name": "HL-2026", "from_date": "2026-01-01", "to_date": "2026-12-31"}]


def test_get_holiday_list_unknown_raises(fake):
    api = fake.api
    fake.db.exists_map = {("Holiday List", "NOPE"): False}
    with pytest.raises(_FrappeError):
        api.get_holiday_list("NOPE")


def test_get_holiday_list_returns_children(fake):
    api = fake.api
    doc = _FakeDoc(
        {
            "name": "HL-2026",
            "holiday_list_name": "Lễ 2026",
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "holidays": [
                {"holiday_date": "2026-01-01", "description": "Tết Dương"},
            ],
        }
    )
    fake._existing[("Holiday List", "HL-2026")] = doc
    res = api.get_holiday_list("HL-2026")
    assert res["name"] == "HL-2026"
    assert res["holiday_list_name"] == "Lễ 2026"
    assert res["holidays"][0]["description"] == "Tết Dương"


# --------------------------------------------------------------------------- #
# save
# --------------------------------------------------------------------------- #
def test_save_holiday_list_requires_name(fake):
    api = fake.api
    with pytest.raises(_FrappeError):
        api.save_holiday_list(holiday_list_name="", holidays=[{"holiday_date": "2026-01-01"}])


def test_save_holiday_list_requires_at_least_one_holiday(fake):
    api = fake.api
    with pytest.raises(_FrappeError):
        api.save_holiday_list(holiday_list_name="Lễ 2026", holidays=[])


def test_save_holiday_list_creates_new_and_audits(fake):
    api = fake.api
    res = api.save_holiday_list(
        holiday_list_name="Lễ 2026",
        from_date="2026-01-01",
        to_date="2026-12-31",
        holidays=[
            {"holiday_date": "2026-01-01", "description": "Tết Dương"},
            {"holiday_date": "2026-09-02", "description": "Quốc khánh"},
        ],
    )
    assert res["name"] == "NEW-0001"
    assert len(fake.docs_created) == 1
    doc = fake.docs_created[0]
    assert doc.inserted is True
    assert doc.holiday_list_name == "Lễ 2026"
    assert len(doc.holidays) == 2
    assert len(fake.audit_calls) == 1
    args, kwargs = fake.audit_calls[0]
    assert kwargs["reference_doctype"] == "Holiday List"
    assert kwargs["reference_name"] == "NEW-0001"


def test_save_holiday_list_updates_existing(fake):
    api = fake.api
    doc = _FakeDoc(
        {
            "name": "HL-2026",
            "holiday_list_name": "Lễ 2026",
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "holidays": [{"holiday_date": "2026-01-01", "description": "Tết Dương"}],
        }
    )
    fake._existing[("Holiday List", "HL-2026")] = doc
    res = api.save_holiday_list(
        name="HL-2026",
        holiday_list_name="Lễ 2026 (revised)",
        from_date="2026-01-01",
        to_date="2026-12-31",
        holidays=[
            {"holiday_date": "2026-01-01", "description": "Tết Dương"},
            {"holiday_date": "2026-09-02", "description": "Quốc khánh"},
        ],
    )
    assert res["name"] == "HL-2026"
    assert doc.saved is True
    assert doc.holiday_list_name == "Lễ 2026 (revised)"
    assert len(doc.holidays) == 2
    assert len(fake.docs_created) == 0


def test_save_holiday_list_update_unknown_raises(fake):
    api = fake.api
    fake.db.exists_map = {("Holiday List", "NOPE"): False}
    with pytest.raises(_FrappeError):
        api.save_holiday_list(
            name="NOPE",
            holiday_list_name="Lễ 2026",
            holidays=[{"holiday_date": "2026-01-01"}],
        )


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
def test_delete_holiday_list_unknown_raises(fake):
    api = fake.api
    fake.db.exists_map = {("Holiday List", "NOPE"): False}
    with pytest.raises(_FrappeError):
        api.delete_holiday_list("NOPE")


def test_delete_holiday_list_deletes_and_audits(fake):
    api = fake.api
    res = api.delete_holiday_list("HL-2026")
    assert res == {"name": "HL-2026", "deleted": True}
    assert fake.deleted == [("Holiday List", "HL-2026")]
    assert len(fake.audit_calls) == 1
