"""Bench-free unit tests for the Attendance Policy API (``api/attendance_policy.py``).

Plan: plans/plan-hr-settings-desk-free.md §4.1 (cases P1–P14, adjusted during
implementation: a pre-existing FE tab edits the ACTIVE policy's penalty rules,
so direct edits of an active policy stay allowed — only a LOCKED policy refuses
edits). Same stub-frappe harness as ``test_holiday_master``.
"""

from __future__ import annotations

import copy
import importlib
import sys
import types

import pytest

DOCTYPE = "VN Attendance Policy"


class _FrappeError(Exception):
    pass


class _LinkExistsError(Exception):
    pass


class _Meta:
    """VN Attendance Policy fields are all writable per EDITABLE_FIELDS."""

    def has_field(self, fieldname):
        return True


class _Child:
    def __init__(self):
        self.name = ""
        self.from_minutes = None
        self.to_minutes = None
        self.penalty_type = None
        self.penalty_value = None
        self.salary_component = None

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _PolicyDoc:
    """Minimal policy doc — defaults mirror the doctype JSON."""

    def __init__(self, **overrides):
        self.doctype = DOCTYPE
        self.meta = _Meta()
        self.name = overrides.get("name", "POL-0001")
        self.policy_name = overrides.get("policy_name", "Default Attendance Policy")
        self.company = overrides.get("company", "GeGe Vietnam")
        self.apply_to = overrides.get("apply_to", "All")
        self.branch = overrides.get("branch", "")
        self.department = overrides.get("department", "")
        self.is_active = overrides.get("is_active", 0)
        self.is_locked = overrides.get("is_locked", 0)
        self.version = overrides.get("version", 1)
        self.cloned_from_policy = overrides.get("cloned_from_policy", "")
        self.effective_from = overrides.get("effective_from", "")
        self.effective_to = overrides.get("effective_to", "")
        self.grace_late_minutes = overrides.get("grace_late_minutes", 5)
        self.grace_early_leave_minutes = overrides.get("grace_early_leave_minutes", 0)
        self.min_working_hours_full_day = overrides.get("min_working_hours_full_day", 4.0)
        self.min_working_hours_half_day = overrides.get("min_working_hours_half_day", 2.0)
        self.multiple_logs_strategy = overrides.get("multiple_logs_strategy", "First IN Last OUT")
        self.min_overtime_minutes = overrides.get("min_overtime_minutes", 30)
        self.max_overtime_hours_per_shift = overrides.get("max_overtime_hours_per_shift", 4.0)
        self.max_total_work_hours_per_shift = overrides.get("max_total_work_hours_per_shift", 20.0)
        self.allow_pre_shift_overtime = overrides.get("allow_pre_shift_overtime", 0)
        self.allow_post_shift_overtime = overrides.get("allow_post_shift_overtime", 1)
        self.require_overtime_approval = overrides.get("require_overtime_approval", 1)
        self.overtime_rounding_method = overrides.get("overtime_rounding_method", "No Rounding")
        self.overtime_rounding_minutes = overrides.get("overtime_rounding_minutes", 15)
        self.allow_ot_compensate_late = overrides.get("allow_ot_compensate_late", 0)
        self.allow_ot_compensate_early_leave = overrides.get("allow_ot_compensate_early_leave", 0)
        self.max_overtime_hours_per_day = overrides.get("max_overtime_hours_per_day", 8.0)
        self.minimum_rest_hours_between_shifts = overrides.get("minimum_rest_hours_between_shifts", 8.0)
        self.night_start_time = overrides.get("night_start_time", "22:00:00")
        self.night_end_time = overrides.get("night_end_time", "06:00:00")
        self.missing_checkin_action = overrides.get("missing_checkin_action", "Need Review")
        self.missing_checkout_action = overrides.get("missing_checkout_action", "Need Review")
        self.auto_mark_absent = overrides.get("auto_mark_absent", 0)
        self.penalty_rules = overrides.get("penalty_rules", [])
        self.flags = types.SimpleNamespace()
        self.inserted = 0
        self.saved = 0

    def insert(self, ignore_permissions=False):
        self.inserted += 1
        if self.policy_name:  # autoname "field:policy_name"
            self.name = self.policy_name
        return self

    def save(self, ignore_permissions=False):
        self.saved += 1
        return self

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)

    def append(self, key, _row):
        child = _Child()
        getattr(self, key).append(child)
        return child

    def as_dict(self):
        return {k: copy.copy(v) for k, v in self.__dict__.items() if not k.startswith("_")}


