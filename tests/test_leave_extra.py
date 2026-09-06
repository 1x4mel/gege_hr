"""Bench-free unit tests for ``api/leave_extra.py`` (NEW-3, hr-gap-audit 🟥).

Extended by plan-test-complete-hr-extra §3.1 (LE-B01→B18): portal-status
projection (vn_status || docstatus), G1 date-order validation, G3 leave-balance
gate, G4 reject persistence via vn_status/vn_note, notification best-effort.
"""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Draft"
        self.docstatus = 0
        self.flags = types.SimpleNamespace()

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self

    def submit(self):
        self.docstatus = 1
        self.status = "Submitted"
        return self

    def cancel(self):
        self.docstatus = 2
        self.status = "Cancelled"
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.employee_company = "Gege"
        self.roles = {"HR Manager"}
        self.leave_alloc_remaining = None  # Leave Allocation lookup result
        self.set_values = []  # recorded frappe.db.set_value calls

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

    def set_user(self, user):
        self._session_user = user

    @property
    def session(self):
        return types.SimpleNamespace(user=getattr(self, "_session_user", "hr@gege.local"))

    def get_roles(self, user):
        return set(self.roles)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, key, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(key, dict):
                return self.fr.employee_for_user
            if doctype == "Employee":
                return self.fr.employee_company
            if doctype == "Leave Allocation":
                return self.fr.leave_alloc_remaining
            return None

        def set_value(self, doctype, name, values, *a, **k):
            self.fr.set_values.append((doctype, name, values))
            return name

        def get_all(self, *a, **k):
            return []

    @property
    def db(self):
        return self._DB(self)

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

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
        rows = list(self.list_rows.get(doctype, []))

        def _match(r, cond):
            op, val = cond[1], cond[2]
            got = r.get(cond[0])
            if op == "like":
                return str(got or "").find(str(val).strip("%")) >= 0
            if op == ">=":
                return got is not None and str(got) >= str(val)
            if op == "<=":
                return got is not None and str(got) <= str(val)
            return got == val

        def keep(r):
            if filters:
                if isinstance(filters, dict):
                    # Frappe dict-filter shorthand: a list value is already a
                    # [field, op, value] condition (e.g. {"from_date": ["<=", d]}).
                    conds = []
                    for k2, v in filters.items():
                        if isinstance(v, list) and len(v) == 2:
                            # {"field": [op, val]} → [field, op, val]
                            conds.append([k2, *v])
                        else:
                            conds.append([k2, "=", v])
                else:
                    conds = filters
                for cond in conds:
                    if not _match(r, cond):
                        return False
            if or_filters:
                if not any(_match(r, c) for c in or_filters):
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.leave_extra")
    importlib.reload(m)
    return m, stub


def _balance(m, remaining):
    """Pin remaining_leave_days for the imported module instance."""
    m.remaining_leave_days = lambda employee, leave_type: remaining


# ── pure (LE-B01 helpers) ────────────────────────────────────────────────────
def test_encashment_amount(mod):
    m, _ = mod
    assert m.encashment_amount(5, 200000) == 1000000.0
    assert m.encashment_amount(0, 100) == 0.0
    assert m.encashment_amount(2.5, 100000) == 250000.0


def test_portal_status_coalesce(mod):
    m, _ = mod
    assert m.portal_status("Approved", 0) == "Approved"  # vn_status wins
    assert m.portal_status(None, 1) == "Approved"  # docstatus fallback
    assert m.portal_status(None, 2) == "Cancelled"
    assert m.portal_status(None, 0) == "Draft"
    assert m.portal_status("  ", None) == "Draft"


def test_date_order_ok(mod):
    m, _ = mod
    assert m.date_order_ok("2026-08-01", "2026-08-01") is True
    assert m.date_order_ok("2026-08-01", "2026-08-02") is True
    assert m.date_order_ok("2026-08-02", "2026-08-01") is False
    assert m.date_order_ok(None, "2026-08-01") is False


def test_project_row_aliases(mod):
    m, _ = mod
    row = m.project_row(
        {"name": "C1", "vn_status": "Rejected", "docstatus": 0, "work_end_date": "2026-08-15"},
        compoff=True,
    )
    assert row["status"] == "Rejected"
    assert row["work_to_date"] == "2026-08-15"
    assert "vn_status" not in row and "work_end_date" not in row


