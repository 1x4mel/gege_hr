"""
Approval routing helpers — pure functions (plan v5 §10.6 / doctype-design §14).

Bench-free and side-effect free so the routing rules can be unit-tested outside a
Frappe site (mirroring the ``utils/calc.py`` convention). All persistence and
``frappe.db`` lookups live in :mod:`gege_hr.gege_hr.api.approval`.

Conceptual model
----------------
A *request* (Leave / Overtime / Correction / Salary Advance) walks an ordered
list of approval *steps* defined by a :doc:`VN Approval Matrix`. Each step maps
to one pending workflow state:

    steps[0]  →  ``Pending Manager``   (first approver)
    steps[1]  →  ``Pending HR``        (second approver, if any)

Approving the last step moves the request to ``Approved``; any approver may send
it to ``Rejected``. The matrix is scoped (apply_to = All / Branch / Department /
Employee Grade); the most specific active match for the requester wins.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Transaction-type → DocType + status-field configuration.
# ``status_field`` is the field that carries the workflow/state value
# (``workflow_state`` for VN doctypes, ``status`` for core Frappe Leave).
# ``pending_states`` lists every state that means "awaiting an approver".
# ``date_field`` is the field used for date-range filtering in the inbox.
# ---------------------------------------------------------------------------
PENDING_MANAGER = "Pending Manager"
PENDING_HR = "Pending HR"
APPROVED = "Approved"
REJECTED = "Rejected"

TRANSACTION_CONFIG: dict[str, dict[str, Any]] = {
    "Leave Application": {
        "doctype": "Leave Application",
        "status_field": "status",
        "date_field": "from_date",
        "pending_states": ["Open"],
        "approve_state": "Approved",
        "reject_state": "Rejected",
    },
    "Overtime Request": {
        "doctype": "VN Overtime Request",
        "status_field": "workflow_state",
        "date_field": "work_date",
        "pending_states": [PENDING_MANAGER, PENDING_HR],
        "approve_state": APPROVED,
        "reject_state": REJECTED,
    },
    "Correction Request": {
        "doctype": "VN Attendance Correction Request",
        "status_field": "workflow_state",
        "date_field": "work_date",
        "pending_states": [PENDING_MANAGER, PENDING_HR],
        "approve_state": APPROVED,
        "reject_state": REJECTED,
    },
    "Salary Advance Request": {
        "doctype": "VN Salary Advance Request",
        "status_field": "workflow_state",
        "date_field": "posting_date",
        "pending_states": [PENDING_MANAGER, PENDING_HR],
        "approve_state": APPROVED,
        "reject_state": REJECTED,
    },
    "Leave Cancellation Request": {
        "doctype": "VN Leave Cancellation Request",
        "status_field": "workflow_state",
        "date_field": "creation",
        "pending_states": [PENDING_MANAGER, PENDING_HR],
        "approve_state": APPROVED,
        "reject_state": REJECTED,
    },
}

# Human labels per type for the inbox grouping (FE falls back to its own map,
# but we send a friendly label so a bare API consumer sees something useful).
TYPE_LABELS = {
    "Leave Application": "Nghỉ phép",
    "Overtime Request": "Tăng ca",
    "Correction Request": "Điều chỉnh công",
    "Salary Advance Request": "Tạm ứng lương",
    "Leave Cancellation Request": "Hủy đơn nghỉ",
}

# ``approver_type`` → the Frappe role that implies it (for HR-style steps).
ROLE_APPROVERS = {
    "HR User": "HR User",
    "HR Manager": "HR Manager",
}

# Specificity used to rank scoped matrices (higher = more specific).
_SCOPE_RANK = {"All": 0, "Employee Grade": 1, "Branch": 2, "Department": 3}


def supported_types() -> list[str]:
    """Transaction types the inbox can serve (insertion order)."""
    return list(TRANSACTION_CONFIG.keys())


def step_pending_state(index: int) -> str:
    """Workflow state while awaiting the *i*-th (0-based) approval step."""
    return PENDING_MANAGER if index == 0 else PENDING_HR


def pending_states_for_steps(steps: list[dict]) -> list[str]:
    """All pending states implied by a matrix's step list (deduped, ordered)."""
    states = [step_pending_state(i) for i in range(len(steps or []))]
    seen: set[str] = set()
    out: list[str] = []
    for s in states:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def state_step_index(state: str) -> int:
    """Inverse of :func:`step_pending_state` (0 → Pending Manager, 1 → Pending HR)."""
    if state == PENDING_HR:
        return 1
    return 0


def next_state_after(current: str, steps: list[dict]) -> str:
    """State to move into after approving ``current``.

    If there is a further step → its pending state; otherwise the configured
    ``approve_state`` (``Approved``).
    """
    steps = steps or []
    idx = state_step_index(current)
    if idx + 1 < len(steps):
        return step_pending_state(idx + 1)
    return APPROVED


def scope_rank(matrix: dict) -> int:
    """Higher = more specific scope (Department > Branch > Grade > All)."""
    return _SCOPE_RANK.get(matrix.get("apply_to") or "All", 0)


def matrix_matches_employee(matrix: dict, attrs: dict) -> bool:
    """Does a scoped matrix apply to the requester described by ``attrs``?

    ``attrs`` carries ``branch`` / ``department`` / ``employee_grade``. An
    ``All`` matrix always matches; otherwise the matching attribute must equal.
    """
    apply_to = matrix.get("apply_to") or "All"
    if apply_to == "All":
        return True
    key = {
        "Branch": "branch",
        "Department": "department",
        "Employee Grade": "employee_grade",
    }.get(apply_to)
    if not key:
        return True
    return bool(matrix.get(key)) and matrix.get(key) == attrs.get(key)


def pick_matrix(matrices: list[dict], attrs: dict) -> dict | None:
    """Most specific active matrix that applies to the requester, or None.

    Ties on specificity break on ``modified`` desc so the latest config wins.
    """
    candidates = [m for m in (matrices or []) if matrix_matches_employee(m, attrs)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda m: (scope_rank(m), str(m.get("modified") or "")),
        reverse=True,
    )
    return candidates[0]


def current_step(matrix: dict | None, current_state: str) -> dict | None:
    """The approval step active for ``current_state`` (None if no matrix/state)."""
    if not matrix:
        return None
    steps = matrix.get("steps") or []
    if not steps:
        return None
    idx = state_step_index(current_state)
    if idx >= len(steps):
        return None
    return steps[idx]


def approver_matches(
    step: dict,
    *,
    user: str,
    roles: set[str],
    line_manager_user: str | None,
    dept_head_user: str | None,
) -> bool:
    """Can ``user`` act on this approval step?

    Resolution by ``approver_type``:

    * Line Manager      — ``user`` equals the requester's ``reports_to`` user.
    * Department Head   — ``user`` equals the requester's department head user.
    * HR User/HR Manager— ``user`` holds that Frappe role.
    * Specific User     — ``user`` equals ``step.approver_user``.
    * Specific Role     — ``user`` holds ``step.approver_role``.
    """
    atype = step.get("approver_type")
    if not user:
        return False
    if atype == "Line Manager":
        return bool(line_manager_user) and user == line_manager_user
    if atype == "Department Head":
        return bool(dept_head_user) and user == dept_head_user
    if atype in ROLE_APPROVERS:
        return ROLE_APPROVERS[atype] in (roles or set())
    if atype == "Specific User":
        return bool(step.get("approver_user")) and user == step.get("approver_user")
    if atype == "Specific Role":
        return bool(step.get("approver_role")) and step.get("approver_role") in (roles or set())
    return False