class _Harness:
    def __init__(self):
        self.policies = {}  # name → _PolicyDoc
        self.created = []  # docs from new_doc (save_policy create path)
        self.copied = []  # docs from copy_doc (clone_policy)
        self.list_rows = []  # rows for list_policies
        self.others_rows = []  # rows for the activate "others" query
        self.deleted = []
        self.audit_calls = []
        self.db = types.SimpleNamespace(
            exists=lambda doctype, name: name in self.policies,
            commit=lambda: None,
            get_all=self._db_get_all,
        )

    def _db_get_all(self, doctype, *args, **kwargs):
        return self.list_rows

    def get_doc(self, doctype, name=None):
        if name is not None:
            return self.policies[name]
        raise AssertionError("get_doc(dict) unexpected here")

    def get_all(self, doctype, filters=None, **kwargs):
        if isinstance(filters, dict):  # the activate/others query
            return list(self.others_rows)
        return self.list_rows

    def new_doc(self, doctype):
        doc = _PolicyDoc(name="", policy_name="")
        self.created.append(doc)
        return doc

    def copy_doc(self, doc):
        clone = _PolicyDoc()
        clone.__dict__.update({k: copy.copy(v) for k, v in doc.__dict__.items() if not k.startswith("_")})
        clone.penalty_rules = [copy.copy(r) for r in doc.penalty_rules]
        clone.inserted = 0
        clone.saved = 0
        self.copied.append(clone)
        return clone

    def delete_doc(self, doctype, name, **kwargs):
        self.deleted.append((doctype, name))

    def throw(self, msg, exc=_FrappeError, *a, **k):
        raise exc(msg)


def _build_stub_frappe(harness):
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
    mod.db = harness.db
    mod.get_doc = harness.get_doc
    mod.get_all = harness.get_all
    mod.new_doc = harness.new_doc
    mod.copy_doc = harness.copy_doc
    mod.delete_doc = harness.delete_doc
    mod.throw = harness.throw
    mod.LinkExistsError = _LinkExistsError
    mod.publish_realtime = lambda *a, **k: None
    mod.log_error = lambda *a, **k: None

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    return mod


@pytest.fixture
def fake(monkeypatch):
    harness = _Harness()
    stub = _build_stub_frappe(harness)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.attendance_policy")
    monkeypatch.setattr(api, "frappe", stub)
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: harness.audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GeGe Vietnam")

    harness.api = api
    harness.stub = stub
    return harness


def _register(fake, **kw):
    doc = _PolicyDoc(**kw)
    fake.policies[doc.name] = doc
    return doc


# --------------------------------------------------------------------------- #
# save_policy
# --------------------------------------------------------------------------- #
def test_save_policy_create_with_penalty_rows(fake):  # P1
    out = fake.api.save_policy(
        {"policy_name": "Chính sách E2E", "company": "GeGe Vietnam"},
        penalty_rules=[
            {"from_minutes": 16, "to_minutes": 30, "penalty_type": "Fixed Amount", "penalty_value": 50000},
            "junk-row",  # non-dict row must be ignored
        ],
    )
    doc = fake.created[0]
    assert doc.inserted == 1
    assert len(doc.penalty_rules) == 1
    assert doc.penalty_rules[0].penalty_value == 50000.0
    assert out["name"] == "Chính sách E2E"
    assert fake.audit_calls


def test_save_policy_legacy_kwargs_style(fake):  # HrSalaryStructureView call shape
    doc = _register(fake, name="LIVE", policy_name="Live", is_active=1)
    fake.api.save_policy(
        name="LIVE",
        penalty_rules=[
            {"from_minutes": 1, "to_minutes": 5, "penalty_type": "Per Minute", "penalty_value": 2000}
        ],
    )
    assert doc.saved == 1
    assert len(doc.penalty_rules) == 1
    assert doc.penalty_rules[0].penalty_type == "Per Minute"


def test_save_policy_update_locked_refuses(fake):  # P2
    _register(fake, name="L1", policy_name="Locked", is_locked=1)
    with pytest.raises(_FrappeError, match="khoá"):
        fake.api.save_policy({"name": "L1", "grace_late_minutes": 10})


def test_save_policy_update_active_allowed(fake):  # P3 (adjusted: legacy FE edits active)
    doc = _register(fake, name="A1", policy_name="Active", is_active=1)
    out = fake.api.save_policy({"name": "A1", "grace_late_minutes": 10})
    assert doc.saved == 1
    assert doc.grace_late_minutes == 10
    assert out["is_active"] == 1


def test_save_policy_cannot_rename(fake):  # P4
    doc = _register(fake, name="D1", policy_name="Draft")
    out = fake.api.save_policy({"name": "D1", "policy_name": "Đổi tên", "grace_late_minutes": 7})
    assert doc.policy_name == "Draft"  # rename silently dropped
    assert doc.grace_late_minutes == 7
    assert out["name"] == "D1"


def test_save_policy_half_gt_full_rejected(fake):  # P5
    with pytest.raises(_FrappeError, match="nửa ngày"):
        fake.api.save_policy(
            {
                "policy_name": "X",
                "company": "GeGe Vietnam",
                "min_working_hours_full_day": 4,
                "min_working_hours_half_day": 5,
            }
        )


def test_save_policy_apply_to_branch_requires_branch(fake):  # P6
    with pytest.raises(_FrappeError, match="Chi nhánh"):
        fake.api.save_policy({"policy_name": "X", "company": "GeGe Vietnam", "apply_to": "Branch"})


