"""Bench-free unit tests for the desk-free /hr/schedule feature
(plans/plan-schedule-desk-free.md §4.1 — cases B1–B20).

Mirrors the stub-frappe harness of ``test_shift_assignment.py`` /
``test_hardening.py``, extended with what this feature needs:
  * role-aware ``only_for`` / ``get_roles`` (schedule-context gating + the
    hardened ``my_schedule``),
  * a write-back ``_RecDoc`` — ``save()``/``cancel()`` update ``db.rows`` so a
    re-query after an allow_on_submit edit (override-day cut) sees new values,
  * ``_FakeDB2`` honouring the ``or_filters`` shapes used by the overlap
    queries (``end_date >= x`` / ``end_date is not set`` / ``!=``) like real
    Frappe does.
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest

SHIFT_API = "gege_hr.gege_hr.api.shift"
ADMIN_API = "gege_hr.gege_hr.api.admin"


def _dt(h, m=0):
    return datetime.datetime(2026, 1, 1, h, m)


def _future(days=3):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


class _DotDict(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _RecDoc:
    """Fake document whose ``save()`` / ``cancel()`` write back into db.rows.

    The override-day flow re-queries Shift Assignments after cutting an
    assignment's ``end_date`` — the write-back makes the stub behave like a
    real database for that re-query.
    """

    def __init__(self, payload, name=None, db=None, doctype="Shift Assignment"):
        if isinstance(payload, dict):
            self.__dict__.update(payload)
        self.name = (payload.get("name") if isinstance(payload, dict) else None) or name or "NEW-0001"
        self._db = db
        self._doctype = doctype
        self.inserted = self.saved = self.submitted = self.cancelled = False

    def _write_back(self):
        if self._db is None:
            return
        for r in self._db.rows.get(self._doctype, []):
            if isinstance(r, dict) and r.get("name") == self.name:
                r.update({k: v for k, v in self.__dict__.items() if not k.startswith("_") and k in r})
                break

    def insert(self, **k):
        self.inserted = True
        return self

    def submit(self):
        self.submitted = True
        return self

    def save(self, **k):
        self.saved = True
        self._write_back()
        return self

    def cancel(self):
        self.cancelled = True
        self.docstatus = 2
        self._write_back()
        return self


class _FakeDB2:
    """FakeDB with or_filters + ``!=`` support (the shapes the overlap queries use)."""

    def __init__(self):
        self.rows = {}  # doctype -> list[dict]
        self.values = {}  # (doctype, name) -> dict
        self.settings = {}  # (setting, key) -> value
        self.exists_set = set()
        self.calls = []

    def _eq_and_excl(self, kw):
        filters = kw.get("filters")
        eq: dict = {}
        excl: list = []
        if isinstance(filters, list):
            for f in filters:
                if isinstance(f, (list, tuple)) and len(f) == 3:
                    if f[1] == "=":
                        eq[f[0]] = f[2]
                    elif f[1] == "!=":
                        excl.append((f[0], f[2]))
        elif isinstance(filters, dict):
            eq = {k: v for k, v in filters.items() if not isinstance(v, (list, tuple))}
        return eq, excl

    def _or_ok(self, row, kw):
        or_filters = kw.get("or_filters")
        if not or_filters:
            return True
        for cond in or_filters:
            if not (isinstance(cond, (list, tuple)) and len(cond) == 3):
                continue
            f, op, val = cond
            rv = row.get(f)
            if op == ">=" and rv is not None and str(rv) >= str(val):
                return True
            if op == "is" and val == "not set" and (rv is None or rv == ""):
                return True
        return False

    def get_all(self, doctype, **kw):
        self.calls.append({"doctype": doctype, **kw})
        base = [r for r in self.rows.get(doctype, []) if isinstance(r, dict)]
        eq, excl = self._eq_and_excl(kw)
        if eq:
            base = [r for r in base if all(r.get(k) == v for k, v in eq.items())]
        for f, v in excl:
            base = [r for r in base if r.get(f) != v]
        base = [r for r in base if self._or_ok(r, kw)]
        pluck = kw.get("pluck")
        if pluck:
            return [r.get(pluck) for r in base]
        return [_DotDict(r) for r in base]

    def get_value(self, doctype, name, fields=None, as_dict=False):
        v = self.values.get((doctype, name))
        if v is None:
            return None
        if as_dict:
            return _DotDict(v)
        if isinstance(fields, str):
            return v.get(fields)
        return v

    def get_single_value(self, setting, key):
        return self.settings.get((setting, key))

    def exists(self, doctype, name):
        return (doctype, name) in self.exists_set


class _ShiftStub:
    """frappe stub for api.shift — role-aware (only_for / get_roles)."""

    def __init__(self, db):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.db = db
        self.get_all = db.get_all
        self.get_value = db.get_value
        self.session = types.SimpleNamespace(user="emp1@gege.test")
        self._roles = {"Employee"}
        self._viewer_emp = "E-1"
        self.created = []
        self.deleted = []
        self.PermissionError = PermissionError
        # Minimal meta — my_schedule asks has_field("vn_work_location") and
        # _safe_sr_fields iterates meta.fields (Shift Request columns).
        _sr_fields = [
            types.SimpleNamespace(fieldname=f)
            for f in (
                "name",
                "shift_type",
                "from_date",
                "to_date",
                "status",
                "docstatus",
                "approver",
                "reason",
                "creation",
            )
        ]
        self.get_meta = lambda dt: types.SimpleNamespace(
            has_field=lambda f: False,
            fields=_sr_fields if dt == "Shift Request" else [],
        )

    def get_roles(self, user=None):
        return sorted(self._roles)

    def only_for(self, roles):
        if not (set(roles or []) & set(self._roles or [])):
            raise PermissionError("not permitted")

    def get_doc(self, payload, name=None):
        if isinstance(payload, str):
            v = self.db.values.get((payload, name))
            if v is not None:
                doc = _RecDoc(dict(v), name=name, db=self.db, doctype=payload)
                self.created.append(doc)
                return doc
            doc = _RecDoc({}, name=name or "NEW-0001", db=self.db, doctype=payload)
            self.created.append(doc)
            return doc
        doc = _RecDoc(payload, "NEW-0001", db=self.db)
        self.created.append(doc)
        return doc

    def get_cached_doc(self, doctype, name):
        v = self.db.values.get((doctype, name)) or {}
        return types.SimpleNamespace(**v)

    def delete_doc(self, doctype, name, **kw):
        self.deleted.append((doctype, name))

    def throw(self, msg, exc=Exception, *a, **k):
        raise exc(msg)

    def log_error(self, *a, **k):
        return None


class _AdminStub(_ShiftStub):
    """frappe stub for api.admin — HR gate always satisfied (existing pattern)."""

    def __init__(self, db, doc_map=None):
        super().__init__(db)
        self._roles = {"HR Manager"}
        self.session = types.SimpleNamespace(user="hr.manager@gege.test")
        self._doc_map = doc_map or {}

    def only_for(self, roles):
        return None  # allow (HR admin gate satisfied)

    def get_doc(self, payload, name=None):
        if isinstance(payload, str) and (payload, name) in self._doc_map:
            return self._doc_map[(payload, name)]
        return super().get_doc(payload, name)


def _utils():
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )

    def _add_days(v, days):
        return utils.getdate(v) + datetime.timedelta(days=days)

    def _flt(v, precision=None):
        try:
            out = float(v) if v not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0
        return round(out, precision) if precision is not None else out

    utils.add_days = _add_days
    utils.flt = _flt
    return utils


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def shift(monkeypatch):
    db = _FakeDB2()
    stub = _ShiftStub(db)
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    sys.modules.pop(SHIFT_API, None)
    mod = importlib.import_module(SHIFT_API)
    monkeypatch.setattr(mod, "frappe", stub)
    # Session-employee + audit/notify seams (audit lazy-imports the real audit
    # module — out of scope here, record instead).
    monkeypatch.setattr(mod.emp_utils, "get_employee_for_user", lambda user=None: stub._viewer_emp)
    audit_calls: list = []
    notify_calls: list = []
    monkeypatch.setattr(mod, "_audit_schedule", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(mod, "_notify_schedule_updated", lambda emp: notify_calls.append(emp))
    # tz helpers are pure display math here — stub them (B2 runs my_schedule
    # end-to-end).
    monkeypatch.setattr(
        mod,
        "tz_utils",
        types.SimpleNamespace(
            now_in_portal=lambda: datetime.datetime(2026, 9, 2, 8, 0),
            planned_window=lambda day, s, e: (
                datetime.datetime.combine(day, s),
                datetime.datetime.combine(day, e),
            ),
            wall=lambda dt: dt,
            is_overnight=lambda s, e: False,
        ),
    )
    return types.SimpleNamespace(mod=mod, stub=stub, db=db, audit=audit_calls, notify=notify_calls)


@pytest.fixture
def admin(monkeypatch):
    db = _FakeDB2()
    stub = _AdminStub(db)
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    sys.modules.pop(ADMIN_API, None)
    mod = importlib.import_module(ADMIN_API)
    monkeypatch.setattr(mod, "frappe", stub)
    monkeypatch.setattr(mod, "_audit_admin", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_company_for_employee", lambda employee: "CO-1")
    return types.SimpleNamespace(mod=mod, stub=stub, db=db)


# --------------------------------------------------------------------------- #
# B1–B2 — hardened my_schedule (§2.7)
# --------------------------------------------------------------------------- #
def test_my_schedule_denies_other_employee_for_plain_employee(shift):
    """B1: a plain Employee querying someone else's schedule → PermissionError."""
    shift.stub._roles = {"Employee"}
    with pytest.raises(PermissionError):
        shift.mod.my_schedule(employee="E-2")
    # Own schedule / no param must NOT trip the guard.
    shift.mod._assert_can_view_employee("E-1", "E-1")
    shift.mod._assert_can_view_employee(None, "E-1")


