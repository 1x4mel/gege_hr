"""NEW-2 (hr-gap-audit 🟥) — Appraisal / Goals API.

Reuses Frappe HR's ``Appraisal Cycle`` / ``Appraisal`` / ``Goal`` / ``KRA``
DocTypes (no duplicate doctype) and exposes a DNA-compliant surface to the
portal: an employee lists/creates/tracks their Goals (progress → auto status) and
reads their Appraisals; an HR/Manager lists cycles + everyone's goals/appraisals.
Pure helpers are split out for unit testing (no frappe).
"""
from __future__ import annotations

import frappe

GOAL_DOCTYPE = "Goal"
CYCLE_DOCTYPE = "Appraisal Cycle"
APPRAISAL_DOCTYPE = "Appraisal"

_GOAL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "goal_name",
    "progress",
    "status",
    "start_date",
    "end_date",
    "appraisal_cycle",
    "kra",
]
_CYCLE_FIELDS = ["name", "cycle_name", "company", "start_date", "end_date", "status"]
_APPRAISAL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "appraisal_cycle",
    "appraisal_template",
    "status",
    "total_score",
]


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if employee != own:
        frappe.throw("Bạn chỉ xem được mục tiêu của chính mình.")


def status_for_progress(progress) -> str:
    """Auto status from completion % (0–100)."""
    p = _num(progress)
    if p >= 100:
        return "Completed"
    if p > 0:
        return "In Progress"
    return "Pending"


def goal_completion(goals) -> float:
    """Average progress (0–100) across a goal list. Empty → 0."""
    rows = list(goals or [])
    if not rows:
        return 0.0

    def _g(r):
        return _num(r.get("progress") if isinstance(r, dict) else getattr(r, "progress", None))

    return round(sum(_g(r) for r in rows) / len(rows), 1)


def _empty_summary() -> dict:
    return {"total": 0, "completed": 0, "in_progress": 0, "pending": 0, "completion": 0.0}


def _summarize_goals(rows) -> dict:
    """Server-aggregated status counts + average completion over a goal set.

    Computed over the FULL filtered set (not just the current page) so the
    summary tiles + ``completion`` stay correct as the user pages/filters.
    """
    items = list(rows or [])
    total = len(items)
    counts = {"Completed": 0, "In Progress": 0, "Pending": 0}
    progress_sum = 0.0
    for r in items:
        st = r.get("status") if isinstance(r, dict) else getattr(r, "status", None)
        if st in counts:
            counts[st] += 1
        progress_sum += _num(
            r.get("progress") if isinstance(r, dict) else getattr(r, "progress", None)
        )
    completion = round(progress_sum / total, 1) if total else 0.0
    return {
        "total": total,
        "completed": counts["Completed"],
        "in_progress": counts["In Progress"],
        "pending": counts["Pending"],
        "completion": completion,
    }


@frappe.whitelist()
def list_appraisal_cycles(status: str | None = None) -> list[dict]:
    filters = {}
    if status:
        filters["status"] = status
    try:
        return (
            frappe.get_all(CYCLE_DOCTYPE, filters=filters, fields=_CYCLE_FIELDS, order_by="start_date desc")
            or []
        )
    except Exception:
        frappe.log_error(title="appraisal.list_cycles failed")
        return []


