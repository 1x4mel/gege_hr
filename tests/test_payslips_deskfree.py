"""Bench-free tests for the payslips desk-free endpoints (plan
plans/payslips-deskfree-complete.md §5.1 B1–B15).

Covers ``api/payroll.py``: ``payslips_all`` (+summary), ``payslip_detail``
enrichment (``auto_confirm_deadline`` / ``can`` / ``timeline``),
``add_payslip_comment``, ``email_payslip`` / ``bulk_email_payslips``,
``download_payslip_pdf`` and the pure ``merge_payslip_timeline`` — plus the
idempotent Print Format seed in ``setup_payslips_deskfree.py``.

Harness mirrors ``test_payroll_api.py``: a stub ``frappe`` (complete with
``frappe.utils`` and ``frappe.utils.print_format``) is injected into
``sys.modules``; ``api.payroll`` is reloaded against it.
"""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

PAYROLL_API = "gege_hr.gege_hr.api.payroll"
SEED_MOD = "gege_hr.gege_hr.setup_payslips_deskfree"


class _DotDict(dict):
    """frappe._dict parity — attribute access on dict rows."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None


class _FakeDoc:
    def __init__(self, payload, name="NEW-0001"):
        if isinstance(payload, dict):
            self.__dict__.update(payload)
            self.name = payload.get("name", name)
        else:
            self.name = name
        self.inserted = self.saved = False

    def insert(self, **k):
        self.inserted = True
        return self

    def save(self, **k):
        self.saved = True
        return self


class _FakeDB:
    """get_all with simple dict-equality filters + per-name get_value store."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.values: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []
        self.exists_set: set[tuple[str, str]] = set()

    def get_all(self, doctype, **kw):
        self.calls.append({"doctype": doctype, **kw})
        base = [dict(r) for r in self.rows.get(doctype, [])]
        filters = kw.get("filters")
        eq: dict = {}
        if isinstance(filters, list):
            eq = {f[0]: f[2] for f in filters if isinstance(f, (list, tuple)) and len(f) == 3 and f[1] == "="}
        elif isinstance(filters, dict):
            eq = {k: v for k, v in filters.items() if not isinstance(v, (list, tuple))}
        if eq:
            base = [r for r in base if all(r.get(k) == v for k, v in eq.items())]
        pluck = kw.get("pluck")
        if pluck:
            return [r[pluck] for r in base]
        lpl = int(kw.get("limit_page_length") or 0)
        ls = int(kw.get("limit_start") or 0)
        if lpl:
            base = base[ls : ls + lpl]
        return [_DotDict(r) for r in base]

    def get_value(self, doctype, name, fields=None, as_dict=False):
        v = self.values.get((doctype, name))
        if v is None:
            return None
        if as_dict:
            return _DotDict(v)
        if isinstance(fields, str):
            return v.get(fields)
        return v

    def count(self, doctype, filters=None):
        return len(self.rows.get(doctype, []))

    def exists(self, doctype, name):
        return (doctype, name) in self.exists_set

    def set_value(self, *a, **k):
        return None


def _build_stub_frappe(db: _FakeDB, roles=("HR Manager",), user="hr.manager@gege.test"):
    mod = types.ModuleType("frappe")
    mod.__path__ = []
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.log_error = lambda *a, **k: None
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.ValidationError = type("ValidationError", (Exception,), {})

    def _throw(msg, exc=Exception, *a, **k):
        raise exc(str(msg))

    mod.throw = _throw
    mod.db = db
    mod.get_all = db.get_all
    mod.session = types.SimpleNamespace(user=user)
    mod.enqueued: list[dict] = []
    mod.sent: list[dict] = []
    mod.published: list[dict] = []

    def _enqueue(method, **kwargs):
        mod.enqueued.append({"method": method, **kwargs})

    mod.enqueue = _enqueue
    mod.sendmail = lambda **kw: mod.sent.append(kw)
    mod.publish_realtime = lambda *a, **kw: mod.published.append({"args": a, **kw})

    created: list = []

    def _get_doc(payload, name=None):
        if isinstance(payload, str):
            # doc lookup (e.g. _get_period) — synthesize a light doc
            d = _FakeDoc({"company": "CO-1", "name": name, "status": "Slips Generated"}, name=name)
            return d
        doc = _FakeDoc(payload)
        created.append(doc)
        db.exists_set.add((payload.get("doctype"), doc.name))
        return doc

    mod.get_doc = _get_doc
    mod._created_docs = created

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: __import__("datetime").date.today()
    utils.today = lambda: __import__("datetime").date.today()
    utils.add_days = lambda d, n: d
    utils.flt = lambda v, p=None: float(v or 0)
    utils.cint = lambda v, p=None: int(v or 0)
    mod.utils = utils

    pf = types.ModuleType("frappe.utils.print_format")
    pf.downloads: list[tuple] = []
    pf.download_pdf = lambda doctype, name, format=None: pf.downloads.append((doctype, name, format))
    utils.print_format = pf

    mod._roles = tuple(roles)
    return mod, utils, pf