def test_my_schedule_allows_manager_to_view_others(shift):
    """B2: an HR Manager may query another employee's schedule (shift-only view)."""
    shift.stub._roles = {"HR Manager"}
    assert shift.mod._is_schedule_manager() is True
    shift.db.exists_set.add(("Employee", "E-2"))
    shift.db.rows["Shift Assignment"] = [
        {
            "name": "SA-X",
            "employee": "E-2",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-5),
            "end_date": _future(5),
        }
    ]
    shift.db.values[("Shift Type", "Day")] = {
        "start_time": datetime.time(9, 0),
        "end_time": datetime.time(17, 0),
    }
    rows = shift.mod.my_schedule(employee="E-2")
    assert isinstance(rows, list) and rows
    assert {r["shift_type"] for r in rows} == {"Day"}
    assert all(r["shift_assignment"] == "SA-X" for r in rows)


# --------------------------------------------------------------------------- #
# B3–B5 — schedule_context (§2.1)
# --------------------------------------------------------------------------- #
def _seed_requests(db):
    db.rows["Shift Request"] = [
        {  # own, in window
            "name": "SR-1",
            "employee": "E-1",
            "docstatus": 0,
            "status": "Draft",
            "from_date": _future(1),
            "to_date": _future(5),
        },
        {  # own, in window
            "name": "SR-2",
            "employee": "E-1",
            "docstatus": 0,
            "status": "Draft",
            "from_date": _future(2),
            "to_date": _future(2),
        },
        {  # own but far outside the ±90d window
            "name": "SR-OLD",
            "employee": "E-1",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2020-01-01",
            "to_date": "2020-01-02",
        },
        {  # someone else's draft
            "name": "SR-3",
            "employee": "E-2",
            "docstatus": 0,
            "status": "Draft",
            "from_date": _future(1),
            "to_date": _future(1),
        },
    ]


