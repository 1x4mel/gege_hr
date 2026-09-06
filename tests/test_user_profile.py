"""User 360° profile API (plans/plan-user-frontend-parity.md §2.1–§2.7).

  BE-UP-01  summarize_logins sort desc + limit + chịu thiếu field (pure)
  BE-UP-02  left_employee_warning chỉ bật khi enabled + Employee Left (pure)
  BE-UP-03  get_user_360 payload shape + scrub field nhạy cảm
  BE-UP-04  get_user_360 user không tồn tại → throw tiếng Việt
  BE-UP-05  get_user_360 thiếu quyền → only_for raise
  BE-UP-06  add_user_comment trim / quá ngắn → throw
  BE-UP-07  add_user_comment hợp lệ → Comment row reference User
  BE-UP-08  logout_all_sessions chặn tự logout session user
  BE-UP-09  logout_all_sessions hợp lệ → clear_sessions gọi 1 lần
  BE-UP-10  logout_all_sessions user không tồn tại
  BE-UP-11  validate_security khung giờ nghịch đảo / ngoài 0-24 (pure)
  BE-UP-12  validate_security simultaneous_sessions 0 / "abc" (pure)
  BE-UP-13  update_user_security restrict_ip khi không phải System Manager
  BE-UP-14  update_user_security bỏ field ngoài whitelist
  BE-UP-15  reset_user_password truyền logout_all_sessions=True + re-enable
  BE-UP-16  list_user_permissions gate + map for_value → value
  BE-UP-17  add_user_permission allow ngoài whitelist / tự gán chính mình
  BE-UP-18  remove_user_permission sai user → throw; đúng user → delete
  BE-UP-19  set_user_role_profile set field + trả roles trước/sau
  BE-UP-20  bulk_assign_roles partial-safe (2 ok / 1 fail)
  BE-UP-21  bulk_assign_roles role ngoài PORTAL_ROLES → throw
  BE-UP-22  list_users filter role/has_employee + summary no_role

Bench-free stub-frappe harness (pattern tests/test_employee_profile.py).
Module under test reuses employee_profile pure helpers, so the fixture reloads
admin → employee_profile → user_profile under the stub.
"""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


class FrappeError(Exception):
    """frappe.throw / only_for stand-in."""


class _InsertDoc:
    """Doc built from a dict payload — insert() mints name/creation/owner."""

    def __init__(self, payload, stub):
        self.__dict__.update(payload)
        self._stub = stub

    def insert(self, **_kw):
        self.name = f"NEW-{len(self._stub.inserted) + 1:05d}"
        self.creation = "2026-08-27 10:00:00"
        self.owner = "hr@test.io"
        self._stub.inserted.append({k: v for k, v in self.__dict__.items() if not k.startswith("_")})
        return self


class _UserDoc:
    """User row wrapper with get/set/save/append for the security & bulk paths."""

    def __init__(self, row: dict):
        self._row = row
        # Expose every column as a real attribute — admin code reads some
        # fields directly (``target.enabled``), not via ``.get()``.
        self.__dict__.update({k: v for k, v in row.items() if not k.startswith("_")})
        self.roles = [SimpleNamespace(role=r) for r in row.get("_roles", [])]
        self.saved = False

    def __setattr__(self, key, value):
        # Keep the backing row dict in sync — admin code assigns directly
        # (``target.enabled = 1``) and tests assert on stub.users.
        super().__setattr__(key, value)
        row = self.__dict__.get("_row")
        if row is not None and not key.startswith("_") and key not in ("roles", "saved"):
            row[key] = value

    def get(self, field, default=None):
        if field == "roles":
            return self.roles
        return self._row.get(field, default)

    def set(self, field, value):
        self._row[field] = value
        setattr(self, field, value)

    def save(self, **_kw):
        self.saved = True
        self._row["_roles"] = [r.role for r in self.roles]
        return self

    def append(self, table, row):
        if table == "roles":
            self.roles.append(SimpleNamespace(**row) if isinstance(row, dict) else row)
        return self


