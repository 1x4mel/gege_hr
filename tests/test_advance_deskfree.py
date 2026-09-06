"""Bench-free unit tests for the desk-free Salary Advance endpoints —
``api/advance.py`` + the ``approved_amount`` extension of ``api/approval.py``
(plans/advance-deskfree-complete.md §4.1, B1–B20).

Mirrors the stub-frappe harness of ``test_shift_assignment.py``
(``monkeypatch.setitem(sys.modules, "frappe", stub)``). No bench is required;
all persistence runs against the in-memory ``_FakeDB``.
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest

ADVANCE_API = "gege_hr.gege_hr.api.advance"
APPROVAL_API = "gege_hr.gege_hr.api.approval"

DOCTYPE = "VN Salary Advance Request"
TODAY = datetime.date(2026, 9, 3)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class _DotDict(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _FakeDoc:
    """Minimal frappe Document stand-in (attr + .get access, save/cancel)."""

    def __init__(self, payload=None, name="NEW-0001", doctype=None):
        payload = dict(payload or {})
        self.__dict__.update(payload)
        self.doctype = doctype or payload.get("doctype")
        self.name = payload.get("name", name)
        self.docstatus = payload.get("docstatus", 0)
        self.inserted = self.saved = self.cancelled = False

    def __getattr__(self, k):
        # Missing fields read as None (frappe Document behaviour) — e.g. a
        # freshly new_doc'ed SAR has no eligible_amount until validate runs.
        return None

    def update(self, payload):
        self.__dict__.update(payload or {})

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def insert(self, **kw):
        self.inserted = True
        return self

    def save(self, **kw):
        self.saved = True
        return self

    def cancel(self):
        self.cancelled = True
        self.docstatus = 2
        return self

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _apply_filters(rows, filters):
    """Simple AND filter eval (dict form incl. operator lists + 'not in'/'<'/>='/'!=')."""
    def _cmp(a, b):
        # Coerce to str when the types differ (posting_date str vs date object).
        if a is None:
            return False
        if type(a) is not type(b):
            a, b = str(a), str(b)
        return a >= b

    out = []
    for r in rows:
        ok = True
        for k, v in (filters or {}).items():
            val = r.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                op, operand = v
                if op == "not in" and val in operand:
                    ok = False
                elif op == "!=" and val == operand:
                    ok = False
                elif op == "<" and not (val is not None and str(val) < str(operand)):
                    ok = False
                elif op == ">=" and not _cmp(val, operand):
                    ok = False
                elif op == "between" and not (
                    operand[0] <= str(val or "") <= operand[1]
                ):
                    ok = False
            elif val != v:
                ok = False
            if not ok:
                break
        if ok:
            out.append(r)
    return out


class _FakeDB:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.values: dict[tuple, dict] = {}
        self.tables: set[str] = set()
        self.sql_calls: list[tuple] = []

    # -- get_all ----------------------------------------------------------- #
    def get_all(self, doctype, **kw):
        base = list(self.rows.get(doctype, []))
        filters = kw.get("filters")
        if isinstance(filters, dict):
            base = _apply_filters(base, filters)
        elif isinstance(filters, list):
            for f in filters:
                if isinstance(f, (list, tuple)) and len(f) == 3 and f[1] == "=":
                    base = [r for r in base if r.get(f[0]) == f[2]]
        fields = kw.get("fields") or []
        lpl = int(kw.get("limit_page_length") or 0)
        ls = int(kw.get("limit_start") or 0)
        if lpl:
            base = base[ls : ls + lpl]
        out = []
        for r in base:
            out.append(_DotDict({f: r.get(f) for f in fields}) if fields else _DotDict(r))
        return out

    def get_value(self, doctype, name, fields=None, as_dict=False):
        # Dict filter form (Employee {"user_id": ...}) — first matching row.
        if isinstance(name, dict):
            for r in self.rows.get(doctype, []):
                if all(r.get(k) == v for k, v in name.items()):
                    return r.get("name") if not as_dict else _DotDict(r)
            return None
        v = self.values.get((doctype, name))
        if v is None:
            return None
        if as_dict:
            return _DotDict(v)
        if isinstance(fields, str):
            return v.get(fields)
        return v

    def count(self, doctype, filters=None):
        return len(_apply_filters(self.rows.get(doctype, []), filters or {}))

    def table_exists(self, doctype):
        return doctype in self.tables

    def sql(self, query, values=None):
        self.sql_calls.append((query, values))
        # guarded_update's claim check: SELECT ROW_COUNT() → 1 changed row.
        if "ROW_COUNT" in str(query):
            return ((1,),)
        return None


class _Stub:
    def __init__(self, db):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.PermissionError = PermissionError  # frappe.throw(exc=frappe.PermissionError)
        self.log_error = lambda *a, **k: None
        self.db = db
        self.session = types.SimpleNamespace(user="emp@gege.test")
        self.roles: dict[str, list[str]] = {"emp@gege.test": ["Employee"]}
        self._doc_map: dict[tuple, _FakeDoc] = {}
        self.created: list[_FakeDoc] = []
        self.events: list[tuple[str, dict]] = []
        self.response = types.SimpleNamespace()

    def get_roles(self, user):
        return self.roles.get(user, ["Employee"])

    def get_doc(self, payload, name=None):
        if isinstance(payload, str):
            key = (payload, name)
            if key in self._doc_map:
                return self._doc_map[key]
            raise Exception(f"{payload} {name} not found")
        doc = _FakeDoc(payload, doctype=payload.get("doctype"))
        self.created.append(doc)
        return doc

    def new_doc(self, doctype):
        doc = _FakeDoc(doctype=doctype)
        self.created.append(doc)
        return doc

    def publish_realtime(self, event, payload=None):
        self.events.append((event, payload or {}))

    def throw(self, msg, exc=Exception, *a, **k):
        raise exc(msg)


def _utils():
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (TODAY if v in (None, "") else datetime.date.fromisoformat(str(v)[:10]))
    utils.nowdate = lambda: TODAY.isoformat()
    return utils


def _seed_common(db):
    db.tables.update(
        {DOCTYPE, "VN Salary Advance Policy", "Comment", "File", "Employee"}
    )
    db.rows["Employee"] = [
        {"name": "E-1", "user_id": "emp@gege.test", "status": "Active", "company": "CO-1"},
        {"name": "E-2", "user_id": "other@gege.test", "status": "Active", "company": "CO-1"},
    ]
    db.values[("Employee", "E-1")] = {"company": "CO-1", "vn_base_salary": 10000000}
    db.values[("Employee", "E-2")] = {"company": "CO-1", "vn_base_salary": 10000000}


def _mk_sar(name, **kw):
    payload = {
        "doctype": DOCTYPE,
        "name": name,
        "employee": "E-1",
        "employee_name": "Employee One",
        "company": "CO-1",
        "posting_date": "2026-09-01",
        "salary_advance_policy": "POL-1",
        "payroll_period": None,
        "requested_amount": 3000000,
        "eligible_amount": 3000000,
        "approved_amount": 0,
        "repayment_plan": "Next Month",
        "workflow_state": "Pending Manager",
        "payment_status": "Unpaid",
        "docstatus": 0,
        "reason": "Cần tiền sửa nhà",
    }
    payload.update(kw)
    return _FakeDoc(payload, name=name, doctype=DOCTYPE)


def _rebind_helpers(monkeypatch, stub):
    """Point the cached gege_hr helper modules' ``frappe`` global at THIS stub.

    ``sys.modules`` caches ``utils/employee`` / ``request_workflow`` / ``_db``
    across tests within one pytest session — without the rebind every test
    after the first resolves roles/SQL against the FIRST fixture's stub.
    """
    import importlib as _il

    for name in (
        "gege_hr.gege_hr.utils.employee",
        "gege_hr.gege_hr.utils.request_workflow",
        "gege_hr.gege_hr.utils._db",
    ):
        try:
            m = _il.import_module(name)
            monkeypatch.setattr(m, "frappe", stub)
        except Exception:
            pass


def _register(stub, db, doc):
    stub._doc_map[(DOCTYPE, doc.name)] = doc
    db.rows.setdefault(DOCTYPE, []).append(doc.as_dict())
    return doc


@pytest.fixture
def advance(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    _seed_common(db)
    sys.modules.pop(ADVANCE_API, None)
    mod = importlib.import_module(ADVANCE_API)
    monkeypatch.setattr(mod, "frappe", stub)
    _rebind_helpers(monkeypatch, stub)
    # Record audit calls without touching the real audit module — the recorder
    # stores the first positional (the audit type string) so assertions read
    # `a[0] == "Advance Submit"` instead of tuple-unpacking.
    rec = {"logs": []}

    def _record_audit(*args, **kwargs):
        rec["logs"].append(args[0] if args else None)
        return None

    monkeypatch.setattr(mod.audit_api, "log", _record_audit)
    return mod, stub, db, rec


@pytest.fixture
def approval(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)
    stub.session.user = "hr@gege.test"
    stub.roles["hr@gege.test"] = ["HR Manager"]
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    _seed_common(db)
    for api in (ADVANCE_API, APPROVAL_API):
        sys.modules.pop(api, None)
    mod = importlib.import_module(APPROVAL_API)
    monkeypatch.setattr(mod, "frappe", stub)
    _rebind_helpers(monkeypatch, stub)
    return mod, stub, db


# --------------------------------------------------------------------------- #
# B1–B3 — get_advance_request (permission / can-matrix / timeline shape)
# --------------------------------------------------------------------------- #
def test_get_other_employee_request_denied(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-1", employee="E-2"))
    with pytest.raises(Exception):
        mod.get_advance_request("SAR-1")


def test_get_paid_request_can_reverse_and_linked(advance):
    mod, stub, db, _ = advance
    doc = _register(
        stub,
        db,
        _mk_sar("SAR-1", workflow_state="Paid", payment_status="Paid",
                linked_additional_salary="AD-1"),
    )
    # manager session
    stub.roles["emp@gege.test"] = ["HR Manager"]
    db.tables.update({"VN Approval Log", "VN Audit Event", "Additional Salary"})
    db.rows["Additional Salary"] = [{"name": "AD-1", "docstatus": 1, "status": "Submitted"}]
    db.values[("Additional Salary", "AD-1")] = {"docstatus": 1, "status": "Submitted"}
    db.rows["VN Approval Log"] = [
        {"reference_doctype": DOCTYPE, "reference_name": "SAR-1", "action": "Approve",
         "from_state": "Pending HR", "to_state": "Approved", "actor": "hr@gege.test",
         "comment": "ok", "action_at": "2026-09-02 10:00:00"}
    ]
    db.rows["VN Audit Event"] = [
        {"reference_doctype": DOCTYPE, "reference_name": "SAR-1", "audit_type": "Advance Submit",
         "actor": "emp@gege.test", "description": "Draft → Pending Manager",
         "old_value": None, "new_value": None, "created_at": "2026-09-01 09:00:00"}
    ]
    res = mod.get_advance_request("SAR-1")
    assert res["can"] == {
        "edit": False, "cancel": False, "mark_paid": False, "reverse": True, "resubmit": False,
    }
    assert res["linked"]["additional_salary_docstatus"] == 1
    assert len(res["timeline"]["approval_logs"]) == 1
    assert len(res["timeline"]["audit"]) == 1
    assert res["doc"]["name"] == "SAR-1"
    assert res["doc"]["requested_amount"] == 3000000


def test_get_timeline_merges_sorted_desc(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-2"))
    db.tables.update({"VN Approval Log", "VN Audit Event"})
    db.rows["VN Approval Log"] = [
        {"reference_doctype": DOCTYPE, "reference_name": "SAR-2", "action": "Approve",
         "from_state": "Pending Manager", "to_state": "Pending HR", "actor": "mgr@gege.test",
         "comment": "", "action_at": "2026-09-02 08:00:00"},
        {"reference_doctype": DOCTYPE, "reference_name": "SAR-2", "action": "Submit",
         "from_state": "Draft", "to_state": "Pending Manager", "actor": "emp@gege.test",
         "comment": "", "action_at": "2026-09-01 08:00:00"},
    ]
    res = mod.get_advance_request("SAR-2")
    logs = res["timeline"]["approval_logs"]
    assert [l["action"] for l in logs] == ["Approve", "Submit"]  # desc by action_at


# --------------------------------------------------------------------------- #
# B4–B7 — update_advance_request
# --------------------------------------------------------------------------- #
def test_update_pending_request_revalidates_and_publishes(advance):
    mod, stub, db, rec = advance
    doc = _register(stub, db, _mk_sar("SAR-3"))
    res = mod.update_advance_request(
        "SAR-3", requested_amount=2500000, reason="Sửa số tiền"
    )
    assert doc.saved is True
    assert doc.requested_amount == 2500000
    assert doc.reason == "Sửa số tiền"
    assert res["status"] == "Pending Manager"
    assert any(a == "Advance Update" for a in rec["logs"])
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_update_approved_request_rejected(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-4", workflow_state="Approved"))
    with pytest.raises(Exception):
        mod.update_advance_request("SAR-4", requested_amount=1000000)


def test_update_no_change_throws(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-5"))
    with pytest.raises(Exception):
        mod.update_advance_request("SAR-5", requested_amount=3000000, reason="Cần tiền sửa nhà")


def test_update_posting_date_invalid_throws(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-6"))
    with pytest.raises(Exception):
        mod.update_advance_request("SAR-6", posting_date="not-a-date")


# --------------------------------------------------------------------------- #
# B8–B9 — resubmit_advance_request
# --------------------------------------------------------------------------- #
def test_resubmit_from_rejected_creates_fresh_pending(advance):
    mod, stub, db, rec = advance
    original = _register(stub, db, _mk_sar("SAR-7", workflow_state="Rejected"))
    res = mod.resubmit_advance_request("SAR-7")
    assert res["original"] == "SAR-7"
    assert original.workflow_state == "Rejected"  # untouched
    news = [d for d in stub.created if d.doctype == DOCTYPE and d.inserted]
    assert news, "a new SAR doc must be inserted"
    assert res["name"] == news[-1].name
    assert res["status"] == "Pending Manager"  # send_for_approval ran via save()
    assert news[-1].requested_amount == original.requested_amount
    assert news[-1].reason == original.reason
    assert any(a == "Advance Submit" for a in rec["logs"])


def test_resubmit_from_approved_rejected(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-8", workflow_state="Approved"))
    with pytest.raises(Exception):
        mod.resubmit_advance_request("SAR-8")


# --------------------------------------------------------------------------- #
# B10–B12 — approve_request(approved_amount=…)
# --------------------------------------------------------------------------- #
def test_approve_with_sanctioned_amount(approval):
    mod, stub, db = approval
    doc = _register(stub, db, _mk_sar("SAR-A", workflow_state="Pending HR"))
    res = mod.approve_request(
        name="SAR-A",
        request_type="Salary Advance Request",
        comment="duyệt 50%",
        approved_amount=1500000,
    )
    assert res["status"] == "Approved"
    assert doc.approved_amount == 1500000
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_approve_amount_above_requested_throws(approval):
    mod, stub, db = approval
    _register(stub, db, _mk_sar("SAR-B", workflow_state="Pending HR"))
    with pytest.raises(Exception):
        mod.approve_request(
            name="SAR-B",
            request_type="Salary Advance Request",
            approved_amount=9900000,
        )


def test_approve_amount_ignored_for_other_types(approval):
    mod, stub, db = approval
    doc = _FakeDoc(
        {
            "doctype": "VN Overtime Request",
            "name": "OT-1",
            "employee": "E-1",
            "workflow_state": "Pending Manager",
            "work_date": "2026-09-01",
            "requested_hours": 2,
            "reason": "OT",
            "docstatus": 0,
        },
        name="OT-1",
        doctype="VN Overtime Request",
    )
    stub._doc_map[("VN Overtime Request", "OT-1")] = doc
    db.rows["VN Overtime Request"] = [doc.as_dict()]
    res = mod.approve_request(
        name="OT-1", request_type="Overtime Request", approved_amount=123
    )
    assert res["status"]  # no crash; param ignored
    assert getattr(doc, "approved_amount", None) in (None, 0)


def test_reject_request_publishes_advance_realtime(approval):
    mod, stub, db = approval
    _register(stub, db, _mk_sar("SAR-C", workflow_state="Pending HR"))
    res = mod.reject_request(
        name="SAR-C", request_type="Salary Advance Request", comment="thiếu chứng từ"
    )
    assert res["status"] == "Rejected"
    assert any(ev[0] == "advance_updated" for ev in stub.events)


# --------------------------------------------------------------------------- #
# B13–B15 — list endpoint upgrades (server-side filters / pagination / perms)
# --------------------------------------------------------------------------- #
def _seed_list(db):
    db.rows[DOCTYPE] = [
        {"name": "SAR-L1", "employee": "E-1", "employee_name": "Employee One",
         "posting_date": "2026-09-01", "requested_amount": 3000000,
         "approved_amount": 0, "workflow_state": "Paid", "payment_status": "Paid",
         "docstatus": 0, "reason": "a"},
        {"name": "SAR-L2", "employee": "E-1", "employee_name": "Employee One",
         "posting_date": "2026-08-20", "requested_amount": 1500000,
         "approved_amount": 0, "workflow_state": "Approved", "payment_status": "Unpaid",
         "docstatus": 0, "reason": "b"},
        {"name": "SAR-L3", "employee": "E-1", "employee_name": "Employee One",
         "posting_date": "2026-08-05", "requested_amount": 500000,
         "approved_amount": 0, "workflow_state": "Rejected", "payment_status": "Unpaid",
         "docstatus": 0, "reason": "c"},
    ]


def test_my_requests_status_and_amount_filters(advance):
    mod, stub, db, _ = advance
    _seed_list(db)
    rows = mod.my_advance_requests(employee="E-1", status="Paid")
    assert [r["name"] for r in rows] == ["SAR-L1"]
    rows = mod.my_advance_requests(employee="E-1", min_amount=1000000, max_amount=2000000)
    assert [r["name"] for r in rows] == ["SAR-L2"]


def test_all_requests_search_pagination_summary(advance):
    mod, stub, db, _ = advance
    stub.roles["emp@gege.test"] = ["HR Manager"]  # manager gate
    _seed_list(db)
    res = mod.all_advance_requests(search="employee one", page=1, page_size=2)
    assert res["total"] == 3
    assert len(res["data"]) == 2
    assert res["summary"]["count"] == 3
    assert res["summary"]["total_requested"] == 5000000
    # bare-list legacy contract preserved without page_size
    rows = mod.all_advance_requests()
    assert isinstance(rows, list) and len(rows) == 3


def test_all_requests_denied_for_plain_employee(advance):
    mod, _, _, _ = advance
    with pytest.raises(Exception):
        mod.all_advance_requests()


# --------------------------------------------------------------------------- #
# B16 — preview_eligibility structured reasons (quota)
# --------------------------------------------------------------------------- #
def test_preview_reports_quota_exhausted(advance):
    mod, stub, db, _ = advance
    db.rows["VN Salary Advance Policy"] = [
        {"name": "POL-1", "company": "CO-1", "is_active": 1, "max_percentage": 30,
         "max_fixed_amount": 0, "min_working_days": 0, "max_requests_per_month": 2,
         "cutoff_day": 25, "modified": "2026-08-01"},
    ]
    db.rows[DOCTYPE] = [
        {"name": "SAR-Q1", "employee": "E-1", "workflow_state": "Approved",
         "docstatus": 0, "posting_date": "2026-09-01"},
        {"name": "SAR-Q2", "employee": "E-1", "workflow_state": "Paid",
         "docstatus": 0, "posting_date": "2026-09-02"},
    ]
    res = mod.preview_eligibility(employee="E-1", requested_amount=1000000)
    assert res["policy"] == "POL-1"
    assert res["policy_details"]["max_requests_per_month"] == 2
    quota = [r for r in res["reasons"] if r["code"] == "quota"]
    assert quota and quota[0]["ok"] is False  # 2/2 used


def test_preview_no_policy_degrades_permissive(advance):
    mod, _, db, _ = advance
    db.rows["VN Salary Advance Policy"] = []
    res = mod.preview_eligibility(employee="E-1", requested_amount=1000000)
    assert res["within_limit"] is True
    assert any(r["code"] == "no_policy" for r in res["reasons"])


# --------------------------------------------------------------------------- #
# B17 — add_advance_comment
# --------------------------------------------------------------------------- #
def test_add_comment_creates_row_and_publishes(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-D"))
    res = mod.add_advance_comment("SAR-D", "Đính kèm bệnh án sớm nhé")
    comments = [d for d in stub.created if d.doctype == "Comment"]
    assert len(comments) == 1
    assert comments[0].reference_doctype == DOCTYPE
    assert comments[0].reference_name == "SAR-D"
    assert res["name"]
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_add_comment_empty_throws(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-E"))
    with pytest.raises(Exception):
        mod.add_advance_comment("SAR-E", "   ")


def test_add_comment_other_employee_denied(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-F", employee="E-2"))
    with pytest.raises(Exception):
        mod.add_advance_comment("SAR-F", "hi")


# --------------------------------------------------------------------------- #
# B18 — export_advance_csv
# --------------------------------------------------------------------------- #
def test_export_csv_manager_only_and_content(advance):
    mod, stub, db, _ = advance
    _seed_list(db)
    # plain employee → denied
    with pytest.raises(Exception):
        mod.export_advance_csv()
    # manager → CSV content
    stub.roles["emp@gege.test"] = ["HR Manager"]
    res = mod.export_advance_csv(status="Paid")
    assert res["rows"] == 1
    assert "SAR-L1" in res["content"]
    assert res["content"].startswith("\ufeff")  # Excel-safe BOM
    assert "Mã yêu cầu" in res["content"]


def test_export_csv_download_binary(advance):
    mod, stub, db, _ = advance
    _seed_list(db)
    stub.roles["emp@gege.test"] = ["HR Manager"]
    res = mod.export_advance_csv(download=1)
    assert res["rows"] == 3
    assert stub.response.type == "binary"
    assert stub.response.filecontent


# --------------------------------------------------------------------------- #
# B19 — mark_paid respects a pre-set approved_amount
# --------------------------------------------------------------------------- #
def test_mark_paid_uses_sanctioned_amount(advance):
    mod, stub, db, _ = advance
    stub.roles["emp@gege.test"] = ["HR Manager"]
    doc = _register(
        stub, db,
        _mk_sar("SAR-P", workflow_state="Approved", approved_amount=1500000),
    )
    res = mod.mark_paid("SAR-P")
    assert res["status"] == "Paid"
    # approved_amount was already set by the inbox → NOT back-filled to requested
    assert doc.approved_amount == 1500000
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_mark_paid_backfills_when_unset(advance):
    mod, stub, db, _ = advance
    stub.roles["emp@gege.test"] = ["HR Manager"]
    doc = _register(stub, db, _mk_sar("SAR-P2", workflow_state="Approved"))
    mod.mark_paid("SAR-P2")
    assert doc.approved_amount == 3000000  # fallback = requested


# --------------------------------------------------------------------------- #
# B20 — regression: cancel + reverse keep their contracts
# --------------------------------------------------------------------------- #
def test_cancel_pending_sets_rejected(advance):
    mod, stub, db, _ = advance
    doc = _register(stub, db, _mk_sar("SAR-R"))
    res = mod.cancel_advance_request("SAR-R")
    assert res["status"] == "Rejected"
    assert doc.saved is True
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_cancel_approved_denied_for_employee(advance):
    mod, stub, db, _ = advance
    _register(stub, db, _mk_sar("SAR-R2", workflow_state="Approved"))
    with pytest.raises(Exception):
        mod.cancel_advance_request("SAR-R2")


def test_reverse_paid_returns_to_approved(advance):
    mod, stub, db, _ = advance
    stub.roles["emp@gege.test"] = ["HR Manager"]
    doc = _register(
        stub, db,
        _mk_sar("SAR-R3", workflow_state="Paid", payment_status="Paid"),
    )
    doc._reverse_advance_deduction = lambda force=False: False  # no deduction row
    res = mod.reverse_advance_payment("SAR-R3")
    assert res["status"] == "Approved"
    assert res["payment_status"] == "Unpaid"
    assert any(ev[0] == "advance_updated" for ev in stub.events)


def test_submit_still_creates_pending_request(advance):
    mod, stub, db, rec = advance
    res = mod.submit_advance_request(
        employee="E-1",
        posting_date="2026-09-03",
        requested_amount=2000000,
        reason="hỏng xe",
    )
    assert res["status"] == "Pending Manager"
    assert any(a == "Advance Submit" for a in rec["logs"])
    assert any(ev[0] == "advance_updated" for ev in stub.events)
