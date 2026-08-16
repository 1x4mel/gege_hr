"""Bench-free unit tests for the checkout-miss API (api/checkout_miss.py) and
the engine fixes from plans/checkout-miss-fix-plan.md.

Covers the API matrix groups:
  A*  explain_checkout_miss  — status guard (BUG-1), ownership guard (BUG-3),
                               correction-request link (BUG-5)
  R*  resolve_checkout_miss  — state machine (BUG-7), penalty default (BUG-4),
                               payroll invalidation (BUG-2)
  S*  penalise_expired       — actual flip counting (S6)

Uses the same stub-frappe harness pattern as tests/test_checkout_miss.py
(monkeypatch.setitem(sys.modules, "frappe", stub)).
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe
# --------------------------------------------------------------------------- #
class FrappeError(Exception):
    """Raised by the stub frappe.throw."""


class NSDict(dict):
    """dict with attribute access — mimics frappe._dict for get_value(as_dict)."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(key) from e


def _throw(msg, exc=None):
    raise FrappeError(msg)


class _FakeDoc:
    """Document double: attribute bag + save()/insert()/db_set() capture."""

    def __init__(self, payload, counter, *, save_raises=False):
        self._counter = counter
        self._save_raises = save_raises
        self.saved = None
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        if not getattr(self, "name", None):
            self.name = f"NEW-{next(counter)}"
        self.doctype = getattr(self, "doctype", None) or payload.get("doctype")

    def insert(self, **_kw):
        return self

    def save(self, **_kw):
        if self._save_raises:
            raise FrappeError("save failed")
        self.saved = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return self

    def db_set(self, field, value):
        setattr(self, field, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


class StubFrappe:
    """Configurable frappe stub. Tickets live in ``tickets`` keyed by name."""

    def __init__(
        self,
        *,
        tickets=None,
        settings=None,
        period=None,
        has_slip=False,
        roles=("HR Manager",),
        employee_for_user="HR-EMP-001",
        pending_names=None,
        save_fail=(),
        occurrence=0,
    ):
        self._counter = iter(range(200000, 999999))
        self.tickets = {t["name"]: dict(t) for t in (tickets or [])}
        self.settings = settings or {}
        self.period = period          # dict or None (VN Payroll Review Period)
        self.has_slip = has_slip
        self.roles = list(roles)
        self.employee_for_user = employee_for_user
        self.pending_names = pending_names or []
        self.save_fail = set(save_fail)
        self.occurrence = occurrence
        # capture
        self.created_docs = []        # get_doc(payload)
        self.loaded_docs = []         # get_doc(DOCTYPE, name) → doc
        self.saved_docs = []          # (doctype, name, final status fields)
        self.set_values = []          # (doctype, name, updates)
        self.count_calls = []         # (doctype, filters)
        self.deleted = []             # doctype/name deleted

    def _build_db(self):
        outer = self

        class _DB:
            def exists(inner, doctype, key=None, **_kw):
                if isinstance(key, str):
                    return key in outer.tickets
                # filter-dict form: {"shift_instance": ...} / payroll-line form
                if isinstance(key, dict):
                    if "payroll_review_period" in key:
                        return outer.has_slip
                    return any(True for _ in outer.tickets)
                return False

            def get_value(inner, doctype, name=None, fieldname=None, as_dict=False, **_kw):
                # ``name`` may arrive as a filters dict/keyword (filters=...)
                # depending on the caller — the stub ignores it for lookups.
                if doctype == "VN Checkout Miss":
                    t = outer.tickets.get(name)
                    if t is None:
                        return None
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict({f: t.get(f) for f in fieldname})
                    return t.get(fieldname)
                if doctype == "VN Payroll Review Period":
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict(
                            {f: (outer.period or {}).get(f) for f in fieldname}
                        )
                    return (outer.period or {}).get(fieldname)
                if doctype == "Employee":
                    return "Test NV"
                return None

            def get_single_value(inner, doctype, field):
                return outer.settings.get(field)

            def count(inner, doctype, filters=None, **_kw):
                outer.count_calls.append((doctype, filters))
                return outer.occurrence

            def get_all(inner, doctype, filters=None, **_kw):
                return list(outer.pending_names)

            def set_value(inner, doctype, name, updates=None, *more, **_kw):
                # Accept both Frappe forms: (dt, name, {field: value}) and
                # (dt, name, field, value).
                if more:
                    updates = {updates: more[0]}
                outer.set_values.append((doctype, name, updates))
                if doctype == "VN Checkout Miss" and name in outer.tickets:
                    outer.tickets[name].update(updates)
                return None

        return _DB()

    def get_doc(self, arg, name=None, **_kw):
        if isinstance(arg, (dict,)) and "doctype" in arg:
            doc = _FakeDoc(arg, self._counter)
            self.created_docs.append(arg)
            return doc
        # get_doc(DOCTYPE, name) → load a stored ticket
        payload = dict(self.tickets.get(name) or {"doctype": arg, "name": name})
        payload.setdefault("doctype", arg)
        payload.setdefault("name", name)
        doc = _FakeDoc(payload, self._counter, save_raises=name in self.save_fail)
        self.loaded_docs.append(doc)
        return doc

    def delete_doc(self, doctype, name, **_kw):
        self.deleted.append((doctype, name))

    def log_error(self, *_a, **_kw):
        return None

    def throw(self, msg, exc=None):
        raise FrappeError(msg)


def _ticket(name="CM-0001", status="Pending", employee="HR-EMP-001", **kw):
    base = {
        "doctype": "VN Checkout Miss",
        "name": name,
        "employee": employee,
        "employee_name": "Test NV",
        "work_date": "2026-08-08",
        "status": status,
        "occurrence_no": 1,
        "penalty_amount": 100000,
        "penalty_waived": 0,
        "company": "GeGe Esport",
        "note": "",
        "auto_checkout": "CKOUT-1",
        "shift_instance": "SI-1",
    }
    base.update(kw)
    return base


@pytest.fixture()
def api(monkeypatch):
    """Build the stubbed api.checkout_miss module. Returns (stub, module)."""

    def _make(**kw):
        stub = StubFrappe(**kw)
        stub.db = stub._build_db()

        utils = types.ModuleType("frappe.utils")

        def get_datetime(v):
            if isinstance(v, dt.datetime):
                return v
            return dt.datetime.fromisoformat(str(v)[:19])

        def now_datetime():
            return dt.datetime(2026, 8, 12, 12)

        utils.get_datetime = get_datetime
        utils.now_datetime = now_datetime
        utils.now = lambda: "2026-08-12 12:00:00"

        frappe_mod = types.ModuleType("frappe")
        frappe_mod.db = stub.db
        frappe_mod.get_doc = stub.get_doc
        frappe_mod.delete_doc = stub.delete_doc
        frappe_mod.log_error = stub.log_error
        frappe_mod.throw = stub.throw
        frappe_mod._ = lambda s: s
        # @frappe.whitelist() decorator — no-op passthrough for the stub
        # (the api module applies it at import time).
        frappe_mod.whitelist = lambda *a, **kw: (
            a[0] if a and callable(a[0]) else (lambda f: f)
        )
        frappe_mod.utils = utils
        frappe_mod.FrappeError = FrappeError
        frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
        frappe_mod.ValidationError = FrappeError
        frappe_mod.PermissionError = FrappeError

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)

        # Fake the audit + employee util deps so the api module imports clean.
        fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
        fake_audit.log = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)

        fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
        fake_emp.HR_MANAGER_ROLES = {"HR Manager", "HR User", "Payroll Manager"}
        fake_emp.get_user_roles = lambda: list(stub.roles)
        fake_emp.get_employee_for_user = lambda: stub.employee_for_user
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

        # Patch the already-imported packages so `from pkg import mod` resolves.
        import gege_hr.gege_hr.api as api_pkg
        import gege_hr.gege_hr.utils as utils_pkg

        monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
        monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)

        # Reload engine first (it must bind to the stub frappe), then the api.
        importlib.reload(importlib.import_module("gege_hr.gege_hr.utils.checkout_miss"))
        mod = importlib.reload(
            importlib.import_module("gege_hr.gege_hr.api.checkout_miss")
        )
        # Track saves on loaded tickets.
        orig_get_doc = stub.get_doc

        def tracking_get_doc(arg, name=None, **kw):
            doc = orig_get_doc(arg, name, **kw)
            if isinstance(arg, str) and name in stub.tickets:
                orig_save = doc.save

                def save(**save_kw):
                    r = orig_save(**save_kw)
                    stub.saved_docs.append((arg, name, {
                        k: getattr(doc, k, None)
                        for k in ("status", "penalty_waived", "penalty_amount", "note")
                    }))
                    if name in stub.tickets:
                        stub.tickets[name].update(
                            {k: getattr(doc, k, None) for k in stub.tickets[name]}
                        )
                    return r

                doc.save = save
            return doc

        stub.get_doc = tracking_get_doc
        frappe_mod.get_doc = tracking_get_doc
        return stub, mod

    return _make


