"""Payslip ack & QR payout API — bench-free stub tests (api/payslip_ack.py).

Covers the plan matrix (plans/payslip-ack-qr-payment-plan.md):
  R2/R3  adjustment guards (short reason, duplicate request)
  R6     request on a confirmed slip → throw
  C1     confirm happy path: snapshot + REF + QR (verified CRC)
  C2     confirm without a bank account → throw (decision #3)
  C6     double confirm → throw
  P4/P5  mark paid with/without proof (decision #4)
  P6     double mark paid → throw
  P9     non-manager mark paid → PermissionError
  P2     non-manager pending list → PermissionError

Harness: stub-injection pattern proven in tests/test_advance_api.py.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class StubFrappe:
    """Configurable frappe stub for the payslip-ack surface."""

    def __init__(self, *, roles, slip=None, bank=None, slips_table=None, now=None):
        self.roles = list(roles)
        self._slip = dict(slip or {})
        self._bank = dict(bank or {})
        self._slips_table = [dict(r) for r in (slips_table or [])]
        self.set_values: list[tuple] = []
        self.deleted: list[tuple] = []
        self.notifications: list[dict] = []
        outer = self

        frappe_mod = types.ModuleType("frappe")
        frappe_mod._ = lambda s: s
        frappe_mod.throw = lambda msg, exc=None: (_ for _ in ()).throw(FrappeError(msg))
        frappe_mod.ValidationError = FrappeError
        frappe_mod.PermissionError = FrappeError
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.session = types.SimpleNamespace(user="tester@example.com")
        frappe_mod.utils = types.ModuleType("frappe.utils")
        frappe_mod.utils.now_datetime = lambda: now or "2026-08-20 12:00:00"

        class _DB:
            def get_value(inner, doctype, name, *args, **kwargs):
                if doctype == "Salary Slip":
                    row = outer._slip
                    if isinstance(name, dict):
                        # filter-style lookup (used by delete_bank_account guard)
                        for k, v in name.items():
                            if isinstance(v, list):
                                continue
                            if row.get(k) != v:
                                return None
                        return type("R", (), {"name": row.get("name")})()
                    fields = args[0] if args else kwargs.get("fieldname")
                    if isinstance(fields, (list, tuple)):
                        return types.SimpleNamespace(**{f: row.get(f) for f in fields})
                    return row.get(fields)
                if doctype == "VN Employee Bank Account":
                    row = outer._bank
                    if isinstance(fields := (args[0] if args else None), (list, tuple)):
                        return types.SimpleNamespace(**{f: row.get(f) for f in fields})
                    return row.get(fields)
                return None

            def set_value(inner, doctype, name, fields, *a, **kw):
                outer.set_values.append((doctype, name, fields))
                if doctype == "Salary Slip":
                    outer._slip.update(
                        fields if isinstance(fields, dict) else {fields: a[0] if a else kw.get("value")}
                    )
                return None

            def get_all(inner, doctype, filters=None, fields=None, **kw):
                if doctype == "VN Employee Bank Account":
                    return [dict(outer._bank)] if outer._bank else []
                if doctype == "Salary Slip":
                    rows = []
                    for r in outer._slips_table:
                        ok = True
                        for k, v in (filters or {}).items():
                            if isinstance(v, list):
                                op, val = v[0], v[1]
                                if op == "in" and r.get(k) not in val:
                                    ok = False
                            elif r.get(k) != v:
                                ok = False
                        if ok:
                            rows.append({f: r.get(f) for f in (fields or [])} if fields else dict(r))
                    return rows
                return []

            def count(inner, doctype, filters=None, **kw):
                return 0

        frappe_mod.db = _DB()
        frappe_mod.get_all = frappe_mod.db.get_all
        frappe_mod.session = frappe_mod.session
        self.mod = frappe_mod

    def get_doc(self, doctype, name=None, **kw):
        if doctype == "VN Employee Bank Account":
            return types.SimpleNamespace(
                **{
                    **self._bank,
                    "save": lambda **k: None,
                    "delete": lambda **k: None,
                    "employee": self._bank.get("employee"),
                }
            )
        raise FrappeError(f"unexpected get_doc {doctype}")


@pytest.fixture()
def api(monkeypatch):
    def _make(**kw):
        stub = StubFrappe(**kw)

        fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
        fake_audit.log = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)

        fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
        fake_emp.HR_MANAGER_ROLES = {"HR Manager", "Payroll Manager", "System Manager"}
        fake_emp.get_user_roles = lambda: list(stub.roles)
        fake_emp.get_employee_for_user = lambda: "HR-EMP-00010"
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

        fake_notify = types.ModuleType("gege_hr.gege_hr.utils.notify")
        fake_notify.push_notification = lambda **kw: stub.notifications.append(kw)
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.notify", fake_notify)

        monkeypatch.setitem(sys.modules, "frappe", stub.mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", stub.mod.utils)

        import gege_hr.gege_hr.api as api_pkg
        import gege_hr.gege_hr.utils as utils_pkg

        monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
        monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)
        monkeypatch.setattr(utils_pkg, "notify", fake_notify, raising=False)

        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payslip_ack"))
        return stub, mod

    return _make


_SLIP = {
    "name": "Sal Slip/HR-EMP-00010/00006",
    "employee": "HR-EMP-00010",
    "employee_name": "Nguyễn Tiến Dũng",
    "company": "GeGe Esport",
    "start_date": "2026-04-01",
    "end_date": "2026-04-30",
    "net_pay": 1449031.1,
    "vn_employee_visible": 1,
    "vn_ack_status": "",
}

_BANK = {
    "name": "EBA-00001",
    "employee": "HR-EMP-00010",
    "bank_bin": "970422",
    "bank_name": "MBBank",
    "account_no": "0029999975666",
    "account_name": "NGUYEN TIEN DUNG",
    "is_default": 1,
}


# --------------------------------------------------------------------------- #
# R2/R3/R6 — adjustment guards
# --------------------------------------------------------------------------- #
def test_r2_short_reason_rejected(api):
    _, mod = api(roles=["Employee"], slip=_SLIP)
    with pytest.raises(FrappeError, match="tối thiểu 10 ký tự"):
        mod.request_payslip_adjustment(name=_SLIP["name"], reason="ngắn")


def test_r3_duplicate_request_rejected(api):
    slip = dict(_SLIP, vn_ack_status="Requested")
    _, mod = api(roles=["Employee"], slip=slip)
    with pytest.raises(FrappeError, match="đã được gửi"):
        mod.request_payslip_adjustment(name=_SLIP["name"], reason="Thiếu tiền làm thêm giờ cuối tháng")


def test_r6_request_on_confirmed_rejected(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment")
    _, mod = api(roles=["Employee"], slip=slip)
    with pytest.raises(FrappeError, match="đã được xác nhận"):
        mod.request_payslip_adjustment(name=_SLIP["name"], reason="Tôi muốn điều chỉnh lại")


def test_r1_request_happy_path(api):
    _, mod = api(roles=["Employee"], slip=_SLIP)
    res = mod.request_payslip_adjustment(name=_SLIP["name"], reason="Thiếu 2 giờ tăng ca ngày 20/04")
    assert res["status"] == "Requested"


# --------------------------------------------------------------------------- #
# C1/C2/C3/C6 — confirm
# --------------------------------------------------------------------------- #
def test_c1_confirm_snapshots_ref_and_qr(api):
    from gege_hr.gege_hr.utils import vietqr

    _, mod = api(roles=["Employee"], slip=_SLIP, bank=_BANK)
    res = mod.confirm_payslip(name=_SLIP["name"], bank_account=_BANK["name"])
    assert res["status"] == "Awaiting Payment"
    assert res["payment_ref"] == "LUONG-042026-HR-EMP-00010"
    assert res["qr_text"] == vietqr.encode_vietqr(
        _BANK["bank_bin"], _BANK["account_no"], amount=_SLIP["net_pay"], description=res["payment_ref"]
    )
    assert vietqr.verify_vietqr(res["qr_text"])


def test_c2_confirm_requires_bank_account(api):
    _, mod = api(roles=["Employee"], slip=_SLIP, bank=_BANK)
    with pytest.raises(FrappeError, match="chọn tài khoản"):
        mod.confirm_payslip(name=_SLIP["name"], bank_account="")


def test_c3_confirm_foreign_bank_rejected(api):
    bank = dict(_BANK, name="EBA-OTHER", employee="HR-EMP-00099")
    _, mod = api(roles=["Employee"], slip=_SLIP, bank=bank)
    with pytest.raises(FrappeError, match="không hợp lệ"):
        mod.confirm_payslip(name=_SLIP["name"], bank_account="EBA-OTHER")


def test_c6_double_confirm_rejected(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment")
    _, mod = api(roles=["Employee"], slip=slip, bank=_BANK)
    with pytest.raises(FrappeError, match="đã được xác nhận"):
        mod.confirm_payslip(name=_SLIP["name"], bank_account=_BANK["name"])


# --------------------------------------------------------------------------- #
# P2/P4/P5/P6/P9 — payout queue
# --------------------------------------------------------------------------- #
def test_p2_pending_list_requires_manager(api):
    _, mod = api(roles=["Employee"])
    with pytest.raises(FrappeError, match="HR/Payroll Manager"):
        mod.pending_payment_slips()


def test_p4_mark_paid_with_proof(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment", vn_payment_ref="LUONG-042026-HR-EMP-00010")
    stub, mod = api(roles=["HR Manager"], slip=slip)
    res = mod.mark_payslip_paid(name=slip["name"], proof_file="/files/receipt.png")
    assert res["status"] == "Paid"
    fields = stub.set_values[-1][2]
    assert fields["vn_payment_proof"] == "/files/receipt.png"
    assert fields["vn_paid_by"]
    assert stub.notifications, "employee got the paid notification"


def test_p5_mark_paid_requires_proof(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment")
    _, mod = api(roles=["HR Manager"], slip=slip)
    with pytest.raises(FrappeError, match="biên lai"):
        mod.mark_payslip_paid(name=slip["name"], proof_file="")


def test_p5b_mark_paid_rejects_foreign_file_path(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment")
    _, mod = api(roles=["HR Manager"], slip=slip)
    with pytest.raises(FrappeError, match="không hợp lệ"):
        mod.mark_payslip_paid(name=slip["name"], proof_file="https://evil.example/x.png")


def test_p6_double_mark_paid_rejected(api):
    slip = dict(_SLIP, vn_ack_status="Paid")
    _, mod = api(roles=["HR Manager"], slip=slip)
    with pytest.raises(FrappeError, match="Chờ thanh toán"):
        mod.mark_payslip_paid(name=slip["name"], proof_file="/files/r.png")


def test_p9_non_manager_mark_paid_rejected(api):
    slip = dict(_SLIP, vn_ack_status="Awaiting Payment")
    _, mod = api(roles=["Employee"], slip=slip)
    with pytest.raises(FrappeError, match="HR/Payroll Manager"):
        mod.mark_payslip_paid(name=slip["name"], proof_file="/files/r.png")