def test_schedule_context_plain_employee(shift):
    """B3: employee sees create_shift_request=True, manage=False, own pending only."""
    _seed_requests(shift.db)
    shift.db.values[("Employee", "E-1")] = {
        "shift_request_approver": "hr@x.test",
        "company": "CO-1",
    }
    shift.db.values[("User", "hr@x.test")] = {"full_name": "HR A"}
    ctx = shift.mod.schedule_context()
    assert ctx["viewer_employee"] == "E-1"
    assert ctx["can"] == {"create_shift_request": True, "manage_schedule": False}
    assert ctx["my_requests"]["pending"] == 2  # SR-1 + SR-2 (SR-OLD out, SR-3 not own)
    assert ctx["pending_team_requests"] == 0  # manager-only field
    assert ctx["approver"]["user"] == "hr@x.test"


def test_schedule_context_manager(shift):
    """B4: an HR Manager also gets pending_team_requests across employees."""
    _seed_requests(shift.db)
    shift.stub._roles = {"HR Manager"}
    ctx = shift.mod.schedule_context()
    assert ctx["can"]["manage_schedule"] is True
    assert ctx["pending_team_requests"] == 3  # SR-1 + SR-2 + SR-3


def test_schedule_context_no_linked_employee(shift):
    """B5: no linked Employee → graceful null viewer, can.create False, no throw."""
    shift.stub._viewer_emp = None
    ctx = shift.mod.schedule_context()
    assert ctx["viewer_employee"] is None
    assert ctx["can"]["create_shift_request"] is False
    assert ctx["approver"] is None


# --------------------------------------------------------------------------- #
# B6 — my_shift_requests (§2.2)
# --------------------------------------------------------------------------- #
def test_my_shift_requests_scoped_to_viewer(shift):
    """B6: only the viewer's requests are returned; approved ones map to their SA."""
    _seed_requests(shift.db)
    shift.db.rows["Shift Request"].append(
        {
            "name": "SR-4",
            "employee": "E-1",
            "docstatus": 1,
            "status": "Approved",
            "from_date": _future(10),
            "to_date": _future(12),
        }
    )
    shift.db.rows["Shift Assignment"] = [
        {
            "name": "SA-FROM-SR4",
            "employee": "E-1",
            "docstatus": 1,
            "shift_request": "SR-4",
        }
    ]
    rows = shift.mod.my_shift_requests()
    names = {r["name"] for r in rows}
    assert names == {"SR-1", "SR-2", "SR-4"}  # SR-3 (E-2) + SR-OLD (window) excluded
    sr4 = next(r for r in rows if r["name"] == "SR-4")
    assert sr4["shift_assignment"] == "SA-FROM-SR4"
    assert sr4["docstatus"] == 1


# --------------------------------------------------------------------------- #
# B7–B11 — create_my_shift_request (§2.3)
# --------------------------------------------------------------------------- #
def _seed_create_ok(db):
    db.exists_set.update({("Shift Type", "Day"), ("Employee", "E-1")})
    db.values[("Shift Type", "Day")] = {"disabled": 0}
    db.values[("Employee", "E-1")] = {
        "shift_request_approver": "hr@x.test",
        "company": "CO-1",
    }
    db.values[("User", "hr@x.test")] = {"full_name": "HR A"}


def test_create_my_shift_request_draft(shift):
    """B7: happy path → Draft Shift Request inserted, audited, approver returned."""
    _seed_create_ok(shift.db)
    res = shift.mod.create_my_shift_request(
        shift_type="Day", from_date=_future(3), to_date=_future(6), reason="việc gia đình"
    )
    assert res["name"]
    assert res["approver"]["user"] == "hr@x.test"
    docs = [d for d in shift.stub.created if getattr(d, "doctype", None) == "Shift Request"]
    assert len(docs) == 1 and docs[0].inserted
    assert docs[0].status == "Draft"
    assert shift.audit  # audit recorded
    assert shift.notify == ["E-1"]  # realtime ping


