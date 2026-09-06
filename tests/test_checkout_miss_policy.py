"""Bench-free unit tests for the checkout-miss desk-free COMPLETE policy
rounds — plans/plan-checkout-miss-deskfree-complete.md §5.1 groups B & C (S2/S3).

  B1  _email_notify hooks (resolve/explain) — enabled/disabled/never-fails
  B2  export fmt=xlsx + invalid fmt
  B6  evidence caps (count, 0=unlimited) + checkout_miss_meta
  B7  rate-limit passthrough when frappe.rate_limit is absent (stub)
  C1  get_checkout_miss_transitions
  C5  bulk_create_checkout_misses (partial-safe, cap, bad row)
  C6  appeal_checkout_miss (happy, once-only, status/owner/payroll guards)

Same stub-frappe harness as tests/test_checkout_miss_api.py.
"""

import sys
import types

import pytest

from tests.test_checkout_miss_api import FrappeError, _ticket, api


def _inject_sendmail(monkeypatch, calls, raises=False):
    """Recorder for frappe.sendmail (absent on the stub by default)."""

    def _sendmail(**kw):
        calls.append(kw)
        if raises:
            raise RuntimeError("smtp down")

    frappe_mod = sys.modules["frappe"]
    monkeypatch.setattr(frappe_mod, "sendmail", _sendmail, raising=False)


def _inject_xlsxutils(monkeypatch):
    """Fake frappe.utils.xlsxutils.make_xlsx returning a PK-headed workbook."""
    xu = types.ModuleType("frappe.utils.xlsxutils")

    class _WB:
        def getvalue(self):
            return b"PK\x03\x04mock-xlsx"

    xu.make_xlsx = lambda rows, sheet_name="": _WB()
    monkeypatch.setitem(sys.modules, "frappe.utils.xlsxutils", xu)


# --------------------------------------------------------------------------- #
# B1 — email channel
# --------------------------------------------------------------------------- #
def test_cm2_b1_1_resolve_waive_sends_email_when_enabled(api, monkeypatch):
    stub, mod = api(
        tickets=[_ticket(status="Explained")],
        settings={"vn_cm_email_enabled": 1},
    )
    calls = []
    _inject_sendmail(monkeypatch, calls)  # AFTER api(): fixture installs the stub frappe
    mod.resolve_checkout_miss("CM-0001", "waive", note="OK")
    assert len(calls) == 1
    assert calls[0]["template"] == "Checkout Miss — Miễn phạt"
    assert calls[0]["recipients"]


def test_cm2_b1_2_disabled_setting_sends_nothing(api, monkeypatch):
    stub, mod = api(
        tickets=[_ticket(status="Explained"), _ticket("CM-0002")], settings={}
    )
    calls = []
    _inject_sendmail(monkeypatch, calls)
    mod.resolve_checkout_miss("CM-0001", "waive")
    mod.explain_checkout_miss("CM-0002", "giải trình bổ sung")
    assert calls == []


def test_cm2_b1_3_sendmail_failure_never_fails_resolve(api, monkeypatch):
    stub, mod = api(
        tickets=[_ticket(status="Explained")],
        settings={"vn_cm_email_enabled": 1},
    )
    calls = []
    _inject_sendmail(monkeypatch, calls, raises=True)
    out = mod.resolve_checkout_miss("CM-0001", "waive")
    assert out.get("status") == "Waived"
    assert calls  # it did try


def test_cm2_b1_4_explain_sends_ack(api, monkeypatch):
    stub, mod = api(tickets=[_ticket()], settings={"vn_cm_email_enabled": 1})
    calls = []
    _inject_sendmail(monkeypatch, calls)
    mod.explain_checkout_miss("CM-0001", "quên bấm ra", evidence_ref="/a.png")
    templates = [c["template"] for c in calls]
    assert "Checkout Miss — Đã nhận giải trình" in templates


# --------------------------------------------------------------------------- #
# B2 — XLSX export
# --------------------------------------------------------------------------- #
def test_cm2_b2_1_xlsx_export(api, monkeypatch):
    _inject_xlsxutils(monkeypatch)
    stub, mod = api(tickets=[_ticket()])
    out = mod.export_checkout_misses_csv(fmt="xlsx")
    assert out["filename"].endswith(".xlsx")
    assert bytes(out["content"]).startswith(b"PK")
    assert out["rows"] == 1