def test_append_note(mod):
    m, _ = mod
    assert m.append_note(None, "thiếu giấy tờ") == "Từ chối: thiếu giấy tờ"
    assert "Từ chối: thiếu giấy tờ" in m.append_note("Ghi chú cũ", "thiếu giấy tờ")
    assert m.append_note("Đã có | Từ chối: X", "X") == "Đã có | Từ chối: X"  # idempotent
    assert m.append_note("Chỉ ghi chú", None) == "Chỉ ghi chú"


# ── encashment I/O ──────────────────────────────────────────────────────────
def test_my_encashments_filters_own(mod):
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"name": "LE-1", "employee": "HR-EMP-1", "employee_name": "An", "encashment_days": 3},
        {"name": "LE-2", "employee": "HR-EMP-2", "employee_name": "Binh", "encashment_days": 1},
    ]
    res = m.my_leave_encashments()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "LE-1"
    assert res["data"][0]["status"] == "Draft"  # docstatus fallback projection


def test_my_encashments_status_filter_uses_vn_status(mod):
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"name": "LE-1", "employee": "HR-EMP-1", "vn_status": "Approved"},
        {"name": "LE-2", "employee": "HR-EMP-1", "vn_status": "Rejected"},
    ]
    res = m.my_leave_encashments(status="Rejected")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "LE-2"


def test_submit_encashment_validates(mod):  # LE-B02
    m, _ = mod
    _balance(m, 10)
    with pytest.raises(Exception):
        m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=0)
    with pytest.raises(Exception):
        m.submit_leave_encashment(employee="HR-EMP-1", leave_type=None, encashment_days=2)


def test_submit_encashment_ok(mod):  # LE-B01
    m, stub = mod
    _balance(m, 10)
    res = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=2)
    assert res["name"]
    doc = stub.store[("Leave Encashment", res["name"])]
    assert doc.vn_status == "Draft"
    assert doc.encashment_days == 2


def test_submit_encashment_blocks_over_balance(mod):  # LE-B03 (G3)
    m, _ = mod
    _balance(m, 1.5)
    with pytest.raises(Exception) as ei:
        m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=3)
    assert "vượt số dư" in str(ei.value)


def test_submit_encashment_allows_exact_balance(mod):  # LE-B04 (G3 boundary)
    m, _ = mod
    _balance(m, 1.5)
    res = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1.5)
    assert res["encashment_days"] == 1.5


def test_all_encashments_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_leave_encashments()


def test_approve_encashment_happy(mod):  # LE-B05
    m, _ = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    res = m.approve_leave_encashment(created["name"])
    assert res["status"] == "Approved"  # pinned — no more best-effort assertion (G9)


def test_approve_encashment_requires_manager(mod):  # LE-B06
    m, stub = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.approve_leave_encashment(created["name"])


def test_approve_encashment_wraps_submit_failure(mod):  # LE-B07
    m, stub = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    doc = stub.store[("Leave Encashment", created["name"])]

    def _boom():
        raise Exception("HRMS says no")

    doc.submit = _boom
    with pytest.raises(Exception) as ei:
        m.approve_leave_encashment(created["name"])
    assert "Duyệt đổi phép thất bại" in str(ei.value)


def test_reject_encashment_draft_persists_note(mod):  # LE-B08 (G4)
    m, stub = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    res = m.reject_leave_encashment(created["name"], reason="thiếu chứng từ")
    assert res["status"] == "Rejected"
    doc = stub.store[("Leave Encashment", created["name"])]
    assert doc.vn_status == "Rejected"
    assert "Từ chối: thiếu chứng từ" in (doc.vn_note or "")


def test_reject_encashment_submitted_cancels(mod):  # LE-B09
    m, stub = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    m.approve_leave_encashment(created["name"])
    doc = stub.store[("Leave Encashment", created["name"])]
    assert doc.docstatus == 1
    res = m.reject_leave_encashment(created["name"], reason="sai số")
    assert res["status"] == "Rejected"
    assert doc.docstatus == 2  # cancelled
    assert (
        "Leave Encashment",
        created["name"],
        {"vn_status": "Rejected", "vn_note": res["note"]},
    ) in stub.set_values


