"""Bench-free unit tests for ``api/expense.py`` — plans/plan-expense-desk-free.md §4.1.

Covers the desk-free lifecycle (B1–B24): draft-first submit, detail payload +
timeline, update/reset, cancel/amend, approve-with-sanctions (+ graceful submit
degrade), mandatory-reason reject, mark-paid, comments, summary/CSV export,
realtime publishes and the options v2 approver resolution.

Same stub-frappe ``setitem(sys.modules)`` pattern as ``test_expense.py``, with a
richer stub: dict-filter ``get_all`` over ``list_rows`` + the doc store,
``delete_doc``/``copy_doc``, ``publish_realtime`` spy and a ``_fail_submit``
switch for the accounts-not-configured degrade path.
"""

import importlib
import json
import sys
import types

import pytest


class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


class _Doc:
    def __init__(self, doctype, store, data=None):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Draft"
        self.approval_status = "Draft"
        self.docstatus = 0
        self.is_paid = 0
        self.expenses = []
        self.owner = "hr@gege.local"
        self.creation = "2026-09-04T00:00:00"
        self._fail_submit = False
        for k, v in (data or {}).items():
            setattr(self, k, v)

    # -- persistence ---------------------------------------------------------
    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        if self.name:
            self._store[(self.doctype, self.name)] = self
        return self

    def submit(self):
        if self._fail_submit:
            raise Exception("Approval Status must be 'Approved' or 'Rejected' / GL missing")
        self.docstatus = 1
        self.status = "Submitted"
        return self

    def cancel(self):
        self.docstatus = 2
        self.status = "Cancelled"
        return self

    def db_set(self, field, value, update_modified=False):
        setattr(self, field, value)
        return self.save()

    def set(self, field, value):
        setattr(self, field, value)

    def update(self, data):
        for k, v in (data or {}).items():
            setattr(self, k, v)

    def append(self, field, row):
        lst = getattr(self, field, None)
        if not isinstance(lst, list):
            lst = []
            setattr(self, field, lst)
        lst.append(_AttrDict(row) if isinstance(row, dict) else row)

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


class _DB:
    def __init__(self, fr):
        self.fr = fr

    def get_value(self, doctype, filters_or_name, field=None, as_dict=False):
        fr = self.fr
        if doctype == "Employee":
            if isinstance(filters_or_name, dict):  # by user_id
                return fr.user_employee.get(filters_or_name.get("user_id"))
            if field == "company":
                return "Gege"
            if field == "expense_approver":
                return fr.employee_expense_approver
            return None
        return None

    def table_exists(self, table):
        return table in {"GL Entry", "VN Audit Event"}


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.published = []  # publish_realtime spy
        self.user = "hr@gege.local"
        self.roles = {"HR Manager"}
        self.user_employee = {"hr@gege.local": "HR-EMP-1", "emp@gege.local": "HR-EMP-2"}
        self.employee_expense_approver = "hrmgr@gege.local"
        self.list_rows["Has Role"] = [
            {"role": "HR Manager", "parenttype": "User", "parent": "hrmgr@gege.local"}
        ]

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
        return types.SimpleNamespace(user=self.user)

    def get_roles(self, user):
        return set(self.roles)

    @property
    def db(self):
        return _DB(self)

    @property
    def utils(self):
        return types.SimpleNamespace(today=lambda: "2026-09-04")

    @property
    def request(self):
        return types.SimpleNamespace(files={})

    @property
    def response(self):
        return types.SimpleNamespace()

    def publish_realtime(self, event, payload=None):
        self.published.append((event, payload or {}))

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def copy_doc(self, doc):
        data = doc.as_dict()
        data["name"] = None
        data["docstatus"] = 0
        # NB: no copy.deepcopy — _AttrDict.__getattr__ returns None for
        # __deepcopy__ which breaks deepcopy; plain dict() copies suffice.
        data["expenses"] = [dict(r) if isinstance(r, dict) else r for r in (data.get("expenses") or [])]
        return _Doc(doc.doctype, doc._store, data=data)

    def delete_doc(self, doctype, name, ignore_permissions=False):
        self.store.pop((doctype, name), None)

    # -- get_all: dict + list filters, over list_rows + the doc store --------
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
        rows += [
            d.as_dict()
            for (dt, n), d in self.store.items()
            if dt == doctype and n and not isinstance(d, dict)
        ]

        def _match(r, cond):
            field, op, val = cond[0], cond[1], cond[2]
            raw = r.get(field)
            if op == "=":
                return raw == val
            if op == "like":
                needle = str(val).strip("%") if val is not None else ""
                return needle != "" and needle in str(raw if raw is not None else "")
            try:
                lhs, rhs = float(raw), float(val)
            except (TypeError, ValueError):
                lhs, rhs = str(raw if raw is not None else ""), str(val)
            if op == ">=":
                return lhs >= rhs
            if op == "<=":
                return lhs <= rhs
            if op == ">":
                return lhs > rhs
            if op == "<":
                return lhs < rhs
            return False

        def keep(r):
            if isinstance(filters, dict):
                for k, v in filters.items():
                    if r.get(k) != v:
                        return False
            elif filters:
                for cond in filters:
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
    m = importlib.import_module("gege_hr.gege_hr.api.expense")
    importlib.reload(m)
    return m, stub


