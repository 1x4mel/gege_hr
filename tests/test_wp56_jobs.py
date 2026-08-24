"""WP5 + WP6 (prod-readiness-plan) — onboarding payroll profile + auto close.

  OB1  complete_payroll_profile creates SSA + stamps bank fields (idempotent)
  OB2  missing_payroll_profiles lists Active>3d employees lacking SSA/bank
  OB3  notify_missing_payroll_profile dedupes to 1 notification/day
  AC1  clean month → period created + calculated, status stays Calculated
  AC2  Pending ticket blocker → NOT calculated, blockers listed
  AC4  blocked past day 5 → red notification fired
  AC5  existing period → untouched ("exists")

Bench-free stub-frappe harness.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


_DOC_SEQ = iter(range(90000, 99999))


class _Doc:
    def __init__(self, payload, store):
        self.__dict__.update(payload)
        self._store = store

    def insert(self, **_kw):
        if not getattr(self, "name", None):
            # mimic Frappe autoname on insert
            object.__setattr__(self, "name", f"NEW-{next(_DOC_SEQ)}")
        self._store.append(self)
        return self

    def submit(self, **_kw):
        self.docstatus = 1
        return self

    def save(self, **_kw):
        return self


class Stub:
    def __init__(self):
        self.employees = {}  # name → row
        self.ssa = []  # list of SSA _Doc
        self.inserted = []  # all inserted docs
        self.notifications = []
        self.periods = []  # existing periods
        self.ws_companies = []  # distinct companies with WS last month
        self.pending_tickets = 0
        self.not_calc_ws = 0
        self.exists_subjects = set()
        self.today = dt.date(2026, 9, 3)
        self.db = self._db()

    def _db(self):
        outer = self

        class _DB:
            def get_value(inner, doctype, filters=None, fieldname=None, **_kw):
                if doctype == "Salary Structure Assignment":
                    for s in outer.ssa:
                        if s.employee == filters.get("employee") and s.docstatus == 1:
                            return getattr(s, fieldname, None) if isinstance(fieldname, str) else s.name
                    return None
                if doctype == "Employee":
                    emp = outer.employees.get(filters if isinstance(filters, str) else None)
                    return (emp or {}).get(fieldname)
                return None

            def exists(inner, doctype, filters=None, **_kw):
                if doctype == "Notification Log":
                    return filters.get("subject") in outer.exists_subjects
                return False

            def get_all(inner, doctype, filters=None, fields=None, distinct=False, **_kw):
                if doctype == "Employee":
                    return [dict(r) for r in outer.employees.values()]
                if doctype == "VN Attendance Work Session" and distinct:
                    return [{"company": c} for c in outer.ws_companies]
                if doctype == "VN Payroll Review Period":
                    return [dict(p) for p in outer.periods]
                return []

            def count(inner, doctype, filters=None, **_kw):
                if doctype == "VN Checkout Miss":
                    return outer.pending_tickets
                if doctype == "VN Attendance Work Session":
                    return outer.not_calc_ws
                return 0

            def set_value(inner, *a, **_k):
                return None

            def commit(inner):
                return None

        return _DB()

    def get_doc(self, arg, name=None, **_kw):
        if isinstance(arg, dict):
            doc = _Doc(arg, self.inserted)
            if arg.get("doctype") == "Salary Structure Assignment":
                self.ssa.append(doc)
            if arg.get("doctype") == "Notification Log":
                self.notifications.append(arg)
            return doc
        if arg == "Employee":
            outer2 = self
            row = dict(self.employees.get(name) or {})
            row.setdefault("doctype", "Employee")

            class _EmpDoc(_Doc):
                def save(self, **_kw):
                    outer2.employees[name].update(
                        {k: v for k, v in self.__dict__.items() if not k.startswith("_") and k != "doctype"}
                    )
                    return self

            return _EmpDoc(row, self.inserted)
        raise FrappeError(f"get_doc {arg}")


def _frappe_mod(stub):
    frappe_mod = types.ModuleType("frappe")
    frappe_mod._ = lambda s: s
    frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    frappe_mod.throw = lambda msg, exc=None: (_ for _ in ()).throw(FrappeError(msg))
    frappe_mod.log_error = lambda *a, **k: None
    frappe_mod.get_traceback = lambda: "tb"
    frappe_mod.db = stub.db
    frappe_mod.get_doc = stub.get_doc
    frappe_mod.new_doc = lambda dt_: stub.get_doc({"doctype": dt_})
    frappe_mod.only_for = lambda *a, **k: None
    frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
    frappe_mod.ValidationError = FrappeError

    utils = types.ModuleType("frappe.utils")
    utils.today = lambda: stub.today.isoformat()
    utils.now = lambda: "2026-09-03 07:30:00"
    utils.now_datetime = lambda: dt.datetime(2026, 9, 3, 7, 30)
    utils.getdate = lambda v=None: v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])
    frappe_mod.utils = utils

    def get_all(doctype, filters=None, pluck=None, **_kw):
        if doctype == "Has Role":
            return ["hr@example.com"]
        return []

    frappe_mod.get_all = get_all

    class _NotifDoc(_Doc):
        def __init__(self, payload, store):
            super().__init__(payload, store)
            stub.notifications.append(payload)

        def insert(self, **_kw):
            # Register the subject so the dedupe exists() check finds it
            # (mirrors a real Notification Log row landing in the DB).
            if getattr(self, "subject", None):
                stub.exists_subjects.add(self.subject)
            return self

    orig_get_doc = stub.get_doc

    def get_doc(arg, name=None, **kw):
        if isinstance(arg, dict) and arg.get("doctype") in ("Notification Log", "Notification"):
            return _NotifDoc(arg, stub.inserted)
        return orig_get_doc(arg, name, **kw)

    frappe_mod.get_doc = get_doc
    return frappe_mod, utils


@pytest.fixture()
def onboard(monkeypatch):
    stub = Stub()
    stub.employees = {
        "E1": {
            "name": "E1",
            "employee_name": "NV Một",
            "company": "GeGe Esport",
            "date_of_joining": "2026-01-05",
            "bank_ac_no": None,
            "status": "Active",
        },
        "E2": {
            "name": "E2",
            "employee_name": "NV Hai",
            "company": "GeGe Esport",
            "date_of_joining": "2026-08-30",
            "bank_ac_no": "123456789",
            "status": "Active",
        },
    }
    frappe_mod, utils = _frappe_mod(stub)
    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    # notify_missing_payroll_profile lazily imports utils.health — the cached
    # module may still hold a PREVIOUS test's frappe stub. Rebind it.
    import gege_hr.gege_hr.utils.health as health_mod

    monkeypatch.setattr(health_mod, "frappe", frappe_mod, raising=False)
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.onboarding"))
    return stub, mod


# --------------------------------------------------------------------------- #
# OB1 — complete_payroll_profile
# --------------------------------------------------------------------------- #
def test_ob1_creates_ssa_and_bank_fields(onboard):
    stub, mod = onboard
    res = mod.complete_payroll_profile(
        employee="E1",
        salary_structure="ST-Chinh",
        base=15000000,
        bank_ac_no="999988887777",
        bank_name="Vietcombank",
    )
    assert res["ok"] is True
    assert res["ssa"] is not None
    ssa = stub.ssa[0]
    assert ssa.salary_structure == "ST-Chinh"
    assert ssa.base == 15000000
    assert stub.employees["E1"]["bank_ac_no"] == "999988887777"
    assert res["complete"] is True


def test_ob1_idempotent_existing_ssa_not_duplicated(onboard):
    stub, mod = onboard
    mod.complete_payroll_profile(
        employee="E1", salary_structure="ST", base=10000000, bank_ac_no="111", bank_name="VCB"
    )
    n_ssa = len(stub.ssa)
    res = mod.complete_payroll_profile(employee="E1", bank_ac_no="222")
    assert res["ssa"] == stub.ssa[0].name
    assert len(stub.ssa) == n_ssa  # no duplicate SSA


# --------------------------------------------------------------------------- #
# OB2 — missing list
# --------------------------------------------------------------------------- #
def test_ob2_missing_lists_employee_without_ssa_bank(onboard):
    stub, mod = onboard
    # E1: no SSA + no bank; E2: has bank but no SSA
    rows = mod.missing_payroll_profiles()
    by_emp = {r["employee"]: r for r in rows}
    assert "E1" in by_emp and set(by_emp["E1"]["missing"]) == {"bank", "ssa"}
    assert "E2" in by_emp and by_emp["E2"]["missing"] == ["ssa"]


# --------------------------------------------------------------------------- #
# OB3 — daily nudge dedupe
# --------------------------------------------------------------------------- #
def test_ob3_nudge_once_per_day(onboard):
    stub, mod = onboard
    r1 = mod.notify_missing_payroll_profile()
    assert r1["notified"] == 2  # E1 + E2
    # second run same day → deduped
    r2 = mod.notify_missing_payroll_profile()
    assert r2.get("deduped") is True or r2["notified"] == 0
    assert len(stub.notifications) == 1


# --------------------------------------------------------------------------- #
# WP6 — auto_close_payroll
# --------------------------------------------------------------------------- #
@pytest.fixture()
def payroll(monkeypatch, onboard):
    stub, mod_onboard = onboard
    frappe_mod = sys.modules["frappe"]
    # utils.payroll (calc) with stub frappe
    import gege_hr.gege_hr.utils.payroll as calc_mod

    monkeypatch.setattr(calc_mod, "frappe", frappe_mod, raising=False)
    # utils.health bound to same stub
    import gege_hr.gege_hr.utils.health as health_mod

    monkeypatch.setattr(health_mod, "frappe", frappe_mod, raising=False)
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll"))
    return stub, mod, mod_onboard


def _clean_month(stub):
    stub.ws_companies = ["GeGe Esport"]
    stub.pending_tickets = 0
    stub.not_calc_ws = 0
    stub.periods = []


def test_ac1_clean_month_calculates_and_stops_at_calculated(payroll, monkeypatch):
    stub, mod, _ = payroll
    _clean_month(stub)
    captured = {}

    def fake_calculate(name=None):
        captured["name"] = name
        return {"status": "Calculated", "total_employees": 7}

    monkeypatch.setattr(mod, "calculate_payroll_review", fake_calculate)
    res = mod.auto_close_payroll(today=stub.today)

    assert res["results"][0]["action"] == "calculated"
    assert res["results"][0]["lines"] == 7
    # period inserted with Draft → calculate ran; stays Calculated (never approved)
    period_docs = [d for d in stub.inserted if getattr(d, "doctype", "") == "VN Payroll Review Period"]
    assert period_docs and getattr(period_docs[0], "vn_auto_created", 0) == 1
    assert captured["name"] == period_docs[0].name
    # HR notified the month is ready
    assert any("đã tính sẵn" in (n.get("subject") or "") for n in stub.notifications)


def test_ac2_pending_ticket_blocks(payroll, monkeypatch):
    stub, mod, _ = payroll
    _clean_month(stub)
    stub.pending_tickets = 2
    called = {"n": 0}

    def fake_calculate(name=None):
        called["n"] += 1
        return {}

    monkeypatch.setattr(mod, "calculate_payroll_review", fake_calculate)
    res = mod.auto_close_payroll(today=stub.today)

    assert res["results"][0]["action"] == "blocked"
    assert any("2 ticket" in b for b in res["results"][0]["blockers"])
    assert called["n"] == 0  # never calculated
    # day 3 (today=2026-09-03 ≤5) → silent retry: no red notification yet
    assert not any("CÒN VƯỚNG" in (n.get("subject") or "") for n in stub.notifications)


def test_ac4_blocked_past_day5_alerts_daily(payroll, monkeypatch):
    stub, mod, _ = payroll
    _clean_month(stub)
    stub.pending_tickets = 1
    stub.today = dt.date(2026, 9, 7)  # day 7 > 5
    monkeypatch.setattr(mod, "calculate_payroll_review", lambda name=None: {})
    mod.auto_close_payroll(today=stub.today)
    assert any("CÒN VƯỚNG" in (n.get("subject") or "") for n in stub.notifications)


def test_ac5_existing_period_untouched(payroll, monkeypatch):
    stub, mod, _ = payroll
    _clean_month(stub)
    stub.periods = [
        {
            "name": "PRP-MANUAL",
            "company": "GeGe Esport",
            "payroll_month": "08",
            "payroll_year": 2026,
            "status": "Calculated",
            "docstatus": 0,
        }
    ]
    monkeypatch.setattr(
        mod,
        "calculate_payroll_review",
        lambda name=None: (_ for _ in ()).throw(AssertionError("must not recalc")),
    )
    res = mod.auto_close_payroll(today=stub.today)
    assert res["results"][0]["action"] == "exists"
    assert res["results"][0]["period"] == "PRP-MANUAL"


def test_previous_month_math(payroll):
    from datetime import date as d

    _, mod, _ = payroll
    y, m, f, t = mod._previous_month(d(2026, 9, 3))
    assert (y, m) == (2026, 8)
    assert (f, t) == (d(2026, 8, 1), d(2026, 8, 31))
    # January edge → December previous year
    y2, m2, f2, t2 = mod._previous_month(d(2026, 1, 15))
    assert (y2, m2) == (2025, 12)
    assert (f2, t2) == (d(2025, 12, 1), d(2025, 12, 31))