def test_reject_encashment_without_reason(mod):  # LE-B10
    m, _ = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    res = m.reject_leave_encashment(created["name"], reason=None)
    assert res["status"] == "Rejected"
    assert res["note"] == ""


def test_reject_requires_manager(mod):
    m, stub = mod
    _balance(m, 10)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.reject_leave_encashment(created["name"])


# ── comp-off I/O ────────────────────────────────────────────────────────────
def test_submit_comp_off_requires_dates(mod):  # LE-B13
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_comp_off(employee="HR-EMP-1", work_from_date=None, work_to_date=None)
    with pytest.raises(Exception):
        m.submit_comp_off(employee="HR-EMP-1", work_from_date="2026-08-01", work_to_date=None)


def test_submit_comp_off_rejects_inverted_range(mod):  # LE-B12 (G1)
    m, _ = mod
    with pytest.raises(Exception) as ei:
        m.submit_comp_off(employee="HR-EMP-1", work_from_date="2026-08-10", work_to_date="2026-08-01")
    assert "không được trước ngày bắt đầu" in str(ei.value)


def test_submit_comp_off_maps_work_end_date(mod):  # LE-B11 + HRMS fieldname
    m, stub = mod
    res = m.submit_comp_off(employee="HR-EMP-1", work_from_date="2026-08-01", work_to_date="2026-08-01")
    doc = stub.store[("Compensatory Leave Request", res["name"])]
    assert getattr(doc, "work_end_date", None) == "2026-08-01"  # real HRMS column
    assert doc.vn_status == "Draft"


def test_approve_comp_off(mod):  # G9 — pinned final status
    m, _ = mod
    created = m.submit_comp_off(
        employee="HR-EMP-1",
        leave_type="Compensatory Off",
        work_from_date="2026-08-01",
        work_to_date="2026-08-01",
    )
    res = m.approve_comp_off(created["name"])
    assert res["status"] == "Approved"


def test_approve_comp_off_cancelled_blocked(mod):  # LE-B10 variant
    m, stub = mod
    created = m.submit_comp_off(employee="HR-EMP-1", work_from_date="2026-08-01", work_to_date="2026-08-01")
    stub.store[("Compensatory Leave Request", created["name"])].docstatus = 2
    with pytest.raises(Exception) as ei:
        m.approve_comp_off(created["name"])
    assert "đã bị hủy" in str(ei.value)


def test_reject_comp_off_draft(mod):  # G4
    m, _ = mod
    created = m.submit_comp_off(employee="HR-EMP-1", work_from_date="2026-08-01", work_to_date="2026-08-01")
    res = m.reject_comp_off(created["name"], reason="thiếu attendance")
    assert res["status"] == "Rejected"


def test_my_comp_off_projects_work_to_date(mod):  # list contract
    m, stub = mod
    stub.list_rows["Compensatory Leave Request"] = [
        {
            "name": "CO-1",
            "employee": "HR-EMP-1",
            "work_from_date": "2026-08-15",
            "work_end_date": "2026-08-15",
            "reason": "làm bù",
            "docstatus": 1,
            "vn_status": None,
        }
    ]
    res = m.my_comp_off_requests()
    row = res["data"][0]
    assert row["work_to_date"] == "2026-08-15"
    assert row["status"] == "Approved"  # docstatus fallback (legacy row)


# ── list filters / broad search / degrade ───────────────────────────────────
def test_list_numeric_and_date_filters(mod):  # LE-B14
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"name": "LE-1", "employee": "HR-EMP-1", "encashment_days": 1, "creation": "2026-08-01 10:00:00"},
        {"name": "LE-2", "employee": "HR-EMP-1", "encashment_days": 9, "creation": "2026-07-01 10:00:00"},
    ]
    res = m.my_leave_encashments(days_min=2, days_max=5, date_from="2026-07-15")
    assert res["total"] == 0  # neither row satisfies both day-range and date


