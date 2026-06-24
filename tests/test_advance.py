"""Unit tests for salary-advance eligibility (plan v5 §16 / doctype-design §22).

All tests are bench-free: the pure helpers live in
``gege_hr.gege_hr.utils.advance`` and guard the optional ``frappe`` import.
"""

from datetime import date

import pytest

from gege_hr.gege_hr.utils import advance as adv


def _policy(**over):
    p = {
        "name": "P",
        "company": "Co",
        "branch": None,
        "employee_group": None,
        "max_percentage": 50,
        "max_fixed_amount": None,
        "min_working_days": 15,
        "max_requests_per_month": 1,
        "cutoff_day": 20,
        "is_active": 1,
        "modified": "2026-01-01",
    }
    p.update(over)
    return p


# --------------------------------------------------------------------------- #
# compute_eligible_amount
# --------------------------------------------------------------------------- #
def test_eligible_percentage_of_base():
    assert adv.compute_eligible_amount(10_000_000, _policy(max_percentage=50)) == 5_000_000


def test_eligible_capped_by_fixed_amount():
    # 50% of 10M = 5M but fixed cap = 3M → 3M.
    assert (
        adv.compute_eligible_amount(10_000_000, _policy(max_percentage=50, max_fixed_amount=3_000_000))
        == 3_000_000
    )


def test_eligible_no_policy_returns_zero():
    assert adv.compute_eligible_amount(10_000_000, None) == 0


def test_eligible_no_base_returns_zero():
    assert adv.compute_eligible_amount(0, _policy()) == 0


def test_eligible_never_negative():
    assert adv.compute_eligible_amount(-5, _policy(max_percentage=50)) == 0


def test_eligible_rounds_two_decimals():
    assert adv.compute_eligible_amount(3333, _policy(max_percentage=33)) == round(3333 * 0.33, 2)


# --------------------------------------------------------------------------- #
# pick_advance_policy
# --------------------------------------------------------------------------- #
def test_pick_policy_skips_inactive():
    policies = [_policy(name="A", is_active=0), _policy(name="B", is_active=1)]
    picked = adv.pick_advance_policy(policies, {"company": "Co"})
    assert picked["name"] == "B"


def test_pick_policy_branch_beats_company_only():
    policies = [
        _policy(name="Co", company="Co", branch=None, modified="2026-01-01"),
        _policy(name="Branch", company="Co", branch="HN", modified="2026-01-01"),
    ]
    picked = adv.pick_advance_policy(policies, {"company": "Co", "branch": "HN"})
    assert picked["name"] == "Branch"


def test_pick_policy_employee_group_strongest():
    policies = [
        _policy(name="Branch", company="Co", branch="HN", modified="2026-01-01"),
        _policy(name="Group", company="Co", branch="HN", employee_group="Sales", modified="2026-01-01"),
    ]
    picked = adv.pick_advance_policy(policies, {"company": "Co", "branch": "HN", "employee_group": "Sales"})
    assert picked["name"] == "Group"


def test_pick_policy_tie_break_modified_desc():
    policies = [
        _policy(name="Old", company="Co", modified="2026-01-01"),
        _policy(name="New", company="Co", modified="2026-06-01"),
    ]
    picked = adv.pick_advance_policy(policies, {"company": "Co"})
    assert picked["name"] == "New"


def test_pick_policy_none_when_empty():
    assert adv.pick_advance_policy([], {"company": "Co"}) is None


def test_pick_policy_company_filter_is_via_match_score():
    # A policy for another company scores lower but is still selectable (no
    # hard company filter in pick_advance_policy — that's the caller's job); we
    # only assert the same-company one wins on score.
    policies = [
        _policy(name="Other", company="Other", modified="2026-06-01"),
        _policy(name="Mine", company="Co", modified="2026-01-01"),
    ]
    picked = adv.pick_advance_policy(policies, {"company": "Co"})
    assert picked["name"] == "Mine"


# --------------------------------------------------------------------------- #
# is_past_cutoff
# --------------------------------------------------------------------------- #
def test_cutoff_before_is_allowed():
    assert adv.is_past_cutoff("2026-06-10", 20) is False


def test_cutoff_on_day_is_allowed():
    assert adv.is_past_cutoff("2026-06-20", 20) is False


def test_cutoff_after_day_is_blocked():
    assert adv.is_past_cutoff("2026-06-21", 20) is True


def test_cutoff_zero_or_none_never_blocks():
    assert adv.is_past_cutoff("2026-06-28", 0) is False
    assert adv.is_past_cutoff("2026-06-28", None) is False


def test_cutoff_accepts_date_object():
    assert adv.is_past_cutoff(date(2026, 6, 25), 20) is True


