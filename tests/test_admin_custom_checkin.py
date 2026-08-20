"""Bench-free unit tests for ``gege_hr.gege_hr.api.admin.admin_custom_checkin``
(team-attendance matrix manual-override — checkin-manager.txt parity).

Uses the same stub-frappe harness as ``test_pagination.py`` /
``test_checkout_miss.py`` (``monkeypatch.setitem(sys.modules, "frappe", stub)``).
The gege_hr submodules ``admin.py`` imports at module top-level are also stubbed
so the module reloads cleanly without a bench.

Covers the validation, permission-gate, TZ conversion (portal-local → UTC) and
insert/update dispatch paths (plan §4 test cases BE-1/2/3/6/7/9/10/12).
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import os
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub builders
# --------------------------------------------------------------------------- #
def _build_gege_submodules(recalc_sink, tz_get_tzinfo):
    """Build the ``gege_hr.*`` submodule stubs ``admin.py`` imports.

    * ``api.audit``   → ``log(...)`` no-op (audit is best-effort).
    * ``utils.pagination`` → unused empty module.
    * ``utils.tz``    → real-ish ``get_tzinfo`` (Asia/Ho_Chi_Minh) so the UTC
      conversion math is exercised; everything else unused.
    * ``utils.employee`` → ``get_employee_for_user`` returns the configured
      "me" (for the Line Manager scope check).
    * ``api.attendance`` → ``on_employee_checkin_create`` records the recalc call.
    """
    from zoneinfo import ZoneInfo

    # Stub packages carry an empty ``__path__`` so Python treats them as
    # packages (lets ``from gege_hr.gege_hr.api import audit`` resolve via the
    # submodule stubs already registered in sys.modules).
    pkg_root = types.ModuleType("gege_hr")
    pkg_root.__path__ = []
    pkg_app = types.ModuleType("gege_hr.gege_hr")
    pkg_app.__path__ = []
    pkg_api = types.ModuleType("gege_hr.gege_hr.api")
    pkg_api.__path__ = []
    pkg_utils = types.ModuleType("gege_hr.gege_hr.utils")
    pkg_utils.__path__ = []

    audit_mod = types.ModuleType("gege_hr.gege_hr.api.audit")
    audit_mod.log = lambda *a, **k: None

    pagination_mod = types.ModuleType("gege_hr.gege_hr.utils.pagination")

    tz_mod = types.ModuleType("gege_hr.gege_hr.utils.tz")
    tz_mod.get_tzinfo = tz_get_tzinfo or (lambda tz=None: ZoneInfo("Asia/Ho_Chi_Minh"))

    def _to_portal(value):
        if value is None:
            return None
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(tz_mod.get_tzinfo())

    tz_mod.to_portal = _to_portal

    employee_mod = types.ModuleType("gege_hr.gege_hr.utils.employee")
    employee_mod.get_employee_for_user = lambda *_a, **_k: "HR-EMP-ME"

    attendance_mod = types.ModuleType("gege_hr.gege_hr.api.attendance")

    def _recalc(doc):
        recalc_sink.append(getattr(doc, "name", None))

    attendance_mod.on_employee_checkin_create = _recalc

    return {
        "gege_hr": pkg_root,
        "gege_hr.gege_hr": pkg_app,
        "gege_hr.gege_hr.api": pkg_api,
        "gege_hr.gege_hr.utils": pkg_utils,
        "gege_hr.gege_hr.api.audit": audit_mod,
        "gege_hr.gege_hr.api.attendance": attendance_mod,
        "gege_hr.gege_hr.utils.pagination": pagination_mod,
        "gege_hr.gege_hr.utils.tz": tz_mod,
        "gege_hr.gege_hr.utils.employee": employee_mod,
    }


class _PermErr(Exception):
    pass


class _NotExistErr(Exception):
    pass


class _Thrown(Exception):
    """Captures a ``frappe.throw`` call (message + exc class label)."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.message = message
        self.kind = kind


