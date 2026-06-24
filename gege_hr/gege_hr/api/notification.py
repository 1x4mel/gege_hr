"""
Notification inbox API — plan v5 §10.9 / doctype-design §25.

Fronts the **VN Notification** DocType (an append-only, per-user inbox row).
The ``hr-ui`` ``useNotification`` composable calls three endpoints defined here:

* ``get_notifications`` → VN Notification rows for the caller's user/employee
* ``mark_read``         → mark one notification read (read=1, read_at=now)
* ``mark_all_read``     → mark every unread notification of the caller read

Rows are scoped to the logged-in user (``user`` field), so an Employee only
ever sees their own inbox. A pure helper (``_row``) shapes each row to the FE
contract documented in ``hr-ui/src/api/index.js``.
"""

from __future__ import annotations

from typing import Any

from gege_hr.gege_hr.utils import employee as emp_utils


# --------------------------------------------------------------------------- #
# Lazy ``frappe`` shims — defined first so endpoint decorators resolve at
# module-import time WITHOUT importing frappe (keeps the pure helpers unit-
# testable outside a bench, matching ``api/device.py``'s pattern).
# --------------------------------------------------------------------------- #
def frappe_whitelist():
    """``@frappe_whitelist()`` → real ``frappe.whitelist()`` in a bench, else a
    no-op marker decorator so the module still imports outside a bench."""
    try:
        import frappe

        return frappe.whitelist()
    except Exception:  # pragma: no cover - outside bench

        def _decorator(func):
            func.whitelisted = True
            return func

        return _decorator


def _(msg: str, *args, **kwargs) -> str:
    """Lazy translation marker — resolves to ``frappe._`` inside a bench."""
    try:
        import frappe

        return frappe._(msg, *args, **kwargs)
    except Exception:  # pragma: no cover
        if args or kwargs:
            try:
                return msg.format(*args, **kwargs)
            except Exception:
                return msg
        return msg


# Field projection — matches the FE row shape documented in api/index.js.
_NOTIFICATION_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "user",
    "notification_type",
    "title",
    "message",
    "reference_doctype",
    "reference_name",
    "read",
    "read_at",
    "action_url",
    "creation",
]


def _to_bool(value: Any) -> bool:
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        if isinstance(value, bool):
            return value
        return False


def _row(doc: dict) -> dict:
    """Normalise a VN Notification projection to the FE contract.

    Coerces ``read`` to a real boolean (Frappe returns 0/1 ints) and fills any
    missing field with a sane default so the frontend never sees ``undefined``.
    """
    return {
        "name": doc.get("name"),
        "employee": doc.get("employee"),
        "employee_name": doc.get("employee_name") or "",
        "user": doc.get("user") or "",
        "notification_type": doc.get("notification_type") or "",
        "title": doc.get("title") or "",
        "message": doc.get("message") or "",
        "reference_doctype": doc.get("reference_doctype") or "",
        "reference_name": doc.get("reference_name") or "",
        "read": _to_bool(doc.get("read")),
        "read_at": doc.get("read_at") or "",
        "action_url": doc.get("action_url") or "",
        "creation": doc.get("creation") or "",
    }


def _unread_only_flag(unread_only: Any) -> bool:
    """Coerce the FE ``unread_only`` (1/0/"true"/bool) into a real bool."""
    if isinstance(unread_only, bool):
        return unread_only
    try:
        return bool(int(unread_only))
    except (TypeError, ValueError):
        return str(unread_only).strip().lower() in ("true", "yes", "1")


def _coerce_int(value: Any, default: int) -> int:
    try:
        n = int(float(value))
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Endpoint: get_notifications
# --------------------------------------------------------------------------- #
@frappe_whitelist()
def get_notifications(
    unread_only: Any = 0,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Plan §10.9 — VN Notification rows for the caller's user/employee.

    Scoped to ``frappe.session.user`` (the ``user`` link field), so an Employee
    only ever reads their own inbox. HR roles are *not* elevated to read others'
    notifications here — the HR-side feeds (approval inbox, exceptions, etc.)
    have their own dedicated endpoints.
    """
    import frappe  # noqa: WPS433 - lazy

    user = frappe.session.user
    filters = [["user", "=", user]]
    if _unread_only_flag(unread_only):
        filters.append(["read", "=", 0])

    rows = (
        frappe.db.get_all(
            "VN Notification",
            filters=filters,
            fields=_NOTIFICATION_FIELDS,
            order_by="creation desc",
            limit_start=_coerce_int(offset, 0),
            limit_page_length=_coerce_int(limit, 50),
        )
        or []
    )
    return [_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# Endpoint: mark_read
# --------------------------------------------------------------------------- #
@frappe_whitelist()
def mark_read(name: str) -> dict:
    """Mark a single VN Notification as read (read=1, read_at=now).

    Enforces ownership: the caller's ``user`` must match the notification's
    ``user`` (an Employee cannot flip another employee's row). Returns an ack
    ``{ name, read, read_at }``.
    """
    import frappe  # noqa: WPS433 - lazy

    if not name:
        frappe.throw(_("Tên thông báo không được để trống."), frappe.ValidationError)

    doc = frappe.get_doc("VN Notification", name)
    _assert_owner(doc.user)

    if not doc.read:
        doc.read = 1
        doc.read_at = frappe.utils.now()
        doc.save()
        frappe.db.commit()

    return {
        "name": doc.name,
        "read": bool(doc.read),
        "read_at": doc.read_at or "",
    }


# --------------------------------------------------------------------------- #
# Endpoint: mark_all_read
# --------------------------------------------------------------------------- #
@frappe_whitelist()
def mark_all_read() -> dict:
    """Mark every unread notification of the caller as read.

    Uses a direct ``db.set_value`` loop (best-effort, batched) rather than
    loading each doc — an inbox can hold hundreds of rows. Returns
    ``{ marked: <count> }``.
    """
    import frappe  # noqa: WPS433 - lazy

    user = frappe.session.user
    now = frappe.utils.now()
    names = (
        frappe.db.get_all(
            "VN Notification",
            filters=[["user", "=", user], ["read", "=", 0]],
            pluck="name",
        )
        or []
    )

    marked = 0
    for name in names:
        try:
            frappe.db.set_value(
                "VN Notification",
                name,
                {"read": 1, "read_at": now},
                update_modified=False,
            )
            marked += 1
        except Exception:  # pragma: no cover - best-effort
            frappe.log_error(frappe.get_traceback(), _("mark_all_read failed: {0}").format(name))

    if marked:
        frappe.db.commit()

    return {"marked": marked}


# --------------------------------------------------------------------------- #
# Ownership guard
# --------------------------------------------------------------------------- #
def _assert_owner(notif_user: str | None) -> None:
    import frappe  # noqa: WPS433 - lazy

    if not notif_user:
        return  # legacy row without a user link — allow.
    roles = set(emp_utils.get_user_roles() or [])
    # HR/System Managers may manage any notification (bulk ops, corrections).
    if roles & emp_utils.HR_MANAGER_ROLES:
        return
    if notif_user != frappe.session.user:
        frappe.throw(
            _("Bạn không có quyền thay đổi thông báo của người khác."),
            frappe.PermissionError,
        )


__all__ = [
    "get_notifications",
    "mark_read",
    "mark_all_read",
    "_row",
    "_unread_only_flag",
]
