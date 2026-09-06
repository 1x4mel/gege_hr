"""Bench-free unit tests for the Settings Single API (``api/settings_single.py``).

Plan: plans/plan-hr-settings-desk-free.md §4.1 (cases S1–S10). Pattern mirrors
``test_holiday_master`` — a stub ``frappe`` is injected into ``sys.modules``
and the admin permission/audit helpers are monkeypatched so the tests stay
focused on settings_single's own allowlist / coercion / validation logic.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class _FrappeError(Exception):
    pass


class _Field(types.SimpleNamespace):
    pass


class _Meta:
    """Tiny meta stand-in: {fieldname: (fieldtype, options)}."""

    def __init__(self, spec):
        self.spec = {
            f: _Field(fieldname=f, fieldtype=t, options=o, label=f) for f, (t, o) in spec.items()
        }

    def has_field(self, fieldname):
        return fieldname in self.spec

    def get_field(self, fieldname):
        return self.spec.get(fieldname)


PORTAL_META = _Meta(
    {
        "enable_mobile_checkin": ("Check", None),
        "require_geolocation": ("Check", None),
        "require_selfie": ("Check", None),
        "require_wifi_validation": ("Check", None),
        "enable_device_sync": ("Check", None),
        "timezone": ("Select", "Asia/Ho_Chi_Minh\nAsia/Bangkok\nUTC"),
        "default_work_location": ("Link", "VN Work Location"),
        "default_attendance_policy": ("Link", "VN Attendance Policy"),
        "payroll_cutoff_day": ("Int", None),
        "lock_attendance_after_days": ("Int", None),
        "enable_employee_self_service": ("Check", None),
        "enable_manager_dashboard": ("Check", None),
    }
)

HR_META = _Meta(
    {
        "emp_created_by": ("Select", "Naming Series\nEmployee Number\nFull Name"),
        "retirement_age": ("Data", None),
        "standard_working_hours": ("Float", None),
        "send_birthday_reminders": ("Check", None),
        "send_work_anniversary_reminders": ("Check", None),
        "send_holiday_reminders": ("Check", None),
        "frequency": ("Select", "Weekly\nMonthly"),
        "sender": ("Link", "Email Account"),
        "sender_email": ("Data", None),
        "send_leave_notification": ("Check", None),
        "leave_approval_notification_template": ("Link", "Email Template"),
        "leave_status_notification_template": ("Link", "Email Template"),
        "leave_approver_mandatory_in_leave_application": ("Check", None),
        "restrict_backdated_leave_application": ("Check", None),
        "role_allowed_to_create_backdated_leave_application": ("Link", "Role"),
        "prevent_self_leave_approval": ("Check", None),
        "prevent_self_expense_approval": ("Check", None),
        "expense_approver_mandatory_in_expense_claim": ("Check", None),
        "auto_leave_encashment": ("Check", None),
        "show_leaves_of_all_department_members_in_calendar": ("Check", None),
        "allow_multiple_shift_assignments": ("Check", None),
        "allow_employee_checkin_from_mobile_app": ("Check", None),
        "allow_geolocation_tracking": ("Check", None),
    }
)


class _SingleDoc:
    def __init__(self, doctype, meta, values):
        self.doctype = doctype
        self.name = doctype
        self.meta = meta
        self.flags = types.SimpleNamespace()
        self._values = dict(values)
        self.saved = 0
        self.modified = "2026-09-01 00:00:00.000000"
        self.modified_by = "hr.demo@gege.demo"

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def save(self, ignore_permissions=False):
        self.saved += 1
        self.modified = "2026-09-01 01:00:00.000000"
        return self


class _FakeDB:
    def __init__(self):
        self.exists_map = {}  # {(doctype, name): bool}; missing → True
        self.get_all_calls = []

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)

    def get_all(self, doctype, *args, **kwargs):
        self.get_all_calls.append((doctype, args, kwargs))
        return [types.SimpleNamespace(name=f"{doctype}-1")]

    def commit(self):
        return None


def _build_stub_frappe():
    import datetime

    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils
    mod.db = _FakeDB()
    mod.get_cached_doc = None
    mod.get_doc = None
    mod.publish_realtime = lambda *a, **k: None
    mod.throw = lambda msg, exc=_FrappeError, *a, **k: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    return mod


@pytest.fixture
def fake(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.settings_single")
    monkeypatch.setattr(api, "frappe", stub)
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    monkeypatch.setattr(api, "_meta_has_active", lambda dt: dt == "VN Work Location")
    audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))

    portal_doc = _SingleDoc(
        "VN HR Portal Setting",
        PORTAL_META,
        {
            "enable_mobile_checkin": 1,
            "require_geolocation": 1,
            "require_selfie": 0,
            "require_wifi_validation": 0,
            "enable_device_sync": 0,
            "timezone": "Asia/Ho_Chi_Minh",
            "default_work_location": "HCM Office",
            "default_attendance_policy": "",
            "payroll_cutoff_day": 25,
            "lock_attendance_after_days": 5,
            "enable_employee_self_service": 1,
            "enable_manager_dashboard": 1,
        },
    )
    hr_doc = _SingleDoc(
        "HR Settings",
        HR_META,
        {
            "emp_created_by": "Naming Series",
            "standard_working_hours": 8.0,
            "send_birthday_reminders": 1,
            "restrict_backdated_leave_application": 0,
            "role_allowed_to_create_backdated_leave_application": "",
            "allow_multiple_shift_assignments": 0,
        },
    )
    docs = {"VN HR Portal Setting": portal_doc, "HR Settings": hr_doc}
    stub.get_cached_doc = lambda doctype, name=None: docs[doctype]
    stub.get_doc = lambda doctype, name=None: docs[doctype]

    harness = types.SimpleNamespace(
        api=api,
        stub=stub,
        docs=docs,
        portal=portal_doc,
        hr=hr_doc,
        audit_calls=audit_calls,
    )
    return harness


# --------------------------------------------------------------------------- #
# get_single_settings
# --------------------------------------------------------------------------- #
def test_get_returns_only_allowlisted_fields(fake):  # S1
    out = fake.api.get_single_settings("VN HR Portal Setting")
    assert out["doctype"] == "VN HR Portal Setting"
    assert out["values"]["payroll_cutoff_day"] == 25
    assert "values" in out and "__options" in out and out["modified_by"]
    writable = set(fake.api.PORTAL_SETTING_FIELDS if hasattr(fake.api, "PORTAL_SETTING_FIELDS") else [])
    # every returned key must be a known registry field (no leakage)
    from gege_hr.gege_hr.api.settings_single import _SETTINGS_REGISTRY

    known = _SETTINGS_REGISTRY["VN HR Portal Setting"]["writable"]
    assert set(out["values"]) <= known | _SETTINGS_REGISTRY["VN HR Portal Setting"]["readonly"]
    assert writable == set() or writable <= known  # admin allowlist stays in sync


def test_get_rejects_unknown_doctype(fake):  # S2
    with pytest.raises(_FrappeError):
        fake.api.get_single_settings("Company")


def test_get_denied_without_hr_admin(fake, monkeypatch):  # S3
    def _deny(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(fake.api, "_require_hr_admin", _deny)
    with pytest.raises(PermissionError):
        fake.api.get_single_settings("VN HR Portal Setting")


def test_save_updates_changed_fields_and_audits(fake):  # S4
    out = fake.api.save_single_settings("VN HR Portal Setting", {"require_selfie": 1, "timezone": "Asia/Bangkok"})
    assert out["ok"] is True
    assert sorted(out["changed"]) == ["require_selfie", "timezone"]
    assert fake.portal.saved == 1
    assert fake.portal._values["require_selfie"] == 1
    assert fake.audit_calls, "audit must be recorded"


def test_save_drops_unknown_keys_and_throws_when_empty(fake):  # S5
    # unknown keys are silently dropped → payload empty after filtering → throw
    with pytest.raises(_FrappeError):
        fake.api.save_single_settings("VN HR Portal Setting", {"hack_field": "x"})
    with pytest.raises(_FrappeError):
        fake.api.save_single_settings("VN HR Portal Setting", {})
    # a no-op save of unchanged values reports zero changes (no throw)
    out = fake.api.save_single_settings("VN HR Portal Setting", {"require_selfie": 0})
    assert out["ok"] is True and out["changed"] == []


def test_save_rejects_bad_select_option(fake):  # S6
    with pytest.raises(_FrappeError):
        fake.api.save_single_settings("VN HR Portal Setting", {"timezone": "Mars/Phobos"})


@pytest.mark.parametrize("field,value", [("payroll_cutoff_day", 45), ("lock_attendance_after_days", 0)])  # S7
def test_save_rejects_out_of_range_ints(fake, field, value):
    with pytest.raises(_FrappeError):
        fake.api.save_single_settings("VN HR Portal Setting", {field: value})


def test_save_rejects_missing_link(fake):  # S8
    fake.stub.db.exists_map[("VN Work Location", "Nowhere")] = False
    with pytest.raises(_FrappeError):
        fake.api.save_single_settings("VN HR Portal Setting", {"default_work_location": "Nowhere"})


def test_save_hr_settings_dependent_field(fake):  # S9
    out = fake.api.save_single_settings(
        "HR Settings",
        {"restrict_backdated_leave_application": 1, "role_allowed_to_create_backdated_leave_application": "HR Manager"},
    )
    assert sorted(out["changed"]) == [
        "restrict_backdated_leave_application",
        "role_allowed_to_create_backdated_leave_application",
    ]
    assert fake.hr._values["role_allowed_to_create_backdated_leave_application"] == "HR Manager"


def test_link_options_allowlist(fake):  # S10
    out = fake.api.settings_link_options("Role", search="HR")
    assert [o["value"] for o in out] == ["Role-1"]
    (doctype, args, kwargs), *_ = fake.stub.db.get_all_calls
    assert doctype == "Role"
    with pytest.raises(_FrappeError):
        fake.api.settings_link_options("User", "admin")  # not in allowlist


def test_get_options_include_select_and_links(fake):
    out = fake.api.get_single_settings("HR Settings")
    assert out["__options"]["frequency"] == ["Weekly", "Monthly"]
    assert out["__options"]["sender"] == ["Email Account-1"]
