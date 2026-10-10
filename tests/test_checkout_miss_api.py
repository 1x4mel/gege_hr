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


def _match_cond(row, cond):
    """Single ``[field, op, value]`` condition (the subset Frappe supports here)."""
    field, op = cond[0], cond[1]
    val = cond[2] if len(cond) > 2 else None
    cur = row.get(field)
    if op == "=":
        return cur == val
    if op == "like":
        core = str(val or "")
        if core.startswith("%"):
            core = core[1:]
        if core.endswith("%"):
            core = core[:-1]
        core = core.replace("\\%", "%").replace("\\_", "_")
        return core.lower() in str(cur or "").lower()
    if op in (">=", "<=", ">", "<"):
        try:
            a, b = float(cur), float(val)
        except (TypeError, ValueError):
            a, b = str(cur or ""), str(val or "")
        return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
    if op == "is":
        return (cur not in (None, "")) if val == "set" else (cur in (None, ""))
    if op == "in":
        return cur in (val or [])
    return True


def _row_matches(row, filters, or_filters):
    """AND over ``filters`` (dict or list form) + OR over ``or_filters``."""

    def all_conds(f):
        if not f:
            return True
        if isinstance(f, dict):
            return all(row.get(k) == v for k, v in f.items())
        return all(_match_cond(row, c) for c in f if isinstance(c, (list, tuple)))

    if not all_conds(filters):
        return False
    if or_filters:
        return any(_match_cond(row, c) for c in or_filters if isinstance(c, (list, tuple)))
    return True


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
        employees=None,
        audit_rows=None,
        version_rows=None,
        comment_rows=None,
        file_rows=None,
        todo_rows=None,
    ):
        self._counter = iter(range(200000, 999999))
        self.tickets = {t["name"]: dict(t) for t in (tickets or [])}
        self.settings = settings or {}
        self.period = period  # dict or None (VN Payroll Review Period)
        self.has_slip = has_slip
        self.roles = list(roles)
        self.employee_for_user = employee_for_user
        self.pending_names = pending_names or []
        self.save_fail = set(save_fail)
        self.occurrence = occurrence
        # None → any Employee name passes exists() (P2 create guard tests may
        # pass an explicit allow-set).
        self.employees = employees
        self.audit_rows = [dict(r) for r in (audit_rows or [])]
        self.version_rows = [dict(r) for r in (version_rows or [])]
        # Desk-free COMPLETE (group A) — collaboration-row registries.
        self.comment_rows = [dict(r) for r in (comment_rows or [])]
        self.file_rows = [dict(r) for r in (file_rows or [])]
        self.todo_rows = [dict(r) for r in (todo_rows or [])]
        self.get_all_calls = []  # every db.get_all(...) call payload
        # capture
        self.created_docs = []  # get_doc(payload)
        self.loaded_docs = []  # get_doc(DOCTYPE, name) → doc
        self.saved_docs = []  # (doctype, name, final status fields)
        self.set_values = []  # (doctype, name, updates)
        self.count_calls = []  # (doctype, filters)
        self.deleted = []  # doctype/name deleted

    def _build_db(self):
        outer = self

        class _DB:
            def exists(inner, doctype, key=None, **_kw):
                if isinstance(key, str):
                    if doctype == "Employee":
                        return outer.employees is None or key in outer.employees
                    return key in outer.tickets
                # filter-dict form: payroll-line / ticket duplicate checks
                if isinstance(key, dict):
                    if "payroll_review_period" in key:
                        return outer.has_slip
                    if doctype == "VN Checkout Miss":
                        conds = [
                            [
                                k,
                                (v[0] if isinstance(v, list) else "="),
                                (v[1] if isinstance(v, list) else v),
                            ]
                            for k, v in key.items()
                        ]
                        return any(_row_matches(t, conds, None) for t in outer.tickets.values())
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
                        return NSDict({f: (outer.period or {}).get(f) for f in fieldname})
                    return (outer.period or {}).get(fieldname)
                if doctype == "Employee":
                    return "Test NV"
                return None

            def get_single_value(inner, doctype, field):
                return outer.settings.get(field)

            def count(inner, doctype, filters=None, **_kw):
                outer.count_calls.append((doctype, filters))
                return outer.occurrence

            def get_all(
                inner,
                doctype,
                filters=None,
                or_filters=None,
                fields=None,
                limit_page_length=None,
                limit_start=None,
                order_by=None,
                **_kw,
            ):
                outer.get_all_calls.append(
                    {
                        "doctype": doctype,
                        "filters": filters,
                        "or_filters": or_filters,
                        "fields": list(fields or []),
                        "limit_page_length": limit_page_length,
                        "limit_start": limit_start,
                        "order_by": order_by,
                    }
                )
                # Engine path (dict-form filters) + legacy callers keep the raw
                # pending_names behaviour — the S* engine tests depend on it.
                # Comment/File/ToDo (group-A collab helpers pass dict filters)
                # flow through the normal registry matching instead.
                if isinstance(filters, dict) and doctype not in ("Comment", "File", "ToDo"):
                    return list(outer.pending_names)
                if doctype == "VN Checkout Miss":
                    base = [dict(t) for t in outer.tickets.values()]
                elif doctype == "VN Audit Event":
                    base = [dict(r) for r in outer.audit_rows]
                elif doctype == "Version":
                    base = [dict(r) for r in outer.version_rows]
                elif doctype == "Comment":
                    base = [dict(r) for r in outer.comment_rows]
                elif doctype == "File":
                    base = [dict(r) for r in outer.file_rows]
                elif doctype == "ToDo":
                    base = [dict(r) for r in outer.todo_rows]
                else:
                    return list(outer.pending_names)
                rows = [t for t in base if _row_matches(t, filters, or_filters)]
                if doctype == "VN Checkout Miss":
                    rows.sort(
                        key=lambda t: (str(t.get("work_date") or ""), str(t.get("name") or "")),
                        reverse=True,
                    )
                if fields:
                    rows = [{f: t.get(f) for f in fields} for t in rows]
                limit = limit_page_length
                start = int(limit_start or 0)
                if limit not in (None, 0):
                    rows = rows[start : start + int(limit)]
                return rows

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
        "docstatus": 0,
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
        frappe_mod.whitelist = lambda *a, **kw: a[0] if a and callable(a[0]) else (lambda f: f)
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
        fake_emp.emp_name = lambda e: e  # passthrough for the list employee filter
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

        # Patch the already-imported packages so `from pkg import mod` resolves.
        import gege_hr.gege_hr.api as api_pkg
        import gege_hr.gege_hr.utils as utils_pkg

        monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
        monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)

        # Reload engine first (it must bind to the stub frappe), then the api.
        importlib.reload(importlib.import_module("gege_hr.gege_hr.utils.checkout_miss"))
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.checkout_miss"))
        # Track saves on loaded tickets.
        orig_get_doc = stub.get_doc

        def tracking_get_doc(arg, name=None, **kw):
            doc = orig_get_doc(arg, name, **kw)
            if isinstance(arg, str) and name in stub.tickets:
                orig_save = doc.save

                def save(**save_kw):
                    r = orig_save(**save_kw)
                    stub.saved_docs.append(
                        (
                            arg,
                            name,
                            {
                                k: getattr(doc, k, None)
                                for k in ("status", "penalty_waived", "penalty_amount", "note")
                            },
                        )
                    )
                    if name in stub.tickets:
                        stub.tickets[name].update({k: getattr(doc, k, None) for k in stub.tickets[name]})
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
    mod.explain_checkout_miss("CM-0001", "Ra cửa lúc 22h", correction_checkout_time="2026-08-08 22:00:00")
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
    frappe_mod.whitelist = lambda *a, **kw: a[0] if a and callable(a[0]) else (lambda f: f)
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
    mod._after_correction_state_change(
        _cr_doc(vn_checkout_miss=None), from_state="Pending HR", to_state="Approved"
    )
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