@frappe.whitelist()
def my_goals(
    employee: str | None = None,
    cycle: str | None = None,
    search: str | None = None,
    status: str | None = None,
    kra: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    emp = _resolve(employee)
    _assert_own(emp)
    return _list_goals(
        [["employee", "=", emp]],
        cycle=cycle,
        search=search,
        status=status,
        kra=kra,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@frappe.whitelist()
def all_goals(
    employee: str | None = None,
    cycle: str | None = None,
    search: str | None = None,
    status: str | None = None,
    kra: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả mục tiêu.")
    flt = [["employee", "=", employee]] if employee else None
    return _list_goals(
        flt,
        cycle=cycle,
        search=search,
        status=status,
        kra=kra,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


def _list_goals(
    filters,
    cycle,
    search,
    page,
    page_size,
    status=None,
    kra=None,
    date_from=None,
    date_to=None,
) -> dict:
    flt = list(filters or [])
    if cycle:
        flt.append(["appraisal_cycle", "=", cycle])
    if status:
        flt.append(["status", "=", status])
    if kra:
        flt.append(["kra", "=", kra])
    # Date range on `end_date` (Hạn) — list filter, NOT `between` (DNA §6.6 B).
    if date_from:
        flt.append(["end_date", ">=", date_from])
    if date_to:
        flt.append(["end_date", "<=", date_to])
    # Broad search (Law #3 / DNA §6.6 A): text fields + numeric `progress`.
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{q}%"
        or_filters = [
            ["goal_name", "like", like],
            ["employee_name", "like", like],
            ["progress", "like", like],
        ]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 50))
    try:
        rows = (
            frappe.get_all(
                GOAL_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=_GOAL_FIELDS,
                order_by="creation desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        # Total + server-aggregated summary over the FULL filtered set (not just
        # the page). `frappe.db.count` ignores `or_filters` (DNA §6.6 A) → count
        # via a no-limit `get_all`; summary reuses the same wide query.
        all_matching = (
            frappe.get_all(
                GOAL_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=["name", "status", "progress"],
                limit_page_length=0,
            )
            or []
        )
        total = len(all_matching)
        summary = _summarize_goals(all_matching)
    except Exception:
        frappe.log_error(title="appraisal.list_goals failed")
        rows, total, summary = [], 0, _empty_summary()
    return {"data": rows, "total": total, "completion": summary["completion"], "summary": summary}


@frappe.whitelist()
def submit_goal(
    employee: str | None = None,
    goal_name: str | None = None,
    kra: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    cycle: str | None = None,
    description: str | None = None,
) -> dict:
    emp = _resolve(employee)
    _assert_own(emp)
    if cycle:
        cyc_status = frappe.db.get_value("Appraisal Cycle", cycle, "status")
        if cyc_status in ("Completed", "Cancelled"):
            frappe.throw("Chu kỳ đánh giá đã đóng — không thể thêm mục tiêu.")
    if not (goal_name or "").strip():
        frappe.throw("Cần tên mục tiêu.")
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(GOAL_DOCTYPE)
    doc.employee = emp
    doc.goal_name = goal_name.strip()
    doc.kra = kra or None
    doc.start_date = start_date or frappe.utils.today()
    doc.end_date = end_date or None
    doc.appraisal_cycle = cycle or None
    doc.description = description or ""
    doc.company = company
    doc.progress = 0
    doc.status = status_for_progress(0)
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def update_goal_progress(name: str | None = None, progress: float = 0) -> dict:
    if not name or not frappe.db.exists(GOAL_DOCTYPE, name):
        frappe.throw("Mục tiêu không tồn tại.")
    emp, cycle = frappe.db.get_value(GOAL_DOCTYPE, name, ["employee", "appraisal_cycle"])
    _assert_own(emp)
    if cycle:
        cyc_status = frappe.db.get_value("Appraisal Cycle", cycle, "status")
        if cyc_status in ("Completed", "Cancelled"):
            frappe.throw("Chu kỳ đánh giá đã đóng — không thể cập nhật mục tiêu.")
    p = max(0.0, min(100.0, _num(progress)))
    status = status_for_progress(p)
    frappe.db.set_value(GOAL_DOCTYPE, name, {"progress": p, "status": status})
    return {"name": name, "progress": p, "status": status}


@frappe.whitelist()
def my_appraisals(employee: str | None = None) -> list[dict]:
    emp = _resolve(employee)
    _assert_own(emp)
    try:
        return (
            frappe.get_all(
                APPRAISAL_DOCTYPE,
                filters=[["employee", "=", emp]],
                fields=_APPRAISAL_FIELDS,
                order_by="creation desc",
            )
            or []
        )
    except Exception:
        frappe.log_error(title="appraisal.my_appraisals failed")
        return []


@frappe.whitelist()
def appraisal_options() -> dict:
    out = {"cycles": [], "kras": []}
    try:
        out["cycles"] = (
            frappe.get_all(CYCLE_DOCTYPE, filters={"status": ["!=", "Completed"]}, fields=_CYCLE_FIELDS) or []
        )
    except Exception:
        pass
    try:
        out["kras"] = frappe.get_all("KRA", pluck="name", order_by="name asc") or []
    except Exception:
        pass
    return out
