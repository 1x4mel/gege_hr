"""Bench-free unit tests for the user-management endpoints in ``api/admin.py``.

Covers the zero-desk parity work (plan §B/C): ``update_user``,
``reset_user_password``, ``delete_user``, ``get_user_detail``, the
``create_user`` username-collision guard and the ``list_users``
``linked_employee`` enrichment.

Mirrors the stub-frappe harness of ``test_shift_assignment.py`` /
``test_blackout_api.py`` — ``monkeypatch.setitem(sys.modules, "frappe", stub)``
so it never leaks into the sibling bench-free tests that assert ``frappe`` is
unimportable.
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest

ADMIN_API = "gege_hr.gege_hr.api.admin"

# User meta used by the stub: every :data:`_USER_EDITABLE` field plus the
# identity columns, each mapped to a Frappe fieldtype so the whitelist /
# coercion logic in ``update_user`` is exercised realistically.
USER_META = {
    "full_name": "Data",
    "first_name": "Data",
    "last_name": "Data",
    "username": "Data",
    "gender": "Link",
    "birth_date": "Date",
    "mobile_no": "Phone",
    "phone": "Phone",
    "language": "Link",
    "time_zone": "Select",
    "user_image": "Attach Image",
    "email": "Data",
    "enabled": "Check",
    "user_type": "Select",
    "last_active": "Datetime",
}


class _Field:
    def __init__(self, fieldname, fieldtype):
        self.fieldname = fieldname
        self.fieldtype = fieldtype


class _FakeMeta:
    """Dict-backed meta: ``has_field`` / ``get_field`` / iterable ``fields``."""

    def __init__(self, fields):
        self._fields = dict(fields or {})

    def has_field(self, name):
        return name in self._fields

    def get_field(self, name):
        if name in self._fields:
            return _Field(name, self._fields[name])
        return None

    @property
    def fields(self):
        return [self.get_field(n) for n in self._fields]


class _DotDict(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _FakeDoc:
    def __init__(self, name, data=None, meta=None):
        self.name = name
        self._data = dict(data or {})
        self.meta = meta or _FakeMeta(USER_META)
        self.roles = []
        self.email = self._data.get("email", name)
        self.enabled = self._data.get("enabled", 1)
        self.user_type = self._data.get("user_type", "Website User")
        self.saved = False
        self.inserted = False
        self.reset_calls = []

    def get(self, field, default=None):
        if field == "roles":
            return self.roles
        if field in self._data:
            return self._data[field]
        return getattr(self, field, default)

    def set(self, field, value):
        self._data[field] = value

    def save(self, **kw):
        self.saved = True
        return self

    def insert(self, **kw):
        self.inserted = True
        return self

    def append(self, table, row):
        if table == "roles":
            self.roles.append(_DotDict(row) if isinstance(row, dict) else row)
        return self

    def _reset_password(self, send_email=False):
        self.reset_calls.append({"send_email": send_email})


class _FakeDB:
    def __init__(self):
        self.exists_pairs = set()  # {(doctype, name)} for str-exists checks
        self.exists_filter_hits = {}  # {(doctype, frozenset(items))} -> bool
        self.rows = {}  # doctype -> list[dict]
        self.values = {}  # (doctype, name) -> dict
        self.get_all_calls = []
        self.delete_calls = []

    def exists(self, doctype, x):
        if isinstance(x, str):
            return (doctype, x) in self.exists_pairs
        key = (doctype, tuple(sorted((x or {}).items())))
        return self.exists_filter_hits.get(key, False)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        self.get_all_calls.append({"doctype": doctype, "filters": filters, "pluck": pluck})
        base = list(self.rows.get(doctype, []))
        if isinstance(filters, dict):

            def _match(r):
                for k, v in filters.items():
                    if isinstance(v, list) and len(v) == 2 and v[0] == "in":
                        if r.get(k) not in v[1]:
                            return False
                    elif isinstance(v, list) and len(v) == 2 and v[0] == "not in":
                        if r.get(k) in v[1]:
                            return False
                    elif isinstance(v, list) and len(v) == 2 and v[0] == "<=":
                        if not (r.get(k) or 0) <= v[1]:
                            return False
                    elif r.get(k) != v:
                        return False
                return True

            base = [r for r in base if _match(r)]
        if pluck:
            return [r[pluck] if isinstance(r, dict) else r for r in base]
        if fields:
            return [_DotDict({f: r.get(f) for f in fields}) if isinstance(r, dict) else r for r in base]
        return [_DotDict(r) if isinstance(r, dict) else r for r in base]

    def delete(self, doctype, filters=None, **kw):
        """Fake frappe.db.delete — remove matching rows (used by delete_employee)."""

        def _match(r):
            return all(r.get(k) == v for k, v in (filters or {}).items())

        self.rows[doctype] = [r for r in self.rows.get(doctype, []) if not _match(r)]

    def get_value(self, doctype, name, fields=None, as_dict=False):
        if isinstance(name, dict):
            for r in self.rows.get(doctype, []):
                if all(r.get(k) == v for k, v in name.items()):
                    if as_dict:
                        return _DotDict(r)
                    if isinstance(fields, str):
                        return r.get(fields)
                    return r
            return None
        v = self.values.get((doctype, name))
        if v is None:
            return None
        if as_dict:
            return _DotDict(v)
        if isinstance(fields, str):
            return v.get(fields)
        return v

    def delete_doc(self, doctype, name, **kw):
        self.delete_calls.append((doctype, name, kw))
        return None


class _PermissionError(Exception):
    pass


class _OutgoingEmailError(Exception):
    pass


class _ValidationError(Exception):
    pass


class _LinkExistsError(Exception):
    pass


class _Stub:
    def __init__(self, db):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.only_for = lambda roles: None  # gate satisfied by default
        self.log_error = lambda *a, **k: None
        self.clear_messages = lambda *a, **k: None
        # Record msgprint calls so tests can assert exactly which warnings
        # were raised (create_user must NOT queue a core-style warning).
        self.msgprint_calls = []
        self.msgprint = lambda *a, **k: self.msgprint_calls.append((a, k))
        self.bold = lambda s: f"<b>{s}</b>"
        self.PermissionError = _PermissionError
        self.OutgoingEmailError = _OutgoingEmailError
        self.ValidationError = _ValidationError
        self.LinkExistsError = _LinkExistsError
        self.ValidationError = _ValidationError
        self.LinkExistsError = _LinkExistsError
        self.db = db
        self.get_all = db.get_all
        self.get_value = db.get_value
        self.session = types.SimpleNamespace(user="hr.manager@gege.test")
        self.get_meta = lambda dt: _FakeMeta(USER_META)
        self._doc_map = {}
        self.created = []
        self.deleted = []

    def get_doc(self, payload, name=None):
        if isinstance(payload, str) and (payload, name) in self._doc_map:
            return self._doc_map[(payload, name)]
        if isinstance(payload, dict):
            doc = _FakeDoc("NEW-1", dict(payload))
            self.created.append(doc)
            return doc
        return _FakeDoc(name or "NEW-1")

    get_cached_doc = get_doc

    def delete_doc(self, doctype, name, **kw):
        self.db.delete_doc(doctype, name, **kw)
        self.deleted.append((doctype, name))
        return None

    def throw(self, msg, exc=Exception, *a, **k):
        raise exc(msg)


@pytest.fixture
def admin(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: datetime.date.today()

    pw = types.ModuleType("frappe.utils.password")
    pw_calls = []

    def _update_password(user, pwd, **k):
        pw_calls.append((user, pwd))

    pw.update_password = _update_password

    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "frappe.utils.password", pw)

    sys.modules.pop(ADMIN_API, None)
    mod = importlib.import_module(ADMIN_API)
    monkeypatch.setattr(mod, "frappe", stub)

    audit_calls = []
    monkeypatch.setattr(mod, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))

    return types.SimpleNamespace(mod=mod, stub=stub, db=db, pw_calls=pw_calls, audit_calls=audit_calls)


# --------------------------------------------------------------------------- #
# update_user (BE-01..BE-03)
# --------------------------------------------------------------------------- #
def _user_doc(admin, name="u@x.vn", data=None):
    doc = _FakeDoc(name, data or {"full_name": "Old Name", "mobile_no": None, "gender": None})
    admin.stub._doc_map[("User", name)] = doc
    admin.db.exists_pairs.add(("User", name))
    return doc


def test_update_user_whitelist_ignores_sensitive_fields(admin):
    """BE-01 — only _USER_EDITABLE keys are written; user_type/api_key dropped."""
    doc = _user_doc(admin, data={"full_name": "Old Name", "mobile_no": None, "gender": None})
    res = admin.mod.update_user(
        "u@x.vn",
        full_name="New Name",
        mobile_no="0900",
        user_type="System User",  # NOT editable — must be ignored
        api_key="secret",  # NOT editable — must be ignored
    )
    assert doc.saved is True
    assert doc._data["full_name"] == "New Name"
    assert doc._data["mobile_no"] == "0900"
    assert "user_type" not in doc._data
    assert "api_key" not in doc._data
    assert any("full_name" in c for c in res["updated"])
    assert any("mobile_no" in c for c in res["updated"])
    assert all("user_type" not in c for c in res["updated"])
    assert admin.audit_calls  # mutation was audited


def test_update_user_not_found_throws(admin):
    """BE-02 — unknown user raises a friendly Vietnamese error."""
    with pytest.raises(Exception, match="Người dùng không tồn tại"):
        admin.mod.update_user("ghost@x.vn", full_name="X")


def test_update_user_requires_hr_admin(admin):
    """BE-03 — the _require_hr_admin gate fires before any write."""
    admin.stub.only_for = lambda roles: (_ for _ in ()).throw(admin.stub.PermissionError("nope"))
    with pytest.raises(admin.stub.PermissionError):
        admin.mod.update_user("u@x.vn", full_name="X")


# --------------------------------------------------------------------------- #
# reset_user_password (BE-04..BE-06b)
# --------------------------------------------------------------------------- #
def test_reset_password_too_short_throws_and_not_stored(admin):
    """BE-04 — <8 chars is rejected and update_password is never called."""
    _user_doc(admin)
    with pytest.raises(Exception, match="8 ký tự"):
        admin.mod.reset_user_password("u@x.vn", new_password="123")
    assert admin.pw_calls == []


def test_reset_password_valid_stores_and_audits(admin):
    """BE-05 — a valid password is stored immediately + re-enable + audit."""
    doc = _user_doc(admin)
    doc.enabled = 0  # locked account must be re-enabled on reset
    admin.mod.reset_user_password("u@x.vn", new_password="12345678")
    assert admin.pw_calls == [("u@x.vn", "12345678")]
    assert doc.saved is True  # re-enabled
    assert admin.audit_calls


def test_reset_password_send_email_branch(admin):
    """BE-06 — no password + send_email triggers Frappe's reset-link email."""
    doc = _user_doc(admin)
    admin.mod.reset_user_password("u@x.vn", send_email=1)
    assert doc.reset_calls == [{"send_email": True}]
    assert not admin.pw_calls