# --------------------------------------------------------------------------- #
# advance_deduction_amount / build_additional_salary_payload
# --------------------------------------------------------------------------- #
def _sar(**over):
    base = {
        "name": "SAR-260621-000001",
        "employee": "HR-EMP-0001",
        "employee_name": "Nguyen Van A",
        "company": "Gege Co",
        "approved_amount": 2_000_000,
        "requested_amount": 2_000_000,
        "posting_date": "2026-06-21",
        "repayment_plan": "Next Month",
        "reason": "Cần ứng lương",
    }
    base.update(over)
    return base


def test_deduction_amount_uses_approved():
    assert adv.advance_deduction_amount(_sar(approved_amount=1_500_000)) == 1_500_000


def test_deduction_amount_falls_back_to_requested_when_approved_zero():
    assert adv.advance_deduction_amount(_sar(approved_amount=0, requested_amount=800_000)) == 800_000


def test_deduction_amount_never_negative():
    assert adv.advance_deduction_amount(_sar(approved_amount=-100, requested_amount=-50)) == 0


def test_payload_shape_is_deduction_and_additive():
    payload = adv.build_additional_salary_payload(_sar())
    assert payload["employee"] == "HR-EMP-0001"
    assert payload["company"] == "Gege Co"
    assert payload["amount"] == 2_000_000
    assert payload["type"] == "Deduction"
    assert payload["overwrite_salary_structure_amount"] == 0
    assert payload["payroll_date"] == "2026-06-21"
    assert payload["ref_doctype"] == "VN Salary Advance Request"
    assert payload["ref_docname"] == "SAR-260621-000001"


def test_payload_default_component_when_blank():
    payload = adv.build_additional_salary_payload(_sar(), salary_component="   ")
    assert payload["salary_component"] == adv.DEFAULT_ADVANCE_DEDUCTION_COMPONENT


def test_payload_honours_explicit_component():
    payload = adv.build_additional_salary_payload(_sar(), salary_component="Ứng lương")
    assert payload["salary_component"] == "Ứng lương"


def test_payload_amount_matches_deduction_amount():
    sar = _sar(approved_amount=0, requested_amount=1_234_567)
    payload = adv.build_additional_salary_payload(sar)
    assert payload["amount"] == adv.advance_deduction_amount(sar) == 1_234_567


def test_payload_payroll_date_override():
    payload = adv.build_additional_salary_payload(_sar(posting_date="2026-06-21"), payroll_date="2026-07-25")
    assert payload["payroll_date"] == "2026-07-25"


# --------------------------------------------------------------------------- #
# linked_deduction_should_reverse + reset_after_reversal
# --------------------------------------------------------------------------- #
def test_should_reverse_when_workflow_left_paid():
    sar = _sar(
        workflow_state="Approved",
        linked_additional_salary="ACC-SAL-0001",
        docstatus=1,
    )
    assert adv.linked_deduction_should_reverse(sar) is True


def test_should_reverse_when_cancelled():
    sar = _sar(
        workflow_state="Cancelled",
        linked_additional_salary="ACC-SAL-0001",
        docstatus=2,
    )
    assert adv.linked_deduction_should_reverse(sar) is True


def test_should_not_reverse_when_still_paid():
    sar = _sar(
        workflow_state="Paid",
        linked_additional_salary="ACC-SAL-0001",
        docstatus=1,
    )
    assert adv.linked_deduction_should_reverse(sar) is False


def test_should_not_reverse_without_linked_row():
    # No deduction was ever created — nothing to cancel.
    sar = _sar(workflow_state="Approved", linked_additional_salary=None, docstatus=1)
    assert adv.linked_deduction_should_reverse(sar) is False


def test_should_not_reverse_with_blank_link():
    sar = _sar(workflow_state="Cancelled", linked_additional_salary="   ", docstatus=2)
    assert adv.linked_deduction_should_reverse(sar) is False


def test_should_not_reverse_none_input():
    assert adv.linked_deduction_should_reverse(None) is False


def test_should_reverse_docstatus_only_ignores_state():
    # docstatus >= 2 (Cancelled) wins even if workflow_state is stale.
    sar = _sar(workflow_state="Paid", linked_additional_salary="ACC-SAL-0001", docstatus=2)
    assert adv.linked_deduction_should_reverse(sar) is True


def test_reset_after_reversal_clears_links():
    sar = _sar(
        linked_additional_salary="ACC-SAL-0001",
        linked_payment_entry="PE-0001",
    )
    delta = adv.reset_after_reversal(sar)
    assert delta == {
        "linked_additional_salary": None,
        "payment_status": "Unpaid",
        "linked_payment_entry": None,
    }


def test_reset_after_reversal_independent_of_input_links():
    delta = adv.reset_after_reversal({})
    assert delta["linked_additional_salary"] is None
    assert delta["payment_status"] == "Unpaid"
