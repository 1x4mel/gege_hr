"""Employee 360° profile API (plans/plan-employee-frontend-parity.md §2.1–§2.2).

  BE-EP-01  scrub_meta_fields / group_sections / parse_depends_on (pure)
  BE-EP-02  _EMPLOYEE_EDITABLE_FIELDS mở rộng + save_employee lọc field ngoài whitelist
  BE-EP-09  get_employee_detail aggregation shape (stub frappe)
  BE-EP-10  merge_activity gộp 3 nguồn + sort desc + limit (pure)
  BE-EP-11  add_employee_comment validate nội dung + quyền
  BE-EP-12  delete_employee_attachment guard gắn sai employee / sai doctype

Bench-free stub-frappe harness (pattern tests/test_wp56_jobs.py). Module under
test is self-contained: admin helpers are imported lazily, so the REAL admin
module reloads under the stub too (its only top-level deps are frappe /
frappe.utils / gege utils, all stdlib-or-stubbable).
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest


class FrappeError(Exception):
    """frappe.throw / only_for stand-in."""


class _InsertDoc:
    """Doc built from a dict payload — insert()/submit() mints name/creation/owner."""

    def __init__(self, payload, stub):
        self.__dict__.update(payload)
        self._stub = stub

    def insert(self, **_kw):
        self.name = f"NEW-{len(self._stub.inserted) + 1:05d}"
        self.creation = "2026-08-27 10:00:00"
        self.owner = "hr@test.io"
        self.docstatus = 0
        self._stub.inserted.append({k: v for k, v in self.__dict__.items() if not k.startswith("_")})
        return self

    def submit(self, **_kw):
        self.docstatus = 1
        for row in self._stub.inserted:
            if row.get("name") == self.name:
                row["docstatus"] = 1
        return self


class _EmpDoc:
    """Employee row wrapper with set/save/as_dict for save_employee."""

    def __init__(self, row: dict):
        self._row = row
        self.__dict__.update({k: v for k, v in row.items() if not k.startswith("_")})

    def set(self, field, value):
        setattr(self, field, value)

    def save(self, **_kw):
        for key, value in self.__dict__.items():
            if not key.startswith("_"):
                self._row[key] = value
        return self

    def as_dict(self):
        return dict(self._row)


class Stub:
    def __init__(self):
        self.deny_roles = False
        self.employees = {
            "E1": {
                "name": "E1",
                "employee_name": "NV Một",
                "first_name": "Một",
                "status": "Active",
                "company": "GeGe Esport",
                "department": "IT",
                "date_of_joining": "2026-01-05",
                "user_id": "u1@x.io",
                "password": "must-not-leak",
            },
        }
        self.set_value_calls: list[tuple] = []
        self.files = {
            "FILE-1": {
                "name": "FILE-1",
                "file_name": "hop-dong.pdf",
                "attached_to_doctype": "Employee",
                "attached_to_name": "E1",
            },
            "FILE-2": {
                "name": "FILE-2",
                "file_name": "khac.pdf",
                "attached_to_doctype": "Leave Application",
                "attached_to_name": "HR-LAP-0001",
            },
        }
        # doctype → fieldnames the stub meta claims exist (drives _existing_fields).
        self.meta_fields: dict[str, list[str]] = {
            "Employee": [
                "name",
                "first_name",
                "status",
                "department",
                "company",
                "employment_type",
                "branch",
                "designation",
                "line_manager",
            ],
            "Comment": ["name", "owner", "comment_email", "creation", "content"],
            "Version": ["name", "owner", "creation", "data"],
            "VN Audit Event": ["name", "audit_type", "actor", "created_at", "description"],
            "File": [
                "name",
                "file_name",
                "file_url",
                "is_private",
                "file_size",
                "owner",
                "creation",
            ],
            "Attendance": ["name", "attendance_date", "status", "in_time", "out_time"],
            "Leave Application": [
                "name",
                "leave_type",
                "from_date",
                "to_date",
                "total_leave_days",
                "status",
            ],
            "Salary Slip": ["name", "start_date", "end_date", "net_pay", "status"],
            "Expense Claim": [
                "name",
                "posting_date",
                "total_claimed_amount",
                "approval_status",
            ],
            "Shift Assignment": ["name", "shift_type", "start_date", "end_date", "docstatus"],
            "Salary Structure Assignment": [
                "name",
                "salary_structure",
                "from_date",
                "base",
                "docstatus",
            ],
            "VN Employee Bank Account": [
                "name",
                "bank_name",
                "bank_bin",
                "account_no",
                "account_name",
                "is_default",
            ],
            "VN Employee Onboarding": ["name", "status", "progress", "boarding_date"],
            "Department": ["name", "department_name"],
            "Gender": ["name"],
        }
        self.rows: dict[str, list[dict]] = {
            "Attendance": [
                {
                    "name": f"ATT-{i}",
                    "employee": "E1",
                    "attendance_date": f"2026-08-{i:02d}",
                    "status": "Present",
                }
                for i in range(1, 8)  # 7 rows → recent slice = 6
            ],
            "Comment": [
                {
                    "name": "C-1",
                    "reference_doctype": "Employee",
                    "reference_name": "E1",
                    "owner": "hr@test.io",
                    "creation": "2026-08-27 09:00:00",
                    "content": "Đã ký HĐ",
                }
            ],
            "Version": [
                {
                    "name": "V-1",
                    "ref_type": "Employee",
                    "ref_name": "E1",
                    "owner": "hr@test.io",
                    "creation": "2026-08-27 08:00:00",
                    "data": json.dumps({"changed": [["status", "Active", "Left"]]}),
                }
            ],
            "VN Audit Event": [
                {
                    "name": "AE-1",
                    "employee": "E1",
                    "audit_type": "Manual Override",
                    "actor": "hr@test.io",
                    "created_at": "2026-08-27 07:00:00",
                    "description": "Sửa nhân viên",
                }
            ],
            "File": [
                {
                    "name": "FILE-1",
                    "attached_to_doctype": "Employee",
                    "attached_to_name": "E1",
                    "file_name": "hop-dong.pdf",
                    "file_url": "/files/hop-dong.pdf",
                    "is_private": 1,
                    "file_size": 1234,
                    "owner": "hr@test.io",
                    "creation": "2026-08-26 09:00:00",
                }
            ],
            "VN Employee Onboarding": [
                {"name": "ONB-1", "employee": "E1", "status": "Completed", "progress": 100.0}
            ],
            "Department": [{"name": "IT", "department_name": "IT"}],
            "Gender": [{"name": "Male"}, {"name": "Female"}],
        }
        self.inserted: list[dict] = []
        self.deleted: list[tuple] = []


def _matches(row: dict, filters) -> bool:
    if not filters:
        return True
    if isinstance(filters, dict):
        return all(row.get(k) == v for k, v in filters.items())
    # list-form filters (or_filters-like) — stub returns True (caller slices).
    return True


def _make_frappe(stub: Stub):
    fr = types.ModuleType("frappe")
    fr._ = lambda s: s
    fr.whitelist = lambda *a, **k: lambda f: f

    def only_for(_roles=None):
        if stub.deny_roles:
            raise FrappeError("PermissionError")

    fr.only_for = only_for

    def throw(msg, *args, **_kw):
        raise FrappeError(str(msg))

    fr.throw = throw
    fr.log_error = lambda *a, **k: None
    fr.session = SimpleNamespace(user="hr@test.io")

    def get_all(doctype, filters=None, fields=None, order_by=None, limit_page_length=None, **_kw):
        rows = [dict(r) for r in stub.rows.get(doctype, []) if _matches(r, filters)]
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
        if arg == "Employee":
            if name not in stub.employees:
                raise FrappeError("not found")
            return _EmpDoc(stub.employees[name])
        if arg == "File":
            if name not in stub.files:
                raise FrappeError("not found")
            return SimpleNamespace(**stub.files[name])
        raise FrappeError(f"unexpected get_doc {arg}")

    fr.get_doc = get_doc
    fr.delete_doc = lambda dt, name, **_kw: stub.deleted.append((dt, name))

    # Aliases: a class body cannot reference an enclosing name it also binds.
    _get_all_fn = get_all
    _get_meta_fn = get_meta

    class _DB:
        get_all = staticmethod(_get_all_fn)
        get_meta = staticmethod(_get_meta_fn)

        @staticmethod
        def exists(doctype, name):
            if doctype == "Employee":
                return name in stub.employees
            if doctype == "File":
                return name in stub.files
            if doctype == "DocType":
                return name in {"Employee Transfer", "Employee Promotion", "Leave Allocation"}
            return False

        @staticmethod
        def get_value(doctype, name, fieldname=None, **_kw):
            if doctype == "Employee" and isinstance(name, str):
                return stub.employees.get(name, {}).get(fieldname)
            return None

        @staticmethod
        def set_value(doctype, name, fieldname, value=None, **_kw):
            stub.set_value_calls.append((doctype, name, fieldname, value))

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


@pytest.fixture()
def env(monkeypatch):
    stub = Stub()
    fr = _make_frappe(stub)
    monkeypatch.setitem(sys.modules, "frappe", fr)
    monkeypatch.setitem(sys.modules, "frappe.utils", _utils_mod())
    admin_mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.admin"))
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.employee_profile"))
    return stub, mod, admin_mod


# --------------------------------------------------------------------------- #
# BE-EP-01 — scrub_meta_fields / group_sections / parse_depends_on (pure)
# --------------------------------------------------------------------------- #
def test_be_ep_01_scrub_drops_sensitive_hidden_unsupported(env):
    _stub, mod, _admin = env
    fields = [
        {"fieldname": "sec1", "fieldtype": "Section Break", "label": "Hồ sơ"},
        {"fieldname": "first_name", "fieldtype": "Data", "label": "Tên", "reqd": 1},
        {"fieldname": "api_key", "fieldtype": "Data", "label": "Key"},
        {"fieldname": "secret_token", "fieldtype": "Data", "label": "Secret"},
        {"fieldname": "birth_date", "fieldtype": "Date", "label": "Ngày sinh", "hidden": 1},
        {"fieldname": "resume", "fieldtype": "Text Editor", "label": "CV"},
        {"fieldname": "contract_copy", "fieldtype": "Attach", "label": "Scan"},
        {"fieldname": "bank_ac_no", "fieldtype": "Data", "label": "STK"},
        {"fieldname": "gross_salary", "fieldtype": "Currency", "label": "Lương"},
        {"fieldname": "status", "fieldtype": "Select", "options": "Active\nLeft"},
    ]
    scrubbed = mod.scrub_meta_fields(fields, ("first_name", "bank_ac_no", "status"))
    names = [f["fieldname"] for f in scrubbed]
    for banned in ("api_key", "secret_token", "birth_date", "resume", "contract_copy"):
        assert banned not in names
    first = next(f for f in scrubbed if f["fieldname"] == "first_name")
    assert first["mandatory"] == 1 and first["read_only"] == 0
    # outside whitelist → still displayed, but read-only
    gross = next(f for f in scrubbed if f["fieldname"] == "gross_salary")
    assert gross["read_only"] == 1
    # layout markers survive with read_only=0
    sec = next(f for f in scrubbed if f["fieldname"] == "sec1")
    assert sec["fieldtype"] == "Section Break" and sec["read_only"] == 0


def test_be_ep_01_group_sections_and_depends(env):
    _stub, mod, _admin = env
    scrubbed = mod.scrub_meta_fields(
        [
            {"fieldname": "lead", "fieldtype": "Data"},
            {"fieldname": "sec_a", "fieldtype": "Section Break", "label": "A"},
            {"fieldname": "f1", "fieldtype": "Data"},
            {"fieldname": "col1", "fieldtype": "Column Break"},
            {"fieldname": "f2", "fieldtype": "Data"},
        ],
        ("f1", "f2"),
    )
    sections = mod.group_sections(scrubbed)
    assert [s["fieldname"] for s in sections] == [None, "sec_a"]
    assert [f["fieldname"] for f in sections[1]["fields"]] == ["f1", "col1", "f2"]
    assert mod.parse_depends_on("eval:doc.status=='Left'") == {
        "field": "status",
        "op": "==",
        "value": "Left",
    }
    assert mod.parse_depends_on("eval:doc.x != 'y'")["op"] == "!="
    assert mod.parse_depends_on(None) is None
    assert mod.parse_depends_on("eval:doc.complex > 5") is None


# --------------------------------------------------------------------------- #
# BE-EP-02 — whitelist mở rộng + save_employee lọc field ngoài whitelist
# --------------------------------------------------------------------------- #
def test_be_ep_02_whelist_extended_and_save_filters_kwargs(env):
    stub, _mod, admin = env
    for field in (
        "bank_name",
        "bank_ac_no",
        "iban",
        "relieving_date",
        "reason_for_leaving",
        "personal_email",
        "person_to_be_contacted",
        "permanent_address",
        "holiday_list",
        "default_shift",
        "image",
        "salutation",
    ):
        assert field in admin._EMPLOYEE_EDITABLE_FIELDS, field
    # field không có thật trên meta runtime → KHÔNG đưa vào whitelist
    for banned in ("emergency_contact_number", "leave_policy", "notes"):
        assert banned not in admin._EMPLOYEE_EDITABLE_FIELDS
    res = admin.save_employee(employee="E1", first_name="Hai", bank_ac_no="9999", khong_co_truong_nay="x")
    assert res["first_name"] == "Hai"
    assert res["bank_ac_no"] == "9999"
    assert "khong_co_truong_nay" not in stub.employees["E1"]


# --------------------------------------------------------------------------- #
# BE-EP-09 — get_employee_detail aggregation shape
# --------------------------------------------------------------------------- #
def test_be_ep_09_detail_shape_and_scrub(env):
    _stub, mod, _admin = env
    res = mod.get_employee_detail("E1")
    assert res["employee"]["name"] == "E1"
    assert "password" not in res["employee"]  # scrub_document
    att = res["related"]["attendance"]
    assert att["count"] == 7 and len(att["recent"]) == 6
    assert res["related"]["onboarding"]["status"] == "Completed"
    assert res["attachments"][0]["file_name"] == "hop-dong.pdf"
    merged = res["activity"]["merged"]
    assert merged, "timeline không được rỗng"
    ats = [item["at"] for item in merged]
    assert ats == sorted(ats, reverse=True)
    # 3 nguồn đều có mặt
    assert {item["source"] for item in merged} == {"comment", "version", "audit"}


def test_be_ep_09_version_ref_doctype_column_preferred(env):
    """Frappe v15 dùng Version.ref_doctype; fallback ref_type cho site cũ."""
    stub, mod, _admin = env
    stub.meta_fields["Version"] = ["name", "owner", "creation", "data", "ref_doctype", "ref_name"]
    stub.rows["Version"] = [
        {
            "name": "V-2",
            "ref_doctype": "Employee",
            "ref_name": "E1",
            "owner": "hr@test.io",
            "creation": "2026-08-27 12:00:00",
            "data": "{}",
        }
    ]
    res = mod.get_employee_detail("E1")
    assert [v["name"] for v in res["activity"]["versions"]] == ["V-2"]


def test_be_ep_09_detail_missing_employee_throws(env):
    _stub, mod, _admin = env
    with pytest.raises(FrappeError):
        mod.get_employee_detail("E-KHONG-TOI")


# --------------------------------------------------------------------------- #
# BE-EP-10 — merge_activity + version_summary (pure)
# --------------------------------------------------------------------------- #
def test_be_ep_10_merge_interleaves_and_limits(env):
    _stub, mod, _admin = env
    comments = [{"owner": "a@x", "creation": "2026-08-27 09:00:00", "content": "c2"}]
    versions = [
        {
            "owner": "b@x",
            "creation": "2026-08-27 10:00:00",
            "data": json.dumps({"changed": [["status", "Active", "Left"], ["x", "1", "2"]]}),
        }
    ]
    audits = [
        {
            "actor": "c@x",
            "created_at": "2026-08-26 10:00:00",
            "description": "audit cũ",
            "audit_type": "Manual Override",
        }
    ]
    merged = mod.merge_activity(comments, versions, audits, limit=2)
    assert len(merged) == 2
    assert merged[0]["source"] == "version"
    assert "status: Active → Left" in merged[0]["text"]
    assert merged[1]["source"] == "comment"
    # audit rơi khỏi limit=2 nhưng mọi dòng đều có source/actor/at/text
    assert mod.version_summary(None) == "Cập nhật hồ sơ"
    assert mod.version_summary("not-json") == "Cập nhật hồ sơ"


# --------------------------------------------------------------------------- #
# BE-EP-11 — add_employee_comment
# --------------------------------------------------------------------------- #
def test_be_ep_11_comment_ok_and_payload(env):
    stub, mod, _admin = env
    res = mod.add_employee_comment("E1", "Ghi chú từ HR")
    assert res["name"] and res["owner"] == "hr@test.io"
    payload = stub.inserted[0]
    assert payload["reference_doctype"] == "Employee"
    assert payload["reference_name"] == "E1"
    assert payload["content"] == "Ghi chú từ HR"
    assert payload["comment_email"] == "hr@test.io"


def test_be_ep_11_comment_validates(env):
    _stub, mod, _admin = env
    with pytest.raises(FrappeError):
        mod.add_employee_comment("E1", "  x  ")  # quá ngắn sau trim
    with pytest.raises(FrappeError):
        mod.add_employee_comment("E-KHONG-TOI", "ghi chú dài đủ")


def test_be_ep_11_comment_role_gated(env):
    stub, mod, _admin = env
    stub.deny_roles = True
    with pytest.raises(FrappeError):
        mod.add_employee_comment("E1", "ghi chú")


# --------------------------------------------------------------------------- #
# BE-EP-12 — delete_employee_attachment
# --------------------------------------------------------------------------- #
def test_be_ep_12_delete_guards(env):
    stub, mod, _admin = env
    # sai employee → từ chối, không xoá
    with pytest.raises(FrappeError):
        mod.delete_employee_attachment("FILE-1", employee="E2")
    assert stub.deleted == []
    # file không gắn Employee → từ chối
    with pytest.raises(FrappeError):
        mod.delete_employee_attachment("FILE-2")
    # file không tồn tại → từ chối
    with pytest.raises(FrappeError):
        mod.delete_employee_attachment("FILE-KHONG-TOI")


def test_be_ep_12_delete_ok(env):
    stub, mod, _admin = env
    res = mod.delete_employee_attachment("FILE-1", employee="E1")
    assert res == {"name": "FILE-1", "deleted": True}
    assert ("File", "FILE-1") in stub.deleted


# --------------------------------------------------------------------------- #
# BE-EP-03 — validate_offboard (pure)
# --------------------------------------------------------------------------- #
def test_be_ep_03_validate_offboard(env):
    _stub, mod, _admin = env
    doc = {"status": "Active", "date_of_joining": "2026-01-05"}
    ok = mod.validate_offboard(doc, "2026-08-27", "Resigned")
    assert ok == {"relieving_date": "2026-08-27", "reason_for_leaving": "Resigned"}
    with pytest.raises(ValueError, match="Nghỉ việc"):
        mod.validate_offboard({"status": "Left"}, "2026-08-27", "Resigned")
    with pytest.raises(ValueError, match="ngày nghỉ việc"):
        mod.validate_offboard(doc, "", "Resigned")
    with pytest.raises(ValueError, match="trước ngày vào làm"):
        mod.validate_offboard(doc, "2025-12-31", "Resigned")
    with pytest.raises(ValueError, match="lý do"):
        mod.validate_offboard(doc, "2026-08-27", "  ")
    with pytest.raises(ValueError, match="không hợp lệ"):
        mod.validate_offboard(doc, "2026-08-27", "XXX", reasons_allowed=["Resigned"])


# --------------------------------------------------------------------------- #
# BE-EP-04/05 — offboard_employee qua stub
# --------------------------------------------------------------------------- #
def test_be_ep_04_05_offboard_flow(env):
    stub, mod, _admin = env
    res = mod.offboard_employee("E1", "2026-08-27", "Resigned", deactivate_user=1)
    assert res["status"] == "Left" and res["user_disabled"] is True
    emp = stub.employees["E1"]
    assert emp["status"] == "Left"
    assert emp["relieving_date"] == "2026-08-27"
    assert emp["reason_for_leaving"] == "Resigned"
    assert ("User", "u1@x.io", "enabled", 0) in stub.set_value_calls


def test_be_ep_05_offboard_no_deactivate_without_flag(env):
    stub, mod, _admin = env
    res = mod.offboard_employee("E1", "2026-08-27", "Retired")
    assert res["user_disabled"] is False
    assert not any(call[0] == "User" for call in stub.set_value_calls)
    with pytest.raises(FrappeError):
        mod.offboard_employee("E1", "2026-08-27", "Resigned")  # đã Left


def test_be_ep_reactivate(env):
    stub, mod, _admin = env
    with pytest.raises(FrappeError):
        mod.reactivate_employee("E1")  # đang Active
    stub.employees["E1"]["status"] = "Left"
    res = mod.reactivate_employee("E1", clear_dates=1)
    assert res["status"] == "Active"
    assert stub.employees["E1"]["relieving_date"] is None


# --------------------------------------------------------------------------- #
# Transfer (B0.2 PASS — wrapper doctype chuẩn HRMS)
# --------------------------------------------------------------------------- #
def test_be_ep_transfer_creates_submitted_doc(env):
    stub, mod, _admin = env
    res = mod.create_transfer("E1", "2026-08-27", new_department="Sales")
    assert res["docstatus"] == 1 and res["changed"] == {"department": "Sales"}
    payload = next(p for p in stub.inserted if p.get("doctype") == "Employee Transfer")
    assert payload["employee"] == "E1"
    detail = payload["transfer_details"][0]
    assert detail["fieldname"] == "department" and detail["new"] == "Sales"
    assert detail["current"] == "IT"


def test_be_ep_transfer_requires_a_change(env):
    _stub, mod, _admin = env
    with pytest.raises(FrappeError):
        mod.create_transfer("E1", "2026-08-27")


# --------------------------------------------------------------------------- #
# BE-EP-06/07 — detect_cycle + build_tree (pure)
# --------------------------------------------------------------------------- #
def test_be_ep_06_detect_cycle():
    from gege_hr.gege_hr.api.employee_profile import detect_cycle

    assert detect_cycle({"A": "B", "B": "A"}, "A") == ["A", "B", "A"]
    assert detect_cycle({"A": "A"}, "A") == ["A", "A"]
    assert detect_cycle({"A": "B"}, "A") == []
    assert detect_cycle({"A": "B", "C": "D"}, "A") == []
    assert detect_cycle({"A": "B", "B": "A"}) == ["A", "B", "A"]  # quét mọi node
    assert detect_cycle({}, "A") == []


def test_be_ep_07_build_tree():
    from gege_hr.gege_hr.api.employee_profile import build_tree

    rows = [
        {"name": "CEO", "reports_to": None, "employee_name": "CEO"},
        {"name": "A", "reports_to": "CEO", "employee_name": "A"},
        {"name": "A1", "reports_to": "A", "employee_name": "A1"},
        {"name": "B", "reports_to": "CEO", "employee_name": "B"},
    ]
    tree, warnings = build_tree(rows, depth=5)
    assert [n["name"] for n in tree] == ["CEO"]
    kids = [c["name"] for c in tree[0]["children"]]
    assert kids == ["A", "B"]
    assert tree[0]["children"][0]["children"][0]["name"] == "A1"
    assert warnings == []
    # depth=1: CEO + con trực tiếp; cháu bị cắt
    tree1, _ = build_tree(rows, depth=1)
    assert [c["name"] for c in tree1[0]["children"]] == ["A", "B"]
    assert tree1[0]["children"][0]["children"] == []
    # root cụ thể
    subtree, _ = build_tree(rows, root="A")
    assert subtree[0]["name"] == "A" and subtree[0]["children"][0]["name"] == "A1"


# --------------------------------------------------------------------------- #
# BE-EP-08 — bulk_update_employees guards + happy path
# --------------------------------------------------------------------------- #
def test_be_ep_08_bulk_guards(env):
    _stub, mod, _admin = env
    with pytest.raises(FrappeError):
        mod.bulk_update_employees(["E1"], "user_permissions", "x")  # ngoài whitelist
    with pytest.raises(FrappeError):
        mod.bulk_update_employees([f"E{i}" for i in range(101)], "department", "IT")
    with pytest.raises(FrappeError):
        mod.bulk_update_employees([], "department", "IT")


def test_be_ep_08_bulk_happy_path(env):
    stub, mod, _admin = env
    stub.employees["E2"] = {"name": "E2", "status": "Active", "department": "Ops"}
    res = mod.bulk_update_employees(["E1", "E2"], "department", "Sales")
    assert res["updated"] == ["E1", "E2"] and res["failed"] == []
    assert stub.employees["E1"]["department"] == "Sales"
    assert stub.employees["E2"]["department"] == "Sales"
