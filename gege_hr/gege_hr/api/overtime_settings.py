"""
Overtime admin settings — the two knobs HR needs WITHOUT touching the Frappe Desk
(plan §11.3 config gaps):

  * ``get_ot_settings``            — can the Employee role submit OT? + the active
                                    policy's ``max_overtime_hours_per_shift`` cap.
  * ``enable_employee_ot_submission`` — grant/revoke the Employee role create/write
                                    on ``VN Overtime Request`` (Custom DocPerm, the
                                    same record type the Role Permissions Manager
                                    writes; ``gege_hr.api._assert_own`` still scopes
                                    each employee to their own docs at the app layer).
  * ``set_ot_cap``                 — set the active ``VN Attendance Policy``'s
                                    ``max_overtime_hours_per_shift`` (the cap the OT
                                    request ``validate`` enforces when a
                                    ``shift_instance`` is supplied — EC-1).

Every call is HR-admin gated (``frappe.only_for(HR_ADMIN_ROLES)``) — defense in
depth on top of the role permission it manages. Mirrors ``api/admin.py`` +
``api/setup_permissions.py`` conventions.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

HR_ADMIN_ROLES = ["HR Manager", "System Manager"]
DOCTYPE = "VN Overtime Request"
EMPLOYEE = "Employee"
POLICY = "VN Attendance Policy"


def _require_hr_admin() -> None:
    frappe.only_for(HR_ADMIN_ROLES)


def _active_policy_name() -> str | None:
    """The most-recently-modified active VN Attendance Policy, or None."""
    try:
        return frappe.db.get_value(POLICY, {"is_active": 1}, "name", order_by="modified desc")
    except Exception:
        return None


def _effective_employee_can_create() -> bool:
    """Can a real Employee-role user actually CREATE an OT request?

    Reflects the FULL Frappe permission resolution (role merge + ``if_owner`` +
    workflow) for an actual Employee user — NOT the raw DocPerm rows. This matters
    because ``VN Overtime Request`` ships a standard Employee DocPerm with
    ``if_owner=1`` that alone does NOT let an employee submit (the workflow gates
    it); "enabling" adds an ``if_owner=0`` Custom DocPerm that does. Reading the
    raw rows would always see create=1 and never flip the toggle to off.
    """
    try:
        user = frappe.db.get_value(
            "Has Role",
            {"role": EMPLOYEE, "parenttype": "User"},
            "parent",
        )
    except Exception:
        user = None
    if not user:
        return False
    return bool(frappe.has_permission(DOCTYPE, "create", user=user))


def _revoke_employee_ot_perm() -> None:
    """Drop the Employee Custom DocPerm on VN Overtime Request (if any)."""
    names = frappe.db.get_all(
        "Custom DocPerm",
        filters={"parent": DOCTYPE, "role": EMPLOYEE},
        pluck="name",
    )
    for name in names or []:
        try:
            frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True)
        except Exception:
            frappe.db.rollback()


@frappe.whitelist()
def get_ot_settings() -> dict:
    """Read the OT admin card state (HR admin only)."""
    _require_hr_admin()
    policy_name = _active_policy_name()
    cap = None
    if policy_name:
        try:
            cap = flt(frappe.db.get_value(POLICY, policy_name, "max_overtime_hours_per_shift") or 0)
            cap = cap or None
        except Exception:
            cap = None
    return {
        "employee_can_submit_ot": _effective_employee_can_create(),
        "ot_cap_hours": cap,
        "policy_name": policy_name,
    }


@frappe.whitelist()
def enable_employee_ot_submission(enable: int | bool = 1) -> dict:
    """Grant (or revoke) the Employee role create/write/read on VN Overtime Request.

    Uses ``setup_permissions._upsert_perm`` so the record is the very ``Custom
    DocPerm`` Frappe's Role Permissions Manager writes — idempotent and re-applied
    on ``bench migrate`` (Employee is also in ``PERMISSION_MATRIX``). The gege_hr
    API still enforces ownership (``overtime._assert_own``) at the app layer.
    """
    _require_hr_admin()
    from gege_hr.gege_hr.api.setup_permissions import _ensure_role, _upsert_perm

    enable = bool(int(enable or 0))
    if enable:
        _ensure_role(EMPLOYEE)
        _upsert_perm(DOCTYPE, EMPLOYEE, {"read": 1, "write": 1, "create": 1})
        msg = _("Đã cấp quyền nộp đơn tăng ca cho nhân viên (chỉ với đơn của chính họ).")
    else:
        _revoke_employee_ot_perm()
        msg = _("Đã thu hồi quyền nộp đơn tăng ca của nhân viên.")
    frappe.clear_cache()
    return {
        "employee_can_submit_ot": _effective_employee_can_create(),
        "message": msg,
    }


@frappe.whitelist()
def set_ot_cap(max_hours=None) -> dict:
    """Set the active VN Attendance Policy's ``max_overtime_hours_per_shift``.

    ``max_hours`` empty/0 clears the cap (``validate`` skips when 0). Otherwise
    OT requests with ``requested_hours`` above this are rejected at submit.
    """
    _require_hr_admin()
    policy_name = _active_policy_name()
    if not policy_name:
        frappe.throw(_("Chưa có Chính sách chấm công (VN Attendance Policy) đang active."))
    val = flt(max_hours) if max_hours not in (None, "", "null") else 0
    if val < 0:
        frappe.throw(_("Giờ OT tối đa không được âm."))
    frappe.db.set_value(POLICY, policy_name, "max_overtime_hours_per_shift", val)
    frappe.db.commit()
    return {
        "ot_cap_hours": val or None,
        "policy_name": policy_name,
        "message": _("Đã lưu giờ OT tối đa/ca = {0} cho chính sách {1}.").format(val or 0, policy_name),
    }


def validate_shift_ot_threshold(doc, method=None) -> None:
    """``Shift Type`` validate hook — keep the two "OT max" settings consistent.

    The per-shift OT review threshold (``vn_max_overtime_hours`` — the value that
    flags a Work Session ``need_review`` when actual OT exceeds it) must NOT
    exceed the active policy's ``max_overtime_hours_per_shift`` (the global
    ceiling that hard-caps an OT request at submission). A shift may be TIGHTER
    (flag review sooner) but never looser than the max an employee may even
    request — otherwise the shift setting is dead config and the two "OT tối đa"
    labels mislead HR (plan §11.5: "ca không vượt mức tổng")."""
    try:
        thr = float(doc.get("vn_max_overtime_hours") or 0)
    except (TypeError, ValueError):
        return
    if thr <= 0:
        return  # 0 = threshold disabled; nothing to enforce.
    policy_name = _active_policy_name()
    if not policy_name:
        return  # No active policy → no ceiling to enforce against.
    try:
        cap = float(frappe.db.get_value(POLICY, policy_name, "max_overtime_hours_per_shift") or 0)
    except Exception:
        return
    if cap > 0 and thr > cap:
        frappe.throw(
            _(
                "Ngưỡng OT cần duyệt lại/ca của ca ({0}h) vượt mức trần OT tối đa của chính sách ({1}h). "
                "Hãy giảm ngưỡng ở ca, hoặc tăng mức trần ở mục 'Cài đặt tăng ca'."
            ).format(thr, cap),
            frappe.ValidationError,
        )


def validate_shift_checkout_window(doc, method=None) -> None:
    """``Shift Type`` validate hook — keep the two "check-out after shift" knobs
    consistent (plan §11.5).

    Two tiers gate a check-out that lands after the shift end:
      * ``vn_latest_checkout_minutes``   — end of the *normal* check-out window
        (a check-out within this is on-time / in-window).
      * ``vn_max_checkout_after_end_minutes`` — the *late-checkout warning*
        threshold (a check-out beyond this raises a "Late Checkout" exception;
        see ``calc._maybe_raise_exceptions``).

    The normal window must END before the late-warning threshold, i.e.
    ``vn_latest_checkout_minutes ≤ vn_max_checkout_after_end_minutes``. Otherwise
    a check-out could be simultaneously "in the normal window" AND "late" —
    contradictory. Current sensible values: 60 ≤ 360."""
    try:
        latest_out = int(doc.get("vn_latest_checkout_minutes") or 0)
        max_after = int(doc.get("vn_max_checkout_after_end_minutes") or 0)
    except (TypeError, ValueError):
        return
    if latest_out > 0 and max_after > 0 and latest_out > max_after:
        frappe.throw(
            _(
                "Check-out muộn nhất / cửa sổ thường ({0} phút) phải ≤ Giới hạn check-out trễ ({1} phút). "
                "Cửa sổ check-out thường phải kết thúc trước ngưỡng cảnh báo check-out trễ."
            ).format(latest_out, max_after),
            frappe.ValidationError,
        )