def test_create_my_shift_request_requires_approver(shift):
    """B8: no shift_request_approver anywhere → friendly throw, nothing inserted."""
    shift.db.exists_set.update({("Shift Type", "Day"), ("Employee", "E-1")})
    shift.db.values[("Shift Type", "Day")] = {"disabled": 0}
    shift.db.values[("Employee", "E-1")] = {"company": "CO-1"}
    with pytest.raises(Exception, match="duyệt"):
        shift.mod.create_my_shift_request(shift_type="Day", from_date=_future(3))
    assert not [d for d in shift.stub.created if getattr(d, "doctype", None) == "Shift Request"]


def test_create_my_shift_request_date_order(shift):
    """B9: to_date < from_date → throw."""
    _seed_create_ok(shift.db)
    with pytest.raises(Exception, match="Ngày kết thúc"):
        shift.mod.create_my_shift_request(shift_type="Day", from_date=_future(5), to_date=_future(3))


def test_create_my_shift_request_overlap_blocked(shift):
    """B10: overlapping Active SA / own Draft SR → Vietnamese overlap error."""
    _seed_create_ok(shift.db)
    shift.db.rows["Shift Assignment"] = [
        {
            "name": "SA-X",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-10),
            "end_date": _future(10),
        }
    ]
    with pytest.raises(Exception, match="Trùng ca"):
        shift.mod.create_my_shift_request(shift_type="Day", from_date=_future(3))
    # Overlapping own Draft request also blocks (SA removed).
    shift.db.rows["Shift Assignment"] = []
    shift.db.rows["Shift Request"] = [
        {
            "name": "SR-1",
            "employee": "E-1",
            "docstatus": 0,
            "status": "Draft",
            "from_date": _future(2),
            "to_date": _future(8),
        }
    ]
    with pytest.raises(Exception, match="yêu cầu ca trùng"):
        shift.mod.create_my_shift_request(shift_type="Day", from_date=_future(3))


def test_create_my_shift_request_disabled_shift(shift):
    """B11: a disabled Shift Type is refused."""
    shift.db.exists_set.update({("Shift Type", "Night"), ("Employee", "E-1")})
    shift.db.values[("Shift Type", "Night")] = {"disabled": 1}
    shift.db.values[("Employee", "E-1")] = {"shift_request_approver": "hr@x.test"}
    with pytest.raises(Exception, match="ngừng hoạt động"):
        shift.mod.create_my_shift_request(shift_type="Night", from_date=_future(3))


# --------------------------------------------------------------------------- #
# B12–B14 — cancel_my_shift_request (§2.4)
# --------------------------------------------------------------------------- #
def test_cancel_my_shift_request_own_draft(shift):
    """B12: own Draft → deleted (docstatus-0 convention) + audit."""
    shift.db.values[("Shift Request", "SR-1")] = {
        "employee": "E-1",
        "docstatus": 0,
        "status": "Draft",
    }
    res = shift.mod.cancel_my_shift_request("SR-1")
    assert res == {"name": "SR-1"}
    assert ("Shift Request", "SR-1") in shift.stub.deleted
    assert shift.audit


def test_cancel_my_shift_request_other_persons(shift):
    """B13: withdrawing someone else's request → PermissionError."""
    shift.db.values[("Shift Request", "SR-2")] = {
        "employee": "E-2",
        "docstatus": 0,
        "status": "Draft",
    }
    with pytest.raises(PermissionError):
        shift.mod.cancel_my_shift_request("SR-2")
    assert not shift.stub.deleted


def test_cancel_my_shift_request_only_draft(shift):
    """B14: an already-Approved request cannot be withdrawn here."""
    shift.db.values[("Shift Request", "SR-3")] = {
        "employee": "E-1",
        "docstatus": 1,
        "status": "Approved",
    }
    with pytest.raises(Exception, match="chờ duyệt"):
        shift.mod.cancel_my_shift_request("SR-3")
    assert not shift.stub.deleted