class _FakeCheckinDoc:
    """Fake Employee Checkin returned by ``frappe.get_doc``."""

    def __init__(self, payload, name):
        for k, v in payload.items():
            setattr(self, k, v)
        self.name = name

    def insert(self, **_kw):
        return self


def _build_frappe_stub(*, roles, employee_exists, checkin_exists_map, reports_to):
    """Minimal frappe stub capturing db/get_doc calls for assertions."""
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.log_error = lambda *a, **k: None
    mod.DoesNotExistError = _NotExistErr
    mod.PermissionError = _PermErr
    mod._roles = set(roles)

    created = []
    set_values = []
    recalc_sink = []
    mod._created = created
    mod._set_values = set_values
    mod._recalc_sink = recalc_sink

    counter = iter(range(700000, 999999))

    def _throw(message, exc=None):
        kind = "Generic"
        if exc is _NotExistErr:
            kind = "DoesNotExist"
        elif exc is _PermErr:
            kind = "Permission"
        raise _Thrown(message, kind)

    mod.throw = _throw

    def _get_roles():
        return set(mod._roles)

    mod.get_roles = _get_roles

    class _DB:
        def __init__(self):
            # Names returned by db.get_all() lookups (the existing-checkin search).
            self.checkin_rows = []

        def exists(self, doctype, name):
            if doctype == "Employee":
                return bool(employee_exists)
            if doctype == "Employee Checkin":
                return name in checkin_exists_map or name in self.checkin_rows
            return False

        def get_value(self, doctype, name, field):
            if doctype == "Employee" and field == "reports_to":
                return reports_to
            return None

        def set_value(self, doctype, name, field, value, **kw):
            set_values.append((doctype, name, field, value))

        def get_all(self, doctype, filters=None, pluck=None, order_by=None, limit=1, **kw):
            return list(self.checkin_rows)

        def commit(self):
            mod._committed = True

    mod.db = _DB()

    def _get_doc(*args):
        # frappe.get_doc(payload) → insert path
        if len(args) == 1 and isinstance(args[0], dict):
            payload = args[0]
            name = f"NEW-{next(counter)}"
            doc = _FakeCheckinDoc(payload, name)
            created.append((payload, name))
            return doc
        # frappe.get_doc(doctype, name) → reload for recalc on update
        doctype, name = args[0], args[1]
        return _FakeCheckinDoc({"doctype": doctype, "name": name}, name)

    mod.get_doc = _get_doc

    return mod


@pytest.fixture
def admin_module(monkeypatch):
    """Install the stubs and reload ``admin`` so tests import the stubbed copy."""
    recalc_sink = []
    frappe_stub = _build_frappe_stub(
        roles={"HR Manager"},
        employee_exists=True,
        checkin_exists_map=set(),
        reports_to="HR-EMP-ME",
    )
    subs = _build_gege_submodules(recalc_sink, None)
    # cross-link the recalc sink onto the frappe stub for assertions
    frappe_stub._recalc_sink = recalc_sink

    # ``admin.py`` does ``from frappe.utils import getdate/get_datetime`` — stub.
    frappe_utils = types.ModuleType("frappe.utils")
    frappe_utils.getdate = lambda v=None: dt.date(2026, 8, 12)
    frappe_utils.get_datetime = lambda v=None: (
        v if isinstance(v, dt.datetime) else dt.datetime(2026, 8, 12, 1, 0, 0)
    )
    frappe_stub.utils = frappe_utils

    # Preserve original modules so the reload does not pollute other tests.
    originals = {name: sys.modules.get(name) for name in ["frappe", "frappe.utils", *subs]}
    monkeypatch.setitem(sys.modules, "frappe", frappe_stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", frappe_utils)
    for name, mod in subs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # Load ``admin.py`` directly from disk (bypass the package import machinery
    # — the stubbed ``gege_hr.gege_hr.api`` package is not a real package on
    # disk in this bench-free harness).
    admin_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "gege_hr", "gege_hr", "api", "admin.py")
    )
    spec = importlib.util.spec_from_file_location("gege_admin_under_test", admin_path)
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    yield admin, frappe_stub
    # restore is handled by monkeypatch teardown for sys.modules entries


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_portal_local_to_utc_vn_offset(admin_module):
    """PHASE-1 FRAME: portal-local 08:00 is stored as naive WALL 08:00."""
    admin, _ = admin_module
    assert admin._portal_local_to_utc_str("2026-08-12 08:00") == "2026-08-12 08:00:00"