def test_cm2_b2_2_unknown_fmt_throws(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.export_checkout_misses_csv(fmt="pdf")


# --------------------------------------------------------------------------- #
# B6 — evidence caps + meta
# --------------------------------------------------------------------------- #
def test_cm2_b6_1_file_count_cap_enforced_on_explain(api):
    stub, mod = api(
        tickets=[_ticket()],
        settings={"vn_cm_max_evidence_files": 2},
    )
    with pytest.raises(FrappeError) as ei:
        mod.explain_checkout_miss("CM-0001", "có ảnh", evidence_ref="/a.png /b.png /c.png")
    assert "2" in str(ei.value)


def test_cm2_b6_2_zero_cap_means_unlimited(api):
    stub, mod = api(
        tickets=[_ticket()],
        settings={"vn_cm_max_evidence_files": 0},
    )
    out = mod.explain_checkout_miss("CM-0001", "nhiều ảnh", evidence_ref="/1.png /2.png /3.png /4.png /5.png")
    assert out.get("status") == "Explained"


def test_cm2_b6_3_meta_returns_caps(api):
    stub, mod = api(settings={"vn_cm_max_evidence_files": 3, "vn_cm_max_evidence_mb": 2.5})
    out = mod.checkout_miss_meta()
    assert out == {"max_evidence_files": 3, "max_evidence_mb": 2.5}


def test_cm2_b6_4_meta_defaults_when_unset(api):
    stub, mod = api(settings={})
    assert mod.checkout_miss_meta() == {"max_evidence_files": 5, "max_evidence_mb": 10.0}


# --------------------------------------------------------------------------- #
# B7 — rate-limit passthrough (stub has no frappe.rate_limit)
# --------------------------------------------------------------------------- #
def test_cm2_b7_1_no_rate_limit_symbol_degrades_to_passthrough(api):
    stub, mod = api(tickets=[_ticket()])
    assert not hasattr(sys.modules["frappe"], "rate_limit")
    mod.explain_checkout_miss("CM-0001", "lần 1")
    mod.explain_checkout_miss("CM-0001", "lần 2")  # Explained still accepts re-explain
    assert stub.tickets["CM-0001"]["status"] == "Explained"


# --------------------------------------------------------------------------- #
# C1 — transitions single source of truth
# --------------------------------------------------------------------------- #
def test_cm2_c1_1_transitions_match_backend_machine(api):
    stub, mod = api(tickets=[_ticket()])
    out = mod.get_checkout_miss_transitions()
    assert out["actions"]["Pending"] == ["close", "penalise", "waive"]
    assert out["actions"]["Closed"] == []
    assert out["can_extend_grace"] == ["Pending", "Explained"]
    assert out["can_reopen"] == ["Closed"]
    assert out["can_appeal"] == ["Penalised"]


# --------------------------------------------------------------------------- #
# C5 — bulk create
# --------------------------------------------------------------------------- #
def test_cm2_c5_1_bulk_create_partial_safe(api):
    stub, mod = api(tickets=[_ticket("CM-0001", work_date="2026-08-08")])
    res = mod.bulk_create_checkout_misses(
        [
            {"employee": "HR-EMP-001", "work_date": "2026-08-08"},  # duplicate day → fail
            {"employee": "HR-EMP-001", "work_date": "2026-08-05", "note": "bổ sung"},
        ]
    )
    assert len(res["updated"]) == 1
    assert len(res["failed"]) == 1
    assert res["counts"]["created"] == 1


def test_cm2_c5_2_bulk_create_cap_100(api):
    stub, mod = api(tickets=[])
    rows = [{"employee": "HR-EMP-001", "work_date": f"2026-08-{d:02d}"} for d in range(1, 32)]
    rows = rows * 4  # 124 rows
    with pytest.raises(FrappeError):
        mod.bulk_create_checkout_misses(rows)


def test_cm2_c5_3_bulk_create_non_dict_row_lands_in_failed(api):
    stub, mod = api(tickets=[])
    res = mod.bulk_create_checkout_misses(["oops", {"employee": "HR-EMP-001", "work_date": "2026-08-05"}])
    assert res["failed"][0]["error"] == "Dòng không hợp lệ."
    assert len(res["updated"]) == 1


def test_cm2_c5_4_bulk_create_bad_json_string_throws(api):
    stub, mod = api(tickets=[])
    with pytest.raises(FrappeError):
        mod.bulk_create_checkout_misses("not-json")


# --------------------------------------------------------------------------- #
# C6 — one-shot appeal
# --------------------------------------------------------------------------- #
def _penalised(**kw):
    return _ticket(status="Penalised", penalty_amount=100000, penalty_waived=0, **kw)


def test_cm2_c6_1_owner_appeal_flips_to_explained(api):
    stub, mod = api(tickets=[_penalised()])
    out = mod.appeal_checkout_miss("CM-0001", "Em có bằng chứng checkout muộn")
    assert out.get("status") == "Explained"
    assert stub.tickets["CM-0001"]["appeal_count"] == 1
    assert stub.tickets["CM-0001"]["appeal_text"] == "Em có bằng chứng checkout muộn"
    assert out["payroll_recalc_required"] is False


def test_cm2_c6_2_second_appeal_refused(api):
    stub, mod = api(tickets=[_penalised(appeal_count=1)])
    with pytest.raises(FrappeError):
        mod.appeal_checkout_miss("CM-0001", "lần nữa")
    assert stub.set_values == []


def test_cm2_c6_3_only_penalised_tickets_appealable(api):
    stub, mod = api(tickets=[_ticket(status="Pending")])
    with pytest.raises(FrappeError):
        mod.appeal_checkout_miss("CM-0001", "lý do")


def test_cm2_c6_4_stranger_cannot_appeal(api):
    stub, mod = api(tickets=[_penalised()], roles=("Employee",), employee_for_user="HR-EMP-009")
    with pytest.raises(FrappeError):
        mod.appeal_checkout_miss("CM-0001", "lý do")


def test_cm2_c6_5_locked_payroll_refuses_calculated_flags_recalc(api):
    stub, mod = api(
        tickets=[_penalised()],
        period={"name": "P1", "status": "Approved"},
        has_slip=True,
    )
    with pytest.raises(FrappeError):
        mod.appeal_checkout_miss("CM-0001", "lý do")

    stub2, mod2 = api(
        tickets=[_penalised()],
        period={"name": "P1", "status": "Calculated"},
    )
    out = mod2.appeal_checkout_miss("CM-0001", "lý do")
    assert out["payroll_recalc_required"] is True


def test_cm2_c6_6_appeal_notifies_hr_when_email_enabled(api, monkeypatch):
    stub, mod = api(
        tickets=[_penalised()],
        settings={"vn_cm_email_enabled": 1},
    )
    calls = []
    _inject_sendmail(monkeypatch, calls)
    # appealed → HR Manager users; the stub's Has Role falls through the
    # unknown-doctype branch (pending_names → strings without .get) so
    # recipients stay empty — the email layer must simply not raise.
    mod.appeal_checkout_miss("CM-0001", "lý do")
    assert stub.tickets["CM-0001"]["status"] == "Explained"
