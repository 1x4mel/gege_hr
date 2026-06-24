"""Bench-free tests for the pure approval routing rules (utils/approval.py).

No Frappe site required — these exercise the matrix resolution, step→state
mapping and approver-matching logic that the inbox relies on.
"""

from __future__ import annotations

from gege_hr.gege_hr.utils import approval as rules


# --------------------------------------------------------------------------- #
# Step ↔ state mapping
# --------------------------------------------------------------------------- #
def test_step_pending_state_index():
    assert rules.step_pending_state(0) == "Pending Manager"
    assert rules.step_pending_state(1) == "Pending HR"


def test_state_step_index_inverse():
    assert rules.state_step_index("Pending Manager") == 0
    assert rules.state_step_index("Pending HR") == 1


def test_pending_states_for_steps_deduped():
    # 3 steps still only produce the 2 workflow states (Manager, HR).
    states = rules.pending_states_for_steps([{}, {}, {}])
    assert states == ["Pending Manager", "Pending HR"]


def test_next_state_single_step_to_approved():
    steps = [{"approver_type": "Line Manager"}]
    assert rules.next_state_after("Pending Manager", steps) == "Approved"


def test_next_state_two_steps_advances_to_hr():
    steps = [
        {"approver_type": "Line Manager"},
        {"approver_type": "HR Manager"},
    ]
    assert rules.next_state_after("Pending Manager", steps) == "Pending HR"
    assert rules.next_state_after("Pending HR", steps) == "Approved"


# --------------------------------------------------------------------------- #
# Matrix scoping & selection
# --------------------------------------------------------------------------- #
def test_scope_rank_ordering():
    assert rules.scope_rank({"apply_to": "All"}) == 0
    assert rules.scope_rank({"apply_to": "Department"}) > rules.scope_rank({"apply_to": "All"})


def test_matrix_matches_all_always_true():
    m = {"apply_to": "All"}
    assert rules.matrix_matches_employee(m, {"department": "Eng"})


def test_matrix_matches_department_specific():
    m = {"apply_to": "Department", "department": "Eng"}
    assert rules.matrix_matches_employee(m, {"department": "Eng"})
    assert not rules.matrix_matches_employee(m, {"department": "Sales"})


def test_pick_matrix_prefers_most_specific():
    matrices = [
        {"apply_to": "All", "modified": "2026-01-01"},
        {"apply_to": "Department", "department": "Eng", "modified": "2026-01-02"},
        {"apply_to": "Department", "department": "Sales", "modified": "2026-01-03"},
    ]
    chosen = rules.pick_matrix(matrices, {"department": "Eng"})
    assert chosen["apply_to"] == "Department"
    assert chosen["department"] == "Eng"


def test_pick_matrix_falls_back_to_all():
    matrices = [{"apply_to": "All", "modified": "2026-01-01"}]
    chosen = rules.pick_matrix(matrices, {"department": "Eng"})
    assert chosen is not None


def test_pick_matrix_none_when_no_match():
    matrices = [{"apply_to": "Department", "department": "Eng", "modified": "x"}]
    assert rules.pick_matrix(matrices, {"department": "Sales"}) is None


# --------------------------------------------------------------------------- #
# current_step
# --------------------------------------------------------------------------- #
def test_current_step_index():
    matrix = {
        "steps": [
            {"approver_type": "Line Manager"},
            {"approver_type": "HR Manager"},
        ]
    }
    assert rules.current_step(matrix, "Pending Manager")["approver_type"] == "Line Manager"
    assert rules.current_step(matrix, "Pending HR")["approver_type"] == "HR Manager"


def test_current_step_none_when_state_beyond_steps():
    matrix = {"steps": [{"approver_type": "Line Manager"}]}
    # Only one step → "Pending HR" has no corresponding step.
    assert rules.current_step(matrix, "Pending HR") is None


def test_current_step_none_when_no_matrix():
    assert rules.current_step(None, "Pending Manager") is None


# --------------------------------------------------------------------------- #
# approver_matches
# --------------------------------------------------------------------------- #
def test_approver_matches_line_manager():
    step = {"approver_type": "Line Manager"}
    assert rules.approver_matches(
        step, user="boss@x", roles=set(), line_manager_user="boss@x", dept_head_user=None
    )
    assert not rules.approver_matches(
        step, user="other@x", roles=set(), line_manager_user="boss@x", dept_head_user=None
    )


def test_approver_matches_hr_manager_role():
    step = {"approver_type": "HR Manager"}
    assert rules.approver_matches(
        step, user="hr@x", roles={"HR Manager"}, line_manager_user=None, dept_head_user=None
    )
    assert not rules.approver_matches(
        step, user="hr@x", roles={"HR User"}, line_manager_user=None, dept_head_user=None
    )


def test_approver_matches_specific_user_and_role():
    su = {"approver_type": "Specific User", "approver_user": "alice@x"}
    assert rules.approver_matches(
        su, user="alice@x", roles=set(), line_manager_user=None, dept_head_user=None
    )
    sr = {"approver_type": "Specific Role", "approver_role": "Projects Lead"}
    assert rules.approver_matches(
        sr, user="bob@x", roles={"Projects Lead"}, line_manager_user=None, dept_head_user=None
    )


def test_approver_no_user_never_matches():
    step = {"approver_type": "HR Manager"}
    assert not rules.approver_matches(
        step, user="", roles={"HR Manager"}, line_manager_user=None, dept_head_user=None
    )


# --------------------------------------------------------------------------- #
# Transaction config sanity
# --------------------------------------------------------------------------- #
def test_supported_types_includes_ot_and_correction():
    types = rules.supported_types()
    assert "Overtime Request" in types
    assert "Correction Request" in types


def test_each_config_has_required_keys():
    for ttype, cfg in rules.TRANSACTION_CONFIG.items():
        for key in ("doctype", "status_field", "pending_states", "approve_state", "reject_state"):
            assert key in cfg, f"{ttype} missing {key}"