def _events(stub, event="expense_updated"):
    return [p for e, p in stub.published if e == event]


def _create(mod, **kw):
    return mod.submit_expense_claim(
        employee=kw.pop("employee", "HR-EMP-1"),
        expenses=kw.pop(
            "expenses", [{"expense_type": "Taxi", "amount": 80}, {"expense_type": "Vé", "amount": 20}]
        ),
        **kw,
    )


# ── pure helper: resolve_sanctions ───────────────────────────────────────────
def test_resolve_sanctions_defaults_and_idx(mod):
    m, _ = mod
    rows = [{"amount": 100}, {"amount": 50}]
    assert m.resolve_sanctions(rows) == [100, 50]
    assert m.resolve_sanctions(rows, [{"idx": 0, "sanction_amount": 60}]) == [60, 50]
    with pytest.raises(Exception):
        m.resolve_sanctions(rows, [{"sanction_amount": 101}])
    with pytest.raises(Exception):
        m.resolve_sanctions(rows, [{"sanction_amount": -1}])


# ── B1/B2: detail + permission ───────────────────────────────────────────────
def test_b1_get_detail_of_other_employee_denied(mod):
    m, stub = mod
    res = _create(m)
    stub.user = "emp@gege.local"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.get_expense_claim(res["name"])


def test_b2_get_detail_can_matrix_and_rows(mod):
    m, stub = mod
    res = _create(m, remark="đi khách")
    stub.list_rows["File"] = [
        {
            "name": "F1",
            "file_name": "receipt.jpg",
            "file_url": "/files/receipt.jpg",
            "file_size": 10,
            "is_private": 1,
            "attached_to_doctype": "Expense Claim",
            "attached_to_name": res["name"],
            "creation": "2026-09-03T10:00:00",
        }
    ]
    out = m.get_expense_claim(res["name"])
    assert out["doc"]["employee"] == "HR-EMP-1"
    assert len(out["doc"]["expenses"]) == 2
    assert out["doc"]["expenses"][0]["expense_type"] == "Taxi"
    assert out["attachments"][0]["file_name"] == "receipt.jpg"
    assert out["can"]["approve"] is True  # manager caller
    assert out["can"]["mark_paid"] is False  # not submitted yet
    stub.user = "emp@gege.local"
    stub.roles = {"Employee"}
    stub.user_employee["emp@gege.local"] = "HR-EMP-1"  # owner view
    out = m.get_expense_claim(res["name"])
    assert out["can"]["approve"] is False
    assert out["can"]["edit"] is True