class Stub:
    def __init__(self):
        self.session_roles = {"HR Manager"}
        self.users = {
            "hr@test.io": {
                "name": "hr@test.io",
                "email": "hr@test.io",
                "full_name": "HR Quản trị",
                "username": "hr",
                "enabled": 1,
                "user_type": "System User",
                "user_image": None,
                "mobile_no": None,
                "language": "vi",
                "time_zone": "Asia/Ho_Chi_Minh",
                "login_before": None,
                "login_after": None,
                "simultaneous_sessions": 2,
                "restrict_ip": None,
                "role_profile_name": None,
                "_roles": ["HR Manager"],
            },
            "u1@x.io": {
                "name": "u1@x.io",
                "email": "u1@x.io",
                "full_name": "Người Một",
                "username": "u1",
                "enabled": 1,
                "user_type": "System User",
                "login_before": None,
                "login_after": None,
                "simultaneous_sessions": None,
                "restrict_ip": None,
                "role_profile_name": None,
                "_roles": ["Employee"],
            },
            "left@x.io": {
                "name": "left@x.io",
                "email": "left@x.io",
                "full_name": "Người Đã Nghỉ",
                "username": "left",
                "enabled": 1,
                "user_type": "System User",
                "login_before": None,
                "login_after": None,
                "simultaneous_sessions": None,
                "restrict_ip": None,
                "role_profile_name": None,
                "_roles": ["Employee"],
            },
            "norole@x.io": {
                "name": "norole@x.io",
                "email": "norole@x.io",
                "full_name": "Người Không Vai Trò",
                "username": "norole",
                "enabled": 1,
                "user_type": "System User",
                "_roles": [],
            },
            "disabled@x.io": {
                "name": "disabled@x.io",
                "email": "disabled@x.io",
                "full_name": "Người Bị Khoá",
                "username": "disabled",
                "enabled": 0,
                "user_type": "System User",
                "_roles": ["Employee"],
            },
        }
        self.meta_fields: dict[str, list[str]] = {
            "User": [
                "name",
                "email",
                "full_name",
                "username",
                "enabled",
                "user_type",
                "user_image",
                "mobile_no",
                "phone",
                "language",
                "time_zone",
                "gender",
                "birth_date",
                "last_login",
                "last_active",
                "creation",
                "login_before",
                "login_after",
                "simultaneous_sessions",
                "restrict_ip",
                "role_profile_name",
            ],
            "Employee": ["name", "employee_name", "status", "image", "user_id"],
            "Comment": ["name", "owner", "comment_email", "creation", "content"],
            "Version": ["name", "owner", "creation", "data", "ref_doctype", "ref_name"],
            "VN Audit Event": [
                "name",
                "audit_type",
                "actor",
                "created_at",
                "description",
                "reference_doctype",
                "reference_name",
            ],
            "Activity Log": ["name", "operation", "status", "ip_address", "creation", "user"],
            "Has Role": ["name", "parent", "parenttype", "role"],
            "User Permission": [
                "name",
                "user",
                "allow",
                "for_value",
                "is_default",
                "applicable_for",
                "creation",
            ],
            "Role Profile": ["name"],
        }
        # get_all reads stub.rows — mirror the users table there (minus _roles).
        self.rows: dict[str, list[dict]] = {
            "User": [
                {k: v for k, v in u.items() if k != "_roles"} for u in self.users.values()
            ],
            "Employee": [
                {
                    "name": "E1",
                    "employee_name": "NV Một",
                    "status": "Active",
                    "user_id": "u1@x.io",
                },
                {
                    "name": "E2",
                    "employee_name": "NV Nghỉ Việc",
                    "status": "Left",
                    "user_id": "left@x.io",
                },
            ],
            "Has Role": [
                {"name": "HR-1", "parent": "hr@test.io", "parenttype": "User", "role": "HR Manager"},
                {"name": "HR-2", "parent": "u1@x.io", "parenttype": "User", "role": "Employee"},
                {"name": "HR-3", "parent": "left@x.io", "parenttype": "User", "role": "Employee"},
                {"name": "HR-4", "parent": "HR Chi nhánh", "parenttype": "Role Profile", "role": "HR User"},
                {"name": "HR-5", "parent": "disabled@x.io", "parenttype": "User", "role": "Employee"},
            ],
            "Comment": [
                {
                    "name": "C-1",
                    "reference_doctype": "User",
                    "reference_name": "u1@x.io",
                    "owner": "hr@test.io",
                    "creation": "2026-08-27 09:00:00",
                    "content": "Đã trao đổi mật khẩu",
                }
            ],
            "Version": [
                {
                    "name": "V-1",
                    "ref_doctype": "User",
                    "ref_name": "u1@x.io",
                    "owner": "hr@test.io",
                    "creation": "2026-08-27 08:00:00",
                    "data": '{"changed": [["full_name", "A", "Người Một"]]}',
                }
            ],
            "VN Audit Event": [
                {
                    "name": "AE-1",
                    "reference_doctype": "User",
                    "reference_name": "u1@x.io",
                    "audit_type": "Manual Override",
                    "actor": "hr@test.io",
                    "created_at": "2026-08-27 07:00:00",
                    "description": "Gán vai trò: Employee",
                }
            ],
            "Activity Log": [
                {
                    "name": "AL-1",
                    "user": "u1@x.io",
                    "operation": "Login",
                    "status": "Successful",
                    "ip_address": "10.0.0.9",
                    "creation": "2026-08-26 09:00:00",
                },
                {
                    "name": "AL-2",
                    "user": "u1@x.io",
                    "operation": "Logout",
                    "status": "Successful",
                    "ip_address": "10.0.0.9",
                    "creation": "2026-08-27 09:30:00",
                },
            ],
            "User Permission": [
                {
                    "name": "UP-1",
                    "user": "u1@x.io",
                    "allow": "Company",
                    "for_value": "GeGe",
                    "is_default": 1,
                    "applicable_for": None,
                    "creation": "2026-08-01 08:00:00",
                }
            ],
            "Role Profile": [{"name": "HR Chi nhánh"}],
        }
        self.inserted: list[dict] = []
        self.deleted: list[tuple] = []
        self.cleared_sessions: list[dict] = []
        self.perm_adds: list[dict] = []
        self.pwd_updates: list[dict] = []


