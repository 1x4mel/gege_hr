"""Bench-free unit tests for ``api/expense.py`` (NEW-1, hr-gap-audit 🟥 Expense).

Pure helpers (``normalize_expenses`` / ``claim_total``) need no frappe. The
request I/O is exercised with a stub-frappe (same ``setitem(sys.modules)``
pattern), covering: own-claim filter, submit builds the doc + expenses, approve
sets the status, and the manager-only gate on ``all_expense_claims``.
"""

import importlib
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
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.expenses = []
        self.name = None
        self.status = "Draft"
        self.approval_status = "Draft"

    def append(self, field, row):
        getattr(self, field)
        lst = getattr(self, field, None)
        if not isinstance(lst, list):
            lst = []
            setattr(self, field, lst)
        lst.append(_AttrDict(row) if isinstance(row, dict) else row)

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def submit(self):
        self.status = "Submitted"
        return self

    def save(self, *a, **k):
        return self


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.employee_company = "Gege"
        self.roles = {"HR Manager"}

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
        return types.SimpleNamespace(user="hr@gege.local")

    def get_roles(self, user):
        return set(self.roles)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, filters_or_name, field=None, as_dict=False):
            if doctype == "Employee":
                if isinstance(filters_or_name, dict):  # by user_id
                    return self.fr.employee_for_user
                # by name → company (or the requested field)
                if field == "company":
                    return self.fr.employee_company
                return self.fr.employee_company if field is None else self.fr.employee_company
            return None

    @property
    def db(self):
        return self._DB(self)

    @property
    def utils(self):
        return types.SimpleNamespace(today=lambda: "2026-08-08")

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def get_all(self, doctype, filters=None, or_filters=None, fields=None, order_by=None, limit_start=0, limit_page_length=0, pluck=None, **k):
        rows = list(self.list_rows.get(doctype, []))

        def _match(r, cond):
            # Minimal Frappe filter evaluator for the operators the expense API
            # uses: "=" (exact), "like" (substring), ">=" / "<=" / ">" / "<"
            # (numeric, falling back to lexicographic for dates/strings).
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
            if filters:
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


# ── pure ────────────────────────────────────────────────────────────────────
def test_normalize_expenses_drops_zero_and_maps(mod):
    m, _ = mod
    rows = m.normalize_expenses(
        [
            {"expense_type": "Taxi", "amount": 50, "description": "khách"},
            {"expense_type": "Ăn", "amount": 0},  # dropped
            {"expense_type": "Vé", "amount": 12.5},
        ]
    )
    assert len(rows) == 2
    assert rows[0]["expense_type"] == "Taxi"
    assert rows[0]["sanction_amount"] == 50


def test_claim_total(mod):
    m, _ = mod
    assert m.claim_total([{"expense_type": "A", "amount": 100}, {"expense_type": "B", "amount": 25.5}]) == 125.5
    assert m.claim_total([]) == 0.0


# ── I/O ─────────────────────────────────────────────────────────────────────
def test_my_expense_claims_filters_own(mod):
    m, stub = mod
    stub.list_rows["Expense Claim"] = [
        {"name": "EC-1", "employee": "HR-EMP-1", "employee_name": "An", "approval_status": "Approved"},
        {"name": "EC-2", "employee": "HR-EMP-2", "employee_name": "Binh", "approval_status": "Draft"},
    ]
    res = m.my_expense_claims()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-1"


def test_submit_creates_doc_with_expenses(mod):
    m, stub = mod
    res = m.submit_expense_claim(
        employee="HR-EMP-1",
        expenses=[{"expense_type": "Taxi", "amount": 80}, {"expense_type": "Vé", "amount": 20}],
        posting_date="2026-08-08",
        remark="đi khách hàng",
    )
    doc = stub.store[("Expense Claim", res["name"])]
    assert res["total"] == 100.0
    assert len(doc.expenses) == 2
    assert doc.expenses[0]["expense_type"] == "Taxi"


def test_submit_requires_expenses(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_expense_claim(employee="HR-EMP-1", expenses=[])


def test_approve_sets_status(mod):
    m, stub = mod
    # seed a submitted claim
    doc = m.submit_expense_claim(employee="HR-EMP-1", expenses=[{"expense_type": "X", "amount": 10}])
    res = m.approve_expense_claim(doc["name"])
    assert res["approval_status"] == "Approved"
    assert stub.store[("Expense Claim", doc["name"])].approval_status == "Approved"


def test_all_claims_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_expense_claims()


# ── DNA Law #2/#3 — server-side popover filters + numeric broad search ───────
def _seed_claims(stub):
    stub.list_rows["Expense Claim"] = [
        {
            "name": "EC-1",
            "employee": "HR-EMP-1",
            "employee_name": "An Nguyen",
            "approval_status": "Approved",
            "total_claimed_amount": 100,
            "posting_date": "2026-08-01",
            "remark": "",
        },
        {
            "name": "EC-2",
            "employee": "HR-EMP-2",
            "employee_name": "Binh Tran",
            "approval_status": "Draft",
            "total_claimed_amount": 250,
            "posting_date": "2026-08-05",
            "remark": "taxi",
        },
        {
            "name": "EC-3",
            "employee": "HR-EMP-3",
            "employee_name": "An Pham",
            "approval_status": "Approved",
            "total_claimed_amount": 500,
            "posting_date": "2026-07-20",
            "remark": "",
        },
    ]


def test_all_claims_employee_name_and_status_filters(mod):
    m, stub = mod
    _seed_claims(stub)
    # Nhân viên LIKE "An" → EC-1 + EC-3
    res = m.all_expense_claims(employee_name="An")
    assert res["total"] == 2
    # + status Approved → EC-1 + EC-3 (both Approved)
    res = m.all_expense_claims(employee_name="An", status="Approved")
    assert res["total"] == 2
    # status Draft → EC-2 only
    res = m.all_expense_claims(status="Draft")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-2"


def test_all_claims_amount_and_date_range_filters(mod):
    m, stub = mod
    _seed_claims(stub)
    # Amount range [200, 400] → EC-2 (250) only (DNA §6.6 B — two >=/<= bounds)
    res = m.all_expense_claims(amount_min=200, amount_max=400)
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-2"
    # amount >= 300 → EC-3 (500)
    res = m.all_expense_claims(amount_min=300)
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-3"
    # date range within August 2026 → EC-1 + EC-2
    res = m.all_expense_claims(date_from="2026-08-01", date_to="2026-08-31")
    assert res["total"] == 2


def test_all_claims_broad_search_covers_amount(mod):
    # Law #3 / DNA §6.6 A — broad search must match the numeric amount column.
    m, stub = mod
    _seed_claims(stub)
    # "500" → matches EC-3 via total_claimed_amount (not name/employee/remark)
    res = m.all_expense_claims(search="500")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-3"
    # "taxi" → matches EC-2 via remark
    res = m.all_expense_claims(search="taxi")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-2"
    # "25" → matches EC-2 via amount 250
    res = m.all_expense_claims(search="25")
    assert res["total"] == 1
    assert res["data"][0]["name"] == "EC-2"
