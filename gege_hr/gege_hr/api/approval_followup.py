"""Approval follow-up jobs — desk-free Phase B3 (plans/approvals-deskfree-complete §3.6).

Two scheduler jobs keep the unified approval inbox from silently rotting:

* ``send_pending_digests``  (cron 08:00 portal) — one email per approver:
  "N requests pending, oldest = X". Skips entirely when the digest is off.
* ``escalate_stale_requests`` (cron 09:00 portal) — requests pending past
  ``vn_approval_stale_hours`` (default 72h) get a reminder to the CURRENT
  step's concrete holders; past ``vn_approval_escalate_hours`` (default 120h)
  HR Managers are escalated too.

Scope note: follow-up targets matrix-driven steps whose holder resolves to a
CONCRETE user (Line Manager / Department Head / Specific User — the same
contract as delegation). Role-based holders (HR User / HR Manager / Specific
Role) are broad sets by design and stay outside per-person nagging; their
queue health is visible in the inbox + stats instead.

Both jobs are bench-guarded: any failure is logged and never raises (a broken
scheduler tick must not take the inbox down). Settings live on
``VN HR Portal Setting`` (custom fields) and are read fail-soft with defaults.
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.api import approval as approval_api
from gege_hr.gege_hr.utils import approval as rules

_SETTING_DOCTYPE = "VN HR Portal Setting"


def _flag(field: str, default: int = 1) -> int:
    try:
        val = frappe.db.get_single_value(_SETTING_DOCTYPE, field)
    except Exception:
        return default
    if val is None or val == "":
        return default
    return 1 if str(val) in ("1", "True", "true", "Yes") else 0


def _int_setting(field: str, default: int) -> int:
    try:
        val = frappe.db.get_single_value(_SETTING_DOCTYPE, field)
    except Exception:
        return default
    try:
        return max(1, int(val or default))
    except (TypeError, ValueError):
        return default


def _pending_rows_by_type(limit_per_type: int = 1000):
    """Yield ``(transaction_type, row)`` for every pending request row."""
    for ttype in rules.supported_types():
        cfg = rules.TRANSACTION_CONFIG.get(ttype) or {}
        doctype = cfg.get("doctype")
        status_field = cfg.get("status_field")
        if not doctype or not status_field:
            continue
        try:
            rows = frappe.db.get_all(
                doctype,
                filters=[[status_field, "in", cfg.get("pending_states") or []]],
                fields=["name", "employee", "employee_name", "company", "creation", status_field],
                limit_page_length=limit_per_type,
            )
        except Exception:
            continue
        for row in rows:
            yield ttype, row


def _row_holders(row: dict, ttype: str, _cache: dict) -> list[str]:
    """Concrete users holding the row's current step (fail-soft → [])."""
    try:
        cfg = rules.TRANSACTION_CONFIG[ttype]
        matrices = approval_api._load_matrices(ttype, row.get("company"))
        attrs = approval_api._employee_attrs(row.get("employee"))
        matrix = rules.pick_matrix(matrices, attrs) if matrices else None
        if not matrix:
            return []
        step = rules.current_step(matrix, row.get(cfg["status_field"]))
        if not step:
            return []
        return rules.step_holder_users(
            step,
            line_manager_user=attrs.get("line_manager_user"),
            dept_head_user=attrs.get("dept_head_user"),
        )
    except Exception:
        return []


def _send(recipients: list[str], subject: str, message: str) -> bool:
    if not recipients:
        return False
    try:
        frappe.sendmail(recipients=recipients, subject=subject, message=message)
        return True
    except Exception:
        try:
            frappe.log_error(title="gege_hr approval followup mail failed", message=subject)
        except Exception:
            pass
        return False


def send_pending_digests() -> dict:
    """Daily digest — one mail per concrete step holder (AD20/AD23)."""
    if not _flag("vn_approval_digest_enabled", 1):
        return {"skipped": "digest disabled"}
    agg: dict[str, dict] = {}
    _cache: dict = {}
    for ttype, row in _pending_rows_by_type():
        for holder in _row_holders(row, ttype, _cache):
            slot = agg.setdefault(holder, {"count": 0, "oldest": "", "oldest_label": ""})
            slot["count"] += 1
            created = str(row.get("creation") or "")
            if not slot["oldest"] or created < slot["oldest"]:
                slot["oldest"] = created
                slot["oldest_label"] = (
                    f"{row.get('employee_name') or row.get('employee') or '?'}"
                    f" · {ttype} {row.get('name')}"
                )
    sent = 0
    for user, slot in agg.items():
        ok = _send(
            [user],
            f"Bạn có {slot['count']} yêu cầu chờ duyệt",
            (
                f"Hộp duyệt của bạn đang có {slot['count']} yêu cầu chờ xử lý.<br>"
                f"Chờ lâu nhất: {slot['oldest_label'] or '—'} (từ {slot['oldest'] or '—'}).<br>"
                "Xem chi tiết tại hộp duyệt trên cổng HR."
            ),
        )
        sent += 1 if ok else 0
    return {"sent": sent, "approvers": sorted(agg.keys())}


def _users_with_role(role: str) -> list[str]:
    try:
        return frappe.get_all(
            "Has Role",
            filters={"role": role, "parenttype": "User"},
            pluck="parent",
        )
    except Exception:
        return []


def _hours_since(creation, now) -> float:
    try:
        dt = frappe.utils.get_datetime(creation)
        return max(0.0, (now - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def escalate_stale_requests() -> dict:
    """SLA reminders + HR escalation for long-pending requests (AD21/AD22)."""
    stale_h = _int_setting("vn_approval_stale_hours", 72)
    escalate_h = _int_setting("vn_approval_escalate_hours", 120)
    try:
        now = frappe.utils.now_datetime()
    except Exception:
        return {"skipped": "no clock"}

    reminded = 0
    escalated = 0
    hr_managers: list[str] = []
    _cache: dict = {}
    for ttype, row in _pending_rows_by_type():
        age_h = _hours_since(row.get("creation"), now)
        if age_h < stale_h:
            continue
        holders = _row_holders(row, ttype, _cache)
        label = f"{row.get('employee_name') or row.get('employee') or '?'} · {ttype} {row.get('name')}"
        if holders and _send(
            holders,
            f"Nhắc SLA: yêu cầu chờ duyệt hơn {int(age_h)} giờ",
            f"Yêu cầu {label} đã chờ hơn {int(age_h)} giờ. Vui lòng xử lý.",
        ):
            reminded += 1
        if age_h >= escalate_h:
            if not hr_managers:
                hr_managers = _users_with_role("HR Manager")
            if hr_managers and _send(
                hr_managers,
                f"Escalate: yêu cầu chờ duyệt hơn {int(age_h)} giờ",
                f"Yêu cầu {label} đã chờ quá {escalate_h} giờ và chưa được xử lý.",
            ):
                escalated += 1
    return {"reminded": reminded, "escalated": escalated}