def _cond(row: dict, cond) -> bool:
    if not isinstance(cond, (list, tuple)) or len(cond) < 3:
        return True
    field, op, value = cond[0], cond[1], cond[2]
    cur = row.get(field)
    if op == "=":
        return cur == value
    if op == "in":
        return cur in (value or [])
    if op == "not in":
        return cur not in (value or [])
    if op == "is":
        return cur not in (None, "") if value == "set" else cur in (None, "")
    return cur == value


def _matches(row: dict, filters) -> bool:
    if not filters:
        return True
    if isinstance(filters, dict):
        for k, v in filters.items():
            if isinstance(v, (list, tuple)) and len(v) >= 2 and v[0] in ("in", "not in", "is", "="):
                if not _cond(row, [k, v[0], v[1] if len(v) > 1 else None]):
                    return False
            elif row.get(k) != v:
                return False
        return True
    return all(_cond(row, c) for c in filters)


def _make_frappe(stub: Stub):
    fr = types.ModuleType("frappe")
    fr._ = lambda s: s
    fr.whitelist = lambda *a, **k: lambda f: f

    def only_for(roles=None):
        if isinstance(roles, str):
            roles = [roles]
        if roles and not (set(roles) & set(stub.session_roles or ())):
            raise FrappeError("PermissionError")

    fr.only_for = only_for

    def throw(msg, *args, **_kw):
        raise FrappeError(str(msg))

    fr.throw = throw
    fr.log_error = lambda *a, **k: None
    fr.session = SimpleNamespace(user="hr@test.io")
    fr.get_roles = lambda: list(stub.session_roles or ())
    fr.msgprint = lambda *a, **k: None

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None, pluck=None, **_kw):
        rows = [dict(r) for r in stub.rows.get(doctype, []) if _matches(r, filters)]
        if fields and not pluck:
            rows = [{k: r.get(k) for k in fields if k in r} or r for r in rows]
        if pluck:
            return [r.get(pluck) for r in rows]
        if limit_page_length:
            rows = rows[:limit_page_length]
        return rows

    fr.get_all = get_all

    def get_meta(doctype):
        names = stub.meta_fields.get(doctype, ["name"])
        return SimpleNamespace(fields=[SimpleNamespace(fieldname=n) for n in names])

    fr.get_meta = get_meta

    def get_doc(arg, name=None, **_kw):
        if isinstance(arg, dict):
            return _InsertDoc(arg, stub)
        if arg == "User":
            if name not in stub.users:
                raise FrappeError("not found")
            return _UserDoc(stub.users[name])
        raise FrappeError(f"unexpected get_doc {arg}")

    fr.get_doc = get_doc
    fr.delete_doc = lambda dt, name, **_kw: stub.deleted.append((dt, name))

    _get_all_fn = get_all
    _get_meta_fn = get_meta

    class _DB:
        get_all = staticmethod(_get_all_fn)
        get_meta = staticmethod(_get_meta_fn)

        @staticmethod
        def exists(doctype, name):
            if doctype == "User":
                return name in stub.users
            if doctype == "Role Profile":
                return any(r.get("name") == name for r in stub.rows.get("Role Profile", []))
            if doctype == "Employee":
                return any(r.get("name") == name for r in stub.rows.get("Employee", []))
            return False

        @staticmethod
        def get_value(doctype, name, fieldname=None, **_kw):
            rows = stub.rows.get(doctype, [])
            row = next((r for r in rows if r.get("name") == name), None)
            if row is None:
                return None
            if isinstance(fieldname, (list, tuple)):
                return {f: row.get(f) for f in fieldname}
            return row.get(fieldname)

        @staticmethod
        def set_value(doctype, name, fieldname, value=None, **_kw):
            pass

        @staticmethod
        def get_single_value(*_a, **_k):
            return None

    fr.db = _DB()
    return fr