def test_broad_search_matches_numeric_without_datetime_like(mod):  # LE-B15
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"name": "LE-3", "employee": "HR-EMP-1", "encashment_days": 3},
        {"name": "LE-9", "employee": "HR-EMP-1", "encashment_days": 9},
    ]
    res = m.my_leave_encashments(search="3")
    names = [r["name"] for r in res["data"]]
    assert "LE-3" in names and "LE-9" not in names
    # the or_filter must hit encashment_days, not creation (ParserError regression)
    or_fields = [c[0] for c in m._or_filters_for("Leave Encashment", "3")]
    assert "encashment_days" in or_fields and "creation" not in or_fields


def test_list_degrades_on_db_error(mod):  # LE-B16
    m, stub = mod

    def _boom(*a, **k):
        raise Exception("db down")

    stub.get_all = _boom
    res = m.my_leave_encashments()
    assert res == {"data": [], "total": 0}


def test_status_options_union_with_db_values(mod):  # LE-B17
    m, stub = mod
    # meta read raises on the stub → only the PORTAL_STATUSES + DB values remain
    opts = m._status_options("Compensatory Leave Request")
    assert set(("Draft", "Approved", "Rejected")) <= set(opts)


def test_options_encashable_flag_version_proof(mod):  # allow_encashment vs is_encash
    m, stub = mod
    stub.list_rows["Leave Type"] = [
        {"name": "Casual Leave", "allow_encashment": 1},
        {"name": "Sick Leave", "allow_encashment": 0},
    ]
    opts = m.leave_extra_options()
    assert opts["leave_types"] == ["Casual Leave"]
    assert set(("Draft", "Approved", "Rejected")) <= set(opts["encash_statuses"])


def test_notify_failure_never_breaks_transition(mod, monkeypatch):  # LE-B18 (G6)
    m, _ = mod
    _balance(m, 10)

    def _boom(**k):
        raise Exception("notify down")

    # monkeypatch (not raw assignment) — notify is a SHARED module; a bare
    # assignment leaks the stub into sibling tests (test_notify).
    monkeypatch.setattr(m.notify, "push_notification", _boom)
    created = m.submit_leave_encashment(employee="HR-EMP-1", leave_type="Năm", encashment_days=1)
    res = m.approve_leave_encashment(created["name"])  # must NOT raise
    assert res["status"] == "Approved"


# ── plan leave-extra-deskfree-complete (LX) — detail / context / update /
#    withdraw / delete / publish-wire. Pattern: LE-B stub harness above. ──────
def _publish_spy(stub):
    stub.publish_events = []
    stub.publish_realtime = lambda event, payload=None: stub.publish_events.append((event, payload))
    return stub.publish_events


def _delete_spy(stub):
    stub.deleted = []
    stub.delete_doc = lambda doctype, name, *a, **k: stub.deleted.append((doctype, name))
    return stub.deleted


def _encash_draft(m, days=1, employee="HR-EMP-1"):
    _balance(m, 10)
    return m.submit_leave_encashment(employee=employee, leave_type="Năm", encashment_days=days)


def _compoff_draft(m, employee="HR-EMP-1"):
    return m.submit_comp_off(
        employee=employee,
        leave_type="Compensatory Off",
        work_from_date="2026-08-01",
        work_to_date="2026-08-02",
    )


def test_lx01_get_encashment_draft_owner_can(mod):  # LX1
    m, stub = mod
    stub.roles = {"Employee"}
    created = _encash_draft(m)
    res = m.get_leave_encashment(created["name"])
    assert res["doc"]["status"] == "Draft"
    assert res["doc"]["docstatus"] == 0
    can = res["can"]
    assert can["edit"] and can["withdraw"] and can["delete"]
    assert can["approve"] is False and can["reject"] is False and can["resend"] is False


def test_lx02_get_denies_other_employee(mod):  # LX2
    m, stub = mod
    created = _encash_draft(m, employee="HR-EMP-2")  # manager mints for another
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.get_leave_encashment(created["name"])


def test_lx03_get_encashment_approved_links_and_can(mod):  # LX3
    m, stub = mod
    created = _encash_draft(m)
    m.approve_leave_encashment(created["name"])
    doc = stub.store[("Leave Encashment", created["name"])]
    doc.additional_salary = "AS-1"
    stub.list_rows["Additional Salary"] = [
        {"name": "AS-1", "status": "Submitted", "docstatus": 1, "amount": 100000}
    ]
    res = m.get_leave_encashment(created["name"])
    assert res["links"]["additional_salary"]["amount"] == 100000
    assert res["doc"]["docstatus"] == 1
    can = res["can"]
    assert can["edit"] is False and can["withdraw"] is False and can["delete"] is False
    assert can["reject"] is True  # HR hủy sau duyệt