# --------------------------------------------------------------------------- #
# B15 — check_schedule_conflicts (§2.5, admin)
# --------------------------------------------------------------------------- #
def test_check_schedule_conflicts_combines_all_three(admin):
    """B15: SA timing conflict + approved leave + draft SR all reported."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    db.exists_set.update({("Employee", "E-1"), ("Shift Type", "Mid")})
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Mid")] = {"start_time": _dt(10), "end_time": _dt(18)}
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-1",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-10),
            "end_date": _future(10),
        }
    ]
    db.rows["Leave Application"] = [
        {
            "name": "LA-1",
            "employee": "E-1",
            "docstatus": 1,
            "status": "Approved",
            "leave_type": "Nghỉ phép",
            "from_date": _future(1),
            "to_date": _future(2),
        }
    ]
    db.rows["Shift Request"] = [
        {
            "name": "SR-1",
            "employee": "E-1",
            "docstatus": 0,
            "status": "Draft",
            "shift_type": "Day",
            "from_date": _future(3),
            "to_date": _future(4),
        }
    ]
    out = mod.check_schedule_conflicts(
        employee="E-1", shift_type="Mid", from_date=_future(1), to_date=_future(5)
    )
    types = {item["type"] for item in out}
    assert types == {"shift_assignment", "leave_application", "shift_request"}
    sa = next(i for i in out if i["type"] == "shift_assignment")
    assert sa["name"] == "SA-1" and sa["shift_type"] == "Day"


# --------------------------------------------------------------------------- #
# B16–B20 — override_day_shift_assignment (§2.6, admin)
# --------------------------------------------------------------------------- #
def _seed_override(db, stub, *, start=None, end=None, shift="Day", extra=None):
    db.exists_set.update({("Employee", "E-1"), ("Shift Type", "Day"), ("Shift Type", "Evening")})
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Evening")] = {"start_time": _dt(18), "end_time": _dt(22)}
    start = start or _future(-10)
    end = end or _future(10)
    row = {
        "name": "SA-1",
        "employee": "E-1",
        "status": "Active",
        "docstatus": 1,
        "shift_type": shift,
        "start_date": start,
        "end_date": end,
    }
    db.rows["Shift Assignment"] = [row]
    doc = _RecDoc(dict(row), name="SA-1", db=db)
    stub._doc_map[("Shift Assignment", "SA-1")] = doc
    if extra:
        db.rows["Shift Assignment"].extend(extra)
        for ex in extra:
            stub._doc_map[("Shift Assignment", ex["name"])] = _RecDoc(dict(ex), db=db)
    return doc


@pytest.fixture
def backfill_recorder(monkeypatch):
    """Fake the api.shift module so the lazy backfill import is deterministic."""
    calls: list = []
    fake = types.SimpleNamespace(
        _materialise_shift_instances=lambda **kw: calls.append(kw) or {"created": 1, "skipped": 0}
    )
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.shift", fake)
    return calls


def test_override_day_middle(admin, backfill_recorder):
    """B16: middle day → original cut to day-1, tail re-created, 1-day inserted."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    doc = _seed_override(db, stub)
    day = _future(0)
    res = mod.override_day_shift_assignment("E-1", day, "Evening")
    assert res["adjusted"] == ["SA-1"] and res["cancelled"] == []
    assert len(res["created"]) == 2  # tail (Day) + 1-day (Evening)
    assert doc.saved and str(doc.end_date) == str(
        datetime.date.fromisoformat(day) - datetime.timedelta(days=1)
    )
    # db.rows reflects the cut (write-back) — the re-query sees end = day-1.
    assert db.rows["Shift Assignment"][0]["end_date"] == doc.end_date
    created_docs = [d for d in stub.created if getattr(d, "doctype", None) == "Shift Assignment"]
    assert len(created_docs) == 2
    one_day = created_docs[-1]
    assert one_day.shift_type == "Evening" and one_day.start_date == one_day.end_date
    tail = created_docs[0]
    assert tail.shift_type == "Day"
    assert str(tail.start_date) == str(datetime.date.fromisoformat(day) + datetime.timedelta(days=1))
    # Day's instance materialised immediately.
    assert len(backfill_recorder) == 1


def test_override_day_at_start_cancels_original(admin, backfill_recorder):
    """B17: day == start → no head; original cancelled, only the 1-day remains."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    doc = _seed_override(db, stub)
    day = datetime.date.fromisoformat(_future(-10))  # == SA-1 start
    res = mod.override_day_shift_assignment("E-1", day.isoformat(), "Evening")
    assert res["cancelled"] == ["SA-1"] and res["adjusted"] == []
    assert doc.cancelled and doc.docstatus == 2
    assert len(res["created"]) == 1  # no tail (whole original was cancelled)
    assert db.rows["Shift Assignment"][0]["docstatus"] == 2  # write-back


def test_override_day_at_end_keeps_head(admin, backfill_recorder):
    """B18: day == end → cut to day-1, NO tail, just the 1-day insert."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    doc = _seed_override(db, stub)
    day = datetime.date.fromisoformat(_future(10))  # == SA-1 end
    res = mod.override_day_shift_assignment("E-1", day.isoformat(), "Evening")
    assert res["adjusted"] == ["SA-1"]
    assert len(res["created"]) == 1  # 1-day only
    assert doc.end_date == day - datetime.timedelta(days=1)


def test_override_day_without_covering_assignment(admin, backfill_recorder):
    """B19: no covering SA (only another employee's) → just the 1-day insert."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    db.exists_set.update({("Employee", "E-2"), ("Employee", "E-1"), ("Shift Type", "Evening")})
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Evening")] = {"start_time": _dt(18), "end_time": _dt(22)}
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-OTHER",
            "employee": "E-2",  # different employee → not covering E-1
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-10),
            "end_date": _future(10),
        }
    ]
    res = mod.override_day_shift_assignment("E-1", _future(1), "Evening")
    assert res == {"created": res["created"], "adjusted": [], "cancelled": []}
    assert len(res["created"]) == 1


def test_override_day_conflict_from_second_assignment(admin, backfill_recorder):
    """B20: a SECOND overlapping SA (not the one being adjusted) blocks the
    1-day insert when its clock timings clash with the new shift."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    db.exists_set.update({("Employee", "E-1"), ("Shift Type", "Day"), ("Shift Type", "Clash")})
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Clash")] = {"start_time": _dt(10), "end_time": _dt(18)}
    day = _future(0)
    db.rows["Shift Assignment"] = [
        {  # first covering SA — gets cut to day-1
            "name": "SA-1",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-10),
            "end_date": _future(2),
        },
        {  # second SA keeps covering `day` with clashing timings
            "name": "SA-2",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": _future(-2),
            "end_date": _future(10),
        },
    ]
    stub._doc_map[("Shift Assignment", "SA-1")] = _RecDoc(
        dict(db.rows["Shift Assignment"][0]), name="SA-1", db=db
    )
    with pytest.raises(Exception, match="trùng giờ"):
        mod.override_day_shift_assignment("E-1", day, "Clash")