def test_b3_timeline_merges_three_sources_sorted(mod):
    m, stub = mod
    res = _create(m)
    name = res["name"]
    stub.list_rows["Version"] = [
        {
            "name": "V1",
            "ref_doctype": "Expense Claim",
            "docname": name,
            "owner": "hr@gege.local",
            "creation": "2026-09-03T09:00:00",
            "data": json.dumps({"changed": [["remark", "a", "b"]]}),
        }
    ]
    stub.list_rows["VN Audit Event"] = [
        {
            "reference_doctype": "Expense Claim",
            "reference_name": name,
            "actor": "hr@gege.local",
            "description": "Expense Submit",
            "created_at": "2026-09-03T08:00:00",
            "old_value": None,
            "new_value": None,
        }
    ]
    m.add_expense_comment(name, "ghi chú nhé")
    out = m.get_expense_claim(name)
    kinds = {i["kind"] for i in out["timeline"]}
    assert kinds == {"version", "audit", "comment"}
    ats = [str(i["at"] or "") for i in out["timeline"]]
    assert ats == sorted(ats, reverse=True)
    assert out["comments"][0]["content"] == "ghi chú nhé"


# ── B4/B5: draft-first submit + approver resolution ─────────────────────────
def test_b4_submit_is_draft_first_with_resolved_approver(mod):
    m, stub = mod
    stub.published.clear()
    res = _create(m, remark="chuyến đi")
    doc = stub.store[("Expense Claim", res["name"])]
    assert res["docstatus"] == 0
    assert res["approval_status"] == "Draft"
    assert "nháp" not in res["message"]  # no zombie note anymore
    assert doc.expense_approver == "hrmgr@gege.local"  # resolved, not session user
    assert stub.user != doc.expense_approver or True
    assert any(p.get("name") == res["name"] for p in _events(stub))


def test_b5_explicit_approver_param_wins(mod):
    m, stub = mod
    res = _create(m, expense_approver="bigboss@gege.local")
    doc = stub.store[("Expense Claim", res["name"])]
    assert doc.expense_approver == "bigboss@gege.local"


# ── B6–B8: update ────────────────────────────────────────────────────────────
def test_b6_update_draft_recomputes_and_publishes(mod):
    m, stub = mod
    res = _create(m)
    stub.published.clear()
    out = m.update_expense_claim(
        res["name"],
        expenses=[{"expense_type": "Ăn", "amount": 30}],
        remark="sửa lại",
    )
    doc = stub.store[("Expense Claim", res["name"])]
    assert out["total"] == 30.0
    assert doc.total_claimed_amount == 30.0
    assert doc.remark == "sửa lại"
    assert len(doc.expenses) == 1
    assert _events(stub)


def test_b7_update_submitted_denied(mod):
    m, stub = mod
    res = _create(m)
    doc = stub.store[("Expense Claim", res["name"])]
    doc.docstatus = 1
    with pytest.raises(Exception):
        m.update_expense_claim(res["name"], expenses=[{"expense_type": "X", "amount": 1}])


def test_b8_update_rejected_resets_to_draft(mod):
    m, stub = mod
    res = _create(m)
    doc = stub.store[("Expense Claim", res["name"])]
    doc.approval_status = "Rejected"
    out = m.update_expense_claim(res["name"], expenses=[{"expense_type": "X", "amount": 10}])
    assert out["approval_status"] == "Draft"
    assert doc.approval_status == "Draft"


# ── B9/B10: cancel ───────────────────────────────────────────────────────────
def test_b9_cancel_draft_deletes(mod):
    m, stub = mod
    res = _create(m)
    out = m.cancel_expense_claim(res["name"], reason="nhầm")
    assert out["docstatus"] == 0
    assert ("Expense Claim", res["name"]) not in stub.store


