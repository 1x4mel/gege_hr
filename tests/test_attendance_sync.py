"""Bench-free unit tests for ``api/attendance_sync.py`` (FIX-1, hr-gap-audit.md I-1).

Mirrors the stub-frappe pattern of ``test_pagination.py``:
``monkeypatch.setitem(sys.modules, "frappe", stub)`` so the module's lazy
``import frappe`` resolves to the stub and never touches a real bench.

Covers the FIX-1 test matrix: status mapping (Present / Absent / Half Day / Leave /
missing logs / Error / Pending), idempotent upsert, no-employee skip, submit-on-lock,
backfill range + permission gate.
"""

import importlib
import sys
import types

import pytest


class _PermissionError(Exception):
    """Stand-in for ``frappe.PermissionError`` raised by ``only_for``."""


class _AttrDict(dict):
    """Mimics frappe ``_dict``: attribute access on top of a plain dict."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None


class _Doc:
    """A minimal fake doctype document: insert/submit/save + name/docstatus."""

    def __init__(self, store, payload):
        self._store = store
        self._payload = dict(payload)
        self.name = payload.get("name")
        self.docstatus = _as_int(payload.get("docstatus"), 0)
        for k, v in payload.items():
            setattr(self, k, v)

    def insert(self, ignore_permissions=False):
        if not self.name:
            self.name = f"ATT-{len(self._store) + 1:04d}"
        row = {"name": self.name, "docstatus": self.docstatus}
        row.update({k: v for k, v in self._payload.items() if k != "doctype"})
        self._store[self.name] = row
        return self

    def submit(self):
        self._store[self.name]["docstatus"] = 1
        self.docstatus = 1
        return self

    def save(self, *a, **k):
        return self


class _DB:
    def __init__(self, fr):
        self.fr = fr

    def _store(self, doctype):
        return self.fr.stores.setdefault(doctype, {})

    def get_value(self, doctype, key, fields=None, as_dict=False):
        store = self._store(doctype)
        row = None
        if isinstance(key, dict):
            for r in store.values():
                if all(r.get(k) == v for k, v in key.items()):
                    row = r
                    break
        else:
            row = store.get(key)
        if row is None:
            return None
        if fields is None:
            return _AttrDict(row) if as_dict else dict(row)
        if isinstance(fields, (list, tuple)):
            d = {f: row.get(f) for f in fields}
            return _AttrDict(d) if as_dict else (d[fields[0]] if len(fields) == 1 else d)
        return row.get(fields)

    def set_value(self, doctype, name, fields, *a, **k):
        store = self._store(doctype)
        store.setdefault(name, {"name": name}).update(fields or {})
        self.fr.set_calls.append((doctype, name, dict(fields or {})))

    def get_all(self, doctype, filters=None, pluck=None, fields=None, **k):
        rows = list(self._store(doctype).values())
        out = []
        for r in rows:
            if self._match(r, filters):
                out.append(r)
        if pluck:
            return [r.get(pluck) for r in out]
        return [{f: r.get(f) for f in fields} for f in (fields or [])] if fields else out

    @staticmethod
    def _match(row, filters):
        if not filters:
            return True
        if isinstance(filters, dict):
            return all(row.get(k) == v for k, v in filters.items())
        # list-of-conditions form: [["field", op, value], ...]
        for cond in filters:
            field, op, value = cond[0], cond[1], cond[2]
            rv = row.get(field)
            if op == ">=" and not (rv is not None and rv >= value):
                return False
            if op == "<=" and not (rv is not None and rv <= value):
                return False
            if op == "=" and rv != value:
                return False
        return True


class _Frappe:
    def __init__(self):
        self.stores = {}
        self.set_calls = []
        self.submitted = []
        self.roles = ["HR Manager"]
        self.db = _DB(self)

    def whitelist(self, fn=None, **kw):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s
    def log_error(self, *a, **k):
        return None

    def only_for(self, roles):
        if not (set(roles) & set(self.roles)):
            raise _PermissionError("not allowed")

    def get_doc(self, payload_or_name, maybe_name=None):
        if isinstance(payload_or_name, dict):
            payload = payload_or_name
            store = self.stores.setdefault(payload.get("doctype"), {})
            doc = _Doc(store, payload)
            return doc
        # get_doc(doctype, name) → wrap the stored row so submit() hits the store
        store = self.stores.setdefault(payload_or_name, {})
        row = store.get(maybe_name, {"name": maybe_name, "docstatus": 0})
        outer = self

        class _Wrap:
            def __init__(self):
                self.name = maybe_name
                self.docstatus = row.get("docstatus", 0)

            def __getattr__(self, attr):
                # Field reads fall through to the stored row (F25 per-field
                # update path compares current values via getattr).
                try:
                    return self.__dict__[attr]
                except KeyError:
                    return row.get(attr)

            def db_set(self, fieldname, value, **_kw):
                store.setdefault(maybe_name, {"name": maybe_name})[fieldname] = value
                return self

            def submit(self):
                store.setdefault(maybe_name, {"name": maybe_name})["docstatus"] = 1
                outer.submitted.append(maybe_name)
                return self

        return _Wrap()

    def get_all(self, doctype, **k):
        return self.db.get_all(doctype, **k)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.attendance_sync")
    importlib.reload(m)
    return m, stub


def _ws(**overrides):
    base = {
        "name": "WS-00001-00001",
        "employee": "HR-EMP-00001",
        "work_date": "2026-08-08",
        "shift_type": "Ca hành chính",
        "company": "Gege",
        "calculation_status": "Calculated",
        "absent": 0,
        "has_leave": 0,
        "payable_day": 1.0,
        "leave_application": None,
        "leave_type": None,
    }
    base.update(overrides)
    return base


# ── pure mapping ────────────────────────────────────────────────────────────
def test_status_present(mod):
    m, _ = mod
    assert m.attendance_status_for(_ws()) == "Present"


def test_status_absent(mod):
    m, _ = mod
    assert m.attendance_status_for(_ws(absent=1, payable_day=0)) == "Absent"


def test_status_half_day(mod):
    m, _ = mod
    assert m.attendance_status_for(_ws(payable_day=0.5)) == "Half Day"


def test_status_leave_is_present_with_link(mod):
    m, _ = mod
    ws = _ws(has_leave=1, payable_day=1.0, leave_application="LA-001", leave_type="Năm")
    assert m.attendance_status_for(ws) == "Present"
    fields = m.build_attendance_fields(ws)
    assert fields["leave_application"] == "LA-001"
    assert fields["leave_type"] == "Năm"


def test_status_missing_logs_still_present(mod):
    m, _ = mod
    # need_review / missing logs → Present (not skipped, not Absent)
    assert m.attendance_status_for(_ws(payable_day=0)) == "Present"


def test_status_error_skipped(mod):
    m, _ = mod
    assert m.attendance_status_for(_ws(calculation_status="Error")) is None


def test_status_pending_skipped(mod):
    m, _ = mod
    assert m.attendance_status_for(_ws(calculation_status="Pending")) is None


def test_build_fields_shape(mod):
    m, _ = mod
    fields = m.build_attendance_fields(_ws())
    assert fields["employee"] == "HR-EMP-00001"
    assert fields["attendance_date"] == "2026-08-08"
    assert fields["status"] == "Present"
    assert fields["shift"] == "Ca hành chính"
    assert fields["vn_work_session"] == "WS-00001-00001"


# ── I/O: upsert ─────────────────────────────────────────────────────────────
def test_sync_creates_new_attendance(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-00001-00001": _ws()}
    name = m.sync_attendance("WS-00001-00001")
    assert name
    atts = stub.stores[m.ATTENDANCE_DOCTYPE]
    assert len(atts) == 1
    row = next(iter(atts.values()))
    assert row["status"] == "Present"
    assert row["employee"] == "HR-EMP-00001"
    # back-link on the Work Session
    assert stub.stores[m.WORK_SESSION_DOCTYPE]["WS-00001-00001"]["attendance"] == name


def test_sync_idempotent_update(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-00001-00001": _ws()}
    first = m.sync_attendance("WS-00001-00001")
    # recalc → now Absent, sync again → updates the SAME row, no duplicate
    stub.stores[m.WORK_SESSION_DOCTYPE]["WS-00001-00001"]["absent"] = 1
    second = m.sync_attendance("WS-00001-00001")
    assert first == second
    assert len(stub.stores[m.ATTENDANCE_DOCTYPE]) == 1
    row = next(iter(stub.stores[m.ATTENDANCE_DOCTYPE].values()))
    assert row["status"] == "Absent"


def test_sync_skips_when_no_employee(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-x": _ws(employee=None)}
    assert m.sync_attendance("WS-x") is None
    assert m.ATTENDANCE_DOCTYPE not in stub.stores or not stub.stores[m.ATTENDANCE_DOCTYPE]


def test_sync_skips_error_status(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-x": _ws(calculation_status="Error")}
    assert m.sync_attendance("WS-x") is None


def test_sync_submits_when_period_locked(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: True)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-00001-00001": _ws()}
    name = m.sync_attendance("WS-00001-00001")
    assert name in stub.submitted
    assert stub.stores[m.ATTENDANCE_DOCTYPE][name]["docstatus"] == 1


def test_sync_no_submit_when_not_locked(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.stores[m.WORK_SESSION_DOCTYPE] = {"WS-00001-00001": _ws()}
    name = m.sync_attendance("WS-00001-00001")
    assert stub.submitted == []
    assert stub.stores[m.ATTENDANCE_DOCTYPE][name]["docstatus"] == 0


# ── backfill ────────────────────────────────────────────────────────────────
def test_backfill_syncs_range(mod, monkeypatch):
    m, stub = mod
    monkeypatch.setattr(m, "is_work_date_locked", lambda date: False)
    stub.roles = ["HR Manager"]
    stub.stores[m.WORK_SESSION_DOCTYPE] = {
        "WS-1": _ws(name="WS-1", employee="E1", work_date="2026-08-01"),
        "WS-2": _ws(name="WS-2", employee="E2", work_date="2026-08-02"),
        "WS-3": _ws(name="WS-3", employee="E3", work_date="2026-09-01"),  # outside range
    }
    res = m.backfill_attendance("2026-08-01", "2026-08-31")
    assert res["synced"] == 2
    assert res["total"] == 2  # WS-3 excluded by the range filter


def test_backfill_permission_denied_for_employee(mod):
    m, stub = mod
    stub.roles = ["Employee"]  # not HR Manager / System Manager
    with pytest.raises(_PermissionError):
        m.backfill_attendance("2026-08-01", "2026-08-31")