# =========================================================================== #
# C-cases — /hr/team/schedule desk-free grid (plans/plan-team-schedule-desk-free.md §5.1)
# tz stub pins "today" at 2026-09-02; grid windows below use mid-September.
# =========================================================================== #
_TODAY = "2026-09-02"


def _seed_team(db, stub):
    """LM E-1 with direct reports E-2/E-3 + foreign E-9 (common C-case setup)."""
    db.rows["Employee"] = [
        {
            "name": "E-2",
            "employee_name": "Member Two",
            "department": "Sales",
            "reports_to": "E-1",
            "status": "Active",
        },
        {
            "name": "E-3",
            "employee_name": "Member Three",
            "department": "Ops",
            "reports_to": "E-1",
            "status": "Active",
        },
        {
            "name": "E-9",
            "employee_name": "Foreign Nine",
            "department": "IT",
            "reports_to": "X-1",
            "status": "Active",
        },
    ]
    db.values[("Shift Type", "Day")] = {"start_time": datetime.time(9, 0), "end_time": datetime.time(17, 0)}
    db.values[("Shift Type", "Evening")] = {
        "start_time": datetime.time(18, 0),
        "end_time": datetime.time(22, 0),
    }


def test_c1_team_schedule_line_manager_scope(shift):
    """C1: Line Manager opens team_schedule — own reports only, no 403."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    rows = mod.team_schedule()
    assert {r["name"] for r in rows} == {"E-2", "E-3"}  # E-9 excluded


def test_c2_team_schedule_plain_employee_denied(shift):
    """C2: plain Employee → PermissionError (their surface is /hr/schedule)."""
    shift.stub._roles = {"Employee"}
    with pytest.raises(PermissionError):
        shift.mod.team_schedule()


def test_c3_team_schedule_hr_company_scope(shift):
    """C3: HR Manager without reports → company-wide scope (E-9 visible)."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"HR Manager"}
    _seed_team(db, stub)
    rows = mod.team_schedule()
    assert "E-9" in {r["name"] for r in rows}


def test_c4_context_line_manager_matrix(shift):
    """C4: LM context — assign/override/approve yes, bulk no, scope=team."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    ctx = mod.team_schedule_context()
    assert ctx["scope"]["mode"] == "team"
    assert ctx["can"]["assign"] is True
    assert ctx["can"]["override_day"] is True
    assert ctx["can"]["approve_requests"] is True
    assert ctx["can"]["export"] is True
    assert ctx["can"]["bulk"] is False  # bulk stays HR Manager / System Manager


def test_c5_context_plain_employee_denied(shift):
    """C5: plain Employee on the team context → PermissionError."""
    shift.stub._roles = {"Employee"}
    with pytest.raises(PermissionError):
        shift.mod.team_schedule_context()


def test_c6_pending_approvals_scoped(shift):
    """C6: pending count = in-window Draft SRs of SCOPE only (2 own + 1 foreign → 2)."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    db.rows["Shift Request"] = [
        {
            "name": "SR-1",
            "employee": "E-2",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2026-09-03",
            "to_date": "2026-09-03",
        },
        {
            "name": "SR-2",
            "employee": "E-3",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2026-09-04",
            "to_date": "2026-09-04",
        },
        {
            "name": "SR-9",
            "employee": "E-9",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2026-09-04",
            "to_date": "2026-09-04",
        },
    ]
    ctx = mod.team_schedule_context("2026-09-03", "2026-09-05")
    assert ctx["pending_approvals"]["count"] == 2


_GRID_DOCTYPES = (
    "Employee",
    "Shift Assignment",
    "VN Employee Shift Instance",
    "Leave Application",
    "VN Attendance Work Session",
    "Shift Request",
)


def test_c7_grid_batched_no_n_plus_1(shift):
    """C7: exactly ONE query per doctype per grid request (batched read)."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    db.calls.clear()
    grid = mod.team_schedule_grid("2026-09-10", "2026-09-16")
    assert grid["members"]  # team rendered
    for dt in _GRID_DOCTYPES:
        assert sum(1 for c in db.calls if c["doctype"] == dt) == 1, f"N+1 leak on {dt}"


def test_c8_grid_instance_status_beats_assignment(shift):
    """C8: a Skipped VESI shows through even with an active covering SA."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-1",
            "employee": "E-2",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
        },
    ]
    db.rows["VN Employee Shift Instance"] = [
        {
            "name": "VESI-1",
            "employee": "E-2",
            "work_date": "2026-09-11",
            "shift_type": "Day",
            "status": "Skipped",
        },
    ]
    grid = mod.team_schedule_grid("2026-09-10", "2026-09-12")
    cell = next(c for c in grid["members"][0]["days"] if c["date"] == "2026-09-11")
    assert cell["instance"] == "VESI-1"
    assert cell["instance_status"] == "Skipped"
    assert cell["shift_assignment"] == "SA-1"


