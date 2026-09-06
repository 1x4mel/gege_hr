"""
Frappe **Workflow** fixtures for the three request DocTypes whose lifecycle is
driven by the VN Approval Matrix:

* ``VN Overtime Request``
* ``VN Attendance Correction Request``
* ``VN Salary Advance Request``

Why Frappe Workflows *and* the Approval Matrix together
-------------------------------------------------------
The unified Approval Inbox
([`gege_hr.gege_hr.api.approval`](gege_hr/gege_hr/gege_hr/api/approval.py:1)) is
the **real** authorisation gate: it resolves the active
**VN Approval Matrix** for the requester, walks its steps (Line Manager → HR
Manager by default) and only lets the approver holding the *current* step act
(``_user_can_act``). Frappe's native Workflow can only authorise transitions by
**role** — it has no notion of *reports_to* (Line Manager). We therefore cannot
let Frappe be the sole gate.

Instead the Workflow is wired as a **permissive state machine** that:

* Declares every state the inbox/API uses (``Draft`` → ``Pending Manager`` →
  ``Pending HR`` → ``Approved`` …), so Frappe's ``validate_workflow`` recognises
  them.
* Declares every ``(current → next)`` transition the API performs, each with a
  broad ``allowed`` role set (``Employee`` *and* ``HR Manager``/``HR User``) so
  **role-based** ``validate_workflow`` never blocks a matrix-driven save — the
  matrix remains the source of truth.

Design choices that keep the existing API unchanged
---------------------------------------------------
* Every state maps to ``doc_status = 0``. The inbox API transitions via plain
  ``doc.save()`` (never ``submit()``/``cancel()``), so docstatus stays 0. Mapping
  ``Approved`` → 1 would diverge from reality; mapping everything to 0 is
  consistent and avoids auto-submit on save. Native cancel stays available
  (no ``Cancelled`` state ⇒ ``can_cancel_document`` is ``True``).
* Transitions duplicate the same logical move once per allowed role (Frappe's
  ``allowed`` is a single ``Role`` link).

The module is **idempotent** (only creates missing records — a manager's manual
workflow tweaks survive re-runs) and **bench-guarded** (any failure is logged,
``install-app`` never aborts). Called from
[`create_seed_data`](gege_hr/gege_hr/gege_hr/setup.py:1) / ``after_install`` and
may be run manually::

    bench --site <site> execute gege_hr.gege_hr.setup_workflows.seed_workflows
"""

from __future__ import annotations

import frappe

# --------------------------------------------------------------------------- #
# Shared state / action vocabulary
# --------------------------------------------------------------------------- #
# Every Workflow State referenced below must exist as a "Workflow State" master.
_STATES = ["Draft", "Pending Manager", "Pending HR", "Approved", "Confirmed", "Paid", "Rejected"]

# Every action referenced below must exist as a "Workflow Action Master".
# "Return" (desk-free Phase B1): approver trả lại request cho nhân viên sửa —
# Pending → Draft, validated như mọi transition khác.
_ACTIONS = [
    "Send for Approval",
    "Approve",
    "Reject",
    "Confirm",
    "Mark Paid",
    "Reverse",
    "Return",
]

# Roles we sprinkle across transitions so role-based validate_workflow never
# blocks a matrix-driven save (matrix is the real gate).
_EMP = "Employee"
_HRU = "HR User"
_HRM = "HR Manager"
_PAY = "Payroll Manager"


def _exists(doctype: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(doctype, name))
    except Exception:
        return False


def _table_ready(doctype: str) -> bool:
    # ``table_exists`` prepends ``tab`` itself — pass the bare doctype name.
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _ensure_masters() -> None:
    """Create the Workflow State / Workflow Action Master rows we reference.

    Both masters are named by their data field (``workflow_state_name`` /
    ``workflow_action_name`` — see their ``autoname:field:`` config), not by a
    bare ``name`` key.
    """
    for state in _STATES:
        if not _exists("Workflow State", state):
            try:
                frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(
                    ignore_permissions=True
                )
            except Exception:
                frappe.log_error(f"gege_hr workflow seed: Workflow State {state}")

    for action in _ACTIONS:
        if not _exists("Workflow Action Master", action):
            try:
                frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
                    ignore_permissions=True
                )
            except Exception:
                frappe.log_error(f"gege_hr workflow seed: Workflow Action {action}")