@pytest.fixture
def api(monkeypatch):
    """Reload api.payroll against the stub; roles configurable per-test via
    ``mod._roles`` on the shared utils.employee module (payroll binds it as
    ``emp_utils``)."""
    db = _FakeDB()
    stub, utils, pf = _build_stub_frappe(db)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    monkeypatch.setitem(sys.modules, "frappe.utils.print_format", pf)
    mod = importlib.reload(importlib.import_module(PAYROLL_API))
    monkeypatch.setattr(mod, "frappe", stub)

    state = {"roles": ["HR Manager"], "employee": None}

    def _roles(user=None):
        return state["roles"]

    def _employee(user=None):
        return state["employee"]

    monkeypatch.setattr(mod.emp_utils, "get_user_roles", _roles)
    monkeypatch.setattr(mod.emp_utils, "get_employee_for_user", _employee)
    mod._test_state = state
    return mod, stub, db, pf


def _slip_rows():
    return [
        {
            "name": "PS-001",
            "employee": "E-1",
            "employee_name": "Nhân viên Một",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "posting_date": "2026-09-01",
            "gross_pay": 10_000_000,
            "total_deduction": 1_000_000,
            "net_pay": 9_000_000,
            "status": "Submitted",
            "docstatus": 1,
            "vn_employee_visible": 1,
            "vn_visible_at": "2026-09-01 06:00:00",
            "vn_ack_status": "",
            "company": "CO-1",
        },
        {
            "name": "PS-002",
            "employee": "E-2",
            "employee_name": "Nhân viên Hai",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "posting_date": "2026-09-01",
            "gross_pay": 8_000_000,
            "total_deduction": 800_000,
            "net_pay": 7_200_000,
            "status": "Submitted",
            "docstatus": 1,
            "vn_employee_visible": 1,
            "vn_visible_at": "2026-09-01 06:00:00",
            "vn_ack_status": "Awaiting Payment",
            "company": "CO-1",
        },
    ]


# --------------------------------------------------------------------------- #
# B1–B4 — payslips_all (manager list + filters + summary)
# --------------------------------------------------------------------------- #
def test_payslips_all_employee_forbidden(api):
    mod, _, _, _ = api
    mod._test_state["roles"] = ["Employee"]
    with pytest.raises(Exception):
        mod.payslips_all()


def test_payslips_all_filter_and_envelope(api):
    mod, stub, db, _ = api
    db.rows["Salary Slip"] = _slip_rows()
    db.rows["Employee"] = [
        {"name": "E-1", "department": "Kinh doanh", "company": "CO-1"},
        {"name": "E-2", "department": "Kỹ thuật", "company": "CO-1"},
    ]
    res = mod.payslips_all(status="Submitted", department="Kinh doanh")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "PS-001"
    assert res["data"][0]["department"] == "Kinh doanh"
    slip_calls = [c for c in db.calls if c["doctype"] == "Salary Slip"]
    assert slip_calls and slip_calls[-1]["filters"].get("status") == "Submitted"


def test_payslips_all_summary_buckets(api):
    mod, _, db, _ = api
    db.rows["Salary Slip"] = _slip_rows()
    db.rows["Employee"] = []
    res = mod.payslips_all()
    s = res["summary"]
    assert s["total"] == 2
    assert s["total_net"] == pytest.approx(16_200_000)
    assert s["awaiting_ack"] == 1  # PS-001 visible + no ack yet
    assert s["awaiting_payment"] == 1  # PS-002


def test_payslips_all_free_text_search(api):
    mod, _, db, _ = api
    db.rows["Salary Slip"] = _slip_rows()
    db.rows["Employee"] = []
    res = mod.payslips_all(q="hai")
    assert res["total"] == 1
    assert res["data"][0]["employee_name"] == "Nhân viên Hai"


def test_payslips_all_departments_manager_only(api):
    mod, _, db, _ = api
    db.rows["Department"] = [
        {"name": "Kinh doanh", "is_group": 0},
        {"name": "Kỹ thuật", "is_group": 0},
    ]
    res = mod.payslips_all_departments()
    assert {r["name"] for r in res} == {"Kinh doanh", "Kỹ thuật"}
    mod._test_state["roles"] = ["Employee"]
    with pytest.raises(Exception):
        mod.payslips_all_departments()