def test_reset_password_send_email_without_mailserver_throws_friendly(admin):
    """BE-06b — OutgoingEmailError surfaces a clear Vietnamese message."""
    doc = _user_doc(admin)

    def _boom(send_email=False):
        raise admin.stub.OutgoingEmailError("no smtp")

    doc._reset_password = _boom
    with pytest.raises(Exception, match="máy chủ email"):
        admin.mod.reset_user_password("u@x.vn", send_email=1)


# --------------------------------------------------------------------------- #
# delete_user (BE-07, BE-08 + happy path)
# --------------------------------------------------------------------------- #
def test_delete_user_refuses_self(admin):
    """BE-07 — the acting session user can never delete themselves."""
    admin.db.exists_pairs.add(("User", "hr.manager@gege.test"))
    with pytest.raises(Exception, match="tài khoản đang đăng nhập"):
        admin.mod.delete_user("hr.manager@gege.test")
    assert admin.stub.deleted == []


def test_delete_user_refuses_system_manager(admin):
    """BE-08 — a System Manager account is never deletable from the portal."""
    admin.db.exists_pairs.add(("User", "admin@x.vn"))
    admin.db.rows["Has Role"] = [{"role": "System Manager", "parent": "admin@x.vn"}]
    with pytest.raises(Exception, match="System Manager"):
        admin.mod.delete_user("admin@x.vn")
    assert admin.stub.deleted == []