def test_lx04_get_comp_off_rejected_draft_can(mod):  # LX4
    m, _ = mod
    created = _compoff_draft(m)
    m.reject_comp_off(created["name"], reason="chưa đủ điều kiện")
    res = m.get_comp_off(created["name"])
    assert res["doc"]["status"] == "Rejected"
    can = res["can"]
    assert can["edit"] and can["delete"] and can["resend"]
    assert can["approve"] is False and can["reject"] is False


def test_lx05_get_comp_off_approved_allocation(mod):  # LX5
    m, stub = mod
    created = _compoff_draft(m)
    m.approve_comp_off(created["name"])
    doc = stub.store[("Compensatory Leave Request", created["name"])]
    doc.leave_allocation = "LA-1"
    stub.list_rows["Leave Allocation"] = [
        {
            "name": "LA-1",
            "new_leaves_allocated": 2,
            "total_leaves_allocated": 2,
            "from_date": "2026-08-01",
            "to_date": "2026-12-31",
            "docstatus": 1,
        }
    ]
    res = m.get_comp_off(created["name"])
    assert res["links"]["leave_allocation"]["new_leaves_allocated"] == 2
    assert res["doc"]["work_to_date"] == "2026-08-02"  # alias survives detail projection


def test_lx06_activity_merges_version_and_comment(mod):  # LX6
    m, stub = mod
    created = _compoff_draft(m)
    stub.list_rows["Comment"] = [
        {
            "reference_doctype": "Compensatory Leave Request",
            "reference_name": created["name"],
            "comment_type": "Comment",
            "owner": "a@b",
            "creation": "2026-09-01 10:00:00",
            "content": "hello",
        }
    ]
    stub.list_rows["Version"] = [
        {
            "ref_doctype": "Compensatory Leave Request",
            "docname": created["name"],
            "owner": "c@d",
            "modified": "2026-09-02 09:00:00",
            "data": "{}",
        }
    ]
    res = m.get_comp_off(created["name"])
    kinds = [r["type"] for r in res["activity"]]
    assert kinds[0] == "version" and set(kinds) == {"version", "comment"}


def test_lx07_update_encashment_revalidates_balance(mod):  # LX7
    m, stub = mod
    calls = []
    created = _encash_draft(m, days=1)
    m.remaining_leave_days = lambda employee, leave_type: calls.append(leave_type) or 10
    res = m.update_leave_encashment(created["name"], encashment_days=2)
    assert res["status"] == "Draft" and res["encashment_days"] == 2
    doc = stub.store[("Leave Encashment", created["name"])]
    assert doc.encashment_days == 2
    assert calls  # G3 balance gate re-ran on the new value


def test_lx08_update_rejects_submitted(mod):  # LX8
    m, _ = mod
    created = _encash_draft(m)
    m.approve_leave_encashment(created["name"])
    with pytest.raises(Exception) as ei:
        m.update_leave_encashment(created["name"], encashment_days=2)
    assert "Chỉ sửa được" in str(ei.value)


def test_lx09_update_denies_other_employee(mod):  # LX9
    m, stub = mod
    created = _encash_draft(m, employee="HR-EMP-2")
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.update_leave_encashment(created["name"], encashment_days=2)


def test_lx10_update_resets_rejected_to_draft(mod):  # LX10
    m, stub = mod
    created = _encash_draft(m)
    m.reject_leave_encashment(created["name"], reason="sai số")
    res = m.update_leave_encashment(created["name"], encashment_days=1)
    assert res["status"] == "Draft"
    assert stub.store[("Leave Encashment", created["name"])].vn_status == "Draft"


def test_lx11_update_blocks_over_balance(mod):  # LX11
    m, _ = mod
    created = _encash_draft(m, days=1)
    m.remaining_leave_days = lambda employee, leave_type: 1.5
    with pytest.raises(Exception) as ei:
        m.update_leave_encashment(created["name"], leave_type="Casual Leave", encashment_days=3)
    assert "vượt số dư" in str(ei.value)


