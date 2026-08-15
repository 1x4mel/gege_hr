"""Helpers that bridge the request-creation endpoints and the Frappe Workflows
seeded by [`setup_workflows`](gege_hr/gege_hr/gege_hr/setup_workflows.py:1).

A freshly inserted request is in the ``Draft`` state (the Workflow's first
state). To make it actionable in the Approval Inbox it must move to
``Pending Manager``. Because the inbox gates approvals by the VN Approval
Matrix — not by Frappe roles — and Frappe's ``validate_workflow`` only recognises
transitions defined on the active Workflow, we move the doc in a second,
best-effort ``save()``: when the Workflow is present the ``Draft → Pending
Manager`` transition validates cleanly; when it is absent (e.g. before seeding or
outside bench) the plain field write still sticks and nothing throws.
"""

from __future__ import annotations

import frappe

PENDING_MANAGER = "Pending Manager"


def send_for_approval(doc) -> str:
    """Move a just-inserted ``Draft`` request to ``Pending Manager``.

    Returns the resulting ``workflow_state``. Failures (e.g. a stale/edited
    workflow missing the transition) are logged but never abort request
    creation — the doc simply stays ``Draft`` and can be re-submitted.
    """
    current = doc.get("workflow_state")
    try:
        if current and current != PENDING_MANAGER and doc.docstatus < 1:
            doc.workflow_state = PENDING_MANAGER
            doc.save(ignore_permissions=True)
    except Exception:
        # Workflow not seeded / transition removed by a manager — keep Draft.
        # F17: reset the in-memory state too, otherwise the API reported the
        # "Pending Manager" value off the failed object while the DB row was
        # still Draft (employee believes it was submitted; inbox never sees it).
        doc.workflow_state = current
        try:
            frappe.log_error(f"gege_hr: send_for_approval failed for {doc.doctype} {doc.get('name')}")
        except Exception:
            pass
    return doc.get("workflow_state")