def test_parse_portal_dt_accepts_with_and_without_seconds(admin_module):
    admin, _ = admin_module
    assert admin._parse_portal_dt("2026-08-12 08:00") == dt.datetime(2026, 8, 12, 8, 0)
    assert admin._parse_portal_dt("2026-08-12 08:00:30") == dt.datetime(2026, 8, 12, 8, 0, 30)
    assert admin._parse_portal_dt("2026-08-12T08:00") == dt.datetime(2026, 8, 12, 8, 0)
    assert admin._parse_portal_dt("") is None
    assert admin._parse_portal_dt("abc") is None


def test_requires_at_least_one_time(admin_module):
    """BE-6: both IN and OUT empty → throw 'Cần điền tối thiểu …'."""
    admin, _ = admin_module
    with pytest.raises(_Thrown) as exc:
        admin.admin_custom_checkin("HR-EMP-1")
    assert "tối thiểu" in exc.value.message


def test_nonexistent_employee_throws(admin_module, monkeypatch):
    """BE-9: employee does not exist → DoesNotExistError."""
    admin, stub = admin_module
    stub.db.__self__ if False else None  # noqa
    # Re-point exists to deny the employee.
    orig_exists = stub.db.exists

    def deny_employee(doctype, name):
        if doctype == "Employee":
            return False
        return orig_exists(doctype, name)

    stub.db.exists = deny_employee
    with pytest.raises(_Thrown) as exc:
        admin.admin_custom_checkin("GHOST", time_in="2026-08-12 08:00")
    assert exc.value.kind == "DoesNotExist"


def test_in_after_out_rejected(admin_module):
    """BE-7: Ra before Vào (same calendar day) → throw."""
    admin, _ = admin_module
    with pytest.raises(_Thrown) as exc:
        admin.admin_custom_checkin(
            "HR-EMP-1",
            time_in="2026-08-12 17:00",
            time_out="2026-08-12 08:00",
        )
    assert "Ra không được trước" in exc.value.message


def test_permission_denied_for_plain_employee(admin_module, monkeypatch):
    """BE-10: no editor role → PermissionError."""
    admin, stub = admin_module
    stub._roles = {"Employee"}
    with pytest.raises(_Thrown) as exc:
        admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 08:00")
    assert exc.value.kind == "Permission"


def test_line_manager_blocked_outside_team(admin_module):
    """R4: Line Manager editing someone NOT reporting to them → blocked."""
    admin, stub = admin_module
    stub._roles = {"Line Manager"}

    def reports_to_other(doctype, name, field):
        return "HR-EMP-SOMEONE-ELSE"

    stub.db.get_value = reports_to_other
    with pytest.raises(_Thrown) as exc:
        admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 08:00")
    assert exc.value.kind == "Permission"


def test_line_manager_allows_own_team(admin_module):
    """R4: Line Manager editing a direct report → allowed (insert happens)."""
    admin, stub = admin_module
    stub._roles = {"Line Manager"}

    def reports_to_me(doctype, name, field):
        return "HR-EMP-ME"

    stub.db.get_value = reports_to_me
    res = admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 08:00")
    assert res["status"] == "ok"
    assert len(stub._created) == 1  # one IN inserted


def test_insert_new_in_uses_utc_and_logs_type(admin_module):
    """BE-1 (PHASE-1): insert new IN → log_type IN + WALL time 08:05."""
    admin, stub = admin_module
    res = admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 08:05")
    assert res["status"] == "ok"
    payload, name = stub._created[0]
    assert payload["doctype"] == "Employee Checkin"
    assert payload["log_type"] == "IN"
    assert payload["time"] == "2026-08-12 08:05:00"  # PHASE-1: wall storage
    assert payload["employee"] == "HR-EMP-1"