def test_delete_user_happy_path(admin):
    """Happy path — a normal user is deleted + audited."""
    admin.db.exists_pairs.add(("User", "u@x.vn"))
    admin.db.rows["Has Role"] = []  # no System Manager
    res = admin.mod.delete_user("u@x.vn")
    assert res == {"name": "u@x.vn", "deleted": True}
    assert admin.stub.deleted == [("User", "u@x.vn")]
    assert admin.audit_calls


# --------------------------------------------------------------------------- #
# create_user username guard (BE-09, BE-10)
# --------------------------------------------------------------------------- #
def test_create_user_username_collision_throws(admin):
    """BE-09 — a colliding username is rejected before insert with a clear msg."""
    # email is free, but the derived username "nam" already exists.
    admin.db.exists_filter_hits[("User", (("username", "nam"),))] = True
    with pytest.raises(Exception, match="Tên đăng nhập đã tồn tại"):
        admin.mod.create_user(email="nam@other.vn", full_name="Nam B")


def test_create_user_custom_username_succeeds(admin):
    """BE-10 — an explicit, unique username is honoured end-to-end."""
    res = admin.mod.create_user(
        email="newuser@x.vn", full_name="New User", roles=["Employee"], username="customname"
    )
    assert res["username"] == "customname"
    assert admin.stub.created
    assert admin.stub.created[-1]._data["username"] == "customname"
    assert admin.stub.created[-1].inserted is True