def _utils_mod():
    import datetime as dt

    utils = types.ModuleType("frappe.utils")

    def getdate(value=None):
        if isinstance(value, dt.date):
            return value
        if value:
            return dt.date.fromisoformat(str(value)[:10])
        return dt.date.today()

    utils.getdate = getdate
    return utils


def _password_mod(stub: Stub):
    mod = types.ModuleType("frappe.utils.password")

    def update_password(user, pwd, doctype="User", fieldname="password", logout_all_sessions=False):
        stub.pwd_updates.append(
            {"user": user, "pwd": pwd, "logout_all_sessions": logout_all_sessions}
        )

    mod.update_password = update_password
    return mod


def _sessions_mod(stub: Stub):
    mod = types.ModuleType("frappe.sessions")

    def clear_sessions(user=None, reason=None, force=False, keep_current=False):
        stub.cleared_sessions.append(
            {"user": user, "reason": reason, "force": force, "keep_current": keep_current}
        )

    mod.clear_sessions = clear_sessions
    return mod


def _permissions_mod(stub: Stub):
    mod = types.ModuleType("frappe.permissions")

    def add_user_permission(doctype, name, user, ignore_permissions=False, applicable_for=None, is_default=0, hide_descendants=0):
        stub.perm_adds.append(
            {"allow": doctype, "value": name, "user": user, "is_default": is_default}
        )

    mod.add_user_permission = add_user_permission
    return mod


def _audit_stub():
    mod = types.ModuleType("gege_hr.gege_hr.api.audit")
    mod.log = lambda *a, **k: None
    mod.record = lambda *a, **k: None
    return mod


@pytest.fixture()
def env(monkeypatch):
    stub = Stub()
    fr = _make_frappe(stub)
    monkeypatch.setitem(sys.modules, "frappe", fr)
    monkeypatch.setitem(sys.modules, "frappe.utils", _utils_mod())
    monkeypatch.setitem(sys.modules, "frappe.utils.password", _password_mod(stub))
    monkeypatch.setitem(sys.modules, "frappe.sessions", _sessions_mod(stub))
    monkeypatch.setitem(sys.modules, "frappe.permissions", _permissions_mod(stub))
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", _audit_stub())
    admin_mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.admin"))
    _ = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.employee_profile"))
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.user_profile"))
    return stub, mod, admin_mod


# --------------------------------------------------------------------------- #
# Pure helpers — BE-UP-01 / BE-UP-02 / BE-UP-11 / BE-UP-12
# --------------------------------------------------------------------------- #
def test_be_up_01_summarize_logins(env):
    _, mod, _ = env
    rows = [
        {"operation": "Login", "creation": "2026-08-26 09:00:00"},
        {"operation": "Logout", "creation": "2026-08-27 09:30:00"},
        {"operation": "Login", "creation": None},  # thiếu creation → sort cuối
        "not-a-dict",  # payload lạ → bỏ, không raise
    ]
    out = mod.summarize_logins(rows, limit=2)
    assert [r["operation"] for r in out] == ["Logout", "Login"]
    assert mod.summarize_logins(None) == []


