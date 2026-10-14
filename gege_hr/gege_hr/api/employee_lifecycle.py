"""WP3 (prod-readiness-plan, F-LC17 prod-side) — Employee lifecycle guards.

Two DocType hooks wired from ``hooks.py``:

* ``guard_employee_delete`` (Employee.on_trash) — BLOCK deleting an Employee
  that still has attendance data (Checkin / Shift Assignment / Shift Instance /
  Work Session / Checkout Miss). A dangling Shift Assignment once broke the
  WHOLE company's materialisation (64 orphan rows); deletion is refused with
  clear guidance to use the "Nghỉ việc" flow instead (set status=Left via
  :func:`handle_employee_status_change`).

* ``handle_employee_status_change`` (Employee.on_update) — when status flips
  Active → Left: end the active Shift Assignments (end_date = relieving_date or
  today) and drop the Shift Instances AFTER that date that have no real
  check-in data (with their empty Work Sessions / draft Attendance); days with
  real data are kept for payroll audit. The scheduler then stops materialising
  shifts for a departed employee.
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


def _was_left_before(doc) -> bool:
    """Trạng thái TRƯỚC lần lưu này có phải ``Left`` không.

    Hook chạy ở ``on_update`` — lúc đó DB đã mang giá trị mới, nên đọc ``Employee.status`` từ DB luôn thấy
    ``Left`` và hook tự coi là "đã nghỉ từ trước" rồi bỏ qua. Hậu quả tới 13/10/2026: cho nghỉ việc mà phân ca vẫn
    Active, lịch ca vẫn được sinh tiếp. Phải hỏi bản trước khi lưu (``get_doc_before_save``).
    """
    getter = getattr(doc, "get_doc_before_save", None)
    before = getter() if callable(getter) else None
    if before is None:
        return False
    return (getattr(before, "status", None) or "") == "Left"


def _drop_shift_data_after(employee: str, end_on) -> dict:
    """Sau ngày nghỉ việc: bỏ lịch ca CHƯA có giờ vào / ra thật, kèm phiên chấm công rỗng và bản nháp điểm danh.

    Ngày nghỉ có thể lùi về quá khứ (báo nghỉ muộn) — các ngày từ đó tới nay nếu để lại sẽ hiện "vắng" cho người đã
    nghỉ. Ngày có giờ vào / ra thật là lịch sử chấm công → giữ nguyên (HR tự quyết). Không dùng ``force``.
    """
    out = {"instances": 0, "sessions": 0, "attendance": 0, "kept": []}
    instances = frappe.db.get_all(
        "VN Employee Shift Instance",
        filters={"employee": employee, "docstatus": 1, "work_date": [">", end_on]},
        fields=["name", "work_date"],
    )
    sessions_of = {
        inst.get("name"): frappe.db.get_all(
            "VN Attendance Work Session",
            filters={"shift_instance": inst.get("name")},
            fields=["name", "actual_checkin", "actual_checkout"],
        )
        for inst in instances
    }
    kept = {
        str(inst.get("work_date"))
        for inst in instances
        if any(s.get("actual_checkin") or s.get("actual_checkout") for s in sessions_of[inst.get("name")])
    }
    out["kept"] = sorted(kept)
    todo = [inst for inst in instances if str(inst.get("work_date")) not in kept]
    # Bản nháp điểm danh và phiên chấm công trỏ VÒNG vào nhau (WS.attendance ↔ Attendance.vn_work_session) nên
    # Frappe không cho xoá cái nào trước → gỡ chiều WS → Attendance, xoá bản nháp, rồi mới xoá phiên.
    for inst in todo:
        for s in sessions_of[inst.get("name")]:
            frappe.db.set_value("VN Attendance Work Session", s.get("name"), "attendance", None)
    drafts = frappe.db.get_all(
        "Attendance",
        filters={"employee": employee, "docstatus": 0, "attendance_date": [">", end_on]},
        fields=["name", "attendance_date"],
    )
    for att in drafts:
        if str(att.get("attendance_date")) in kept:
            continue
        frappe.delete_doc("Attendance", att.get("name"), ignore_permissions=True)
        out["attendance"] += 1
    for inst in todo:
        for s in sessions_of[inst.get("name")]:
            frappe.delete_doc("VN Attendance Work Session", s.get("name"), ignore_permissions=True)
            out["sessions"] += 1
        si_doc = frappe.get_doc("VN Employee Shift Instance", inst.get("name"))
        si_doc.flags.ignore_permissions = True
        si_doc.cancel()
        frappe.delete_doc("VN Employee Shift Instance", inst.get("name"), ignore_permissions=True)
        out["instances"] += 1
    return out


def handle_employee_status_change(doc, method: str | None = None) -> None:
    """Active → Left: kết thúc phân ca vào ngày nghỉ + bỏ lịch ca rỗng sau ngày nghỉ.

    Phiên chấm công có dữ liệu thật được GIỮ (đối soát lương). Chỉ chạy đúng lần lưu chuyển trạng thái.
    """
    if (doc.status or "") != "Left":
        return
    if _was_left_before(doc):
        return

    end_on = getattr(doc, "relieving_date", None) or getattr(doc, "custom_relieving_date", None)
    if not end_on:
        end_on = frappe.utils.today()

    # 1) Kết thúc phân ca đang Active để job sinh lịch ca hằng ngày bỏ qua người này.
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

    # 2) Bỏ lịch ca sau ngày nghỉ (kể cả các ngày đã qua nếu ngày nghỉ lùi về trước); giữ ngày có chấm công thật.
    try:
        _drop_shift_data_after(doc.name, end_on)
    except Exception:
        frappe.log_error(title=f"employee Left: drop shift data failed {doc.name}")

    try:
        frappe.logger().info(
            f"[gege_hr] Employee {doc.name} → Left: SAs ended, shift data after {end_on} dropped"
        )
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