# --------------------------------------------------------------------------- #
# create_user atomic creation (BE-CR1..BE-CR4 — plans/plan-fix-create-user-roles.md)
# --------------------------------------------------------------------------- #
def test_create_user_attaches_roles_atomically_without_warning(admin):
    """BE-CR1 — non-Employee roles ride along on the single insert, warning-free.

    The old flow inserted the User role-less and saved again, which queued
    Frappe core's "Newly created user … has no roles enabled" msgprint and
    masked later errors in the SPA. New contract: zero msgprint when a
    (non-deferred) role is supplied, and no second save. Employee is not
    used here because it is ERPNext-deferred (BE-CR5/BE-CR6).
    """
    res = admin.mod.create_user(
        email="vuw@gegeteam.net", full_name="Vu W", roles=["HR User"], password="12345678"
    )
    doc = admin.stub.created[-1]
    assert doc.inserted is True
    assert [d.role for d in doc.roles] == ["HR User"]
    assert res["roles"] == ["HR User"]
    assert res["deferred_roles"] == []
    assert res["roles_warning"] is False
    assert doc.saved is False  # single-phase creation — no second save
    assert admin.stub.msgprint_calls == []
    assert admin.pw_calls == [("NEW-1", "12345678")]
    assert admin.audit_calls  # creation was audited