def test_lx12_update_comp_off_inverted_range(mod):  # LX12
    m, _ = mod
    created = _compoff_draft(m)
    with pytest.raises(Exception) as ei:
        m.update_comp_off(created["name"], work_from_date="2026-08-10", work_to_date="2026-08-01")
    assert "không được trước ngày bắt đầu" in str(ei.value)


def test_lx13_withdraw_draft_with_note_and_publish(mod):  # LX13
    m, stub = mod
    events = _publish_spy(stub)
    created = _encash_draft(m)
    res = m.withdraw_leave_encashment(created["name"], note="đổi ý")
    assert res["status"] == "Rejected"
    doc = stub.store[("Leave Encashment", created["name"])]
    assert doc.vn_status == "Rejected"
    assert "Rút đơn: đổi ý" in doc.vn_note
    assert events and events[-1][1]["status"] == "Rejected"


def test_lx13b_withdraw_default_note(mod):  # LX13 — no reason given
    m, _ = mod
    created = _encash_draft(m)
    res = m.withdraw_leave_encashment(created["name"])
    assert "Nhân viên tự rút đơn" in res["note"]


def test_lx14_withdraw_rejects_submitted(mod):  # LX14
    m, _ = mod
    created = _encash_draft(m)
    m.approve_leave_encashment(created["name"])
    with pytest.raises(Exception) as ei:
        m.withdraw_leave_encashment(created["name"])
    assert "Chỉ rút được" in str(ei.value)


def test_lx15_withdraw_denies_other_employee(mod):  # LX15
    m, stub = mod
    created = _compoff_draft(m, employee="HR-EMP-2")
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.withdraw_comp_off(created["name"])


def test_lx16_delete_draft_publishes(mod):  # LX16
    m, stub = mod
    events = _publish_spy(stub)
    deleted = _delete_spy(stub)
    created = _encash_draft(m)
    res = m.delete_leave_extra_draft(doctype="Leave Encashment", name=created["name"])
    assert res["deleted"] is True
    assert ("Leave Encashment", created["name"]) in deleted
    assert events[-1][1]["status"] == "Deleted"


def test_lx17_delete_rejects_submitted(mod):  # LX17
    m, _ = mod
    created = _compoff_draft(m)
    m.approve_comp_off(created["name"])
    with pytest.raises(Exception):
        m.delete_leave_extra_draft(doctype="Compensatory Leave Request", name=created["name"])


def test_lx18_delete_rejects_unknown_doctype(mod):  # LX18
    m, _ = mod
    with pytest.raises(Exception):
        m.delete_leave_extra_draft(doctype="Leave Application", name="X")


def test_lx19_context_balances_and_components(mod):  # LX19
    m, stub = mod
    stub.list_rows["Leave Type"] = [{"name": "Casual Leave", "allow_encashment": 1}]
    stub.list_rows["Salary Component"] = [
        {"name": "Basic", "type": "earning", "disabled": 0},
        {"name": "Deduction X", "type": "deduction", "disabled": 0},
    ]
    stub.list_rows["Leave Period"] = [
        {
            "name": "LP-2026",
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "docstatus": 1,
            "is_active": 1,
            "company": "Gege",
        }
    ]
    m.remaining_leave_days = lambda employee, leave_type: 5
    res = m.leave_extra_context()
    assert res["balances"] == {"Casual Leave": 5}
    assert res["earning_components"] == ["Basic"]
    assert res["leave_period"] == "LP-2026"
    assert res["health"] == "ok"
    # legacy options contract stays intact (LX23 half)
    assert set(("Draft", "Approved", "Rejected")) <= set(res["encash_statuses"])


def test_lx20_context_no_leave_period_health(mod):  # LX20
    m, stub = mod
    stub.list_rows["Leave Type"] = []
    res = m.leave_extra_context()
    assert res["leave_period"] is None
    assert res["health"] == "no_leave_period"
    assert res["balances"] == {}


def test_lx21_context_caller_without_employee(mod):  # LX21
    m, stub = mod
    stub.employee_for_user = None
    res = m.leave_extra_context()
    assert res["balances"] == {}