def test_c9_grid_leave_chip(shift):
    """C9: approved leave renders as a chip beside the shift."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-1",
            "employee": "E-2",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
        },
    ]
    db.rows["Leave Application"] = [
        {
            "name": "LA-1",
            "employee": "E-2",
            "leave_type": "Annual Leave",
            "from_date": "2026-09-11",
            "to_date": "2026-09-11",
            "status": "Approved",
            "docstatus": 1,
        },
    ]
    grid = mod.team_schedule_grid("2026-09-10", "2026-09-12")
    cell = next(c for c in grid["members"][0]["days"] if c["date"] == "2026-09-11")
    assert cell["leave"]["type"] == "Annual Leave"


def test_c10_grid_coverage_gap(shift):
    """C10: empty non-leave future day flagged unassigned + counted in summary."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Line Manager"}
    _seed_team(db, stub)  # E-3 has no SA at all
    grid = mod.team_schedule_grid("2026-09-10", "2026-09-11")
    e3 = next(m for m in grid["members"] if m["employee"] == "E-3")
    assert all(c.get("unassigned") for c in e3["days"])
    assert grid["summary"]["unassigned_days"] >= 2


def test_c11_grid_window_clamp(shift):
    """C11: >31-day window → ValidationError."""
    mod, stub = shift.mod, shift.stub
    stub._roles = {"Line Manager"}
    stub.ValidationError = type("ValidationError", (Exception,), {})
    with pytest.raises(Exception):
        mod.team_schedule_grid("2026-09-01", "2026-10-15")


def test_c12_cell_can_past_day_readonly(shift):
    """C12: past dates are read-only (mutations false, view_detail true)."""
    can = shift.mod._grid_cell_can(
        is_hr=True,
        is_lm_of=True,
        day="2026-08-01",
        today="2026-09-02",
        has_assignment=True,
        has_instance=True,
        instance_status="Scheduled",
    )
    assert can["view_detail"] is True
    assert not any(can[k] for k in ("assign", "override", "skip", "amend", "end", "approve"))


def test_c13_cell_can_active_instance_engine_owned(shift):
    """C13: Active/Completed instances are engine-owned — no manual mutations."""
    can = shift.mod._grid_cell_can(
        is_hr=True,
        is_lm_of=True,
        day="2026-09-10",
        today="2026-09-02",
        has_assignment=True,
        has_instance=True,
        instance_status="Active",
    )
    assert not any(can[k] for k in ("assign", "override", "skip", "amend", "end"))


def test_c14_cell_can_line_manager_scope(shift):
    """C14: a non-report member gets no mutation flags for an LM."""
    can = shift.mod._grid_cell_can(
        is_hr=False,
        is_lm_of=False,
        day="2026-09-10",
        today="2026-09-02",
        has_assignment=True,
        has_instance=True,
        instance_status="Scheduled",
    )
    assert not any(can[k] for k in ("assign", "override", "skip", "amend", "end", "approve"))
    ok = shift.mod._grid_cell_can(
        is_hr=False,
        is_lm_of=True,
        day="2026-09-10",
        today="2026-09-02",
        has_assignment=True,
        has_instance=True,
        instance_status="Scheduled",
    )
    assert ok["override"] is True and ok["skip"] is True


def _patch_real_emp_utils(monkeypatch, me):
    import gege_hr.gege_hr.utils.employee as emp_utils_real

    monkeypatch.setattr(emp_utils_real, "get_employee_for_user", lambda user=None: me)


def test_c15_list_shift_requests_lm_team_scope(admin, monkeypatch):
    """C15: LM list_shift_requests → only own team's drafts."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"Line Manager"}
    _patch_real_emp_utils(monkeypatch, "LM-1")
    db.rows["Employee"] = [
        {"name": "E-2", "status": "Active", "reports_to": "LM-1"},
        {"name": "E-9", "status": "Active", "reports_to": "OTHER"},
    ]
    db.rows["Shift Request"] = [
        {
            "name": "SR-1",
            "employee": "E-2",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2026-09-10",
            "to_date": "2026-09-10",
        },
        {
            "name": "SR-9",
            "employee": "E-9",
            "docstatus": 0,
            "status": "Draft",
            "from_date": "2026-09-10",
            "to_date": "2026-09-10",
        },
    ]
    rows = mod.list_shift_requests(scope="team")
    assert {r["employee"] for r in rows} == {"E-2"}


def test_c16_lm_approve_foreign_denied(admin, monkeypatch):
    """C16: LM approving a non-report member's SR → PermissionError."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"Line Manager"}
    stub.session = types.SimpleNamespace(user="lm@gege.test")
    _patch_real_emp_utils(monkeypatch, "LM-1")
    db.exists_set.add(("Shift Request", "SR-X"))
    db.values[("Employee", "E-9")] = {"reports_to": "OTHER"}
    stub._doc_map[("Shift Request", "SR-X")] = _RecDoc(
        {"employee": "E-9", "approver": None, "status": "Draft"}, name="SR-X", db=db, doctype="Shift Request"
    )
    with pytest.raises(PermissionError):
        mod.reject_shift_request("SR-X")