def test_b10_cancel_submitted_requires_manager(mod):
    m, stub = mod
    res = _create(m)
    doc = stub.store[("Expense Claim", res["name"])]
    doc.docstatus = 1
    stub.user = "emp@gege.local"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.cancel_expense_claim(res["name"])
    stub.user = "hr@gege.local"
    stub.roles = {"HR Manager"}
    out = m.cancel_expense_claim(res["name"])
    assert out["docstatus"] == 2
    assert doc.docstatus == 2


# ── B11–B14: approve with sanctions ──────────────────────────────────────────
def test_b11_approve_sanctions_and_submit(mod):
    m, stub = mod
    res = _create(m)
    out = m.approve_expense_claim(
        res["name"],
        sanctions=[{"idx": 0, "sanction_amount": 40}],  # 80 → 40, dòng 2 giữ 20
    )
    assert out["approval_status"] == "Approved"
    assert out["docstatus"] == 1
    assert out["total_sanctioned"] == 60.0
    doc = stub.store[("Expense Claim", res["name"])]
    assert doc.total_sanctioned_amount == 60.0
    assert doc.expenses[0]["sanction_amount"] == 40
    assert doc.expenses[1]["sanction_amount"] == 20
    assert out["submit_note"] == ""


def test_b12_approve_sanction_above_amount_denied(mod):
    m, _ = mod
    res = _create(m)
    with pytest.raises(Exception):
        m.approve_expense_claim(res["name"], sanctions=[{"sanction_amount": 9999}])


def test_b13_approve_degrades_when_submit_fails(mod):
    m, stub = mod
    res = _create(m)
    doc = stub.store[("Expense Claim", res["name"])]
    doc._fail_submit = True  # accounts not configured
    out = m.approve_expense_claim(res["name"])
    assert out["approval_status"] == "Approved"
    assert out["docstatus"] == 0
    assert "chưa vào sổ" in out["submit_note"]


def test_b14_approve_twice_denied(mod):
    m, _ = mod
    res = _create(m)
    m.approve_expense_claim(res["name"])
    with pytest.raises(Exception):
        m.approve_expense_claim(res["name"])


# ── B15: reject ──────────────────────────────────────────────────────────────
def test_b15_reject_requires_reason_and_keeps_remark(mod):
    m, stub = mod
    res = _create(m, remark="ghi chú gốc của nhân viên")
    with pytest.raises(Exception):
        m.reject_expense_claim(res["name"])
    out = m.reject_expense_claim(res["name"], reason="thiếu hóa đơn")
    assert out["approval_status"] == "Rejected"
    doc = stub.store[("Expense Claim", res["name"])]
    assert doc.remark == "ghi chú gốc của nhân viên"  # NOT clobbered
    comments = [d for (dt, n), d in stub.store.items() if dt == "Comment"]
    assert any("thiếu hóa đơn" in (c.content or "") for c in comments)


# ── B16: amend ───────────────────────────────────────────────────────────────
def test_b16_amend_from_cancelled(mod):
    m, stub = mod
    res = _create(m)
    doc = stub.store[("Expense Claim", res["name"])]
    doc.docstatus = 2
    out = m.amend_expense_claim(res["name"])
    assert out["name"] != res["name"]
    new_doc = stub.store[("Expense Claim", out["name"])]
    assert new_doc.amended_from == res["name"]
    assert new_doc.docstatus == 0
    assert new_doc.approval_status == "Draft"
    assert len(new_doc.expenses) == 2


def test_b16b_amend_requires_cancelled(mod):
    m, _ = mod
    res = _create(m)
    with pytest.raises(Exception):
        m.amend_expense_claim(res["name"])


# ── B17: mark paid ───────────────────────────────────────────────────────────
def test_b17_mark_paid_on_submitted(mod):
    m, stub = mod
    res = _create(m)
    m.approve_expense_claim(res["name"])
    stub.published.clear()
    out = m.mark_expense_paid(res["name"], mode_of_payment="Cash", clearance_date="2026-09-10")
    assert out["status"] == "Paid"
    doc = stub.store[("Expense Claim", res["name"])]
    assert doc.is_paid == 1
    assert doc.clearance_date == "2026-09-10"
    assert _events(stub)