# --------------------------------------------------------------------------- #
# B5–B7 — payslip_detail enrichment
# --------------------------------------------------------------------------- #
def _seed_detail_slip(db):
    db.rows["Salary Slip"] = _slip_rows()
    db.values[("Salary Slip", "PS-001")] = dict(_slip_rows()[0])
    db.values[("Employee", "E-1")] = {"name": "E-1", "user_id": "nv1@gege.test"}
    db.rows["Comment"] = [
        {
            "name": "C-1",
            "owner": "nv1@gege.test",
            "creation": "2026-09-02 10:00:00",
            "content": "Số giờ tăng ca thiếu 1h",
            "reference_doctype": "Salary Slip",
            "reference_name": "PS-001",
        }
    ]
    db.rows["Version"] = [
        {
            "name": "V-1",
            "owner": "Administrator",
            "creation": "2026-09-01 08:00:00",
            "data": json.dumps({"changed": [["vn_employee_visible", 0, 1]]}),
            "ref_doctype": "Salary Slip",
            "docname": "PS-001",
        }
    ]
    db.rows["VN Audit Event"] = [
        {
            "name": "A-1",
            "actor": "hr.manager@gege.test",
            "creation": "2026-09-03 09:00:00",
            "audit_type": "Payslip Published",
            "description": "Công bố phiếu lương",
            "reference_doctype": "Salary Slip",
            "reference_name": "PS-001",
        }
    ]