def test_be_up_02_left_employee_warning(env):
    _, mod, _ = env
    assert mod.left_employee_warning(1, "Left", "NV Nghỉ") is not None
    assert "đã nghỉ việc" in mod.left_employee_warning(1, "Left", "NV Nghỉ")
    assert mod.left_employee_warning(1, "Active", "NV Một") is None
    assert mod.left_employee_warning(0, "Left", "NV Nghỉ") is None
    assert mod.left_employee_warning(1, None) is None


def test_be_up_11_validate_security_window(env):
    _, mod, _ = env
    # after >= before → nghịch đảo / bằng nhau đều sai
    errs = mod.validate_security({"login_after": 18, "login_before": 8})
    assert any("không hợp lệ" in e for e in errs)
    errs = mod.validate_security({"login_after": 9, "login_before": 9})
    assert errs
    # ngoài 0-24
    assert mod.validate_security({"login_before": 25})
    assert mod.validate_security({"login_after": -1})
    # window hợp lệ 8→18
    assert mod.validate_security({"login_after": 8, "login_before": 18}) == []
    # giờ không phải số
    assert mod.validate_security({"login_before": "abc"})


def test_be_up_12_validate_security_sessions(env):
    _, mod, _ = env
    assert mod.validate_security({"simultaneous_sessions": 0})
    assert mod.validate_security({"simultaneous_sessions": "abc"})
    assert mod.validate_security({"simultaneous_sessions": 3}) == []
    assert mod.validate_security({}) == []


# --------------------------------------------------------------------------- #
# get_user_360 — BE-UP-03/04/05
# --------------------------------------------------------------------------- #
def test_be_up_03_get_user_360_payload(env):
    _, mod, _ = env
    payload = mod.get_user_360("u1@x.io")
    assert payload["user"]["email"] == "u1@x.io"
    assert "Employee" in payload["roles"]
    assert payload["linked_employee"]["name"] == "E1"
    assert payload["linked_employee"]["status"] == "Active"
    assert payload["left_employee_warning"] is None
    assert payload["activity"], "timeline gộp 3 nguồn phải có item"
    assert {"comment", "version", "audit"} <= {i["source"] for i in payload["activity"]}
    assert payload["logins"][0]["operation"] == "Logout"  # mới nhất trước
    assert payload["counts"]["comments"] == 1
    # scrub — không field nhạy cảm nào rời server
    for key in list(payload["user"].keys()) + list(payload.keys()):
        assert not any(s in key.lower() for s in ("password", "api_key", "api_secret"))
    # Employee Left nhưng enabled → warning
    left = mod.get_user_360("left@x.io")
    assert left["left_employee_warning"] and "nghỉ việc" in left["left_employee_warning"]


def test_be_up_04_get_user_360_missing_user(env):
    _, mod, _ = env
    with pytest.raises(FrappeError, match="Người dùng không tồn tại"):
        mod.get_user_360("ghost@x.io")


def test_be_up_05_get_user_360_requires_hr(env):
    stub, mod, _ = env
    stub.session_roles = {"Employee"}
    with pytest.raises(FrappeError):
        mod.get_user_360("u1@x.io")


# --------------------------------------------------------------------------- #
# add_user_comment — BE-UP-06/07
# --------------------------------------------------------------------------- #
def test_be_up_06_add_user_comment_too_short(env):
    _, mod, _ = env
    with pytest.raises(FrappeError, match="quá ngắn"):
        mod.add_user_comment("u1@x.io", " a ")
    with pytest.raises(FrappeError, match="quá ngắn"):
        mod.add_user_comment("u1@x.io", "")


def test_be_up_07_add_user_comment_creates_comment(env):
    stub, mod, _ = env
    res = mod.add_user_comment("u1@x.io", "  Ghi chú từ HR  ")
    assert res["name"] and res["creation"]
    inserted = [d for d in stub.inserted if d.get("doctype") == "Comment"]
    assert inserted, "phải tạo Comment row"
    assert inserted[-1]["reference_doctype"] == "User"
    assert inserted[-1]["reference_name"] == "u1@x.io"
    assert inserted[-1]["content"] == "Ghi chú từ HR"