def test_insert_new_out(admin_module):
    """BE-3 (PHASE-1): only OUT provided → inserts an OUT checkin (wall)."""
    admin, stub = admin_module
    admin.admin_custom_checkin("HR-EMP-1", time_out="2026-08-12 17:30")
    payload, _ = stub._created[0]
    assert payload["log_type"] == "OUT"
    assert payload["time"] == "2026-08-12 17:30:00"


def test_insert_both_in_one_call(admin_module):
    """BE-5: IN + OUT together → exactly two checkins inserted."""
    admin, stub = admin_module
    admin.admin_custom_checkin(
        "HR-EMP-1",
        time_in="2026-08-12 08:00",
        time_out="2026-08-12 17:00",
    )
    types_inserted = sorted(p["log_type"] for p, _ in stub._created)
    assert types_inserted == ["IN", "OUT"]


def test_update_existing_in_updates_time_and_recalcs(admin_module):
    """BE-2: update an existing IN → set_value with UTC + recalc triggered."""
    admin, stub = admin_module
    # Pretend the IN checkin already exists.
    stub.db.checkin_exists = {"CHK-IN-1"}  # not used directly; rewire exists
    orig_exists = stub.db.exists

    def exists_with_checkin(doctype, name):
        if doctype == "Employee Checkin" and name == "CHK-IN-1":
            return True
        return orig_exists(doctype, name)

    stub.db.exists = exists_with_checkin

    res = admin.admin_custom_checkin(
        "HR-EMP-1", in_id="CHK-IN-1", time_in="2026-08-12 09:00"
    )
    assert res["status"] == "ok"
    assert stub._set_values[0][:3] == ("Employee Checkin", "CHK-IN-1", "time")
    assert stub._set_values[0][3] == "2026-08-12 09:00:00"  # PHASE-1: wall
    # update path must trigger recalc explicitly (hook is after_insert-only)
    assert stub._recalc_sink == ["CHK-IN-1"]
    # and must NOT insert a new doc
    assert stub._created == []


def test_overnight_checkout_next_day_not_falsely_rejected(admin_module):
    """BE-11: overnight shift — OUT typed on the next day must be accepted."""
    admin, stub = admin_module
    res = admin.admin_custom_checkin(
        "HR-EMP-1",
        time_in="2026-08-12 22:00",
        time_out="2026-08-13 06:00",
    )
    assert res["status"] == "ok"
    types_inserted = sorted(p["log_type"] for p, _ in stub._created)
    assert types_inserted == ["IN", "OUT"]


def test_update_finds_existing_checkin_when_no_id(admin_module):
    """No id passed (the team grid never sends one) but an IN already exists for
    that day → the endpoint must UPDATE it (via the portal-day lookup) instead of
    inserting a duplicate. This is the realistic edit path from the grid."""
    admin, stub = admin_module
    stub.db.checkin_rows = ["CHK-IN-FOUND"]  # the existing-checkin lookup result
    res = admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 09:00")
    assert res["status"] == "ok"
    # updated the found doc, did NOT insert a duplicate
    assert stub._set_values[0][:3] == ("Employee Checkin", "CHK-IN-FOUND", "time")
    assert stub._created == []
    assert stub._recalc_sink == ["CHK-IN-FOUND"]


def test_inserts_when_no_existing_checkin_found(admin_module):
    """No id passed and lookup returns nothing → insert a new row."""
    admin, stub = admin_module
    stub.db.checkin_rows = []  # nothing found
    res = admin.admin_custom_checkin("HR-EMP-1", time_in="2026-08-12 08:00")
    assert res["status"] == "ok"
    assert len(stub._created) == 1
    assert stub._created[0][0]["log_type"] == "IN"