def test_save_policy_penalty_rows_validated(fake):  # P7
    with pytest.raises(_FrappeError, match="lớn hơn"):
        fake.api.save_policy(
            {"policy_name": "X", "company": "GeGe Vietnam"},
            penalty_rules=[
                {"from_minutes": 30, "to_minutes": 20, "penalty_type": "Fixed Amount", "penalty_value": 1}
            ],
        )


def test_save_policy_penalty_none_keeps_rows(fake):  # P8
    doc = _register(fake, name="D2", policy_name="Draft2")
    doc.penalty_rules = [
        types.SimpleNamespace(
            from_minutes=1,
            to_minutes=2,
            penalty_type="Per Minute",
            penalty_value=1,
            salary_component=None,
            name="r1",
        )
    ]
    fake.api.save_policy({"name": "D2", "grace_late_minutes": 9})
    assert len(doc.penalty_rules) == 1  # untouched


# --------------------------------------------------------------------------- #
# clone / activate / lock / delete
# --------------------------------------------------------------------------- #
def test_clone_policy_versions_and_resets(fake):  # P9
    _register(fake, name="P1", policy_name="Gốc", version=3, is_active=1)
    out = fake.api.clone_policy("P1")
    doc = fake.copied[0]
    assert doc.version == 4
    assert doc.cloned_from_policy == "P1"
    assert doc.is_active == 0 and doc.is_locked == 0
    assert doc.policy_name == "Gốc v4"
    assert out == {"name": "Gốc v4", "version": 4, "cloned_from_policy": "P1"}


def test_activate_policy_warns_and_can_deactivate_others(fake):  # P10
    _register(fake, name="A9", policy_name="Mới")
    fake.others_rows = [types.SimpleNamespace(name="OLD")]
    old = _register(fake, name="OLD", policy_name="Cũ", is_active=1)

    out = fake.api.activate_policy("A9")
    assert out["warnings"] == ["OLD"]
    assert old.is_active == 1  # not touched without the flag

    out2 = fake.api.activate_policy("A9", deactivate_others=1)
    assert out2["warnings"] == []
    assert old.is_active == 0
    assert fake.policies["A9"].is_active == 1


def test_lock_policy_active_refuses(fake):  # P11
    _register(fake, name="LA", policy_name="Live", is_active=1)
    with pytest.raises(_FrappeError, match="Ngừng kích hoạt"):
        fake.api.lock_policy("LA", 1)
    # after deactivating, lock succeeds
    fake.api.deactivate_policy("LA")
    out = fake.api.lock_policy("LA", 1)
    assert out["is_locked"] == 1


def test_delete_policy_active_refuses(fake):  # P12
    _register(fake, name="DA", policy_name="Live", is_active=1)
    with pytest.raises(_FrappeError, match="đang áp dụng"):
        fake.api.delete_policy("DA")
    assert fake.deleted == []


def test_delete_policy_draft_ok(fake):  # P13
    _register(fake, name="DD", policy_name="Draft")
    out = fake.api.delete_policy("DD")
    assert out == {"deleted": True, "name": "DD"}
    assert fake.deleted == [(DOCTYPE, "DD")]
    assert fake.audit_calls


def test_delete_policy_missing_returns_deleted_false(fake):
    assert fake.api.delete_policy("NOPE") == {"deleted": False, "message": "Không tồn tại."}


def test_list_policies_engine_pick(fake):  # P14
    fake.list_rows = [
        dict(
            name="b",
            policy_name="B",
            company="C1",
            is_active=0,
            is_locked=0,
            version=1,
            modified="2026-08-02",
        ),
        dict(
            name="a",
            policy_name="A",
            company="C1",
            is_active=1,
            is_locked=0,
            version=2,
            modified="2026-08-03",
        ),
        dict(
            name="c",
            policy_name="C",
            company="C2",
            is_active=1,
            is_locked=0,
            version=1,
            modified="2026-08-01",
        ),
        dict(
            name="d",
            policy_name="D",
            company="C1",
            is_active=1,
            is_locked=0,
            version=1,
            modified="2026-08-01",
        ),
    ]
    rows = fake.api.list_policies()
    picks = {r["name"] for r in rows if r["is_engine_pick"]}
    assert picks == {"a", "c"}  # newest modified active per company


def test_get_policy_can_matrix(fake):
    _register(fake, name="AC", policy_name="ActiveClone", is_active=1)
    out = fake.api.get_policy("AC")
    assert out["can"]["edit"] is True  # active stays editable (legacy tab)
    assert out["can"]["deactivate"] is True
    assert out["can"]["lock"] is False  # must deactivate first
    assert out["can"]["delete"] is False
    assert isinstance(out["penalty_rules"], list)


def test_permission_gate_short_circuits(fake, monkeypatch):
    def _deny(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(fake.api, "_require_hr_admin", _deny)
    with pytest.raises(PermissionError):
        fake.api.list_policies()
    with pytest.raises(PermissionError):
        fake.api.save_policy({"policy_name": "x", "company": "y"})
    assert fake.audit_calls == []