# --------------------------------------------------------------------------- #
# logout_all_sessions — BE-UP-08/09/10
# --------------------------------------------------------------------------- #
def test_be_up_08_logout_all_sessions_blocks_self(env):
    _, mod, _ = env
    with pytest.raises(FrappeError, match="Không thể tự đăng xuất"):
        mod.logout_all_sessions("hr@test.io")


def test_be_up_09_logout_all_sessions_clears(env):
    stub, mod, _ = env
    res = mod.logout_all_sessions("u1@x.io")
    assert res == {"name": "u1@x.io", "ok": True}
    assert len(stub.cleared_sessions) == 1
    assert stub.cleared_sessions[0]["user"] == "u1@x.io"


def test_be_up_10_logout_all_sessions_missing(env):
    _, mod, _ = env
    with pytest.raises(FrappeError, match="Người dùng không tồn tại"):
        mod.logout_all_sessions("ghost@x.io")


# --------------------------------------------------------------------------- #
# update_user_security — BE-UP-13/14 (11/12 qua endpoint ở trên)
# --------------------------------------------------------------------------- #
def test_be_up_13_update_user_security_restrict_ip_sm_only(env):
    stub, mod, _ = env
    stub.session_roles = {"HR Manager"}
    with pytest.raises(FrappeError):
        mod.update_user_security("u1@x.io", **{"restrict_ip": "10.0.0.1"})
    # System Manager thì được
    stub.session_roles = {"System Manager"}
    res = mod.update_user_security("u1@x.io", **{"restrict_ip": "10.0.0.1"})
    assert "restrict_ip" in " ".join(res["updated"])
    assert stub.users["u1@x.io"]["restrict_ip"] == "10.0.0.1"


def test_be_up_14_update_user_security_whitelist(env):
    stub, mod, _ = env
    res = mod.update_user_security(
        "u1@x.io",
        **{
            "login_after": 8,
            "login_before": 18,
            "simultaneous_sessions": 4,
            "user_type": "Website User",  # ngoài whitelist → bỏ
        },
    )
    assert stub.users["u1@x.io"]["login_after"] == 8
    assert stub.users["u1@x.io"]["login_before"] == 18
    assert stub.users["u1@x.io"]["simultaneous_sessions"] == 4
    assert stub.users["u1@x.io"]["user_type"] == "System User"
    assert all("user_type" not in u for u in res["updated"])
    # window nghịch đảo → throw
    with pytest.raises(FrappeError, match="không hợp lệ"):
        mod.update_user_security("u1@x.io", **{"login_after": 18, "login_before": 8})


# --------------------------------------------------------------------------- #
# reset_user_password — BE-UP-15 (sửa admin.py §2.3.3)
# --------------------------------------------------------------------------- #
def test_be_up_15_reset_user_password_logout_flag(env):
    stub, _, admin_mod = env
    admin_mod.reset_user_password("disabled@x.io", new_password="12345678", send_email=0)
    assert stub.pwd_updates, "phải gọi update_password"
    assert stub.pwd_updates[-1]["logout_all_sessions"] is True
    # account bị khoá được re-enable để mật khẩu dùng được ngay
    assert stub.users["disabled@x.io"]["enabled"] == 1


# --------------------------------------------------------------------------- #
# User Permission — BE-UP-16/17/18
# --------------------------------------------------------------------------- #
def test_be_up_16_list_user_permissions(env):
    stub, mod, _ = env
    rows = mod.list_user_permissions("u1@x.io")
    assert rows and rows[0]["allow"] == "Company"
    assert rows[0]["value"] == "GeGe"
    stub.session_roles = {"Employee"}
    with pytest.raises(FrappeError):
        mod.list_user_permissions("u1@x.io")


def test_be_up_17_add_user_permission_guards(env):
    stub, mod, _ = env
    with pytest.raises(FrappeError, match="không được phép"):
        mod.add_user_permission("u1@x.io", allow="ToDo", value="X")
    with pytest.raises(FrappeError, match="chính tài khoản đang dùng"):
        mod.add_user_permission("hr@test.io", allow="Company", value="GeGe")
    res = mod.add_user_permission("u1@x.io", allow="Company", value="GeGe", is_default=1)
    assert res["ok"] is True
    assert stub.perm_adds[-1] == {
        "allow": "Company",
        "value": "GeGe",
        "user": "u1@x.io",
        "is_default": 1,
    }