def test_lx22_publish_wired_into_every_mutation(mod):  # LX22
    m, stub = mod
    events = _publish_spy(stub)
    _delete_spy(stub)
    a = _encash_draft(m)
    m.update_leave_encashment(a["name"], encashment_days=2)
    m.withdraw_leave_encashment(a["name"])
    b = _encash_draft(m)
    m.approve_leave_encashment(b["name"])
    c = _encash_draft(m)
    m.reject_leave_encashment(c["name"], reason="x")
    d = _compoff_draft(m)
    m.approve_comp_off(d["name"])
    e = _compoff_draft(m)
    m.reject_comp_off(e["name"], reason="y")
    f = _compoff_draft(m)
    m.delete_leave_extra_draft(doctype="Compensatory Leave Request", name=f["name"])
    statuses = [payload["status"] for _, payload in events]
    assert "Draft" in statuses and "Approved" in statuses
    assert "Rejected" in statuses and "Deleted" in statuses
    assert len(events) >= 10


def test_lx23_list_contract_unchanged(mod):  # LX23
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [{"name": "LE-1", "employee": "HR-EMP-1"}]
    res = m.my_leave_encashments()
    assert set(res) == {"data", "total"} and res["total"] == 1


def test_lx24_add_comment_inserts_and_publishes(mod):  # LX24 (P1)
    m, stub = mod
    events = _publish_spy(stub)
    created = _encash_draft(m)

    # Seam: the stub's get_doc takes (doctype, name); teach it the dict-payload
    # form used by the Comment insert (parity the real frappe.get_doc(dict)).
    def _get_doc(*a, **k):
        if len(a) == 1 and isinstance(a[0], dict):
            payload = dict(a[0])
            doc = stub.new_doc(payload.get("doctype") or "Comment")
            for k2, v in payload.items():
                if k2 != "doctype":
                    setattr(doc, k2, v)
            doc.insert(ignore_permissions=True)
            return doc
        return _Frappe.get_doc(stub, *a, **k)

    stub.get_doc = _get_doc
    res = m.add_leave_extra_comment(
        doctype="Leave Encashment", name=created["name"], text="xin duyệt sớm"
    )
    assert res["content"] == "xin duyệt sớm"
    assert res["owner"]
    assert events[-1][1]["status"] == "Comment"
    with pytest.raises(Exception):
        m.add_leave_extra_comment(doctype="Leave Encashment", name=created["name"], text="   ")


def test_lx25_bulk_action_partial_safe(mod):  # LX25 (P1)
    m, stub = mod
    _publish_spy(stub)
    a = _encash_draft(m)
    b = _encash_draft(m)
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.bulk_leave_extra_action(doctype="Leave Encashment", names=[a["name"]], action="approve")
    stub.roles = {"HR Manager"}
    # b is un-approvable (already cancelled) → per-row failure, not a throw.
    stub.store[("Leave Encashment", b["name"])].docstatus = 2
    res = m.bulk_leave_extra_action(
        doctype="Leave Encashment", names=[a["name"], b["name"]], action="approve"
    )
    assert res["updated"] == [a["name"]]
    assert len(res["failed"]) == 1 and res["failed"][0]["name"] == b["name"]


def test_lx26_bulk_reject_appends_reason(mod):  # LX26 (P1)
    m, stub = mod
    a = _encash_draft(m)
    b = _encash_draft(m)
    res = m.bulk_leave_extra_action(
        doctype="Leave Encashment",
        names=[a["name"], b["name"]],
        action="reject",
        reason="thiếu chứng từ",
    )
    assert sorted(res["updated"]) == sorted([a["name"], b["name"]])
    doc = stub.store[("Leave Encashment", a["name"])]
    assert "Từ chối: thiếu chứng từ" in (doc.vn_note or "")


def test_lx27_summary_counts_by_vn_status(mod):  # LX27 (P1)
    m, stub = mod
    stub.list_rows["Leave Encashment"] = [
        {"vn_status": "Draft"},
        {"vn_status": "Draft"},
        {"vn_status": "Approved"},
        {"vn_status": None},
    ]
    stub.list_rows["Compensatory Leave Request"] = [{"vn_status": "Rejected"}]
    res = m.leave_extra_summary()
    assert res["encash"] == {"Draft": 2, "Approved": 1, "Rejected": 0}
    assert res["compoff"]["Rejected"] == 1
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.leave_extra_summary()
