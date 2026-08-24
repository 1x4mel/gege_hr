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
                conds = [[k2, "=", v] for k2, v in filters.items()] if isinstance(filters, dict) else filters
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
