"""Bench-free regression tests: catalog CRUD must not round coordinates.

``VN Work Location.latitude/longitude`` store GPS coordinates that the
geofence check (``api/attendance._enforce_geofence``) compares against the
phone's live GPS. Rounding to 2 decimals at save time shifts the "office"
by up to ~1.6 km and silently blocks every check-in (real incident
2026-09-10: 10.7979842 was saved as 10.79 → 1,616 m away).

These tests pin ``save_catalog_master`` (update AND create paths) so the
7-decimal payload reaching ``doc.set()`` is bit-identical to what the SPA
sent — the only rounding allowed is Frappe's own DocField ``precision``
(8 for both fields) at the ORM layer, which is far beyond GPS accuracy.

Mirrors the stub-frappe harness of ``test_holiday_master``.
"""

from __future__ import annotations

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
    utils.getdate = lambda v=None: v
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils

    mod.db = None
    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.delete_doc = lambda *a, **k: None
    mod.get_meta = lambda doctype: types.SimpleNamespace(autoname="")
    mod.throw = lambda msg, exc=_FrappeError, *a, **k: (_ for _ in ()).throw(exc(msg))

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    mod.local = _S()
    mod.local.request_ip = None
    return mod


class _FakeDoc:
    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.inserted = False
        self.saved = False

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
        self.exists_map = {}

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)


@pytest.fixture
def fake(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.catalog_master")
    monkeypatch.setattr(api, "frappe", stub)

    class _Harness:
        pass

    h = _Harness()
    h.existing = {}  # {(doctype, name): _FakeDoc}
    h.created = []

    def fake_get_doc(payload_or_doctype, name=None):
        if name is not None:
            return h.existing[(payload_or_doctype, name)]
        doc = _FakeDoc(payload_or_doctype)
        h.created.append(doc)
        return doc

    monkeypatch.setattr(stub, "db", _FakeDB())
    monkeypatch.setattr(stub, "get_doc", fake_get_doc)
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    h.audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: h.audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")
    h.api = api
    return h


LAT = 10.7979842
LNG = 106.6623683


def test_update_preserves_seven_decimal_coordinates(fake):
    doc = _FakeDoc(
        {"latitude": 10.79, "longitude": 106.65, "location_name": "126 Ngo Thi Thu Minh"},
        name="126 Ngo Thi Thu Minh",
    )
    fake.existing[("VN Work Location", doc.name)] = doc

    fake.api.save_catalog_master(
        "VN Work Location",
        {"name": doc.name, "latitude": LAT, "longitude": LNG},
        is_new=0,
    )

    assert doc.saved is True
    # Bit-identical pass-through — any rounding here moves the geofence.
    assert doc.latitude == LAT
    assert doc.longitude == LNG


def test_create_preserves_seven_decimal_coordinates(fake):
    fake.api.save_catalog_master(
        "VN Work Location",
        {"location_name": "Chi nhánh Q1", "latitude": LAT, "longitude": LNG},
        is_new=1,
    )

    assert len(fake.created) == 1
    created = fake.created[0]
    assert created.inserted is True
    assert created.latitude == LAT
    assert created.longitude == LNG


def test_create_drops_meta_keys_from_payload(fake):
    fake.api.save_catalog_master(
        "VN Work Location",
        {
            "location_name": "Chi nhánh Q7",
            "latitude": LAT,
            "longitude": LNG,
            # internal Frappe keys must never be written onto the doc
            "modified_by": "someone@x",
            "__islocal": True,
            "__unsaved": True,
        },
        is_new=1,
    )

    created = fake.created[0]
    assert getattr(created, "modified_by", None) is None
    assert getattr(created, "__islocal", None) is None
    assert getattr(created, "__unsaved", None) is None
    assert created.latitude == LAT