# --------------------------------------------------------------------------- #
# L* — list_checkout_misses: filters + pagination envelope (plan §5.1 B1)
# --------------------------------------------------------------------------- #
def _seed_tickets():
    return [
        _ticket("CM-0001", status="Pending", work_date="2026-08-01", shift_type="Ca sáng"),
        _ticket("CM-0002", status="Pending", work_date="2026-08-02", shift_type="Ca chiều", occurrence_no=2),
        _ticket("CM-0003", status="Explained", work_date="2026-08-10", shift_type="Ca sáng"),
        _ticket("CM-0004", status="Waived", work_date="2026-07-15", shift_type="Ca sáng"),
        _ticket(
            "CM-0005", status="Penalised", work_date="2026-08-05", employee="HR-EMP-002", occurrence_no=3
        ),
        _ticket("CM-0006", status="Closed", work_date="2026-08-06"),
    ]


def test_l1_pagination_envelope_with_summary(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.list_checkout_misses(page=1, page_size=3)
    assert set(out.keys()) == {"data", "total", "summary"}
    assert out["total"] == 6
    assert len(out["data"]) == 3
    s = out["summary"]
    assert s["total"] == 6
    assert s["pending"] == 2 and s["explained"] == 1
    assert s["waived"] == 1 and s["penalised"] == 1 and s["closed"] == 1


def test_l1b_summary_ignores_status_filter(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.list_checkout_misses(status="Pending", page=1, page_size=10)
    assert out["total"] == 2  # the list honours the status filter...
    assert out["summary"]["total"] == 6  # ...but the tiles stay full-picture
    assert out["summary"]["waived"] == 1


def test_l2_employee_filter(api):
    stub, mod = api(tickets=_seed_tickets())
    rows = mod.list_checkout_misses(employee="HR-EMP-002")
    assert [r["name"] for r in rows] == ["CM-0005"]


def test_l3_date_window_inclusive(api):
    stub, mod = api(tickets=_seed_tickets())
    rows = mod.list_checkout_misses(from_date="2026-08-01", to_date="2026-08-05")
    assert {r["name"] for r in rows} == {"CM-0001", "CM-0002", "CM-0005"}


def test_l4_shift_and_occurrence_filters(api):
    stub, mod = api(tickets=_seed_tickets())
    rows = mod.list_checkout_misses(shift_type="Ca sáng")
    assert {r["name"] for r in rows} == {"CM-0001", "CM-0003", "CM-0004"}
    rows = mod.list_checkout_misses(min_occurrence=2)
    assert {r["name"] for r in rows} == {"CM-0002", "CM-0005"}


def test_l5_pagination_limit_start(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.list_checkout_misses(page=3, page_size=2)
    assert out["total"] == 6 and len(out["data"]) == 2
    paged = [c for c in stub.get_all_calls if c["limit_page_length"] == 2]
    assert any(c["limit_start"] == 4 for c in paged)


def test_l6_legacy_call_keeps_bare_list(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.list_checkout_misses()
    assert isinstance(out, list)
    assert len(out) == 6


def test_l7_search_wildcards_escaped(api):
    stub, mod = api(tickets=_seed_tickets())
    rows = mod.list_checkout_misses(search="100%")
    assert rows == []  # an escaped % must not match everything
    miss_calls = [c for c in stub.get_all_calls if c["doctype"] == "VN Checkout Miss" and c["or_filters"]]
    assert miss_calls
    vals = [c[2] for c in miss_calls[-1]["or_filters"]]
    assert "%100\\%%" in vals


# --------------------------------------------------------------------------- #
# DTL* — get_checkout_miss: detail + payroll precheck + timeline (plan §5.1 B2)
# --------------------------------------------------------------------------- #
def _audit_row(name="CM-0001", created_at="2026-08-09 10:00:00", **kw):
    base = {
        "name": "AE-1",
        "audit_type": "Checkout Miss Resolve",
        "actor": "hr@example.com",
        "description": "waive ticket CM-0001",
        "old_value": "Pending",
        "new_value": "Waived",
        "created_at": created_at,
        "reference_doctype": "VN Checkout Miss",
        "reference_name": name,
    }
    base.update(kw)
    return base


def _version_row(docname="CM-0001", creation="2026-08-10 09:00:00", data=None, **kw):
    base = {
        "name": "VER-1",
        "ref_doctype": "VN Checkout Miss",
        "docname": docname,
        "owner": "nv@example.com",
        "creation": creation,
        "data": data or {"changed": [["status", ["Pending", "Explained"]]]},
    }
    base.update(kw)
    return base


def test_dtl1_returns_ticket_payroll_and_timeline(api):
    stub, mod = api(
        tickets=[_ticket()],
        audit_rows=[_audit_row()],
        version_rows=[_version_row()],
    )
    out = mod.get_checkout_miss("CM-0001")
    assert out["ticket"]["name"] == "CM-0001"
    assert out["payroll_state"] is None  # no period covers 2026-08-08
    assert {r["source"] for r in out["timeline"]} == {"audit", "version"}


def test_dtl1b_payroll_state_calculated_and_locked(api):
    stub, mod = api(tickets=[_ticket()], period={"name": "P1", "status": "Calculated"})
    assert mod.get_checkout_miss("CM-0001")["payroll_state"] == "calculated"
    stub, mod = api(
        tickets=[_ticket()],
        period={"name": "P1", "status": "Approved"},
        has_slip=True,
    )
    assert mod.get_checkout_miss("CM-0001")["payroll_state"] == "locked"


def test_dtl2_missing_ticket_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.get_checkout_miss("CM-NOPE")


def test_dtl3_timeline_newest_first_and_version_summary(api):
    stub, mod = api(
        tickets=[_ticket()],
        audit_rows=[_audit_row(created_at="2026-08-09 10:00:00")],
        version_rows=[_version_row(creation="2026-08-10 09:00:00")],
    )
    tl = mod.get_checkout_miss("CM-0001")["timeline"]
    assert tl[0]["source"] == "version"
    assert "status: Pending → Explained" in tl[0]["description"]
    assert tl[1]["source"] == "audit" and tl[1]["new_value"] == "Waived"


# --------------------------------------------------------------------------- #
# RP* — resolve_checkout_miss: penalty_amount override (plan §5.1 B3)
# --------------------------------------------------------------------------- #
def test_rp1_override_sets_amount(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    mod.resolve_checkout_miss("CM-0001", "penalise", penalty_amount=50000)
    assert stub.saved_docs[-1][2]["penalty_amount"] == 50000


def test_rp2_negative_and_non_numeric_throw(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "penalise", penalty_amount=-1)
    with pytest.raises(FrappeError):
        mod.resolve_checkout_miss("CM-0001", "penalise", penalty_amount="abc")
    assert stub.saved_docs == []  # nothing was mutated


def test_rp3_override_ignored_on_waive(api):
    stub, mod = api(tickets=[_ticket(status="Explained")])
    mod.resolve_checkout_miss("CM-0001", "waive", penalty_amount=999)
    saved = stub.saved_docs[-1][2]
    assert saved["penalty_waived"] == 1
    assert saved["penalty_amount"] == 100000  # untouched by the override


def test_rp4_none_keeps_default_stamp(api):
    stub, mod = api(
        tickets=[_ticket(status="Explained", penalty_amount=0)],
        settings={"vn_cm_penalty_amount": 150000},
    )
    mod.resolve_checkout_miss("CM-0001", "penalise")
    assert stub.saved_docs[-1][2]["penalty_amount"] == 150000


# --------------------------------------------------------------------------- #
# BK* — bulk_resolve_checkout_misses: partial-safe bulk (plan §5.1 B4)
# --------------------------------------------------------------------------- #
def test_bk1_bulk_waive_updates_all(api):
    stub, mod = api(tickets=_seed_tickets())
    res = mod.bulk_resolve_checkout_misses(["CM-0001", "CM-0002", "CM-0003"], "waive", note="OK")
    assert res["updated"] == ["CM-0001", "CM-0002", "CM-0003"]
    assert res["failed"] == []
    assert res["counts"] == {"waived": 3, "penalised": 0, "closed": 0}
    assert all(stub.tickets[n]["status"] == "Waived" for n in res["updated"])


def test_bk2_closed_row_fails_others_update(api):
    stub, mod = api(tickets=[_ticket("CM-0009", status="Closed"), _ticket("CM-0001")])
    res = mod.bulk_resolve_checkout_misses(["CM-0009", "CM-0001"], "waive")
    assert res["updated"] == ["CM-0001"]
    assert len(res["failed"]) == 1
    assert res["failed"][0]["name"] == "CM-0009"
    assert "đã đóng" in res["failed"][0]["error"]


def test_bk3_over_cap_throws(api):
    stub, mod = api(tickets=_seed_tickets())
    with pytest.raises(FrappeError):
        mod.bulk_resolve_checkout_misses([f"CM-{i}" for i in range(101)], "waive")


def test_bk4_invalid_action_throws(api):
    stub, mod = api(tickets=_seed_tickets())
    with pytest.raises(FrappeError):
        mod.bulk_resolve_checkout_misses(["CM-0001"], "explode")


def test_bk5_non_hr_role_throws(api):
    stub, mod = api(tickets=_seed_tickets(), roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.bulk_resolve_checkout_misses(["CM-0001"], "waive")


def test_bk6_locked_payroll_row_fails_rest_update(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", work_date="2026-08-08"),
            _ticket("CM-0010", work_date="2026-08-10"),
        ]
    )
    mod._payroll_state_for = lambda ctx: "locked" if ctx.get("work_date") == "2026-08-10" else None
    res = mod.bulk_resolve_checkout_misses(["CM-0001", "CM-0010"], "waive")
    assert res["updated"] == ["CM-0001"]
    assert res["failed"][0]["name"] == "CM-0010"
    assert stub.tickets["CM-0001"]["status"] == "Waived"


def test_bk7_bulk_penalty_override_applies_to_all(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", status="Explained", penalty_amount=0),
            _ticket("CM-0002", status="Explained", penalty_amount=0),
        ]
    )
    res = mod.bulk_resolve_checkout_misses(["CM-0001", "CM-0002"], "penalise", penalty_amount=50000)
    assert res["counts"]["penalised"] == 2
    amounts = {n: stub.tickets[n]["penalty_amount"] for n in ("CM-0001", "CM-0002")}
    assert amounts == {"CM-0001": 50000, "CM-0002": 50000}


# --------------------------------------------------------------------------- #
# EX* — export_checkout_misses_csv (plan §5.1 B5)
# --------------------------------------------------------------------------- #
def test_ex1_returns_csv_payload(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.export_checkout_misses_csv()
    assert out["rows"] == 6 and out["truncated"] is False
    assert out["filename"].startswith("quen-checkout_")
    assert out["content"].startswith("\ufeff")
    assert out["content"].splitlines()[0].startswith("\ufeffMã ticket")
    assert "CM-0001" in out["content"]


def test_ex2_respects_filters(api):
    stub, mod = api(tickets=_seed_tickets())
    out = mod.export_checkout_misses_csv(status="Pending")
    assert out["rows"] == 2
    assert "CM-0003" not in out["content"]


def test_ex3_truncation_flag(api, monkeypatch):
    stub, mod = api(tickets=_seed_tickets())
    monkeypatch.setattr(mod, "EXPORT_MAX_ROWS", 1)
    out = mod.export_checkout_misses_csv()
    assert out["rows"] == 1 and out["truncated"] is True


# --------------------------------------------------------------------------- #
# GR* — extend_checkout_miss_grace (P2 §8) — stub now = 2026-08-12 12:00
# --------------------------------------------------------------------------- #
def test_gr1_extend_updates_deadline(api):
    stub, mod = api(tickets=[_ticket(grace_deadline="2026-08-13 18:00:00")])
    out = mod.extend_checkout_miss_grace("CM-0001", "2026-08-14 09:30", reason="NV có việc đột xuất")
    assert out["grace_deadline"] == "2026-08-14 09:30:00"
    assert stub.tickets["CM-0001"]["grace_deadline"] == "2026-08-14 09:30:00"


def test_gr2_closed_ticket_throws(api):
    stub, mod = api(tickets=[_ticket(status="Closed")])
    with pytest.raises(FrappeError):
        mod.extend_checkout_miss_grace("CM-0001", "2026-08-14 09:30", reason="r")
    assert stub.saved_docs == []


def test_gr3_past_deadline_throws(api):
    stub, mod = api(tickets=[_ticket(grace_deadline="2026-08-13 18:00:00")])
    with pytest.raises(FrappeError):
        mod.extend_checkout_miss_grace("CM-0001", "2026-08-01 09:00", reason="r")
    assert stub.saved_docs == []


def test_gr4_missing_reason_throws(api):
    stub, mod = api(tickets=[_ticket(grace_deadline="2026-08-13 18:00:00")])
    with pytest.raises(FrappeError):
        mod.extend_checkout_miss_grace("CM-0001", "2026-08-14 09:30", reason="  ")
    assert stub.saved_docs == []


def test_gr5_missing_ticket_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.extend_checkout_miss_grace("CM-NOPE", "2026-08-14 09:30", reason="r")


# --------------------------------------------------------------------------- #
# MC* — create_checkout_miss (P2 §8)
# --------------------------------------------------------------------------- #
def test_mc1_create_ok_first_n_free(api):
    stub, mod = api(tickets=[])
    mod.create_checkout_miss("HR-EMP-001", "2026-08-01", shift_type="Ca sáng", note="bổ sung")
    docs = [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"]
    assert len(docs) == 1
    assert docs[0]["employee"] == "HR-EMP-001"
    assert docs[0]["status"] == "Pending"
    assert docs[0]["occurrence_no"] == 1
    assert docs[0]["penalty_amount"] == 0.0  # occurrence 1 ≤ free_first_n (2)
    assert docs[0]["grace_deadline"]


def test_mc1b_penalty_override_and_third_occurrence(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", employee="HR-EMP-001", work_date="2026-07-01"),
            _ticket("CM-0002", employee="HR-EMP-001", work_date="2026-07-02"),
        ]
    )
    # Two prior tickets → occurrence 3 > free_first_n → default penalty…
    mod.create_checkout_miss("HR-EMP-001", "2026-08-01")
    doc = [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"][-1]
    assert doc["occurrence_no"] == 3
    assert doc["penalty_amount"] == 100000.0  # stub count() default = occurrence(0)… see below
    # …unless an explicit override is given.
    mod.create_checkout_miss("HR-EMP-002", "2026-08-01", penalty_amount=50000)
    doc2 = [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"][-1]
    assert doc2["penalty_amount"] == 50000.0


def test_mc2_duplicate_day_throws(api):
    stub, mod = api(tickets=[_ticket(employee="HR-EMP-001", work_date="2026-08-08")])
    with pytest.raises(FrappeError):
        mod.create_checkout_miss("HR-EMP-001", "2026-08-08")
    assert not [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"]


def test_mc3_unknown_employee_throws(api):
    stub, mod = api(tickets=[], employees={"HR-EMP-001"})
    with pytest.raises(FrappeError):
        mod.create_checkout_miss("HR-EMP-009", "2026-08-01")


def test_mc4_bad_date_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.create_checkout_miss("HR-EMP-001", "31-08-2026")


# --------------------------------------------------------------------------- #
# DL* — delete_checkout_miss (P2 §8 — HR Manager only)
# --------------------------------------------------------------------------- #
def test_dl1_hr_manager_deletes(api):
    stub, mod = api(tickets=[_ticket()])
    out = mod.delete_checkout_miss("CM-0001", reason="engine tạo nhầm")
    assert out == {"deleted": "CM-0001"}
    assert ("VN Checkout Miss", "CM-0001") in stub.deleted


def test_dl2_reason_required(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.delete_checkout_miss("CM-0001", reason="")
    assert stub.deleted == []


def test_dl3_plain_hr_user_denied(api):
    stub, mod = api(tickets=[_ticket()], roles=("HR User",))
    with pytest.raises(FrappeError):
        mod.delete_checkout_miss("CM-0001", reason="r")
    assert stub.deleted == []


def test_dl4_locked_period_refuses(api):
    stub, mod = api(tickets=[_ticket()])
    mod._payroll_state_for = lambda ctx: "locked"
    with pytest.raises(FrappeError):
        mod.delete_checkout_miss("CM-0001", reason="r")
    assert stub.deleted == []


# --------------------------------------------------------------------------- #
# RO* — reopen_checkout_miss (P2 §8 — Closed → Pending)
# --------------------------------------------------------------------------- #
def test_ro1_reopen_closed(api):
    stub, mod = api(tickets=[_ticket(status="Closed", note="đã xử lý")])
    out = mod.reopen_checkout_miss("CM-0001", note="mở lại xử lý tiếp")
    assert out["status"] == "Pending"
    saved = stub.saved_docs[-1][2]
    assert saved["status"] == "Pending"
    assert "mở lại" in (saved["note"] or "")


def test_ro2_non_closed_throws(api):
    stub, mod = api(tickets=[_ticket(status="Pending")])
    with pytest.raises(FrappeError):
        mod.reopen_checkout_miss("CM-0001")
    assert stub.saved_docs == []


def test_ro3_missing_ticket_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.reopen_checkout_miss("CM-NOPE")


def test_ro4_locked_period_throws(api):
    stub, mod = api(tickets=[_ticket(status="Closed")])
    mod._payroll_state_for = lambda ctx: "locked"
    with pytest.raises(FrappeError):
        mod.reopen_checkout_miss("CM-0001")
    assert stub.saved_docs == []


# --------------------------------------------------------------------------- #
# RM* — remind_pending_checkout_misses (P2 §8 — Notification Log)
# --------------------------------------------------------------------------- #
def test_rm1_remind_within_window(api):
    stub, mod = api(tickets=[_ticket("CM-0001", status="Pending", grace_deadline="2026-08-12 20:00:00")])
    out = mod.remind_pending_checkout_misses()
    assert out == {"reminded": 1, "tickets": 1}
    notes = [d for d in stub.created_docs if d.get("doctype") == "Notification Log"]
    assert len(notes) == 1
    assert notes[0]["for_user"] == "Test NV"  # stub Employee get_value


def test_rm2_out_of_window_and_expired_skipped(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", status="Pending", grace_deadline="2026-08-15 12:00:00"),
            _ticket("CM-0002", status="Pending", grace_deadline="2026-08-11 12:00:00"),
            _ticket("CM-0003", status="Waived", grace_deadline="2026-08-12 18:00:00"),
        ]
    )
    out = mod.remind_pending_checkout_misses()
    assert out == {"reminded": 0, "tickets": 0}
    assert not [d for d in stub.created_docs if d.get("doctype") == "Notification Log"]


def test_rm3_names_filter(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", status="Pending", grace_deadline="2026-08-12 20:00:00"),
            _ticket("CM-0002", status="Pending", grace_deadline="2026-08-12 21:00:00"),
        ]
    )
    out = mod.remind_pending_checkout_misses(names=["CM-0001"])
    assert out["tickets"] == 1


# --------------------------------------------------------------------------- #
# MS* — my_checkout_misses(search) (P2 §8 — clears HR-BL-checkout-miss)
# --------------------------------------------------------------------------- #
def test_ms1_search_filters_own_tickets(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", shift_type="Ca sáng"),
            _ticket("CM-0002", shift_type="Ca đêm"),
        ]
    )
    out = mod.my_checkout_misses(search="đêm")
    assert [r["name"] for r in out] == ["CM-0002"]


def test_ms2_no_search_returns_all_own(api):
    stub, mod = api(tickets=[_ticket("CM-0001"), _ticket("CM-0002", shift_type="Ca đêm")])
    assert len(mod.my_checkout_misses()) == 2


# --------------------------------------------------------------------------- #
# SO* — sort whitelist (P2 §8 UX round)
# --------------------------------------------------------------------------- #
def test_so1_valid_sort_maps_to_column(api):
    stub, mod = api(tickets=_seed_tickets())
    mod.list_checkout_misses(sort="penalty asc")
    calls = [c for c in stub.get_all_calls if c["doctype"] == "VN Checkout Miss" and c["order_by"]]
    assert any(c["order_by"] == "penalty_amount asc, name asc" for c in calls)


def test_so2_invalid_sort_throws(api):
    stub, mod = api(tickets=_seed_tickets())
    with pytest.raises(FrappeError):
        mod.list_checkout_misses(sort="name; DROP TABle users")


# --------------------------------------------------------------------------- #
# ST* — checkout_miss_stats monthly buckets (P2 §8 UX round)
# --------------------------------------------------------------------------- #
def test_st1_buckets_by_month_and_penalty_sum(api):
    today = dt.date.today()
    this_m = today.strftime("%Y-%m")
    last_day_prev = today.replace(day=1) - dt.timedelta(days=1)
    last_m = last_day_prev.strftime("%Y-%m")
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", status="Pending", work_date=today.isoformat()),
            _ticket("CM-0002", status="Penalised", work_date=today.isoformat(), penalty_amount=100000),
            _ticket(
                "CM-0003",
                status="Penalised",
                work_date=today.isoformat(),
                penalty_amount=50000,
                penalty_waived=1,
            ),
            _ticket("CM-0004", status="Closed", work_date=last_day_prev.isoformat()),
        ]
    )
    out = mod.checkout_miss_stats(months=2)
    assert [m["month"] for m in out["months"]] == [last_m, this_m]
    cur = out["months"][1]
    assert cur["tickets"] == 3 and cur["pending"] == 1 and cur["penalised"] == 2
    assert cur["penalty_total"] == 100000.0  # the waived 50k is excluded
    assert out["months"][0]["closed"] == 1


def test_st2_months_clamped_1_to_24(api):
    stub, mod = api(tickets=[])
    assert len(mod.checkout_miss_stats(months=99)["months"]) == 24
    assert len(mod.checkout_miss_stats(months=0)["months"]) == 1


def test_st3_non_hr_throws(api):
    stub, mod = api(tickets=[], roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.checkout_miss_stats()