# --------------------------------------------------------------------------- #
# Transition specs
# --------------------------------------------------------------------------- #
def _approve_to(steps_allowed: list[str]) -> list[tuple]:
    """Pending Manager → Pending HR / Approved, one row per allowed role.

    Both next-states carry the ``Approve`` action so the matrix API — which sets
    ``workflow_state`` directly — matches one of them regardless of whether the
    active matrix has one or two steps.
    """
    rows = []
    for role in steps_allowed:
        rows.append(("Pending Manager", "Approve", "Pending HR", role))
        rows.append(("Pending Manager", "Approve", "Approved", role))
    return rows


def _common_transitions() -> list[tuple]:
    """Transitions shared by all three request workflows.

    Each tuple = ``(from_state, action, next_state, allowed_role)``.
    """
    broad = [_EMP, _HRU, _HRM]
    rows = [
        # Employee (or HR on their behalf) sends the saved draft for approval.
        ("Draft", "Send for Approval", "Pending Manager", _EMP),
        ("Draft", "Send for Approval", "Pending Manager", _HRM),
        ("Draft", "Send for Approval", "Pending Manager", _HRU),
    ]
    # Step-0 approvals (Line Manager / single-step matrix).
    rows += _approve_to(broad)
    # Step-1 approval (HR tier).
    for role in (_HRU, _HRM):
        rows.append(("Pending HR", "Approve", "Approved", role))
    # Rejections from any pending step.
    for role in broad:
        rows.append(("Pending Manager", "Reject", "Rejected", role))
    # B-1 fix (plans/overtime-deskfree-complete.md OT7): the owner Employee must
    # also be able to cancel their own request while it sits at Pending HR —
    # cancel_overtime_request writes Rejected via doc.save(), which validate_workflow
    # checks against these rows. Verified missing on the live site (2026-09-03).
    for role in broad:
        rows.append(("Pending HR", "Reject", "Rejected", role))
    # Cancelling a just-saved draft directly → Rejected.
    for role in (_EMP, _HRM):
        rows.append(("Draft", "Reject", "Rejected", role))
    # Desk-free Phase B1 (plans/approvals-deskfree-complete §3.7): "Trả lại để
    # sửa" — the approver bounces an incomplete request to Draft (with a
    # comment) instead of rejecting it. validate_workflow needs these rows for
    # the doc.save() inside return_request.
    for role in broad:
        rows.append(("Pending Manager", "Return", "Draft", role))
        rows.append(("Pending HR", "Return", "Draft", role))
    return rows


def _state_rows(names: list[str], allow_edit: str = _HRM) -> list[dict]:
    """Build Workflow Document State rows (all doc_status 0)."""
    return [{"state": n, "doc_status": "0", "allow_edit": allow_edit, "is_optional_state": 0} for n in names]


def _transition_rows(specs: list[tuple]) -> list[dict]:
    return [
        {
            "state": s,
            "action": a,
            "next_state": nxt,
            "allowed": role,
            "allow_self_approval": 1,
        }
        for (s, a, nxt, role) in specs
    ]