def test_c17_configured_approver_can_approve(admin):
    """C17: the SR's configured approver (any role) may approve; SA is created."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"Employee"}  # NOT an HR role — only the approver check lets them in
    stub.session = types.SimpleNamespace(user="boss@gege.test")
    db.exists_set.update({("Shift Request", "SR-A"), ("Employee", "E-2"), ("Shift Type", "Day")})
    stub._doc_map[("Shift Request", "SR-A")] = _RecDoc(
        {
            "employee": "E-2",
            "approver": "boss@gege.test",
            "status": "Draft",
            "company": None,
            "shift_type": "Day",
            "from_date": "2026-09-10",
            "to_date": "2026-09-10",
        },
        name="SR-A",
        db=db,
        doctype="Shift Request",
    )
    res = mod.approve_shift_request("SR-A")
    assert res["shift_assignment"]


def test_c18_swap_same_instance_pair_denied(admin):
    """C18: swapping a day with itself → ValidationError."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"HR Manager"}
    stub.ValidationError = type("ValidationError", (Exception,), {})
    db.values[("VN Employee Shift Instance", "V-1")] = {
        "employee": "E-1",
        "work_date": "2026-09-10",
        "shift_type": "Day",
    }
    with pytest.raises(Exception):
        mod.swap_shift_days("V-1", "V-1")


def test_c19_swap_happy_path(admin):
    """C19: two-cell swap → both overrides applied, union result, audit row."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"HR Manager"}
    db.exists_set.update(
        {("Employee", "E-1"), ("Employee", "E-2"), ("Shift Type", "Day"), ("Shift Type", "Evening")}
    )
    db.values[("VN Employee Shift Instance", "V-1")] = {
        "employee": "E-1",
        "work_date": "2026-09-10",
        "shift_type": "Day",
    }
    db.values[("VN Employee Shift Instance", "V-2")] = {
        "employee": "E-2",
        "work_date": "2026-09-11",
        "shift_type": "Evening",
    }
    res = mod.swap_shift_days("V-1", "V-2")
    assert len(res["created"]) == 2  # one 1-day SA per side
    assert res["adjusted"] == [] and res["cancelled"] == []


def test_c20_copy_week_partial_safe(admin):
    """C20: mid-batch conflict on E-1 is recorded; E-2 still copies fine."""
    mod, stub, db = admin.mod, admin.stub, admin.db
    stub._roles = {"HR Manager"}
    db.exists_set.update(
        {("Employee", "E-1"), ("Employee", "E-2"), ("Shift Type", "Day"), ("Shift Type", "Evening")}
    )
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Evening")] = {"start_time": _dt(18), "end_time": _dt(22)}
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-SRC",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-09-07",
            "end_date": "2026-09-07",
        },
        # Blocker shares the SAME shift (Day) on the target day → genuine G5
        # conflict (a disjoint-timing Evening would be legitimately allowed).
        {
            "name": "SA-BLK",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-09-14",
            "end_date": "2026-09-14",
        },
        {
            "name": "SA-E2",
            "employee": "E-2",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Evening",
            "start_date": "2026-09-08",
            "end_date": "2026-09-08",
        },
    ]
    res = mod.copy_week_schedule("2026-09-07", "2026-09-14", ["E-1", "E-2"])
    assert res["enqueued"] is False
    by_emp = {r["employee"]: r for r in res["results"]}
    # Per-weekday replay: E-1's only pattern day (Mon Day) is blocked on the
    # target Monday → created 0 + conflict recorded; E-2's Tue Evening copies.
    assert by_emp["E-1"]["created"] == 0 and by_emp["E-1"]["conflict"]
    assert by_emp["E-2"]["created"] == 1 and by_emp["E-2"]["conflict"] is None


def test_c21_export_denied_for_employee(shift):
    """C21: export is manager/HR only — plain Employee → PermissionError."""
    shift.stub._roles = {"Employee"}
    with pytest.raises(PermissionError):
        shift.mod.team_schedule_export("2026-09-10", "2026-09-16")


def test_c22_ics_token_gate(shift):
    """C22: bad token → PermissionError; valid token → VCALENDAR feed."""
    mod, stub, db = shift.mod, shift.stub, shift.db
    stub._roles = {"Employee"}  # ics is role-free (token-gated, not session-gated)
    with pytest.raises(PermissionError):
        mod.team_schedule_ics(employee="E-2", token="forged")
    _seed_team(db, stub)
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-1",
            "employee": "E-2",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-09-10",
            "end_date": "2026-09-12",
        },
    ]
    out = mod.team_schedule_ics(employee="E-2", token=mod._schedule_ics_token("E-2"))
    assert out.startswith("BEGIN:VCALENDAR")
    assert "SUMMARY:Day" in out and out.endswith("END:VCALENDAR")