# --------------------------------------------------------------------------- #
# A* — explain_checkout_miss
# --------------------------------------------------------------------------- #
def test_a1_explain_pending_own_ticket(api):
    stub, mod = api(tickets=[_ticket()])
    out = mod.explain_checkout_miss("CM-0001", "Quên bấm ra khỏi cửa")
    sv = [s for s in stub.set_values if s[0] == "VN Checkout Miss"]
    assert sv and sv[0][2]["status"] == "Explained"
    assert sv[0][2]["explanation"] == "Quên bấm ra khỏi cửa"


def test_a2_empty_explanation_throws(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.explain_checkout_miss("CM-0001", "   ")


def test_a3_missing_ticket_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.explain_checkout_miss("CM-NOPE", "lý do")


def test_a4_other_employee_ticket_throws(api):
    stub, mod = api(tickets=[_ticket(employee="HR-EMP-OTHER")])
    with pytest.raises(FrappeError):
        mod.explain_checkout_miss("CM-0001", "lý do")


def test_a5_user_without_employee_throws(api):
    """BUG-3: emp=None must NOT slip past the ownership check."""
    stub, mod = api(tickets=[_ticket()], employee_for_user=None)
    with pytest.raises(FrappeError):
        mod.explain_checkout_miss("CM-0001", "lý do")


@pytest.mark.parametrize("status", ["Penalised", "Waived", "Closed"])
def test_a6_a7_non_pending_status_throws(api, status):
    """BUG-1: a resolved/penalised ticket can't be flipped back to Explained."""
    stub, mod = api(tickets=[_ticket(status=status)])
    with pytest.raises(FrappeError):
        mod.explain_checkout_miss("CM-0001", "chữa cháy")


def test_a8_re_explain_while_explained_allowed(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    mod.explain_checkout_miss("CM-0001", "bổ sung bằng chứng")
    sv = [s for s in stub.set_values if s[0] == "VN Checkout Miss"]
    assert sv and sv[0][2]["explanation"] == "bổ sung bằng chứng"


def test_a9_correction_request_linked(api):
    """BUG-5: the CR created from an explanation links back to the ticket."""
    stub, mod = api(tickets=[_ticket()])
    mod.explain_checkout_miss(
        "CM-0001", "Ra cửa lúc 22h", correction_checkout_time="2026-08-08 22:00:00"
    )
    crs = [d for d in stub.created_docs if d.get("doctype") == "VN Attendance Correction Request"]
    assert len(crs) == 1
    assert crs[0]["vn_checkout_miss"] == "CM-0001"
    assert crs[0]["requested_checkout_time"] == "2026-08-08 22:00:00"
    sv = [s for s in stub.set_values if s[0] == "VN Checkout Miss"]
    assert sv[0][2]["correction_request"]


# --------------------------------------------------------------------------- #
# R* — resolve_checkout_miss
# --------------------------------------------------------------------------- #
def test_r1_waive_sets_waived_flag(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    out = mod.resolve_checkout_miss("CM-0001", "waive", note="OK")
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Waived"
    assert saved["penalty_waived"] == 1
    assert out["payroll_recalc_required"] is False


def test_r2_penalise_from_pending(api):
    stub, mod = api(tickets=[_ticket(status="Pending", penalty_amount=100000)])
    mod.resolve_checkout_miss("CM-0001", "penalise")
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Penalised"
    assert saved["penalty_waived"] == 0


def test_r3_close_keeps_penalty_fields(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    mod.resolve_checkout_miss("CM-0001", "close")
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Closed"
    assert saved["penalty_waived"] == 0


def test_r4_invalid_action_throws(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "explode")


def test_r5_missing_hr_role_throws(api):
    stub, mod = api(tickets=[_ticket()], roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "waive")


def test_r6_penalise_zero_amount_stamps_configured_penalty(api):
    """BUG-4: penalising a first-N (amount=0) ticket applies vn_cm_penalty_amount."""
    stub, mod = api(
        tickets=[_ticket(status="Explained", penalty_amount=0)],
        settings={"vn_cm_penalty_amount": 150000},
    )
    mod.resolve_checkout_miss("CM-0001", "penalise")
    saved = stub.saved_docs[-1][2]
    assert saved["penalty_amount"] == 150000


def test_r6b_penalise_zero_amount_falls_back_to_engine_default(api):
    stub, mod = api(tickets=[_ticket(status="Explained", penalty_amount=0)])
    mod.resolve_checkout_miss("CM-0001", "penalise")
    saved = stub.saved_docs[-1][2]
    assert saved["penalty_amount"] == 100000.0  # DEFAULTS["penalty_amount"]


def test_r7_closed_is_terminal(api):
    """BUG-7: a Closed ticket can never be re-resolved."""
    stub, mod = api(tickets=[_ticket(status="Closed")])
    for action in ("waive", "penalise", "close"):
        with pytest.raises(FrappeError):
            mod.resolve_checkout_miss("CM-0001", action)


def test_r8_noop_same_status_throws(api):
    stub, mod = api(tickets=[_ticket(status="Waived", penalty_waived=1)])
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "waive")


def test_r_waived_can_be_repenalised(api):
    stub, mod = api(tickets=[_ticket(status="Waived", penalty_waived=1, penalty_amount=100000)])
    mod.resolve_checkout_miss("CM-0001", "penalise")
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Penalised"
    assert saved["penalty_waived"] == 0


def test_r_penalised_can_be_waived(api):
    stub, mod = api(tickets=[_ticket(status="Penalised")])
    mod.resolve_checkout_miss("CM-0001", "waive")
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Waived"


def test_r9_calculated_period_flags_recalc(api):
    """BUG-2: resolving inside a Calculated period flags payroll_recalc_required."""
    stub, mod = api(
        tickets=[_ticket(status="Explained")],
        period={"name": "PR-2026-08", "status": "Calculated"},
    )
    out = mod.resolve_checkout_miss("CM-0001", "waive")
    assert out["payroll_recalc_required"] is True


def test_r10_approved_period_with_slips_throws(api):
    """BUG-2: an Approved period with generated slips locks the ticket."""
    stub, mod = api(
        tickets=[_ticket(status="Explained")],
        period={"name": "PR-2026-08", "status": "Approved"},
        has_slip=True,
    )
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "waive")
    assert stub.saved_docs == []  # nothing was mutated


def test_r10b_approved_period_without_slips_only_flags(api):
    stub, mod = api(
        tickets=[_ticket(status="Explained")],
        period={"name": "PR-2026-08", "status": "Approved"},
        has_slip=False,
    )
    out = mod.resolve_checkout_miss("CM-0001", "waive")
    assert out["payroll_recalc_required"] is True


# --------------------------------------------------------------------------- #
# S* — engine: penalise_expired + occurrence (utils/checkout_miss.py)
# --------------------------------------------------------------------------- #
def _engine(stub_kw, monkeypatch):
    """Reload the ENGINE against a fresh stub (separate from the api fixture)."""
    stub = StubFrappe(**stub_kw)
    stub.db = stub._build_db()
    utils = types.ModuleType("frappe.utils")
    utils.get_datetime = lambda v: v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v)[:19])
    utils.now_datetime = lambda: dt.datetime(2026, 8, 12, 12)
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.db = stub.db
    frappe_mod.get_doc = stub.get_doc
    frappe_mod.log_error = stub.log_error
    # penalise_expired catches LinkValidationError on dead-link tickets
    frappe_mod.LinkValidationError = type("LinkValidationError", (FrappeError,), {})
    frappe_mod.utils = utils
    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.utils.checkout_miss"))
    return stub, mod


