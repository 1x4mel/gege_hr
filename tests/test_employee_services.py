"""Bench-free unit tests for ``api/employee_services.py`` (NEW-4, hr-gap-audit 🟥).

Extended by plan-test-complete-hr-extra §3.2 (SV-B01→B11): HRMS field mapping
(``raised_by``/``date``/``resolution_detail``), G2 date-order validation, G5
``reject_travel_request`` + reason persistence, vn_* travel projection, and
best-effort notifications.
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
        self.status = "Open"
        # services-deskfree — update/withdraw/delete endpoints cần các attr này.
        self.docstatus = 0
        self.flags = types.SimpleNamespace(ignore_permissions=False)

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self

    def submit(self):  # P1 approve=submit (F3)
        self.docstatus = 1
        return self

    def cancel(self):  # P1 cancel-sau-duyệt (G17)
        self.docstatus = 2
        return self

    def reload(self):  # no-op stub — real frappe re-reads DB (race-guard pattern)
        return self

    def delete(self, *a, **k):
        self._store.pop((self.doctype, self.name), None)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.roles = {"HR Manager"}
        # services-deskfree — spies cho publish_realtime / delete_doc.
        self.published = []
        self.deleted = []
        # P1 — set_user("Administrator") scoped quanh submit/cancel.
        self.current_user = None
        self.set_user_calls = []
        self.flags = types.SimpleNamespace()

    def set_user(self, user):
        self.set_user_calls.append(user)
        self.current_user = user

    def publish_realtime(self, event, message=None):
        self.published.append((event, message))

    def delete_doc(self, doctype, name, ignore_permissions=False, **k):
        self.deleted.append((doctype, name))
        self.store.pop((doctype, name), None)
        return None

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

    @property
    def session(self):
        return types.SimpleNamespace(user=self.current_user or "hr@gege.local")

    def get_roles(self, user):
        return set(self.roles)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, key, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(key, dict):
                return self.fr.employee_for_user
            return None

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
    m = importlib.import_module("gege_hr.gege_hr.api.employee_services")
    importlib.reload(m)
    return m, stub


# ── pure projections ────────────────────────────────────────────────────────
def test_project_grievance_row(mod):
    m, _ = mod
    row = m.project_grievance_row({"name": "G1", "raised_by": "E1", "date": "2026-08-19"})
    assert row["employee"] == "E1"
    assert row["raised_on"] == "2026-08-19"
    assert "raised_by" not in row and "date" not in row


def test_project_travel_row(mod):
    m, _ = mod
    row = m.project_travel_row(
        {
            "name": "T1",
            "vn_purpose": "Đi KH",
            "vn_from_date": "2026-08-10",
            "vn_to_date": "2026-08-12",
            "vn_total_cost": 2000000,
            "vn_status": None,
            "docstatus": 1,
        }
    )
    assert row["status"] == "Approved"  # docstatus fallback
    assert row["purpose_of_travel"] == "Đi KH"
    assert row["from_date"] == "2026-08-10"
    assert row["to_date"] == "2026-08-12"
    assert row["total_travel_cost"] == 2000000


def test_date_order_ok(mod):
    m, _ = mod
    assert m.date_order_ok("2026-08-10", "2026-08-12") is True
    assert m.date_order_ok("2026-08-12", "2026-08-10") is False


# ── grievance ───────────────────────────────────────────────────────────────
def test_my_grievances_filters_own(mod):  # uses raised_by (HRMS column)
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "G-1", "raised_by": "HR-EMP-1", "subject": "A"},
        {"name": "G-2", "raised_by": "HR-EMP-2", "subject": "B"},
    ]
    res = m.my_grievances()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "G-1"
    assert res["data"][0]["employee"] == "HR-EMP-1"  # projected key


def test_submit_grievance_requires_subject(mod):  # SV-B02
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_grievance(employee="HR-EMP-1", subject="   ")


def test_submit_grievance_fills_hrms_mandatory(mod):  # SV-B01
    m, stub = mod
    res = m.submit_grievance(employee="HR-EMP-1", grievance_type="Lương", subject="Sai lương")
    doc = stub.store[("Employee Grievance", res["name"])]
    assert doc.subject == "Sai lương"
    assert doc.raised_by == "HR-EMP-1"
    assert doc.grievance_against_party == "Employee"  # HRMS mandatory
    assert doc.grievance_against == "HR-EMP-1"
    assert doc.grievance_type == "Lương"
    assert doc.description == "Sai lương"  # falls back to subject (mandatory)
    assert doc.date  # set when absent


def test_submit_grievance_seeds_default_type(mod):
    m, stub = mod
    stub.list_rows["Grievance Type"] = []
    m.submit_grievance(employee="HR-EMP-1", subject="X")
    seeded = [k for k in stub.store if k[0] == "Grievance Type"]
    assert seeded, "a default Grievance Type master must be seeded on first use"


def test_resolve_grievance_sets_status(mod):  # SV-B06
    m, _ = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.resolve_grievance(created["name"], resolution="Đã giải quyết")
    assert res["status"] == "Resolved"
    doc = m.frappe.store[("Employee Grievance", created["name"])]
    assert doc.resolution_detail == "Đã giải quyết"  # HRMS field
    assert doc.resolved_by == "hr@gege.local"


def test_resolve_grievance_requires_manager(mod):  # SV-B07
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.resolve_grievance(created["name"])


def test_all_grievances_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_grievances()


# ── travel ──────────────────────────────────────────────────────────────────
def test_submit_travel_validates(mod):  # SV-B04 + missing fields
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_travel_request(employee="HR-EMP-1", purpose_of_travel="KH", from_date=None, to_date=None)
    with pytest.raises(Exception):
        m.submit_travel_request(
            employee="HR-EMP-1", purpose_of_travel="  ", from_date="2026-08-10", to_date="2026-08-12"
        )


def test_submit_travel_rejects_inverted_dates(mod):  # SV-B04 (G2)
    m, _ = mod
    with pytest.raises(Exception) as ei:
        m.submit_travel_request(
            employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-12", to_date="2026-08-10"
        )
    assert "Ngày về không được trước ngày đi" in str(ei.value)


def test_submit_travel_bad_cost_is_ignored(mod):  # SV-B05
    m, stub = mod
    res = m.submit_travel_request(
        employee="HR-EMP-1",
        purpose_of_travel="Đi khách",
        from_date="2026-08-10",
        to_date="2026-08-12",
        estimated_cost="không-phải-số",
    )
    doc = stub.store[("Travel Request", res["name"])]
    assert doc.vn_purpose == "Đi khách"
    assert not getattr(doc, "vn_total_cost", None)  # skipped, no crash
    assert doc.travel_type == "Domestic"  # HRMS mandatory default


def test_approve_travel_sets_status(mod):  # SV-B08
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-10", to_date="2026-08-12"
    )
    res = m.approve_travel_request(created["name"])
    assert res["status"] == "Approved"
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Approved"
    stub.list_rows["Travel Request"] = [
        {"name": created["name"], "employee": "HR-EMP-1", "vn_status": "Approved", "docstatus": 0}
    ]
    rows = m.my_travel_requests(status="Approved")
    assert rows["total"] == 1 and rows["data"][0]["status"] == "Approved"


def test_reject_travel_request(mod):  # SV-B03 (G5)
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Công tác SG", from_date="2026-09-01", to_date="2026-09-03"
    )
    res = m.reject_travel_request(created["name"], reason="Hết ngân sách")
    assert res["status"] == "Rejected"
    assert "Từ chối: Hết ngân sách" in res["note"]
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Rejected"
    assert "Từ chối: Hết ngân sách" in doc.vn_note


def test_reject_travel_requires_manager(mod):
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="X", from_date="2026-08-10", to_date="2026-08-12"
    )
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.reject_travel_request(created["name"])


def test_all_travel_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_travel_requests()


# ── options / notify ────────────────────────────────────────────────────────
def test_grievance_options_empty_masters(mod):  # SV-B09
    m, _ = mod
    opts = m.grievance_options()
    assert opts["grievance_types"] == []
    assert set(("Draft", "Approved", "Rejected")) <= set(opts["travel_statuses"])


def test_notify_failure_never_breaks_resolve(mod, monkeypatch):  # SV-B11 (G6)
    m, _ = mod

    def _boom(**k):
        raise Exception("notify down")

    # monkeypatch (not raw assignment) — notify is a SHARED module; a bare
    # assignment leaks the stub into sibling tests (test_notify).
    monkeypatch.setattr(m.notify, "push_notification", _boom)
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.resolve_grievance(created["name"], resolution="ok")  # must NOT raise
    assert res["status"] == "Resolved"


# ── services-deskfree P0 — detail / update / withdraw / delete (SV1-SV24) ────
def test_get_grievance_owner_open(mod):  # SV1
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", grievance_type="Lương", subject="Sai lương")
    stub.roles = {"Employee"}  # owner (không phải HR) vẫn xem được đơn mình
    res = m.get_grievance(created["name"])
    assert res["status"] == "Open"
    assert res["doc"]["subject"] == "Sai lương"
    assert res["doc"]["description"] == "Sai lương"
    assert res["doc"]["employee"] == "HR-EMP-1"  # projected key
    assert res["doc"]["raised_on"]  # date → raised_on
    can = res["can"]
    assert can["edit"] is True and can["withdraw"] is True and can["delete"] is True
    assert can["resolve"] is False and can["comment"] is True


def test_get_travel_request_draft(mod):  # SV2
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1",
        purpose_of_travel="Đi KH",
        from_date="2026-09-01",
        to_date="2026-09-03",
        estimated_cost=1500000,
    )
    stub.roles = {"Employee"}
    res = m.get_travel_request(created["name"])
    assert res["status"] == "Draft"
    assert res["doc"]["purpose_of_travel"] == "Đi KH"
    assert res["doc"]["total_travel_cost"] == 1500000
    assert res["doc"]["docstatus"] == 0  # projector pops — endpoint trả lại
    can = res["can"]
    assert can["edit"] and can["withdraw"] and can["delete"]
    assert not can["approve"] and not can["reject"]


def test_get_denies_other_employee(mod):  # SV3 (IDOR)
    m, stub = mod
    created = m.submit_grievance(employee="OTHER-EMP", subject="X")
    created_t = m.submit_travel_request(
        employee="OTHER-EMP", purpose_of_travel="Y", from_date="2026-09-01", to_date="2026-09-02"
    )
    stub.roles = {"Employee"}
    stub.employee_for_user = "HR-EMP-1"
    with pytest.raises(Exception):
        m.get_grievance(created["name"])
    with pytest.raises(Exception):
        m.get_travel_request(created_t["name"])


def test_get_grievance_resolved(mod):  # SV4
    m, _ = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(created["name"], resolution="Đã xử lý")
    res = m.get_grievance(created["name"])
    doc = res["doc"]
    assert doc["resolution_detail"] == "Đã xử lý"
    assert doc["cause_of_grievance"] == "Đã xử lý"  # F2 fallback chain
    assert doc["resolved_by"] == "hr@gege.local"
    assert res["can"]["resolve"] is False and res["can"]["reopen"] is True


def test_activity_rows_merges_version_comment(mod):  # SV5
    m, stub = mod
    stub.list_rows["Version"] = [
        {
            "ref_doctype": "Employee Grievance",
            "docname": "G-1",
            "name": "V1",
            "owner": "a@x",
            "creation": "2026-09-01 10:00:00",
        },
        {
            "ref_doctype": "Employee Grievance",
            "docname": "G-1",
            "name": "V2",
            "owner": "b@x",
            "creation": "2026-09-03 10:00:00",
        },
    ]
    stub.list_rows["Comment"] = [
        {
            "reference_doctype": "Employee Grievance",
            "reference_name": "G-1",
            "comment_type": "Comment",
            "name": "C1",
            "owner": "c@x",
            "creation": "2026-09-02 10:00:00",
            "content": "hello",
        },
    ]
    rows = m._activity_rows("Employee Grievance", "G-1")
    assert [r["name"] for r in rows] == ["V2", "C1", "V1"]  # sort creation desc
    assert rows[1]["type"] == "comment" and rows[1]["detail"] == "hello"


def test_update_grievance_open(mod):  # SV6
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="Cũ")
    res = m.update_grievance(created["name"], subject="Mới", description="Mô tả mới")
    assert res["status"] == "Open"
    doc = stub.store[("Employee Grievance", created["name"])]
    assert doc.subject == "Mới" and doc.description == "Mô tả mới"


def test_update_grievance_rejects_empty_subject(mod):  # SV6b
    m, _ = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="Cũ")
    with pytest.raises(Exception):
        m.update_grievance(created["name"], subject="   ")


def test_update_travel_inverted_dates(mod):  # SV7
    m, _ = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    with pytest.raises(Exception) as ei:
        m.update_travel_request(created["name"], from_date="2026-09-05", to_date="2026-09-03")
    assert "Ngày về không được trước ngày đi" in str(ei.value)


def test_update_travel_blocked_when_approved(mod):  # SV8
    m, _ = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    m.approve_travel_request(created["name"])  # vn_status → Approved
    with pytest.raises(Exception):
        m.update_travel_request(created["name"], purpose_of_travel="Đổi")


def test_update_grievance_blocked_when_resolved(mod):  # SV8b
    m, _ = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(created["name"], resolution="ok")
    with pytest.raises(Exception):
        m.update_grievance(created["name"], subject="Đổi")


def test_update_travel_rejected_resets_draft(mod):  # SV9
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    m.reject_travel_request(created["name"], reason="Hết ngân sách")
    res = m.update_travel_request(created["name"], purpose_of_travel="KH sửa")
    assert res["status"] == "Draft"
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Draft" and doc.vn_purpose == "KH sửa"


def test_update_denies_other_employee(mod):  # SV10 (IDOR)
    m, stub = mod
    created = m.submit_grievance(employee="OTHER-EMP", subject="X")
    created_t = m.submit_travel_request(
        employee="OTHER-EMP", purpose_of_travel="Y", from_date="2026-09-01", to_date="2026-09-02"
    )
    stub.roles = {"Employee"}
    stub.employee_for_user = "HR-EMP-1"
    with pytest.raises(Exception):
        m.update_grievance(created["name"], subject="Đổi")
    with pytest.raises(Exception):
        m.update_travel_request(created_t["name"], purpose_of_travel="Đổi")


def test_withdraw_grievance_open(mod):  # SV11
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    stub.roles = {"Employee"}  # chủ đơn tự rút (không cần HR)
    res = m.withdraw_grievance(created["name"], note="Không cần nữa")
    assert res["status"] == "Invalid"
    assert "Không cần nữa" in res["note"]
    doc = stub.store[("Employee Grievance", created["name"])]
    assert doc.status == "Invalid"
    assert stub.published and stub.published[-1][0] == "employee_services_updated"


def test_withdraw_travel_draft(mod):  # SV12
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    stub.roles = {"Employee"}
    res = m.withdraw_travel_request(created["name"])
    assert res["status"] == "Rejected"
    assert "Nhân viên tự rút đơn" in res["note"]
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Rejected"


def test_withdraw_blocked_after_outcome(mod):  # SV13
    m, _ = mod
    created_g = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(created_g["name"], resolution="ok")
    with pytest.raises(Exception):
        m.withdraw_grievance(created_g["name"])
    created_t = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    m.approve_travel_request(created_t["name"])
    with pytest.raises(Exception):
        m.withdraw_travel_request(created_t["name"])


def test_delete_grievance_draft(mod):  # SV14
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    stub.roles = {"Employee"}
    res = m.delete_service_draft("Employee Grievance", created["name"])
    assert res["deleted"] is True
    assert ("Employee Grievance", created["name"]) not in stub.store
    assert ("Employee Grievance", created["name"]) in stub.deleted


def test_delete_travel_draft(mod):  # SV15
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    res = m.delete_service_draft("Travel Request", created["name"])
    assert res["deleted"] is True
    assert ("Travel Request", created["name"]) not in stub.store


def test_delete_guards(mod):  # SV16
    m, stub = mod
    with pytest.raises(Exception):
        m.delete_service_draft("Sneaky DocType", "X")  # doctype lạ bị whitelist chặn
    created_t = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-03"
    )
    stub.store[("Travel Request", created_t["name"])].docstatus = 1
    with pytest.raises(Exception):
        m.delete_service_draft("Travel Request", created_t["name"])
    created_g = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(created_g["name"], resolution="ok")
    with pytest.raises(Exception):
        m.delete_service_draft("Employee Grievance", created_g["name"])


def test_resolve_grievance_cause_chain(mod):  # SV17 (F2)
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="Chủ đề X")
    m.resolve_grievance(created["name"], resolution="R")  # không truyền cause
    assert stub.store[("Employee Grievance", created["name"])].cause_of_grievance == "R"
    created2 = m.submit_grievance(employee="HR-EMP-1", subject="S2")
    m.resolve_grievance(created2["name"], resolution=None, cause="Nguyên nhân")
    assert stub.store[("Employee Grievance", created2["name"])].cause_of_grievance == "Nguyên nhân"


def test_publish_wired_everywhere(mod):  # SV19 (G9)
    m, stub = mod
    g = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(g["name"], resolution="ok")
    t = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-02"
    )
    m.approve_travel_request(t["name"])
    t2 = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH2", from_date="2026-09-01", to_date="2026-09-02"
    )
    m.reject_travel_request(t2["name"], reason="R")
    g2 = m.submit_grievance(employee="HR-EMP-1", subject="Y")
    m.update_grievance(g2["name"], subject="Y2")
    m.withdraw_grievance(g2["name"])
    t3 = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH3", from_date="2026-09-01", to_date="2026-09-02"
    )
    m.update_travel_request(t3["name"], purpose_of_travel="KH3b")
    m.withdraw_travel_request(t3["name"])
    t4 = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH4", from_date="2026-09-01", to_date="2026-09-02"
    )
    m.delete_service_draft("Travel Request", t4["name"])
    assert len(stub.published) == 14  # mỗi mutate đúng 1 publish
    assert all(ev[0] == "employee_services_updated" for ev in stub.published)


def test_grievance_options_extended(mod):  # SV20 (G12)
    m, stub = mod
    stub.list_rows["Purpose of Travel"] = [{"name": "Công tác SG"}]
    opts = m.grievance_options()
    assert opts["purposes_of_travel"] == ["Công tác SG"]
    assert "Open" in opts["grievance_all_statuses"] and "Invalid" in opts["grievance_all_statuses"]
    assert opts["travel_types"] == ["Domestic", "International"]
    # key cũ nguyên vẹn (FE cũ không vỡ)
    assert "grievance_types" in opts and "travel_statuses" in opts and "grievance_statuses" in opts


def test_all_grievances_employee_filter(mod):  # SV21 (G13)
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "G-1", "raised_by": "HR-EMP-1", "subject": "A", "status": "Open"},
        {"name": "G-2", "raised_by": "HR-EMP-2", "subject": "B", "status": "Open"},
    ]
    res = m.all_grievances(employee="HR-EMP-2")
    assert res["total"] == 1 and res["data"][0]["name"] == "G-2"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_grievances(employee="HR-EMP-2")


def test_all_travel_employee_filter(mod):  # SV22 (G13)
    m, stub = mod
    stub.list_rows["Travel Request"] = [
        {"name": "T-1", "employee": "HR-EMP-1", "vn_status": "Draft", "docstatus": 0},
        {"name": "T-2", "employee": "HR-EMP-2", "vn_status": "Draft", "docstatus": 0},
    ]
    res = m.all_travel_requests(employee="HR-EMP-1")
    assert res["total"] == 1 and res["data"][0]["name"] == "T-1"


def test_notify_deep_link(mod, monkeypatch):  # SV23 (G24)
    m, _ = mod
    captured = {}
    monkeypatch.setattr(m.notify, "push_notification", lambda **k: captured.update(k))
    g = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(g["name"], resolution="ok")
    assert captured["action_url"] == f"/services?tab=grievance&doc={g['name']}"
    captured.clear()
    t = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-02"
    )
    m.approve_travel_request(t["name"])
    assert captured["action_url"] == f"/services?tab=travel&doc={t['name']}"


def test_list_shape_regression(mod):  # SV24
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "G-1", "raised_by": "HR-EMP-1", "subject": "A", "status": "Open"}
    ]
    res = m.my_grievances()
    assert set(res) >= {"data", "total", "summary"}
    assert res["data"][0]["employee"] == "HR-EMP-1"
    stub.list_rows["Travel Request"] = [
        {"name": "T-1", "employee": "HR-EMP-1", "vn_status": "Draft", "docstatus": 0}
    ]
    res_t = m.my_travel_requests()
    assert res_t["data"][0]["status"] == "Draft"


# ── services-deskfree P1 — lifecycle / collaboration / operations (SV25-SV32) ─
def test_investigate_grievance(mod):  # SV25
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    with pytest.raises(Exception):  # cause bắt buộc
        m.investigate_grievance(created["name"])
    res = m.investigate_grievance(created["name"], cause="Sai cấu hình ca")
    assert res["status"] == "Investigated"
    doc = stub.store[("Employee Grievance", created["name"])]
    assert doc.status == "Investigated" and doc.cause_of_grievance == "Sai cấu hình ca"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.investigate_grievance(created["name"], cause="x")


def test_invalidate_grievance(mod):  # SV26
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.invalidate_grievance(created["name"], reason="Trùng đơn")
    assert res["status"] == "Invalid"
    assert "HR đóng đơn" in res["note"]
    doc = stub.store[("Employee Grievance", created["name"])]
    assert doc.status == "Invalid"


def test_reopen_grievance(mod):  # SV27
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    m.resolve_grievance(created["name"], resolution="ok")
    stub.roles = {"Employee"}  # chủ đơn tự mở lại
    res = m.reopen_grievance(created["name"], note="Chưa thỏa đáng")
    assert res["status"] == "Open"
    assert "Chưa thỏa đáng" in res["note"]  # note user được giữ nguyên
    # đơn không Resolved → throw
    with pytest.raises(Exception):
        m.reopen_grievance(created["name"])
    # fallback prefix khi không có note
    stub.roles = {"HR Manager"}  # resolve cần manager
    m.resolve_grievance(created["name"], resolution="lần 2")
    stub.roles = {"Employee"}
    res2 = m.reopen_grievance(created["name"])
    assert "NV đề nghị mở lại" in res2["note"]


def test_approve_travel_submits(mod):  # SV28 (F3)
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-02"
    )
    res = m.approve_travel_request(created["name"])
    assert res["status"] == "Approved" and res["docstatus"] == 1
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.docstatus == 1 and doc.vn_status == "Approved"
    # set_user Administrator scoped + restore user thật
    assert "Administrator" in stub.set_user_calls
    assert stub.set_user_calls[-1] == "hr@gege.local"
    # duyệt lần 2 → throw
    with pytest.raises(Exception):
        m.approve_travel_request(created["name"])


def test_cancel_travel_after_approve(mod):  # SV29 (G17)
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-02"
    )
    with pytest.raises(Exception):  # reason bắt buộc
        m.cancel_travel_request(created["name"], reason="  ")
    with pytest.raises(Exception):  # chưa duyệt (ds0) → throw
        m.cancel_travel_request(created["name"], reason="x")
    m.approve_travel_request(created["name"])
    res = m.cancel_travel_request(created["name"], reason="Đổi lịch")
    assert res["status"] == "Rejected" and res["docstatus"] == 2
    assert "Hủy sau duyệt: Đổi lịch" in res["note"]
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.docstatus == 2 and doc.vn_status == "Rejected"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.cancel_travel_request(created["name"], reason="x")


def test_add_service_comment(mod):  # SV30 (G6)
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    with pytest.raises(Exception):  # rỗng → throw
        m.add_service_comment("Employee Grievance", created["name"], text="  ")
    res = m.add_service_comment("Employee Grievance", created["name"], text="Cho hỏi tiến độ?")
    assert res["content"] == "Cho hỏi tiến độ?" and res["actor"] == "hr@gege.local"
    c = stub.store[("Comment", res["name"])]
    assert c.reference_doctype == "Employee Grievance" and c.comment_type == "Comment"
    stub.roles = {"Employee"}
    stub.employee_for_user = "HR-EMP-1"
    m.add_service_comment("Employee Grievance", created["name"], text="của mình")  # owner OK
    stub.employee_for_user = "HR-EMP-2"
    with pytest.raises(Exception):  # NV khác link doc mù → throw qua _assert_own
        m.add_service_comment("Employee Grievance", "OTHER-DOC", text="x")


def test_upload_service_attachment(mod, monkeypatch):  # SV30b (G7)
    import sys as _sys

    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH", from_date="2026-09-01", to_date="2026-09-02"
    )
    saved = {}

    def fake_save_file(fname, content, dt, dn, is_private=False):
        saved["args"] = (fname, dt, dn)
        return {"file_name": fname, "file_url": f"/files/{fname}"}

    fm = types.ModuleType("frappe.utils.file_manager")
    fm.save_file = fake_save_file
    monkeypatch.setitem(_sys.modules, "frappe.utils", types.ModuleType("frappe.utils"))
    monkeypatch.setitem(_sys.modules, "frappe.utils.file_manager", fm)
    stub.request = types.SimpleNamespace(
        files={"file": types.SimpleNamespace(filename="ve.pdf", read=lambda: b"PDF")}
    )
    res = m.upload_service_attachment("Travel Request", created["name"])
    assert saved["args"][0] == "ve.pdf" and saved["args"][1] == "Travel Request"
    assert res["file_url"] == "/files/ve.pdf"
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.travel_proof == "/files/ve.pdf"  # F4 — native Attach mirror
    stub.request = types.SimpleNamespace(files={})
    with pytest.raises(Exception):
        m.upload_service_attachment("Travel Request", created["name"])


def test_bulk_travel_action_partial(mod):  # SV31 (G14)
    m, stub = mod
    t1 = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH1", from_date="2026-09-01", to_date="2026-09-02"
    )
    t2 = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="KH2", from_date="2026-09-01", to_date="2026-09-02"
    )
    stub.store[("Travel Request", t2["name"])].docstatus = 1  # t2 đã submit → approve fail
    res = m.bulk_travel_action([t1["name"], t2["name"]], action="approve")
    assert res["updated"] == 1 and len(res["failed"]) == 1
    assert res["failed"][0]["name"] == t2["name"]
    assert stub.store[("Travel Request", t1["name"])].docstatus == 1
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.bulk_travel_action([t1["name"]], action="approve")


def test_service_summary(mod):  # SV32 (G22)
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "G-1", "status": "Open"},
        {"name": "G-2", "status": "Open"},
        {"name": "G-3", "status": "Resolved"},
    ]
    stub.list_rows["Travel Request"] = [
        {"name": "T-1", "vn_status": "Draft", "docstatus": 0},
        {"name": "T-2", "vn_status": "Approved", "docstatus": 1},
    ]
    res = m.service_summary()
    assert res["grievance"] == {"Open": 2, "Resolved": 1}
    assert res["travel"] == {"Draft": 1, "Approved": 1}
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.service_summary()
