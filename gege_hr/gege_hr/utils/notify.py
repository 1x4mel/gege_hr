"""
Notification producer helpers — plan v5 §10.9 / §14 / doctype-design §25.

The **VN Notification** inbox (DocType + ``api/notification.py`` + the
``hr-ui`` ``NotificationBell``) existed since session 20, but *nothing wrote
notification rows*. This module is the missing producer: pure payload builders
plus a bench-guarded ``push_notification`` that creates the VN Notification
document and fires a realtime event so the bell badge refreshes instantly.

Design (mirrors the ``utils/calc.py`` / ``utils/advance.py`` convention):

* Pure helpers (``build_notification_payload`` / ``notification_type_for`` /
  ``resolve_user_for_employee`` / ``notify_request_outcome`` payload) are
  side-effect free and bench-free → unit-tested outside a Frappe site.
* ``push_notification`` is the only frappe-aware entry point. It resolves the
  target user, inserts a VN Notification row (best-effort, ``log_error`` on
  failure — never aborts the caller's transition) and publishes a realtime
  ``hr-portal:{employee_id}`` tickle consumed by ``useNotification``.
"""

from __future__ import annotations

from typing import Any

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover - outside bench
    frappe = None


# --------------------------------------------------------------------------- #
# Notification type vocabulary (matches VN Notification.notification_type)
# --------------------------------------------------------------------------- #
NOTIFICATION_TYPES = (
    "Check-in",
    "Check-out",
    "Leave",
    "OT",
    "Correction",
    "Advance",
    "Shift Change",
    "Payroll",
    "Alert",
    "System",
)

# Transaction-type (from utils/approval.py TRANSACTION_CONFIG) → notification_type.
_TRANSACTION_TO_TYPE = {
    "Leave Application": "Leave",
    "Overtime Request": "OT",
    "Correction Request": "Correction",
    "Salary Advance Request": "Advance",
    # services-deskfree P1c — inbox decisions trên 2 doctype này.
    "Employee Grievance": "Alert",
    "Travel Request": "Alert",
}

# Terminal/progress outcomes → (title template, message template).
# ``{label}`` is the friendly request label, ``{state}`` the resulting state.
_OUTCOME_TEMPLATES = {
    "approved": (
        "{label} đã được duyệt",
        "Yêu cầu {label} của bạn đã được duyệt ({state}).",
    ),
    "rejected": (
        "{label} bị từ chối",
        "Yêu cầu {label} của bạn đã bị từ chối ({state}).",
    ),
    "submitted": (
        "{label} đang chờ duyệt",
        "Yêu cầu {label} của bạn đã gửi và đang chờ phê duyệt.",
    ),
    # Desk-free Phase B1 (plans/approvals-deskfree-complete §3.4) — approver
    # bounced the request back to Draft for the employee to fix + resend.
    "returned": (
        "{label} bị trả lại để sửa",
        "Yêu cầu {label} của bạn bị trả lại để chỉnh sửa rồi gửi lại.",
    ),
}


def notification_type_for(transaction_type: str | None) -> str:
    """Map an approval transaction type to a VN Notification type (fallback Alert)."""
    if not transaction_type:
        return "Alert"
    return _TRANSACTION_TO_TYPE.get(transaction_type, "Alert")


def is_valid_notification_type(value: str | None) -> bool:
    return bool(value) and value in NOTIFICATION_TYPES


def build_notification_payload(
    *,
    employee: str | None,
    notification_type: str,
    title: str,
    message: str,
    user: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    action_url: str | None = None,
) -> dict[str, Any]:
    """Compose the field dict for a new VN Notification (pure, no frappe).

    ``user`` is optional: when omitted the caller resolves it from the employee
    inside ``push_notification`` (which needs a live bench). The payload is
    intentionally tolerant — empty strings rather than ``None`` — so it can be
    passed straight to ``frappe.get_doc({...}).insert()``.
    """
    ntype = notification_type if is_valid_notification_type(notification_type) else "Alert"
    return {
        "doctype": "VN Notification",
        "employee": employee or "",
        "user": user or "",
        "notification_type": ntype,
        "title": (title or "").strip(),
        "message": (message or "").strip(),
        "reference_doctype": reference_doctype or "",
        "reference_name": reference_name or "",
        "read": 0,
        "action_url": action_url or "",
    }