def test_s1_s5_penalise_expired_counts_actual_flips(monkeypatch):
    stub, mod = _engine({"pending_names": ["CM-1", "CM-2", "CM-3"]}, monkeypatch)
    n = mod.penalise_expired(now=dt.datetime(2026, 8, 12, 12))
    assert n == 3
    assert len(stub.saved_docs) == 0  # engine path: get_doc from tickets store (none)


def test_s6_failed_save_excluded_from_count(monkeypatch):
    stub, mod = _engine(
        {"pending_names": ["CM-1", "CM-2"], "save_fail": {"CM-2"}},
        monkeypatch,
    )
    # _FakeDoc.save raises for CM-2 → only CM-1 flips.
    n = mod.penalise_expired(now=dt.datetime(2026, 8, 12, 12))
    assert n == 1


def test_bug8_occurrence_filters_docstatus_and_work_date(monkeypatch):
    stub, mod = _engine({"occurrence": 2}, monkeypatch)
    occ = mod._occurrence_no("HR-EMP-001", window_days=90)
    assert occ == 3
    doctype, filters = stub.count_calls[-1]
    assert filters["docstatus"] == ["<", 2]
    assert "work_date" in filters
    assert "creation" not in filters


def test_defaults_shared_with_engine(monkeypatch):
    """BUG-6: the engine exposes its defaults for the settings API."""
    stub, mod = _engine({}, monkeypatch)
    assert mod.DEFAULTS == mod._DEFAULTS
    assert mod.DEFAULTS["free_first_n"] == 2
    assert mod.DEFAULTS["penalty_amount"] == 100000.0


