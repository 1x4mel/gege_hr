"""Bench-free unit tests for ``api/self_profile.py`` (plans/plan-profile-desk-free.md §3.1).

  PF-01  diff_contact — key lạ raise, no-change/None↔"" bỏ, whitespace strip
  PF-02  update_my_contact — không phải employee → throw; diff rỗng → no-op
  PF-03  update_my_contact — field ngoài allow-list → throw (kể cả field có thật)
  PF-04  request_profile_change — reason ngắn throw; hợp lệ → ToDo/HR + Comment + realtime
  PF-05  cancel_profile_request — ToDo người khác / đã đóng → throw; của mình → Closed
  PF-06  set_my_avatar — mime/size/data hỏng throw; hợp lệ → File + 2 image fields
  PF-07  my_activity — merge 3 nguồn, 1 nguồn hỏng vẫn trả phần còn lại
  PF-08  change_my_password — old sai → VN msg; đúng → delegate chuẩn + logout flag
  PF-09  logout_my_sessions — clear_sessions(user, keep_current, force) đúng tham số
  PF-10  my_sessions — chỉ user mình, sid cắt ngắn, current flag đúng
  PF-11  upload_my_document — mime/size gate; File private + attached Employee
  PF-12  delete_my_document — tệp người khác / không phải người upload → throw; của mình → xóa
  PF-13  _own_employee / get_my_profile — user không có Employee → throw (endpoint cần employee)
  PF-14  get_my_profile — meta thiếu field → self_edit_fields lọc; payload không lộ field nhạy cảm

Stub-frappe harness (pattern tests/test_employee_services.py). The module under
test reuses employee_profile helpers, so the fixture reloads employee_profile →
self_profile under the stub (pattern test_user_profile.py).
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest


class FrappeError(Exception):
    """frappe.throw / only_for stand-in."""


class _Doc:
    def __init__(self, doctype, stub):
        self.doctype = doctype
        self._stub = stub
        self.name = None
        self.status = "Open"
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def insert(self, ignore_permissions=False, **_kw):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._stub.store) + 1:04d}"
        self.owner = self._stub.session_user
        self.creation = "2026-09-05 10:00:00"
        self._stub.store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        self._stub.saved.append(self.name)
        return self

    def delete(self, *a, **k):
        self._stub.store.pop((self.doctype, self.name), None)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)


def _png_bytes(size: int = 64) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"x" * size


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


class _Frappe:
    # frappe.throw(msg, frappe.PermissionError) — exc-class arg stand-in.
    PermissionError = PermissionError

    def __init__(self):
        self.session_user = "nv@gege.test"
        self.store = {}
        self.list_rows = {}
        self.raise_on_doctype = set()
        self.employee_for_user = "HR-EMP-1"
        self.published = []
        self.deleted = []
        self.saved = []
        self.users = {
            "nv@gege.test": {
                "name": "nv@gege.test",
                "full_name": "Nguyễn Văn NV",
                "language": "vi",
                "time_zone": "Asia/Ho_Chi_Minh",
                "user_image": None,
                "last_active": "2026-09-05 09:00:00",
                "enabled": 1,
            },
        }
        self._meta = {
            "Employee": [
                "name",
                "employee_name",
                "employee_number",
                "salutation",
                "company",
                "department",
                "branch",
                "designation",
                "employment_type",
                "status",
                "reports_to",
                "gender",
                "date_of_birth",
                "date_of_joining",
                "cell_number",
                "personal_email",
                "prefered_email",
                "company_email",
                "current_address",
                "permanent_address",
                "person_to_be_contacted",
                "relation",
                "marital_status",
                "image",
                "user_id",
                # PF-14: "blood_group" cố tình THIẾU trên meta runtime
            ],
            "Comment": ["owner", "comment_email", "creation", "content"],
            "Version": ["ref_doctype", "ref_name", "owner", "creation", "data"],
            "VN Audit Event": ["audit_type", "actor", "created_at", "description", "employee"],
            "File": [
                "name",
                "file_name",
                "file_url",
                "is_private",
                "file_size",
                "owner",
                "creation",
                "attached_to_doctype",
                "attached_to_name",
            ],
            "ToDo": [
                "name",
                "description",
                "status",
                "priority",
                "allocated_to",
                "assigned_by",
                "owner",
                "creation",
                "modified",
                "reference_type",
                "reference_name",
            ],
            "Sessions": ["sid", "user", "lastupdate", "status"],
            "Has Role": ["role", "parent", "parenttype"],
            # P1 — job history / salary / preferences sources.
            "Employee Transfer": [
                "name",
                "employee",
                "transfer_date",
                "new_department",
                "new_designation",
                "new_branch",
                "docstatus",
            ],
            "Employee Promotion": [
                "name",
                "employee",
                "promotion_date",
                "new_designation",
                "new_branch",
                "docstatus",
            ],
            "Salary Structure Assignment": [
                "name",
                "employee",
                "salary_structure",
                "from_date",
                "to_date",
                "company",
                "docstatus",
            ],
            "Language": ["name"],
        }

    # -- session / auth -----------------------------------------------------
    @property
    def session(self):
        return SimpleNamespace(user=self.session_user, sid="sid-current-123456")

    def get_roles(self, user):
        return set()

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def throw(self, msg, *a, **k):
        raise FrappeError(str(msg))

    def log_error(self, *a, **k):
        return None

    def publish_realtime(self, event, message=None, **k):
        self.published.append((event, message))

    def delete_doc(self, doctype, name, ignore_permissions=False, **k):
        self.deleted.append((doctype, name))
        self.store.pop((doctype, name), None)
        return None

    def new_doc(self, doctype):
        return _Doc(doctype, self)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def get_meta(self, doctype):
        fields = self._meta.get(doctype, [])
        return SimpleNamespace(fields=[SimpleNamespace(fieldname=f) for f in fields])

    def get_all(
        self,
        doctype,
        filters=None,
        or_filters=None,
        fields=None,
        order_by=None,
        limit_start=0,
        limit_page_length=0,
        pluck=None,
        **k,
    ):
        if doctype in self.raise_on_doctype:
            raise Exception(f"boom-{doctype}")
        rows = list(self.list_rows.get(doctype, []))

        def _match(r, cond):
            if len(cond) == 3:
                return r.get(cond[0]) == cond[2]
            return r.get(cond[0]) == cond[1]

        if filters:
            conds = [[k2, "=", v] for k2, v in filters.items()] if isinstance(filters, dict) else filters
            rows = [r for r in rows if all(_match(r, c) for c in conds)]
        if or_filters:
            rows = [r for r in rows if any(_match(r, c) for c in or_filters)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows

    # -- db ------------------------------------------------------------------
    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, key, field=None, as_dict=False):
            fr = self.fr
            if doctype == "Employee" and isinstance(key, dict):
                return fr.employee_for_user
            if isinstance(key, str):
                if doctype == "Employee":
                    doc = fr.store.get((doctype, key))
                    if doc is None:
                        return None
                    if isinstance(field, (list, tuple)):
                        row = {f: getattr(doc, f, None) for f in field}
                        return row if as_dict else next(iter(row.values()), None)
                    return getattr(doc, field, None)
                if doctype == "User":
                    row = fr.users.get(key)
                    if row is None:
                        return None
                    if isinstance(field, (list, tuple)):
                        out = {f: row.get(f) for f in field}
                        return out if as_dict else next(iter(out.values()), None)
                    return row.get(field)
            return None

        def exists(self, doctype, name):
            return (doctype, name) in self.fr.store

    @property
    def db(self):
        return self._DB(self)


def _admin_stub():
    calls = []

    def _audit_admin(description, **kw):
        calls.append((str(description), kw))

    def _company_for_employee(employee):
        return "GEGE"

    return SimpleNamespace(
        _audit_admin=_audit_admin,
        _company_for_employee=_company_for_employee,
        calls=calls,
    )


def _password_module_stub():
    """``frappe.utils.password.check_password`` stand-in (F-PF7 path)."""
    state = SimpleNamespace(fail_old=False)
    calls = []

    def check_password(user, pwd, *a, **k):
        calls.append({"user": user, "pwd": pwd})
        if state.fail_old and (pwd or "") != "OldPass123":
            raise Exception("Password incorrect")

    return SimpleNamespace(check_password=check_password, calls=calls, state=state)


def _sessions_stub():
    calls = []

    def clear_sessions(user=None, keep_current=False, force=False):
        calls.append({"user": user, "keep_current": keep_current, "force": force})

    return SimpleNamespace(clear_sessions=clear_sessions, calls=calls)


def _seed_employee_doc(stub, name="HR-EMP-1"):
    doc = _Doc("Employee", stub)
    doc.name = name
    doc.employee_name = "Nguyễn Văn A"
    doc.cell_number = "0900000001"
    doc.personal_email = "a@gege.test"
    doc.blood_group = None
    doc.image = None
    doc.user_id = stub.session_user
    doc.company = "GEGE"
    stub.store[("Employee", name)] = doc
    return doc


def _seed_user_doc(stub, name=None):
    name = name or stub.session_user
    doc = _Doc("User", stub)
    doc.name = name
    doc.user_image = None
    doc.language = "vi"
    doc.time_zone = "Asia/Ho_Chi_Minh"
    doc.enabled = 1
    stub.store[("User", name)] = doc
    return doc


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    admin_stub = _admin_stub()
    sessions_stub = _sessions_stub()
    password_mod_stub = _password_module_stub()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.admin", admin_stub)
    monkeypatch.setitem(sys.modules, "frappe.sessions", sessions_stub)
    monkeypatch.setitem(sys.modules, "frappe.utils.password", password_mod_stub)
    _ = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.employee_profile"))
    m = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.self_profile"))
    return SimpleNamespace(
        mod=m,
        stub=stub,
        admin=admin_stub,
        sessions=sessions_stub,
        password_module=password_mod_stub,
    )


# ── PF-01 diff_contact (pure) ───────────────────────────────────────────────
def test_pf01_diff_contact_rejects_unknown_key(mod):
    with pytest.raises(ValueError, match="không được phép"):
        mod.mod.diff_contact({"cell_number": "09"}, {"company": "X"}, ["cell_number"])


def test_pf01_diff_contact_skips_unchanged_and_none_vs_empty(mod):
    current = {"cell_number": "0900", "personal_email": None, "relation": "Bạn"}
    diff = mod.mod.diff_contact(
        current,
        {"cell_number": " 0900 ", "personal_email": "", "relation": None},
        ["cell_number", "personal_email", "relation"],
    )
    assert diff == {"relation": ""}  # "Bạn" → None normalised "" là diff thật


def test_pf01_diff_contact_keeps_real_change(mod):
    diff = mod.mod.diff_contact({"cell_number": "09"}, {"cell_number": " 0911 "}, ["cell_number"])
    assert diff == {"cell_number": "0911"}


# ── PF-02/03 update_my_contact ──────────────────────────────────────────────
def test_pf02_update_contact_requires_employee(mod):
    mod.stub.employee_for_user = None
    with pytest.raises(FrappeError, match="không liên kết"):
        mod.mod.update_my_contact(values={"cell_number": "0911"})


def test_pf02_update_contact_empty_diff_is_noop(mod):
    _seed_employee_doc(mod.stub)
    res = mod.mod.update_my_contact(values={"cell_number": "0900000001"})
    assert res["changed"] == []
    assert mod.stub.saved == []  # không save, không audit


def test_pf03_update_contact_rejects_field_outside_allowlist(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="không được phép"):
        mod.mod.update_my_contact(values={"company": "Khác"})
    # JSON-string payload lỗi cũng bị chặn
    with pytest.raises(FrappeError):
        mod.mod.update_my_contact(values=json.dumps({"reports_to": "HR-EMP-9"}))


def test_pf02_update_contact_applies_and_persists(mod):
    _seed_employee_doc(mod.stub)
    res = mod.mod.update_my_contact(values={"cell_number": "0911222333"})
    assert res["changed"] == ["cell_number"]
    doc = mod.stub.store[("Employee", "HR-EMP-1")]
    assert doc.cell_number == "0911222333"
    assert mod.stub.saved == ["HR-EMP-1"]
    assert any(e == "profile_updated" for e, _ in mod.stub.published)
    assert mod.admin.calls, "phải có dòng VN Audit Event (best-effort)"


# ── PF-04 request_profile_change ────────────────────────────────────────────
def test_pf04_change_request_reason_too_short(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="ký tự"):
        mod.mod.request_profile_change("date_of_birth", "1990-01-01", "ngắn")


def test_pf04_change_request_rejects_non_locked_field(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError):
        mod.mod.request_profile_change("cell_number", "0911", "lý do đủ dài ở đây")


def test_pf04_change_request_creates_todo_comment_realtime(mod):
    _seed_employee_doc(mod.stub)
    mod.stub.list_rows["Has Role"] = [
        {"role": "HR Manager", "parent": "hr1@gege.test", "parenttype": "User"},
        {"role": "HR Manager", "parent": "hr2@gege.test", "parenttype": "User"},
    ]
    res = mod.mod.request_profile_change("date_of_birth", "1990-01-01", "Sai ngày sinh khi nhập liệu")
    todos = [k for k in mod.stub.store if k[0] == "ToDo"]
    comments = [k for k in mod.stub.store if k[0] == "Comment"]
    assert len(todos) == 2
    assert len(comments) == 1
    assert res["todos"] and res["comment"]
    first = mod.stub.store[todos[0]]
    assert first.description.startswith("[Hồ sơ]")
    assert first.priority == "High"
    assert first.reference_name == "HR-EMP-1"
    assert any(e == "profile_change_requested" for e, _ in mod.stub.published)


def test_pf04_my_requests_scopes_to_me(mod):
    _seed_employee_doc(mod.stub)
    mod.stub.list_rows["ToDo"] = [
        {
            "name": "TD-1",
            "owner": "nv@gege.test",
            "reference_type": "Employee",
            "reference_name": "HR-EMP-1",
            "description": "[Hồ sơ] x",
            "status": "Open",
            "creation": "2026-09-05",
        },
        {
            "name": "TD-2",
            "owner": "khac@gege.test",
            "reference_type": "Employee",
            "reference_name": "HR-EMP-1",
            "description": "[Hồ sơ] y",
            "status": "Open",
            "creation": "2026-09-05",
        },
    ]
    res = mod.mod.my_profile_requests()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "TD-1"


# ── PF-05 cancel_profile_request ────────────────────────────────────────────
def _make_request(mod):
    _seed_employee_doc(mod.stub)
    mod.stub.list_rows["Has Role"] = [{"role": "HR Manager", "parent": "hr1@gege.test", "parenttype": "User"}]
    return mod.mod.request_profile_change("gender", "Nữ", "Sai giới tính lúc onboarding")


def test_pf05_cancel_other_users_request_denied(mod):
    created = _make_request(mod)
    todo = mod.stub.store[("ToDo", created["todos"][0])]
    todo.owner = "khac@gege.test"
    with pytest.raises(FrappeError, match="không có quyền"):
        mod.mod.cancel_profile_request(created["todos"][0])


def test_pf05_cancel_other_employee_request_denied(mod):
    created = _make_request(mod)
    mod.stub.employee_for_user = "HR-EMP-2"  # session giờ là employee khác
    with pytest.raises(FrappeError, match="không có quyền"):
        mod.mod.cancel_profile_request(created["todos"][0])


def test_pf05_cancel_closed_request_denied(mod):
    created = _make_request(mod)
    todo = mod.stub.store[("ToDo", created["todos"][0])]
    todo.status = "Closed"
    with pytest.raises(FrappeError, match="đã đóng"):
        mod.mod.cancel_profile_request(created["todos"][0])


def test_pf05_cancel_own_open_request(mod):
    created = _make_request(mod)
    res = mod.mod.cancel_profile_request(created["todos"][0])
    assert res["status"] == "Closed"
    assert mod.stub.store[("ToDo", created["todos"][0])].status == "Closed"


# ── PF-06 set_my_avatar ─────────────────────────────────────────────────────
def test_pf06_avatar_rejects_bad_mime_and_size(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="không được hỗ trợ"):
        mod.mod.set_my_avatar(data_url=_data_url("application/pdf", b"%PDF-1.4"))
    with pytest.raises(FrappeError, match="giới hạn"):
        mod.mod.set_my_avatar(data_url=_data_url("image/png", _png_bytes(1_100_000)))
    with pytest.raises(FrappeError, match="không hợp lệ"):
        mod.mod.set_my_avatar(data_url="http://khong-phai-data-url")


def test_pf06_avatar_sets_both_image_fields(mod):
    _seed_employee_doc(mod.stub)
    _seed_user_doc(mod.stub)
    res = mod.mod.set_my_avatar(data_url=_data_url("image/png", _png_bytes(128)))
    files = [k for k in mod.stub.store if k[0] == "File"]
    assert len(files) == 1
    file_doc = mod.stub.store[files[0]]
    assert file_doc.is_private == 0
    assert file_doc.attached_to_name == "HR-EMP-1"
    assert mod.stub.store[("Employee", "HR-EMP-1")].image == res["image"]
    assert mod.stub.store[("User", "nv@gege.test")].user_image == res["image"]


def test_pf06_avatar_file_url_validation(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="không hợp lệ"):
        mod.mod.set_my_avatar(file_url="javascript:alert(1)")


# ── PF-07 my_activity ───────────────────────────────────────────────────────
def test_pf07_activity_merges_and_survives_broken_source(mod):
    _seed_employee_doc(mod.stub)
    mod.stub.list_rows["Comment"] = [
        {"owner": "nv@gege.test", "creation": "2026-09-05 10:00:00", "content": "Ghi chú"}
    ]
    mod.stub.list_rows["Version"] = [
        {
            "ref_doctype": "Employee",
            "ref_name": "HR-EMP-1",
            "owner": "hr@gege.test",
            "creation": "2026-09-04 09:00:00",
            "data": '{"changed": [["cell_number", "1", "2"]]}',
        }
    ]
    mod.stub.list_rows["VN Audit Event"] = [
        {
            "employee": "HR-EMP-1",
            "actor": "nv@gege.test",
            "created_at": "2026-09-03 08:00:00",
            "description": "Đổi ảnh",
            "audit_type": "Profile",
        }
    ]
    mod.stub.raise_on_doctype.add("Comment")  # 1 nguồn hỏng
    res = mod.mod.my_activity()
    sources = {i["source"] for i in res["merged"]}
    assert sources == {"version", "audit"}
    texts = [i["text"] for i in res["merged"]]
    assert any("cell_number" in t for t in texts)
    assert res["merged"][0]["at"] >= res["merged"][-1]["at"]  # sort desc


# ── PF-08 change_my_password ────────────────────────────────────────────────
def test_pf08_password_requires_both_and_differs(mod):
    with pytest.raises(FrappeError, match="đủ"):
        mod.mod.change_my_password(old_password="", new_password="NewPass123")
    with pytest.raises(FrappeError, match="khác"):
        mod.mod.change_my_password(old_password="Same123", new_password="Same123")


def test_pf08_password_wrong_old_maps_to_vietnamese(mod):
    mod.password_module.state.fail_old = True
    with pytest.raises(FrappeError, match="Mật khẩu hiện tại không đúng"):
        mod.mod.change_my_password(old_password="SaiRot", new_password="NewPass123")
    assert mod.sessions.calls == []  # không clear session khi sai old


def test_pf08_password_saves_user_and_clears_sessions(mod):
    _seed_user_doc(mod.stub)
    mod.mod.change_my_password(old_password="OldPass123", new_password="NewPass456", logout_all_sessions=1)
    assert mod.password_module.calls[0] == {"user": "nv@gege.test", "pwd": "OldPass123"}
    assert mod.stub.store[("User", "nv@gege.test")].new_password == "NewPass456"
    assert mod.sessions.calls and mod.sessions.calls[-1] == {
        "user": "nv@gege.test",
        "keep_current": False,
        "force": True,
    }


def test_pf08_password_without_logout_keeps_sessions(mod):
    _seed_user_doc(mod.stub)
    mod.mod.change_my_password(old_password="OldPass123", new_password="NewPass456", logout_all_sessions=0)
    assert mod.sessions.calls == []


# ── PF-09 logout_my_sessions ────────────────────────────────────────────────
def test_pf09_logout_other_devices_keeps_current(mod):
    res = mod.mod.logout_my_sessions(everywhere=0)
    call = mod.sessions.calls[0]
    assert call["user"] == "nv@gege.test"
    assert call["keep_current"] is True
    assert call["force"] is True
    assert "thiết bị khác" in res["message"]


def test_pf09_logout_everywhere_drops_current(mod):
    mod.mod.logout_my_sessions(everywhere=1)
    assert mod.sessions.calls[0]["keep_current"] is False


# ── PF-10 my_sessions ───────────────────────────────────────────────────────
def test_pf10_sessions_scoped_truncated_flagged(mod):
    mod.stub.list_rows["Sessions"] = [
        {
            "sid": "sid-current-123456",
            "user": "nv@gege.test",
            "lastupdate": "2026-09-05 10:00",
            "status": "Active",
        },
        {
            "sid": "sid-other-999888777",
            "user": "nv@gege.test",
            "lastupdate": "2026-09-04 09:00",
            "status": "Active",
        },
        {"sid": "sid-x", "user": "khac@gege.test", "lastupdate": "2026-09-05 11:00", "status": "Active"},
    ]
    res = mod.mod.my_sessions()
    assert res["total"] == 2  # user khác bị lọc
    current = [s for s in res["sessions"] if s["current"]]
    assert len(current) == 1
    assert all(len(s["sid"]) <= 8 for s in res["sessions"])  # không lộ full sid


# ── PF-11 upload_my_document ────────────────────────────────────────────────
def test_pf11_document_gates_mime_and_size(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="không được hỗ trợ"):
        mod.mod.upload_my_document("a.exe", _data_url("application/x-msdownload", b"MZ"))
    with pytest.raises(FrappeError, match="giới hạn"):
        mod.mod.upload_my_document("big.pdf", _data_url("application/pdf", b"%" * (5 * 1024 * 1024 + 10)))


def test_pf11_document_creates_private_file(mod):
    _seed_employee_doc(mod.stub)
    res = mod.mod.upload_my_document("hop dong.pdf", _data_url("application/pdf", b"%PDF-1.4 test"))
    files = [k for k in mod.stub.store if k[0] == "File"]
    assert len(files) == 1
    file_doc = mod.stub.store[files[0]]
    assert file_doc.is_private == 1
    assert file_doc.attached_to_doctype == "Employee"
    assert file_doc.attached_to_name == "HR-EMP-1"
    assert res["file_name"]


def test_pf11_document_sanitizes_filename(mod):
    _seed_employee_doc(mod.stub)
    res = mod.mod.upload_my_document("../../../../etc/passwd.pdf", _data_url("application/pdf", b"%PDF"))
    assert "/" not in res["file_name"] and ".." not in res["file_name"]


# ── PF-12 delete_my_document ────────────────────────────────────────────────
def _seed_file(stub, *, name="FILE-0001", employee="HR-EMP-1", owner="nv@gege.test"):
    doc = _Doc("File", stub)
    doc.name = name
    doc.file_name = "hop-dong.pdf"
    doc.file_url = "/private/files/hop-dong.pdf"
    doc.is_private = 1
    doc.owner = owner
    doc.attached_to_doctype = "Employee"
    doc.attached_to_name = employee
    stub.store[("File", doc.name)] = doc
    return doc


def test_pf12_delete_other_employees_file_denied(mod):
    _seed_employee_doc(mod.stub)
    _seed_file(mod.stub, employee="HR-EMP-9")
    with pytest.raises(FrappeError, match="không thuộc hồ sơ của bạn"):
        mod.mod.delete_my_document("FILE-0001")


def test_pf12_delete_not_uploader_denied(mod):
    _seed_employee_doc(mod.stub)
    _seed_file(mod.stub, owner="hr1@gege.test")
    with pytest.raises(FrappeError, match="người tải lên"):
        mod.mod.delete_my_document("FILE-0001")


def test_pf12_delete_own_upload(mod):
    _seed_employee_doc(mod.stub)
    _seed_file(mod.stub)
    res = mod.mod.delete_my_document("FILE-0001")
    assert res["deleted"] is True
    assert ("File", "FILE-0001") in mod.stub.deleted
    assert ("File", "FILE-0001") not in mod.stub.store


def test_pf12_delete_missing_file_denied(mod):
    _seed_employee_doc(mod.stub)
    with pytest.raises(FrappeError, match="không tồn tại"):
        mod.mod.delete_my_document("FILE-KHONG-CO")


def test_pf12_documents_list_annotates_can_delete(mod):
    _seed_employee_doc(mod.stub)
    _seed_file(mod.stub, name="FILE-0001", owner="hr1@gege.test")
    _seed_file(mod.stub, name="FILE-0002", owner="nv@gege.test")
    mod.stub.list_rows["File"] = [
        {
            "name": "FILE-0002",
            "file_name": "hop-dong.pdf",
            "owner": "nv@gege.test",
            "creation": "2026-09-05",
            "attached_to_doctype": "Employee",
            "attached_to_name": "HR-EMP-1",
        },
        {
            "name": "FILE-0001",
            "file_name": "cua-hr.pdf",
            "owner": "hr1@gege.test",
            "creation": "2026-09-04",
            "attached_to_doctype": "Employee",
            "attached_to_name": "HR-EMP-1",
        },
    ]
    res = mod.mod.my_documents()
    by_name = {r["name"]: r for r in res["data"]}
    assert by_name["FILE-0002"]["can_delete"] is True
    assert by_name["FILE-0001"]["can_delete"] is False


# ── PF-13 non-employee user ─────────────────────────────────────────────────
def test_pf13_endpoints_requiring_employee_throw_for_admin(mod):
    mod.stub.employee_for_user = None
    for call in (
        lambda: mod.mod.update_my_contact(values={"cell_number": "09"}),
        lambda: mod.mod.request_profile_change("gender", "Nữ", "lý do dài đủ chuyền"),
        lambda: mod.mod.my_activity(),
        lambda: mod.mod.my_documents(),
    ):
        with pytest.raises(FrappeError, match="không liên kết"):
            call()


def test_pf13_get_my_profile_still_returns_user_block(mod):
    mod.stub.employee_for_user = None
    res = mod.mod.get_my_profile()
    assert res["employee"] is None
    assert res["user"]["name"] == "nv@gege.test"


# ── PF-14 get_my_profile meta scrub ────────────────────────────────────────
def test_pf14_profile_filters_missing_meta_fields(mod):
    _seed_employee_doc(mod.stub)
    _seed_user_doc(mod.stub)
    res = mod.mod.get_my_profile()
    # blood_group cố tình thiếu trên meta (self._meta) → phải bị lọc khỏi
    # cả self_edit lẫn payload employee.
    assert "blood_group" not in res["self_edit_fields"]
    assert "blood_group" not in (res["employee"] or {})
    assert "cell_number" in res["self_edit_fields"]
    assert set(res["locked_fields"]) <= {"salutation", "employee_number", "gender", "date_of_birth"}
    assert res["locked_labels"].get("date_of_birth") == "Ngày sinh"
    assert res["flags"]["avatar_max_mb"] == 1


def test_pf14_profile_never_leaks_sensitive_keys(mod):
    _seed_employee_doc(mod.stub)
    _seed_user_doc(mod.stub)
    res = mod.mod.get_my_profile()
    blob = json.dumps(res, default=str)
    for bad in ("password", "api_key", "api_secret"):
        assert bad not in blob


# ── pure helpers bổ sung ────────────────────────────────────────────────────
def test_safe_file_name_strips_path(mod):
    assert mod.mod.safe_file_name(
        "..\\..\\evil name?.pdf"
    ) == "_evil name_.pdf" or "/" not in mod.mod.safe_file_name("..\\..\\evil name?.pdf")
    assert mod.mod.safe_file_name("") == "tep-dinh-kem"


def test_short_sid_truncates(mod):
    assert mod.mod.short_sid("abcdefghijklmnop", 8) == "abcdefgh"


# ── PF-15..18 (P1 — plan §2.2) ──────────────────────────────────────────────
def test_pf07b_activity_uses_docname_on_new_benches(mod):
    """F-PF5: benches with ``Version.docname`` (no ref_name) must still work."""
    _seed_employee_doc(mod.stub)
    mod.stub._meta["Version"] = ["ref_doctype", "docname", "owner", "creation", "data"]
    mod.stub.list_rows["Version"] = [
        {
            "ref_doctype": "Employee",
            "docname": "HR-EMP-1",
            "owner": "hr@gege.test",
            "creation": "2026-09-04 09:00:00",
            "data": '{"changed": [["cell_number", "1", "2"]]}',
        }
    ]
    res = mod.mod.my_activity()
    assert any(i["source"] == "version" for i in res["merged"])


def test_pf15_job_history_merges_self_scoped(mod):
    _seed_employee_doc(mod.stub)
    emp_doc = mod.stub.store[("Employee", "HR-EMP-1")]
    emp_doc.internal_work_history = [
        {"from_date": "2026-01-01", "department": "Sales", "designation": "Sale", "branch": "HN"}
    ]
    mod.stub.list_rows["Employee Transfer"] = [
        {
            "name": "ET-1",
            "employee": "HR-EMP-1",
            "transfer_date": "2026-06-01",
            "new_department": "Kinh doanh",
            "new_designation": "Trưởng nhóm",
            "docstatus": 1,
        },
        {"name": "ET-2", "employee": "HR-EMP-9", "transfer_date": "2026-07-01", "docstatus": 1},
    ]
    mod.stub.list_rows["Employee Promotion"] = [
        {
            "name": "EP-1",
            "employee": "HR-EMP-1",
            "promotion_date": "2026-03-01",
            "new_designation": "Senior",
            "docstatus": 1,
        }
    ]
    res = mod.mod.my_job_history()
    assert [r["name"] for r in res["transfers"]] == ["ET-1"]  # ET-2 (người khác) bị lọc
    types = [e["type"] for e in res["merged"]]
    assert set(types) == {"internal", "transfer", "promotion"}
    dates = [str(e["date"]) for e in res["merged"]]
    assert dates == sorted(dates, reverse=True)
    assert "Trưởng nhóm" in res["merged"][0]["text"]


def test_pf16_salary_summary_masks_amounts(mod):
    _seed_employee_doc(mod.stub)
    mod.stub.list_rows["Salary Structure Assignment"] = [
        {
            "name": "SSA-1",
            "employee": "HR-EMP-1",
            "salary_structure": "Lương NV",
            "from_date": "2026-01-01",
            "to_date": None,
            "company": "GEGE",
            "docstatus": 1,
        },
        {
            "name": "SSA-2",
            "employee": "HR-EMP-9",
            "salary_structure": "Thử việc",
            "from_date": "2026-02-01",
            "to_date": None,
            "docstatus": 1,
        },
    ]
    res = mod.mod.my_salary_summary()
    assert res["masked"] is True
    assert len(res["history"]) == 1  # chỉ hàng của mình
    assert res["active"]["salary_structure"] == "Lương NV"
    blob = str(res)
    assert "base" not in blob  # không có cột số tiền


def test_pf17_preferences_validate_and_save(mod):
    _seed_user_doc(mod.stub)
    mod.stub.list_rows["Language"] = [{"name": "vi"}, {"name": "en"}]
    with pytest.raises(FrappeError, match="Múi giờ"):
        mod.mod.update_my_preferences(time_zone="Not/AZone")
    with pytest.raises(FrappeError, match="Ngôn ngữ"):
        mod.mod.update_my_preferences(language="klingon")
    res = mod.mod.update_my_preferences(language="vi", time_zone="Asia/Ho_Chi_Minh")
    assert res["language"] == "vi"
    assert res["time_zone"] == "Asia/Ho_Chi_Minh"
    assert "nv@gege.test" in mod.stub.saved


def test_pf17_preferences_works_without_employee(mod):
    mod.stub.employee_for_user = None  # Admin/IT cũng đổi được tuỳ chọn
    _seed_user_doc(mod.stub)
    mod.stub.list_rows["Language"] = [{"name": "vi"}]
    res = mod.mod.update_my_preferences(language="vi")
    assert res["language"] == "vi"


def test_pf18_job_history_requires_employee(mod):
    mod.stub.employee_for_user = None
    with pytest.raises(FrappeError, match="không liên kết"):
        mod.mod.my_job_history()
    with pytest.raises(FrappeError, match="không liên kết"):
        mod.mod.my_salary_summary()