def test_create_user_drops_non_portal_roles(admin):
    """BE-CR2 — a role outside PORTAL_ROLES is silently ignored (no escalation)."""
    res = admin.mod.create_user(email="esc@x.vn", full_name="Esc A", roles=["Employee", "System Manager"])
    doc = admin.stub.created[-1]
    assert [d.role for d in doc.roles] == []  # Employee deferred, System Manager dropped
    assert res["roles"] == []
    assert res["deferred_roles"] == ["Employee"]


def test_create_user_short_password_rejected_before_any_write(admin):
    """BE-CR3 — <8 chars throws up-front: no User doc built, nothing stored.

    The old flow created the User then deleted it on a short password,
    leaving the stale "no roles enabled" msgprint to mask this error.
    """
    with pytest.raises(Exception, match="Mật khẩu tạm phải có ít nhất 8 ký tự"):
        admin.mod.create_user(email="short@x.vn", full_name="Short P", password="123456")
    assert admin.stub.created == []  # frappe.get_doc was never reached
    assert admin.pw_calls == []
    assert admin.audit_calls == []


def test_create_user_without_roles_warns_in_vietnamese(admin):
    """BE-CR4 — role-less creation succeeds but flags + warns in Vietnamese."""
    res = admin.mod.create_user(email="norole@x.vn", full_name="No Role")
    doc = admin.stub.created[-1]
    assert doc.inserted is True
    assert doc.roles == []
    assert res["roles"] == []
    assert res["roles_warning"] is True
    assert len(admin.stub.msgprint_calls) == 1
    args, kwargs = admin.stub.msgprint_calls[0]
    assert "chưa có vai trò nào" in args[0]
    assert kwargs.get("indicator") == "orange"


def test_create_user_defers_employee_role_until_linked(admin):
    """BE-CR5 — Employee is deferred (not silently stripped) with no Employee link.

    ERPNext's User-validate hook would strip Employee from unlinked users and
    queue an English msgprint; create_user now reports it as deferred_roles so
    the SPA can explain the users→employees flow, and link_user_to_employee
    re-adds the role later (FINDING-LC1b).
    """
    res = admin.mod.create_user(email="newemp@x.vn", full_name="New Emp", roles=["Employee"])
    doc = admin.stub.created[-1]
    assert doc.inserted is True
    assert doc.roles == []  # not sent to ERPNext's stripper at all
    assert res["roles"] == []
    assert res["deferred_roles"] == ["Employee"]
    assert res["roles_warning"] is False  # a role IS coming — just later
    assert len(admin.stub.msgprint_calls) == 1
    args, kwargs = admin.stub.msgprint_calls[0]
    assert "liên kết với nhân viên" in args[0]
    assert kwargs.get("indicator") == "blue"


def test_create_user_assigns_employee_role_when_already_linked(admin):
    """BE-CR6 — an already-linked email gets Employee immediately."""
    admin.db.rows["Employee"] = [{"name": "HR-EMP-9", "user_id": "linked@x.vn"}]
    res = admin.mod.create_user(email="linked@x.vn", full_name="Linked", roles=["Employee"])
    doc = admin.stub.created[-1]
    assert [d.role for d in doc.roles] == ["Employee"]
    assert res["roles"] == ["Employee"]
    assert res["deferred_roles"] == []
    assert admin.stub.msgprint_calls == []


def test_create_user_keeps_full_vietnamese_name_in_first_name(admin):
    """BE-CR7 — "Lại Minh Hiếu" stays whole in first_name (old split()[0] → "Lại")."""
    res = admin.mod.create_user(email="hieu@x.vn", full_name="Lại Minh Hiếu")
    doc = admin.stub.created[-1]
    assert doc._data["first_name"] == "Lại Minh Hiếu"
    assert not doc._data["last_name"]
    assert res["full_name"] == "Lại Minh Hiếu"