# --------------------------------------------------------------------------- #
# C* — approval._after_correction_state_change (BUG-5 sync)
# --------------------------------------------------------------------------- #
def _approval(stub_kw, monkeypatch):
    """Reload api.approval against a fresh stub frappe (same recipe as _engine)."""
    stub = StubFrappe(**stub_kw)
    stub.db = stub._build_db()
    utils = types.ModuleType("frappe.utils")
    utils.get_datetime = lambda v: v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v)[:19])
    utils.now_datetime = lambda: dt.datetime(2026, 8, 12, 12)
    utils.now = lambda: "2026-08-12 12:00:00"
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.db = stub.db
    frappe_mod.get_doc = stub.get_doc
    frappe_mod.delete_doc = stub.delete_doc
    frappe_mod.log_error = stub.log_error
    frappe_mod.throw = stub.throw
    frappe_mod._ = lambda s: s
    frappe_mod.whitelist = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda f: f))
    frappe_mod.utils = utils
    frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
    fake_audit.log = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)
    fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
    fake_emp.HR_MANAGER_ROLES = {"HR Manager"}
    fake_emp.get_user_roles = lambda: ["HR Manager"]
    fake_emp.get_current_user = lambda: "hr@example.com"
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

    import gege_hr.gege_hr.api as api_pkg
    import gege_hr.gege_hr.utils as utils_pkg

    monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
    monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)

    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.approval"))
    return stub, mod


