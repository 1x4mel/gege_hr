"""Advance API — bench-free stub tests (api/advance.py).

2026-08 rule under test: the salary advance supports exactly ONE repayment
method ("Next Month" = deduct from the payroll period containing the request,
disbursed the next month), plus the Mark Paid / Reverse guards:

  TC-A1  submit_advance_request ignores a client-sent ``Installment`` plan
  TC-A2  submit_advance_request ignores garbage/blank plans
  TC-D4  mark_paid as a plain Employee   → PermissionError
  TC-D5  mark_paid on a Draft request    → ValidationError
  TC-D2  mark_paid twice                 → idempotent response, the API layer
         never itself creates a second Additional Salary (materialisation is
         the DocType on_update hook's job, link-guarded)
  TC-D3  mark_paid race loser (claim fails, state not Paid) → throw
  TC-H5  reverse_advance_payment as Employee → PermissionError
  TC-H6  reverse_advance_payment on a non-Paid request → ValidationError

The hook-driven Additional Salary creation / cancellation (TC-D1 / TC-H1) is
bench-run territory; its pure halves (payload shape, amount fallback,
should-reverse, reset-after-reversal) are pinned in tests/test_advance.py.

Harness: same stub-injection pattern as tests/test_payroll_slip_generate.py.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    """Stand-in for both frappe.ValidationError and frappe.PermissionError."""


class FakeNewDoc:
    """frappe.new_doc() double — records the update() payload + insert()."""

    def __init__(self, stub):
        self._stub = stub
        self.fields: dict = {}
        self.inserted = False
        self.name = "SAR-260820-000001"
        self.workflow_state = "Draft"
        self.eligible_amount = 5_000_000

    def update(self, payload):
        self.fields.update(payload or {})
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        return self

    def insert(self, **_kw):
        self.inserted = True
        return self

    def as_dict(self):
        return dict(self.fields)


class FakeDoc:
    """frappe.get_doc(DOCTYPE, name) double — a saved SAR row."""

    def __init__(self, stub, payload):
        self._stub = stub
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        self.saved = False
        self.cancelled = False

    def save(self, **_kw):
        self.saved = True
        return self

    def reload(self):
        return self

    def cancel(self, **_kw):
        self.cancelled = True
        self.docstatus = 2
        return self

    def _reverse_advance_deduction(self):
        # The bench controller cancels the linked Additional Salary here; the
        # pure decision tree is covered in tests/test_advance.py.
        return False


class StubFrappe:
    """Configurable frappe stub for the advance API surface."""

    def __init__(self, *, roles, doc=None, claim_wins=True):
        self.roles = list(roles)
        self.doc_payload = dict(doc or {})
        self.claim_wins = claim_wins
        self.new_docs: list[FakeNewDoc] = []
        self.saved_values: list[tuple] = []
        self.employee_for_user = "HR-EMP-001"
        outer = self

        frappe_mod = types.ModuleType("frappe")
        frappe_mod._ = lambda s: s
        frappe_mod.throw = lambda msg, exc=None: (_ for _ in ()).throw(FrappeError(msg))
        frappe_mod.ValidationError = FrappeError
        frappe_mod.PermissionError = FrappeError
        frappe_mod.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))

        utils_mod = types.ModuleType("frappe.utils")
        utils_mod.getdate = lambda v=None: v
        frappe_mod.utils = utils_mod

        class _DB:
            def get_value(inner, doctype, name=None, *args, **kwargs):
                if doctype == "VN Salary Advance Request":
                    # value asked after mark_paid (linked_additional_salary)
                    return outer.doc_payload.get("linked_additional_salary") or ""
                return None

            def table_exists(inner, _name):
                return True

        frappe_mod.db = _DB()
        frappe_mod.get_doc = self._get_doc
        frappe_mod.new_doc = self._new_doc
        self.mod = frappe_mod

    def _new_doc(self, doctype):
        doc = FakeNewDoc(self)
        self.new_docs.append(doc)
        return doc

    def _get_doc(self, doctype, name=None, **_kw):
        if doctype == "VN Salary Advance Request":
            return FakeDoc(self, self.doc_payload)
        raise FrappeError(f"unexpected get_doc {doctype} {name}")


@pytest.fixture()
def api(monkeypatch):
    def _make(**kw):
        stub = StubFrappe(**kw)

        fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
        fake_audit.log = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)

        fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
        fake_emp.HR_MANAGER_ROLES = {"HR Manager", "HR User", "Payroll Manager", "System Manager"}
        fake_emp.get_user_roles = lambda: list(stub.roles)
        fake_emp.get_employee_for_user = lambda: stub.employee_for_user
        fake_emp.emp_name = lambda v: v
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

        fake_pagination = types.ModuleType("gege_hr.gege_hr.utils.pagination")
        fake_pagination.paginate_filtered = lambda rows, **k: rows
        fake_pagination.MAX_PAGE_SIZE = 500
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.pagination", fake_pagination)

        fake_wf = types.ModuleType("gege_hr.gege_hr.utils.request_workflow")
        fake_wf.send_for_approval = lambda doc, **k: None
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.request_workflow", fake_wf)

        fake_db_util = types.ModuleType("gege_hr.gege_hr.utils._db")
        fake_db_util.guarded_update = lambda q, v=None: stub.claim_wins
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils._db", fake_db_util)

        monkeypatch.setitem(sys.modules, "frappe", stub.mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", stub.mod.utils)

        # When the FULL suite runs, the real parent packages were imported
        # earlier and hold attributes pointing at the REAL submodules —
        # `from pkg import sub` resolves via getattr(pkg, sub), bypassing the
        # sys.modules fakes above. Patch the parent attributes too (same fix
        # as tests/test_payroll_slip_generate.py) so the reload below binds
        # every dependency to our fakes.
        import gege_hr.gege_hr.api as api_pkg
        import gege_hr.gege_hr.utils as utils_pkg

        monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
        monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)
        monkeypatch.setattr(utils_pkg, "pagination", fake_pagination, raising=False)
        monkeypatch.setattr(utils_pkg, "request_workflow", fake_wf, raising=False)
        monkeypatch.setattr(utils_pkg, "_db", fake_db_util, raising=False)

        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.advance"))
        return stub, mod

    return _make


_APPROVED_DOC = {
    "name": "SAR-260710-000001",
    "employee": "HR-EMP-001",
    "employee_name": "NV Test",
    "company": "GeGe Co",
    "posting_date": "2026-07-10",
    "requested_amount": 2_000_000,
    "approved_amount": 2_000_000,
    "repayment_plan": "Next Month",
    "workflow_state": "Approved",
    "payment_status": "Unpaid",
    "docstatus": 0,
}


# --------------------------------------------------------------------------- #
# TC-A1/A2 — submit forces the single repayment method
# --------------------------------------------------------------------------- #
def test_a1_submit_forces_next_month_ignoring_installment(api):
    stub, mod = api(roles=["Employee"])
    res = mod.submit_advance_request(
        employee="HR-EMP-001",
        posting_date="2026-07-10",
        requested_amount=2_000_000,
        reason="Cần ứng lương tháng 7",
        repayment_plan="Installment",
    )
    assert res["name"]
    assert res["status"] in ("Draft", "Pending Manager")
    # the created doc carries the forced plan, not the client-sent one
    assert len(stub.new_docs) == 1
    assert stub.new_docs[0].fields["repayment_plan"] == "Next Month"


def test_a2_submit_forces_next_month_ignoring_garbage(api):
    stub, mod = api(roles=["Employee"])
    for bad in ("Custom", "Trả góp 3 kỳ", "", None):
        mod.submit_advance_request(
            employee="HR-EMP-001",
            posting_date="2026-07-10",
            requested_amount=1_000_000,
            reason="Ứng lương",
            repayment_plan=bad,
        )
    assert len(stub.new_docs) == 4
    for doc in stub.new_docs:
        assert doc.fields["repayment_plan"] == "Next Month"


# --------------------------------------------------------------------------- #
# TC-D4/D5/D2/D3 — mark_paid guards + idempotency
# --------------------------------------------------------------------------- #
def test_d4_mark_paid_requires_manager(api):
    _, mod = api(roles=["Employee"], doc=_APPROVED_DOC)
    with pytest.raises(FrappeError, match="HR/Payroll Manager"):
        mod.mark_paid(name="SAR-260710-000001")


def test_d5_mark_paid_rejects_draft(api):
    draft = dict(_APPROVED_DOC, workflow_state="Draft")
    _, mod = api(roles=["HR Manager"], doc=draft)
    with pytest.raises(FrappeError, match="đã duyệt"):
        mod.mark_paid(name="SAR-260710-000001")


def test_d2_mark_paid_twice_is_idempotent_at_api_layer(api):
    paid = dict(_APPROVED_DOC, workflow_state="Paid", payment_status="Paid")
    stub, mod = api(roles=["HR Manager"], doc=paid)
    res = mod.mark_paid(name="SAR-260710-000001")
    assert res["status"] == "Paid"
    assert "trước đó" in res["message"]
    # the API layer itself never materialises an Additional Salary — that is
    # the DocType on_update hook's job (link-guarded), so zero new_doc calls.
    assert stub.new_docs == []


def test_d3_mark_paid_race_loser_throws(api):
    # guarded_update loses the claim AND the reloaded row is not Paid → the
    # request must fail loudly instead of double-paying.
    _, mod = api(roles=["HR Manager"], doc=_APPROVED_DOC, claim_wins=False)
    with pytest.raises(FrappeError, match="đã duyệt"):
        mod.mark_paid(name="SAR-260710-000001")


# --------------------------------------------------------------------------- #
# TC-H5/H6 — reverse guards
# --------------------------------------------------------------------------- #
def test_h5_reverse_requires_manager(api):
    _, mod = api(roles=["Employee"], doc=_APPROVED_DOC)
    with pytest.raises(FrappeError, match="HR/Payroll Manager"):
        mod.reverse_advance_payment(name="SAR-260710-000001")


def test_h6_reverse_rejects_non_paid(api):
    _, mod = api(roles=["HR Manager"], doc=_APPROVED_DOC)
    with pytest.raises(FrappeError, match="Paid"):
        mod.reverse_advance_payment(name="SAR-260710-000001")