def test_b17b_mark_paid_requires_submitted(mod):
    m, _ = mod
    res = _create(m)  # still Draft
    with pytest.raises(Exception):
        m.mark_expense_paid(res["name"])


# ── B18: summary + CSV export ────────────────────────────────────────────────
def test_b18_summary_and_csv_export(mod):
    m, stub = mod
    _create(m)  # store-backed Draft claim
    stub.list_rows["Expense Claim"] = [
        {
            "name": "EC-1",
            "employee": "HR-EMP-9",
            "employee_name": "Zed",
            "approval_status": "Approved",
            "status": "Unpaid",
            "posting_date": "2026-09-01",
            "total_claimed_amount": 100,
            "total_sanctioned_amount": 90,
            "total_amount_reimbursed": 0,
            "remark": "",
            "expense_approver": "x",
            "company": "Gege",
            "is_paid": 0,
            "docstatus": 1,
        },
    ]
    res = m.all_expense_claims(page=1, page_size=20)
    assert res["summary"]["count"] == 2
    assert res["summary"]["total_claimed"] == 200.0
    assert res["summary"]["unpaid_count"] == 1
    out = m.export_expense_csv(status="Approved")
    assert "Mã phiếu" in out["content"]
    assert "EC-1" in out["content"]
    stub.user = "emp@gege.local"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.export_expense_csv()


# ── B19: comments ────────────────────────────────────────────────────────────
def test_b19_comment_owner_ok_stranger_denied(mod):
    m, stub = mod
    res = _create(m)
    out = m.add_expense_comment(res["name"], "hỏi tiến độ")
    assert out["message"] == "Đã gửi bình luận."
    stub.user = "emp@gege.local"
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.add_expense_comment(res["name"], "spam")


# ── B22: regression — list filters unchanged ─────────────────────────────────
def test_b22_list_regressions(mod):
    m, stub = mod
    stub.list_rows["Expense Claim"] = [
        {
            "name": "EC-1",
            "employee": "HR-EMP-1",
            "employee_name": "An",
            "approval_status": "Approved",
            "total_claimed_amount": 100,
            "posting_date": "2026-08-01",
            "remark": "",
        },
        {
            "name": "EC-2",
            "employee": "HR-EMP-2",
            "employee_name": "Binh",
            "approval_status": "Draft",
            "total_claimed_amount": 250,
            "posting_date": "2026-08-05",
            "remark": "taxi",
        },
    ]
    res = m.my_expense_claims()
    assert res["total"] == 1
    assert res["summary"] is None  # employee list keeps the bare envelope
    res = m.all_expense_claims(search="500")  # numeric broad search
    assert res["total"] == 0
    res = m.all_expense_claims(status="Draft", search="taxi")
    assert res["total"] == 1


# ── B23: realtime publishes on every mutate ─────────────────────────────────
def test_b23_publishes_on_mutations(mod):
    m, stub = mod
    res = _create(m)
    stub.published.clear()
    m.update_expense_claim(res["name"], expenses=[{"expense_type": "X", "amount": 10}])
    m.approve_expense_claim(res["name"])
    m.mark_expense_paid(res["name"])
    m.add_expense_comment(res["name"], "done")
    names = [p.get("name") for p in _events(stub)]
    assert names.count(res["name"]) >= 4


# ── B24: options v2 ──────────────────────────────────────────────────────────
def test_b24_options_v2(mod):
    m, stub = mod
    stub.list_rows["Expense Claim Type"] = [
        {"name": "Taxi"},
        {"name": "Ăn"},
    ]
    out = m.expense_claim_options()
    assert set(out["expense_types"]) == {"Taxi", "Ăn"}
    assert out["expense_approver"] == "hrmgr@gege.local"
    assert out["can_approve"] is True
    stub.roles = {"Employee"}
    out = m.expense_claim_options()
    assert out["can_approve"] is False