def _cr_doc(**kw):
    payload = {
        "doctype": "VN Attendance Correction Request",
        "name": "CR-001",
        "employee": "HR-EMP-001",
        "work_date": "2026-08-08",
        "vn_checkout_miss": "CM-0001",
        "requested_checkout_time": "2026-08-08 22:00:00",
    }
    payload.update(kw)
    counter = iter(range(1))
    return _FakeDoc(payload, counter)


def test_c1_cr_without_link_is_noop(monkeypatch):
    stub, mod = _approval({"tickets": [_ticket()]}, monkeypatch)
    mod._after_correction_state_change(_cr_doc(vn_checkout_miss=None), from_state="Pending HR", to_state="Approved")
    assert stub.created_docs == []
    assert stub.deleted == []


def test_c2_non_approved_transition_is_noop(monkeypatch):
    stub, mod = _approval({"tickets": [_ticket()]}, monkeypatch)
    mod._after_correction_state_change(_cr_doc(), from_state="Draft", to_state="Pending Manager")
    assert stub.created_docs == []
    assert stub.deleted == []


def test_c3_non_correction_doctype_is_noop(monkeypatch):
    stub, mod = _approval({"tickets": [_ticket()]}, monkeypatch)
    doc = _FakeDoc({"doctype": "VN Overtime Request", "name": "OT-1"}, iter(range(1)))
    mod._after_correction_state_change(doc, from_state="Pending HR", to_state="Approved")
    assert stub.created_docs == []


def test_c4_approved_cr_replaces_fake_out_and_waives(monkeypatch):
    stub, mod = _approval({"tickets": [_ticket(status="Explained")]}, monkeypatch)
    mod._after_correction_state_change(_cr_doc(), from_state="Pending HR", to_state="Approved")
    # Real OUT synthesised at the requested time, linked to the ticket.
    outs = [d for d in stub.created_docs if d.get("doctype") == "Employee Checkin"]
    assert len(outs) == 1
    assert outs[0]["log_type"] == "OUT"
    assert outs[0]["vn_checkout_miss"] == "CM-0001"
    assert outs[0]["time"] == dt.datetime(2026, 8, 8, 22, 0)
    # Fake OUT at planned_end deleted.
    assert ("Employee Checkin", "CKOUT-1") in stub.deleted
    # Work Session repointed to the real OUT.
    ws = [s for s in stub.set_values if s[0] == "VN Attendance Work Session"]
    assert ws and isinstance(ws[0][2].get("last_checkout_log"), str)
    assert ws[0][2].get("actual_checkout") == dt.datetime(2026, 8, 8, 22, 0)
    # CR stamped with the generated checkin.
    crs = [s for s in stub.set_values if s[0] == "VN Attendance Correction Request"]
    assert crs and crs[0][2]["generated_checkin"]
    # Ticket auto-waived.
    tickets = [d for d in stub.loaded_docs if d.doctype == "VN Checkout Miss"]
    assert tickets and tickets[0].status == "Waived"
    assert tickets[0].penalty_waived == 1
    assert "CR CR-001" in (tickets[0].note or "")
