"""WP3 (prod-readiness-plan, F-LC17 prod-side) — Employee lifecycle guards.

Two DocType hooks wired from ``hooks.py``:

* ``guard_employee_delete`` (Employee.on_trash) — BLOCK deleting an Employee
  that still has attendance data (Checkin / Shift Assignment / Shift Instance /
  Work Session / Checkout Miss). A dangling Shift Assignment once broke the
  WHOLE company's materialisation (64 orphan rows); deletion is refused with
  clear guidance to use the "Nghỉ việc" flow instead (set status=Left via
  :func:`handle_employee_status_change`).

* ``handle_employee_status_change`` (Employee.on_update) — when status flips
  Active → Left: end the active Shift Assignments (end_date = relieved_date or
  today), cancel FUTURE Shift Instances (history Work Sessions are kept for
  payroll audit), so the scheduler stops materialising shifts for a departed
  employee.
"""

from __future__ import annotations

import frappe
from frappe import _

# Doctypes that reference an Employee and would dangle after a hard delete.
_GUARD_DOCTYPES = (
    ("Employee Checkin", "employee"),
    ("Shift Assignment", "employee"),
    ("VN Employee Shift Instance", "employee"),
    ("VN Attendance Work Session", "employee"),
    ("VN Checkout Miss", "employee"),
)


def guard_employee_delete(doc, method: str | None = None) -> None:
    """Refuse Employee deletion while attendance data still exists (MH3)."""
    blockers: list[str] = []
    for doctype, field in _GUARD_DOCTYPES:
        try:
            n = frappe.db.count(doctype, {field: doc.name}) or 0
        except Exception:
            # Table missing (fresh install / partial deploy) → nothing to guard.
            continue
        if n:
            blockers.append(f"{doctype}: {n}")
    if not blockers:
        return
    frappe.throw(
        _(
            "Không thể xoá nhân viên còn dữ liệu chấm công ({0}). "
            "Dùng luồng 'Nghỉ việc' (đặt trạng thái Left) để giữ lịch sử lương — "
            "xoá cứng sẽ để lại Shift Assignment / Work Session treo."
        ).format("; ".join(blockers)),
        frappe.ValidationError,
    )


def handle_employee_status_change(doc, method: str | None = None) -> None:
    """Active → Left: end Shift Assignments + cancel future Shift Instances.

    Historical Work Sessions are INTENTIONALLY kept (payroll audits need
    them). Idempotent — safe on every save, acts only on the flip.
    """
    if (doc.status or "") != "Left":
        return
    # Only act when this save is the flip (db_value None → not previously Left).
    try:
        prev = frappe.db.get_value("Employee", doc.name, "status")
    except Exception:
        prev = None
    if prev == "Left":
        return

    end_on = getattr(doc, "relieving_date", None) or getattr(doc, "custom_relieving_date", None)
    if not end_on:
        end_on = frappe.utils.today()

    # 1) End active Shift Assignments so the daily generator skips them.
    try:
        active = frappe.db.get_all(
            "Shift Assignment",
            filters={"employee": doc.name, "docstatus": 1, "status": "Active"},
            pluck="name",
        )
        for sa in active:
            try:
                frappe.db.set_value("Shift Assignment", sa, {"end_date": end_on, "status": "Inactive"})
            except Exception:
                frappe.log_error(title=f"employee Left: end SA failed {sa}")
    except Exception:  # noqa: BLE001 — partial install tolerance
        pass

    # 2) Cancel FUTURE Shift Instances (today onwards); keep history.
    try:
        future = frappe.db.get_all(
            "VN Employee Shift Instance",
            filters={
                "employee": doc.name,
                "docstatus": 1,
                "work_date": [">=", frappe.utils.today()],
            },
            pluck="name",
        )
        for si in future:
            try:
                si_doc = frappe.get_doc("VN Employee Shift Instance", si)
                si_doc.cancel()
                si_doc.delete(ignore_permissions=True, force=True)
            except Exception:
                frappe.log_error(title=f"employee Left: cancel SI failed {si}")
    except Exception:
        pass

    try:
        frappe.logger().info(f"[gege_hr] Employee {doc.name} → Left: SAs ended, future SIs cancelled")
    except Exception:
        pass


def guard_reports_to_cycle(doc, method: str | None = None) -> None:
    """``Employee.validate`` (doc_events) — chặn chuỗi reports_to tạo vòng lặp.

    plan-employee-frontend-parity §2.5: một vòng A→B→A từng khiến org chart
    và duyệt đơn treo vĩnh viễn. Đi ngược chuỗi quản lý tối đa 200 cấp — gặp
    lại chính mình thì throw (message tiếng Việt kèm chuỗi để HR sửa).
    """
    from gege_hr.gege_hr.api.employee_profile import detect_cycle

    reports_to = str(getattr(doc, "reports_to", "") or "").strip()
    if not reports_to:
        return
    if reports_to == doc.name:
        frappe.throw(_("Nhân viên không thể báo cáo cho chính mình."))
    try:
        rows = frappe.get_all("Employee", fields=["name", "reports_to"], limit_page_length=0)
    except Exception:
        return  # partial install — không chặn save vì meta thiếu
    chain = {r.name: r.reports_to for r in rows if r.get("name")}
    chain[doc.name] = reports_to
    cycle = detect_cycle(chain, doc.name)
    if cycle:
        frappe.throw(_("Chuỗi báo cáo tạo vòng lặp: {0}").format(" → ".join(cycle)))