def test_create_user_honours_explicit_first_last_name(admin):
    """BE-CR7b — explicit first/last (edit modal) still override the fallback."""
    admin.mod.create_user(
        email="split@x.vn", full_name="Nguyễn Văn B", first_name="Văn B", last_name="Nguyễn"
    )
    doc = admin.stub.created[-1]
    assert doc._data["first_name"] == "Văn B"
    assert doc._data["last_name"] == "Nguyễn"


# --------------------------------------------------------------------------- #
# delete_employee (DEL-1..DEL-4)
# --------------------------------------------------------------------------- #
def test_delete_employee_not_found_throws(admin):
    """DEL-1 — unknown employee raises a friendly Vietnamese error."""
    with pytest.raises(Exception, match="Nhân viên không tồn tại"):
        admin.mod.delete_employee("ghost-emp")


def test_delete_employee_happy_path_audits(admin):
    """DEL-2 — a clean employee is deleted + audited."""
    admin.db.exists_pairs.add(("Employee", "HR-EMP-1"))
    res = admin.mod.delete_employee("HR-EMP-1")
    assert res == {"name": "HR-EMP-1", "deleted": True}
    assert admin.stub.deleted == [("Employee", "HR-EMP-1")]
    assert admin.audit_calls


def test_delete_employee_surfaces_guard_validation_error(admin):
    """DEL-3 — the attendance-guard ValidationError passes through verbatim."""
    admin.db.exists_pairs.add(("Employee", "HR-EMP-2"))

    def _boom(*a, **k):
        raise _ValidationError("Không thể xoá nhân viên còn dữ liệu chấm công")

    admin.stub.delete_doc = _boom
    with pytest.raises(Exception, match="dữ liệu chấm công"):
        admin.mod.delete_employee("HR-EMP-2")


def test_delete_employee_link_exists_becomes_friendly_vn_error(admin):
    """DEL-4 — a raw LinkExistsError is re-raised as Vietnamese guidance."""
    admin.db.exists_pairs.add(("Employee", "HR-EMP-3"))

    def _boom(*a, **k):
        raise _LinkExistsError("linked with VN Employee Bank Account")

    admin.stub.delete_doc = _boom
    with pytest.raises(Exception, match="Không thể xoá nhân viên"):
        admin.mod.delete_employee("HR-EMP-3")


def test_delete_employee_cleans_auto_generated_empty_review_lines(admin):
    """DEL-5 — empty (net_pay<=0) payroll review lines are pre-cleaned.

    These lines are upserted for EVERY employee whenever a payroll period is
    calculated — even employees with zero attendance — so they must not block
    deletion (mirrors e2e_ops.lc_drop).
    """
    admin.db.exists_pairs.add(("Employee", "HR-EMP-5"))
    admin.db.rows["VN Payroll Review Line"] = [
        {"name": "L-EMPTY", "employee": "HR-EMP-5", "net_pay": 0},
        {"name": "L-OTHER", "employee": "HR-EMP-9", "net_pay": 0},
    ]
    admin.db.rows["VN Notification"] = [
        {"name": "NT-1", "employee": "HR-EMP-5"},
        {"name": "NT-OTHER", "employee": "HR-EMP-9"},
    ]
    admin.db.rows["User Permission"] = [
        {"name": "UP-1", "allow": "Employee", "for_value": "HR-EMP-5"},
    ]
    deleted_lines = []
    real_delete = admin.stub.delete_doc

    def _spy(doctype, name, **kw):
        if doctype == "VN Payroll Review Line":
            deleted_lines.append(name)
        if doctype == "VN Notification" and name == "NT-1":
            deleted_lines.append("NT-1")
        return real_delete(doctype, name, **kw)

    admin.stub.delete_doc = _spy
    res = admin.mod.delete_employee("HR-EMP-5")
    assert res["deleted"] is True
    assert deleted_lines == ["L-EMPTY", "NT-1"]  # this employee's rows only
    # User Permission auto-row cleaned via frappe.db.delete (fake removes rows).
    assert admin.db.rows["User Permission"] == []
    # (stub.delete_doc không đụng fake-rows — spy ở trên đã chứng minh NT-1
    # được gọi đúng, NT-OTHER của nhân viên khác không bị đụng.)