def test_be_up_18_remove_user_permission_guard(env):
    stub, mod, _ = env
    with pytest.raises(FrappeError, match="không thuộc người dùng này"):
        mod.remove_user_permission("UP-1", user="hr@test.io")
    res = mod.remove_user_permission("UP-1", user="u1@x.io")
    assert res["deleted"] is True
    assert ("User Permission", "UP-1") in stub.deleted
    with pytest.raises(FrappeError, match="không tồn tại"):
        mod.remove_user_permission("UP-X", user="u1@x.io")


# --------------------------------------------------------------------------- #
# Role Profile — BE-UP-19
# --------------------------------------------------------------------------- #
def test_be_up_19_set_user_role_profile(env):
    stub, mod, _ = env
    with pytest.raises(FrappeError, match="Nhóm vai trò không tồn tại"):
        mod.set_user_role_profile("u1@x.io", "Ghost Profile")
    res = mod.set_user_role_profile("u1@x.io", "HR Chi nhánh")
    assert res["role_profile"] == "HR Chi nhánh"
    assert "Employee" in res["roles_before"]
    assert stub.users["u1@x.io"]["role_profile_name"] == "HR Chi nhánh"
    # clear profile
    res = mod.set_user_role_profile("u1@x.io", None)
    assert res["role_profile"] is None
    assert stub.users["u1@x.io"]["role_profile_name"] is None


# --------------------------------------------------------------------------- #
# bulk_assign_roles — BE-UP-20/21
# --------------------------------------------------------------------------- #
def test_be_up_20_bulk_assign_roles_partial_safe(env):
    stub, mod, _ = env
    res = mod.bulk_assign_roles(["u1@x.io", "left@x.io", "ghost@x.io"], "HR User")
    assert sorted(res["updated"]) == ["left@x.io", "u1@x.io"]
    assert len(res["failed"]) == 1 and res["failed"][0]["user"] == "ghost@x.io"
    # idempotent — gọi lại 2 user thành công không fail
    res2 = mod.bulk_assign_roles(["u1@x.io", "left@x.io"], "HR User")
    assert res2["failed"] == [] and len(res2["updated"]) == 2
    assert "HR User" in stub.users["u1@x.io"]["_roles"]


def test_be_up_21_bulk_assign_roles_portal_only(env):
    _, mod, _ = env
    with pytest.raises(FrappeError, match="không được phép"):
        mod.bulk_assign_roles(["u1@x.io"], "System Manager")


# --------------------------------------------------------------------------- #
# list_users P3 — BE-UP-22
# --------------------------------------------------------------------------- #
def test_be_up_22_list_users_filters_and_no_role(env):
    _, _, admin_mod = env
    # 5 user thật (hr, u1, left, norole, disabled) + bucket no_role = 1
    out = admin_mod.list_users(page_size=10)
    assert out["total"] == 5
    assert out["summary"]["no_role"] == 1
    assert out["summary"]["active"] == 4
    # filter role=Employee → u1 + left + disabled
    out = admin_mod.list_users(role="Employee", page_size=10)
    assert out["total"] == 3
    assert {r["name"] for r in out["data"]} == {"u1@x.io", "left@x.io", "disabled@x.io"}
    # has_employee=unlinked → hr + norole + disabled (chưa link Employee)
    out = admin_mod.list_users(has_employee="unlinked", page_size=10)
    assert out["total"] == 3
    assert {r["name"] for r in out["data"]} == {"hr@test.io", "norole@x.io", "disabled@x.io"}
    # user_type
    out = admin_mod.list_users(user_type="Website User", page_size=10)
    assert out["total"] == 0


# --------------------------------------------------------------------------- #
# bulk_set_users_enabled (P3) — bonus guard checks
# --------------------------------------------------------------------------- #
def test_bulk_set_users_enabled_guards(env):
    stub, mod, _ = env
    res = mod.bulk_set_users_enabled(["hr@test.io", "Administrator", "u1@x.io"], enabled=0)
    assert res["updated"] == ["u1@x.io"]
    assert stub.users["u1@x.io"]["enabled"] == 0
    assert {f["user"] for f in res["failed"]} == {"hr@test.io", "Administrator"}