# Per-DocType workflow blueprints: (workflow_name, doctype, states, transitions)
_WORKFLOWS = [
    {
        "name": "VN Overtime Request Workflow",
        "doctype": "VN Overtime Request",
        "states": _state_rows(
            ["Draft", "Pending Manager", "Pending HR", "Approved", "Confirmed", "Rejected"]
        ),
        "transitions": _transition_rows(_common_transitions() + [("Approved", "Confirm", "Confirmed", _HRM)]),
    },
    {
        "name": "VN Correction Request Workflow",
        "doctype": "VN Attendance Correction Request",
        "states": _state_rows(["Draft", "Pending Manager", "Pending HR", "Approved", "Rejected"]),
        "transitions": _transition_rows(_common_transitions()),
    },
    {
        "name": "VN Salary Advance Workflow",
        "doctype": "VN Salary Advance Request",
        "states": _state_rows(["Draft", "Pending Manager", "Pending HR", "Approved", "Paid", "Rejected"]),
        "transitions": _transition_rows(
            _common_transitions()
            + [
                ("Approved", "Mark Paid", "Paid", _HRM),
                ("Approved", "Mark Paid", "Paid", _PAY),
                ("Paid", "Reverse", "Approved", _HRM),
                ("Paid", "Reverse", "Approved", _PAY),
            ]
        ),
    },
]

# --------------------------------------------------------------------------- #
# Desk-free COMPLETE (C2) — checkout-miss ticket workflow.
#
# Same "permissive state machine" doctrine as above, with one twist: the
# status field IS the workflow field (``workflow_state_field="status"``). The
# API layer's ``_ALLOWED_ACTIONS`` (api/checkout_miss.py) stays the real gate;
# the workflow only exists so Frappe's native ``validate_workflow`` recognises
# every state/transition the API performs (resolve/reopen via doc.save) and
# never blocks it. Engine + explain + appeal write via ``db.set_value``/
# ``db_set`` (out-of-band by design), so only the save-driven paths matter —
# every transition is duplicated across all four broad roles.
# --------------------------------------------------------------------------- #
_CM_STATES = ["Pending", "Explained", "Waived", "Penalised", "Closed"]
_CM_ACTIONS = ["Explain", "Waive", "Penalise", "Close", "Reopen"]


def _cm_transitions() -> list[tuple]:
    moves = [
        ("Pending", "Explain", "Explained"),
        ("Pending", "Waive", "Waived"),
        ("Pending", "Penalise", "Penalised"),
        ("Pending", "Close", "Closed"),
        ("Explained", "Waive", "Waived"),
        ("Explained", "Penalise", "Penalised"),
        ("Explained", "Close", "Closed"),
        ("Waived", "Penalise", "Penalised"),
        ("Waived", "Close", "Closed"),
        ("Penalised", "Waive", "Waived"),
        ("Penalised", "Close", "Closed"),
        ("Closed", "Reopen", "Pending"),
    ]
    rows = []
    for (s, a, nxt) in moves:
        for role in (_EMP, _HRU, _HRM, _PAY):
            rows.append((s, a, nxt, role))
    return rows