def test_payslip_detail_auto_confirm_deadline(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    out = mod.payslip_detail("PS-001")
    # default 3 days when the payslip_ack setting is unreachable (stub)
    assert out["auto_confirm_deadline"] == "2026-09-04 06:00:00"
    # already-acked slips must NOT carry a deadline (same slip, now acked)
    db.values[("Salary Slip", "PS-001")] = dict(db.values[("Salary Slip", "PS-001")], vn_ack_status="Paid")
    out2 = mod.payslip_detail("PS-001")
    assert out2["auto_confirm_deadline"] is None


def test_payslip_detail_can_matrix_manager_vs_employee(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["HR Manager"]
    out = mod.payslip_detail("PS-001")
    assert out["can"] == {
        "pdf": True,
        "email": True,
        "comment": True,
        "view_review_line": True,
    }
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    out2 = mod.payslip_detail("PS-001")
    assert out2["can"]["pdf"] is True
    assert out2["can"]["view_review_line"] is False


def test_payslip_detail_timeline_merged_newest_first(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["HR Manager"]
    out = mod.payslip_detail("PS-001")
    tl = out["timeline"]
    assert [i["kind"] for i in tl] == ["audit", "comment", "version"]
    assert tl[0]["at"] >= tl[-1]["at"]


def test_merge_payslip_timeline_pure(api):
    mod, _, _, _ = api
    comments = [{"creation": "2026-09-02 10:00:00", "owner": "u@x", "content": "hello"}]
    versions = [
        {
            "creation": "2026-09-01 08:00:00",
            "owner": "admin",
            "data": json.dumps({"changed": [["net_pay", 0, 9000000], ["status", "Draft", "Submitted"]]}),
        }
    ]
    audits = [{"creation": "2026-09-03 09:00:00", "actor": "hr", "description": "Published"}]
    merged = mod.merge_payslip_timeline(comments=comments, versions=versions, audits=audits)
    assert [i["kind"] for i in merged] == ["audit", "comment", "version"]
    assert "net_pay: 0 → 9000000" in merged[2]["text"]
    assert merged == [i for i in merged if i["text"]]


def test_merge_payslip_timeline_empty_and_cap(api):
    mod, _, _, _ = api
    assert mod.merge_payslip_timeline() == []
    many = [{"creation": f"2026-09-01 00:00:{i:02d}", "owner": "u", "content": f"c{i}"} for i in range(60)]
    assert len(mod.merge_payslip_timeline(comments=many, limit=50)) == 50
    # blank texts are dropped
    assert (
        mod.merge_payslip_timeline(comments=[{"creation": "2026-09-01", "owner": "u", "content": "  "}]) == []
    )


# --------------------------------------------------------------------------- #
# B8–B9 — add_payslip_comment
# --------------------------------------------------------------------------- #
def test_add_payslip_comment_owner_creates_row(api):
    mod, stub, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    res = mod.add_payslip_comment("PS-001", "Xin HR kiểm tra lại giờ tăng ca")
    assert res["name"] == "PS-001"
    docs = [d for d in stub._created_docs if getattr(d, "doctype", None) == "Comment"]
    assert docs and docs[-1].content.startswith("Xin HR")
    assert docs[-1].reference_doctype == "Salary Slip"


def test_add_payslip_comment_blank_rejected(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    with pytest.raises(Exception):
        mod.add_payslip_comment("PS-001", "   ")


def test_add_payslip_comment_stranger_forbidden(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-2"  # not the slip's owner
    with pytest.raises(Exception):
        mod.add_payslip_comment("PS-001", "giả mạo")


# --------------------------------------------------------------------------- #
# B10–B12 — email_payslip / bulk_email_payslips
# --------------------------------------------------------------------------- #
def test_email_payslip_visible_enqueues(api):
    mod, stub, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    res = mod.email_payslip("PS-001")
    assert res["queued"] is True
    assert res["recipients"] == ["nv1@gege.test"]
    assert stub.enqueued and stub.enqueued[0]["method"].endswith("_send_payslip_email")


def test_email_payslip_invisible_rejected(api):
    mod, _, db, _ = api
    row = dict(_slip_rows()[0])
    row["vn_employee_visible"] = 0
    db.values[("Salary Slip", "PS-001")] = row
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    with pytest.raises(Exception):
        mod.email_payslip("PS-001")


def test_email_payslip_manager_bad_recipient_rejected(api):
    mod, _, db, _ = api
    _seed_detail_slip(db)
    mod._test_state["roles"] = ["HR Manager"]
    with pytest.raises(Exception):
        mod.email_payslip("PS-001", recipient="khong-dung")


def test_bulk_email_payslips_partial_safe(api):
    mod, stub, db, _ = api
    mod._test_state["roles"] = ["Payroll Manager"]
    db.rows["VN Payroll Review Line"] = [
        {"name": "L-1", "salary_slip": "PS-001", "employee": "E-1", "payroll_review_period": "PR-2026-08"},
        {"name": "L-2", "salary_slip": "PS-002", "employee": "E-2", "payroll_review_period": "PR-2026-08"},
        {"name": "L-3", "salary_slip": "PS-003", "employee": "E-3", "payroll_review_period": "PR-2026-08"},
    ]
    slips = {r["name"]: dict(r) for r in _slip_rows()}
    slips["PS-003"] = dict(slips["PS-001"], name="PS-003", employee="E-3", employee_name="Ba")
    db.values[("Salary Slip", "PS-001")] = slips["PS-001"]
    db.values[("Salary Slip", "PS-002")] = slips["PS-002"]
    db.values[("Salary Slip", "PS-003")] = slips["PS-003"]
    db.values[("Employee", "E-1")] = {"name": "E-1", "user_id": "nv1@gege.test"}
    db.values[("Employee", "E-2")] = {"name": "E-2", "user_id": None}  # missing email
    db.values[("Employee", "E-3")] = {"name": "E-3", "user_id": "nv3@gege.test"}
    res = mod.bulk_email_payslips("PR-2026-08")
    assert res["queued"] == ["PS-001", "PS-003"]
    assert {s["name"] for s in res["skipped"]} == {"PS-002"}
    assert res["skipped"][0]["reason"]


def test_bulk_email_payslips_employee_forbidden(api):
    mod, _, _, _ = api
    mod._test_state["roles"] = ["Employee"]
    with pytest.raises(Exception):
        mod.bulk_email_payslips("PR-2026-08")


# --------------------------------------------------------------------------- #
# B13–B14 — download_payslip_pdf
# --------------------------------------------------------------------------- #
def test_download_payslip_pdf_delegates_to_print_format(api):
    mod, _, db, pf = api
    db.values[("Salary Slip", "PS-001")] = dict(_slip_rows()[0])
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    mod.download_payslip_pdf("PS-001")
    assert pf.downloads == [("Salary Slip", "PS-001", "Phiếu lương VN")]


def test_download_payslip_pdf_invisible_forbidden(api):
    mod, _, db, _ = api
    row = dict(_slip_rows()[0])
    row["vn_employee_visible"] = 0
    db.values[("Salary Slip", "PS-001")] = row
    mod._test_state["roles"] = ["Employee"]
    mod._test_state["employee"] = "E-1"
    with pytest.raises(Exception):
        mod.download_payslip_pdf("PS-001")


def test_download_payslip_pdf_missing_seed_friendly_error(api):
    mod, _, db, pf = api
    db.values[("Salary Slip", "PS-001")] = dict(_slip_rows()[0])
    mod._test_state["roles"] = ["HR Manager"]

    def _boom(doctype, name, format=None):
        raise RuntimeError("no print format")

    pf.download_pdf = _boom
    with pytest.raises(Exception) as ei:
        mod.download_payslip_pdf("PS-001")
    assert "bench migrate" in str(ei.value)


# --------------------------------------------------------------------------- #
# B15 — Print Format seed idempotence
# --------------------------------------------------------------------------- #
def test_seed_print_format_idempotent(monkeypatch):
    db = _FakeDB()
    stub, utils, pf = _build_stub_frappe(db)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    seed = importlib.reload(importlib.import_module(SEED_MOD))
    monkeypatch.setattr(seed, "frappe", stub)

    first = seed.seed()
    second = seed.seed()
    assert first["print_format"] == "Phiếu lương VN"
    assert second["print_format"] is None  # already exists — no duplicate insert
    docs = [d for d in stub._created_docs if getattr(d, "doctype", None) == "Print Format"]
    assert len(docs) == 1