def build_request_outcome_payload(
    *,
    transaction_type: str,
    name: str,
    employee: str | None,
    outcome: str,
    state: str | None = None,
    label: str | None = None,
    user: str | None = None,
    action_url: str | None = None,
) -> dict[str, Any]:
    """Build a notification payload for an approval-routed request outcome.

    ``outcome`` ∈ {"approved", "rejected", "submitted"}. ``label`` defaults to a
    friendly request label derived from the transaction type.
    """
    outcome = (outcome or "").strip().lower()
    if outcome not in _OUTCOME_TEMPLATES:
        outcome = "submitted"
    title_tpl, msg_tpl = _OUTCOME_TEMPLATES[outcome]
    if not label:
        label = _LABEL_FALLBACK.get(transaction_type) or transaction_type or "Yêu cầu"
    title = title_tpl.format(label=label)
    message = msg_tpl.format(label=label, state=state or "")
    return build_notification_payload(
        employee=employee,
        notification_type=notification_type_for(transaction_type),
        title=title,
        message=message,
        user=user,
        reference_doctype=transaction_type,
        reference_name=name,
        action_url=action_url,
    )


# Friendly Vietnamese labels (mirror utils/approval.TYPE_LABELS to stay bench-free).
_LABEL_FALLBACK = {
    "Leave Application": "nghỉ phép",
    "Overtime Request": "tăng ca",
    "Correction Request": "điều chỉnh công",
    "Salary Advance Request": "tạm ứng lương",
}


# --------------------------------------------------------------------------- #
# Bench-aware producer (the only frappe entry point)
# --------------------------------------------------------------------------- #
def _table_ready(doctype: str) -> bool:
    """True when the VN Notification table exists (guard for fresh installs)."""
    if frappe is None:
        return False
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:  # pragma: no cover - defensive
        return False


def resolve_user_for_employee(employee: str | None) -> str | None:
    """Resolve the ``User`` linked to an Employee (via ``user_id``). Bench-only."""
    if frappe is None or not employee:
        return None
    try:
        return frappe.db.get_value("Employee", employee, "user_id")
    except Exception:  # pragma: no cover - defensive
        return None


def push_notification(
    *,
    employee: str | None,
    notification_type: str,
    title: str,
    message: str,
    user: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    action_url: str | None = None,
    realtime_channel: bool = True,
) -> str | None:
    """Create a VN Notification row + fire a realtime tickle. Best-effort.

    Returns the new notification name, or ``None`` when the table is missing or
    no resolvable target user exists. Any error is logged via ``log_error`` and
    swallowed so a notification failure never breaks a domain transition.
    """
    if frappe is None or not _table_ready("VN Notification"):
        return None
    if not employee:
        return None
    target_user = user or resolve_user_for_employee(employee)
    payload = build_notification_payload(
        employee=employee,
        notification_type=notification_type,
        title=title,
        message=message,
        user=target_user,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        action_url=action_url,
    )
    name = None
    try:
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=True)
        name = doc.name
    except Exception:
        try:
            frappe.log_error(
                title="gege_hr: push_notification failed",
                message=f"employee={employee} type={notification_type}",
            )
        except Exception:  # pragma: no cover - defensive
            pass
        return None

    if realtime_channel:
        _tickle(employee, name)
    return name


def push_request_outcome(
    *,
    transaction_type: str,
    name: str,
    employee: str | None,
    outcome: str,
    state: str | None = None,
    label: str | None = None,
    action_url: str | None = None,
) -> str | None:
    """Convenience wrapper: build a request-outcome payload then push it."""
    payload = build_request_outcome_payload(
        transaction_type=transaction_type,
        name=name,
        employee=employee,
        outcome=outcome,
        state=state,
        label=label,
        action_url=action_url,
    )
    return push_notification(
        employee=payload["employee"],
        notification_type=payload["notification_type"],
        title=payload["title"],
        message=payload["message"],
        user=payload["user"] or None,
        reference_doctype=payload["reference_doctype"] or None,
        reference_name=payload["reference_name"] or None,
        action_url=payload["action_url"] or None,
    )


def _tickle(employee: str | None, notification_name: str | None) -> None:
    """Publish a realtime event so the bell badge refreshes instantly.

    The FE ``useNotification.subscribeRealtime`` listens on a socket event named
    ``hr-portal:{employee_id}``, so we publish with exactly that event name and
    target the employee's user room (no broadcast to others).
    """
    if frappe is None or not employee:
        return
    try:
        target_user = resolve_user_for_employee(employee)
        if not target_user:
            # user=None broadcasts to EVERY connected socket — an unlinked
            # employee must not leak the event (and payload) site-wide.
            return
        frappe.publish_realtime(
            event=f"hr-portal:{employee}",
            message={"employee": employee, "name": notification_name},
            user=target_user,
        )
    except Exception:  # pragma: no cover - defensive
        pass