def seed_checkout_miss_workflow() -> str | None:
    """Create the permissive VN Checkout Miss workflow if absent.

    Idempotent + bench-guarded; returns the workflow name (or None).
    """
    if not (_table_ready("Workflow") and _table_ready("Workflow State")):
        return None
    # Ensure the extra masters referenced only by this workflow exist.
    for state in _CM_STATES:
        if not _exists("Workflow State", state):
            try:
                frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(
                    ignore_permissions=True
                )
            except Exception:
                frappe.log_error(f"gege_hr workflow seed: Workflow State {state}")
    for action in _CM_ACTIONS:
        if not _exists("Workflow Action Master", action):
            try:
                frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
                    ignore_permissions=True
                )
            except Exception:
                frappe.log_error(f"gege_hr workflow seed: Workflow Action {action}")

    name = "VN Checkout Miss Workflow"
    if _exists("Workflow", name):
        try:
            frappe.db.set_value("Workflow", name, "is_active", 1)
        except Exception:
            frappe.log_error(f"gege_hr workflow seed: reactivate {name}")
        return name
    try:
        frappe.get_doc(
            {
                "doctype": "Workflow",
                "workflow_name": name,
                "document_type": "VN Checkout Miss",
                "workflow_state_field": "status",
                "is_active": 1,
                "send_email_alert": 0,
                "states": _state_rows(_CM_STATES),
                "transitions": _transition_rows(_cm_transitions()),
            }
        ).insert(ignore_permissions=True)
        return name
    except Exception:
        frappe.log_error("gege_hr workflow seed: failed to create VN Checkout Miss Workflow")
        return None


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def seed_workflows() -> dict:
    """Create the three request workflows if absent (idempotent, bench-guarded).

    Re-runs never overwrite a manager's manual edits — once a Workflow exists it
    is left untouched (only ``is_active`` is reaffirmed).
    """
    if not (_table_ready("Workflow") and _table_ready("Workflow State")):
        # Running outside bench or before migrate — nothing to do.
        return {"seeded": [], "reason": "workflow tables not ready"}

    _ensure_masters()

    created = []
    transitions_added = 0
    for spec in _WORKFLOWS:
        wf_name = spec["name"]
        if _exists("Workflow", wf_name):
            # Reaffirm active without clobbering the rest of the definition.
            try:
                frappe.db.set_value("Workflow", wf_name, "is_active", 1)
            except Exception:
                frappe.log_error(f"gege_hr workflow seed: reactivate {wf_name}")
            # Bring NEW blueprint transitions into an already-seeded workflow
            # (B-1 upsert — seeding itself is insert-if-absent and would never
            # deliver the fix to an existing site).
            transitions_added += _ensure_transition_rows(wf_name, spec["transitions"])
            continue
        try:
            doc = frappe.get_doc(
                {
                    "doctype": "Workflow",
                    "workflow_name": wf_name,
                    "document_type": spec["doctype"],
                    "workflow_state_field": "workflow_state",
                    "is_active": 1,
                    "send_email_alert": 0,
                    "states": spec["states"],
                    "transitions": spec["transitions"],
                }
            )
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
            created.append(wf_name)
        except Exception:
            frappe.log_error(f"gege_hr workflow seed: failed to create {wf_name}")

    return {"seeded": created, "transitions_added": transitions_added}


def _ensure_transition_rows(workflow_name: str, wanted_rows: list[dict]) -> int:
    """Append any MISSING transition rows to an existing Workflow (B-1 upsert).

    The seed is insert-if-absent by design ("re-runs never overwrite a manager's
    manual edits"), so a transition added to the blueprint after a site already
    seeded would never land. This upsert appends only rows whose
    ``(state, action, next_state, allowed)`` key is absent — it never deletes
    or edits anything. Returns the number of rows added (0 on no-op / failure).
    """
    if not wanted_rows:
        return 0
    try:
        wf = frappe.get_doc("Workflow", workflow_name)
    except Exception:
        return 0
    have = {
        (t.get("state"), t.get("action"), t.get("next_state"), t.get("allowed"))
        for t in (wf.transitions or [])
    }
    added = 0
    for row in wanted_rows:
        key = (row.get("state"), row.get("action"), row.get("next_state"), row.get("allowed"))
        if key in have:
            continue
        wf.append("transitions", dict(row))
        added += 1
    if not added:
        return 0
    try:
        wf.flags.ignore_permissions = True
        wf.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(f"gege_hr workflow seed: ensure transitions failed for {workflow_name}")
        return 0
    return added


def verify_workflows() -> dict:
    """Diagnostic: confirm the workflows are attached to each doctype meta
    and that re-seeding is a no-op. Safe to call via ``bench execute``.

    >>> bench --site <site> execute gege_hr.gege_hr.setup_workflows.verify_workflows
    """
    out: dict = {
        "workflows": [r.name for r in frappe.get_all("Workflow", fields=["name"])],
        "meta_workflow": {},
    }
    for dt in (
        "VN Overtime Request",
        "VN Attendance Correction Request",
        "VN Salary Advance Request",
    ):
        wf = frappe.get_meta(dt).get_workflow()
        out["meta_workflow"][dt] = wf  # name string (or None)
    out["reseed"] = seed_workflows()  # idempotency check (no-op)
    return out