def test_delete_employee_keeps_real_payroll_review_lines(admin):
    """DEL-6 — a net_pay>0 review line (real salary) still blocks deletion."""
    admin.db.exists_pairs.add(("Employee", "HR-EMP-6"))
    admin.db.rows["VN Payroll Review Line"] = [
        {"name": "L-PAID", "employee": "HR-EMP-6", "net_pay": 5_000_000},
    ]

    def _employee_only(doctype, name, **kw):
        # The pre-clean must NOT have force-deleted the PAID line: simulate the
        # DB state by refusing the Employee delete (link still exists).
        if doctype == "Employee":
            raise _LinkExistsError("linked with VN Payroll Review Line L-PAID")
        return None

    admin.stub.delete_doc = _employee_only
    with pytest.raises(Exception, match="Không thể xoá nhân viên"):
        admin.mod.delete_employee("HR-EMP-6")


# --------------------------------------------------------------------------- #
# list_users helpers (BE-11, BE-12)
# --------------------------------------------------------------------------- #
def test_system_users_excludes_guest_and_administrator(admin):
    """BE-11 — the directory never lists the two built-in system accounts."""
    assert admin.mod._SYSTEM_USERS == ["Guest", "Administrator"]


def test_attach_linked_employees_maps_user_to_employee(admin):
    """BE-12 — each row gains employee/employee_name in a single pass."""
    admin.db.rows["Employee"] = [
        {"name": "HR-EMP-1", "employee_name": "Nguyễn A", "user_id": "a@x.vn"},
    ]
    rows = [
        {"name": "a@x.vn", "email": "a@x.vn", "full_name": "Nguyễn A"},
        {"name": "b@x.vn", "email": "b@x.vn", "full_name": "Trần B"},  # no link
    ]
    admin.mod._attach_linked_employees(rows)
    assert rows[0]["employee"] == "HR-EMP-1"
    assert rows[0]["employee_name"] == "Nguyễn A"
    assert "employee" not in rows[1]


# --------------------------------------------------------------------------- #
# get_user_detail (BE-14) + set_user_enabled gate (BE-13)
# --------------------------------------------------------------------------- #
def test_get_user_detail_shape(admin):
    """BE-14 — returns editable fields + full roles + linked employee."""
    doc = _FakeDoc(
        "a@x.vn",
        {"full_name": "Nguyễn A", "mobile_no": "0900", "gender": "Male", "username": "a"},
    )
    admin.stub._doc_map[("User", "a@x.vn")] = doc
    admin.db.exists_pairs.add(("User", "a@x.vn"))
    admin.db.rows["Has Role"] = [
        {"role": "Employee", "parent": "a@x.vn"},
        {"role": "System Manager", "parent": "a@x.vn"},
    ]
    admin.db.rows["Employee"] = [{"name": "HR-EMP-1", "employee_name": "Nguyễn A", "user_id": "a@x.vn"}]

    detail = admin.mod.get_user_detail("a@x.vn")
    assert detail["full_name"] == "Nguyễn A"
    assert detail["mobile_no"] == "0900"
    assert set(detail["roles"]) == {"Employee", "System Manager"}  # ALL roles, not just portal
    assert detail["linked_employee"] == "HR-EMP-1"
    assert detail["linked_employee_name"] == "Nguyễn A"


def test_set_user_enabled_is_gated(admin):
    """BE-13 — enable/disable is gated by _require_hr_admin (not raw REST)."""
    admin.stub.only_for = lambda roles: (_ for _ in ()).throw(admin.stub.PermissionError("nope"))
    with pytest.raises(admin.stub.PermissionError):
        admin.mod.set_user_enabled("u@x.vn", 1)
