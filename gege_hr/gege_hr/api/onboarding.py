"""FIX-2 (I-2, hr-gap-audit.md) — VN Employee Onboarding (custom gege_hr module).

Decision (hr-fix-plan.md §QUYẾT ĐỊNH #4): a self-contained onboarding process —
**do NOT reuse hrms Employee Onboarding**, so the portal keeps full control and
stays in lock-step with the trader-ui DNA. HR picks a ``VN Onboarding Template``,
starts a ``VN Employee Onboarding`` for a new hire, and ticks the task checklist
to completion. Pure helpers (progress / status / due-date instantiation) are kept
separate from the frappe I/O so they can be unit-tested without a bench.
"""
from __future__ import annotations

from datetime import date, timedelta

import frappe

from gege_hr.gege_hr.utils import pagination

TEMPLATE_DOCTYPE = "VN Onboarding Template"
ONBOARDING_DOCTYPE = "VN Employee Onboarding"

TEMPLATE_FIELDS = ["name", "template_name", "company", "is_active"]
ONBOARDING_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "company",
    "template",
    "boarding_date",
    "status",
    "progress",
]
_TASK_FIELDS = ["task_name", "assignee", "due_in_days", "due_date", "status", "completed_at", "completed_by", "note"]


def _require_hr() -> None:
    frappe.only_for(["HR Manager", "HR User", "System Manager"])


def _t(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default=False) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on") if value is not None else default


def compute_progress(tasks) -> float:
    """Percent of Done+Skipped tasks (0–100). An empty list → 0."""
    rows = list(tasks or [])
    if not rows:
        return 0.0
    done = sum(1 for t in rows if _t(t, "status") in ("Done", "Skipped"))
    return round(done * 100.0 / len(rows), 1)


def derive_status(tasks, current_status: str | None = None) -> str:
    """Lifecycle status from the task set. ``Cancelled`` is sticky."""
    if (current_status or "") == "Cancelled":
        return "Cancelled"
    rows = list(tasks or [])
    if not rows:
        return "Open"
    done = sum(1 for t in rows if _t(t, "status") in ("Done", "Skipped"))
    if done >= len(rows):
        return "Completed"
    if done > 0:
        return "In Progress"
    return "Open"


def instantiate_task(template_task, boarding_date) -> dict:
    """Copy a template task row into a live task row with a concrete due_date.

    Pure (no frappe) so it is unit-testable: ``due_date`` = boarding_date +
    ``due_in_days`` (parsed via stdlib ``datetime``).
    """
    due_in_days = _to_int(_t(template_task, "due_in_days"))
    due_date = boarding_date
    if boarding_date and due_in_days:
        try:
            d = date.fromisoformat(str(boarding_date))
            due_date = (d + timedelta(days=due_in_days)).isoformat()
        except ValueError:
            due_date = boarding_date
    return {
        "task_name": _t(template_task, "task_name"),
        "assignee": _t(template_task, "assignee"),
        "due_in_days": due_in_days,
        "due_date": due_date,
        "status": "Open",
    }


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_templates(company: str | None = None) -> list[dict]:
    _require_hr()
    filters = {"is_active": 1}
    if company:
        filters["company"] = company
    return frappe.get_all(
        TEMPLATE_DOCTYPE, filters=filters, fields=TEMPLATE_FIELDS, order_by="template_name asc"
    )


@frappe.whitelist()
def save_template(values: dict | None = None, **kwargs) -> dict:
    _require_hr()
    data = dict(values or kwargs)
    name = data.get("name")
    tasks = [
        {
            "task_name": _t(t, "task_name"),
            "assignee": _t(t, "assignee"),
            "due_in_days": _to_int(_t(t, "due_in_days")),
        }
        for t in (data.get("tasks") or [])
    ]
    if not data.get("template_name"):
        frappe.throw("Cần tên template.")
    if name:
        doc = frappe.get_doc(TEMPLATE_DOCTYPE, name)
    else:
        doc = frappe.new_doc(TEMPLATE_DOCTYPE)
    doc.template_name = data.get("template_name")
    doc.company = data.get("company")
    doc.is_active = 1 if _to_bool(data.get("is_active", 1), True) else 0
    doc.set("tasks", [])
    for t in tasks:
        doc.append("tasks", t)
    if name:
        doc.save()  # proper (HR Manager grant via setup_permissions)
    else:
        doc.insert()  # proper (HR Manager grant via setup_permissions)
    return {"name": doc.name, "template_name": doc.template_name, "task_count": len(tasks)}


# --------------------------------------------------------------------------- #
# Employee onboarding lifecycle
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def start_onboarding(
    employee: str,
    boarding_date: str,
    template: str | None = None,
    tasks: list | None = None,
) -> dict:
    _require_hr()
    if not employee or not boarding_date:
        frappe.throw("Cần employee + boarding_date.")

    doc = frappe.new_doc(ONBOARDING_DOCTYPE)
    doc.employee = employee
    doc.template = template
    doc.boarding_date = boarding_date
    doc.status = "Open"

    if template:
        try:
            tpl = frappe.get_doc(TEMPLATE_DOCTYPE, template)
            for t in (tpl.tasks or []):
                doc.append("tasks", instantiate_task(t, boarding_date))
        except Exception:
            frappe.log_error(title="onboarding.start_onboarding template load failed")
    for t in (tasks or []):
        doc.append("tasks", instantiate_task(t, boarding_date))

    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.insert()  # proper (HR Manager grant via setup_permissions)
    return {
        "name": doc.name,
        "status": doc.status,
        "progress": doc.progress,
        "task_count": len(doc.tasks or []),
    }


@frappe.whitelist()
def boarding_tasks(name: str) -> list[dict]:
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    return [{f: _t(t, f) for f in _TASK_FIELDS} for t in (doc.tasks or [])]


@frappe.whitelist()
def complete_task(name: str, task_name: str, status: str = "Done", note: str | None = None) -> dict:
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if doc.status == "Cancelled":
        frappe.throw("Onboarding đã huỷ — không thể đánh dấu task.")
    target = None
    for t in (doc.tasks or []):
        if _t(t, "task_name") == task_name and _t(t, "status") not in ("Done", "Skipped"):
            target = t
            break
    if target is None:
        frappe.throw(f"Không tìm thấy task mở tên '{task_name}'.")
    target.status = status
    if status in ("Done", "Skipped"):
        target.completed_at = frappe.utils.now_datetime()
        target.completed_by = frappe.session.user
    if note is not None:
        target.note = note
    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.save()  # proper (HR Manager grant via setup_permissions)
    return {"name": doc.name, "status": doc.status, "progress": doc.progress}


@frappe.whitelist()
def cancel_onboarding(name: str, reason: str | None = None) -> dict:
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    doc.status = "Cancelled"
    if reason:
        doc.add_comment("Comment", reason) if hasattr(doc, "add_comment") else None
    doc.save()  # proper (HR Manager grant via setup_permissions)
    return {"name": doc.name, "status": "Cancelled"}


@frappe.whitelist()
def onboarding_list(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Server-side paginated list (DNA §6.6 — 3 supreme laws).

    - ``status`` → exact match (filter).
    - ``boarding_date`` range → two list conditions ``[">=", date_from]`` /
      ``["<=", date_to]`` (list filter, NOT ``between`` — DNA §6.6 B).
    - ``search`` → broad ``or_filters`` LIKE across every text/identifier field
      (name/employee/employee_name/template), SERVER-SIDE so total + pagination
      reflect the search (DNA §6.6 A). ``frappe.db.count`` ignores
      ``or_filters``, so the count comes from ``get_all(..., fields=["name"])``
      + ``len()``.
    """
    _require_hr()
    filters = []
    if status:
        filters.append(["status", "=", status])
    if date_from:
        filters.append(["boarding_date", ">=", date_from])
    if date_to:
        filters.append(["boarding_date", "<=", date_to])

    or_filters = []
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [
            ["name", "like", like],
            ["employee", "like", like],
            ["employee_name", "like", like],
            ["template", "like", like],
        ]

    page = max(1, _to_int(page, 1))
    page_size = max(1, _to_int(page_size, 20))
    start = (page - 1) * page_size

    rows = (
        frappe.get_all(
            ONBOARDING_DOCTYPE,
            filters=filters or None,
            or_filters=or_filters or None,
            fields=ONBOARDING_FIELDS,
            order_by="boarding_date desc",
            limit_start=start,
            limit_page_length=page_size,
        )
        or []
    )

    # Total must reflect search → frappe.db.count() ignores or_filters (DNA §6.6 A),
    # so when searching we count matching names explicitly.
    if or_filters:
        total = len(
            frappe.get_all(
                ONBOARDING_DOCTYPE,
                filters=filters or None,
                or_filters=or_filters,
                fields=["name"],
                limit_page_length=0,
            )
            or []
        )
    else:
        total = frappe.db.count(ONBOARDING_DOCTYPE, filters=filters or None)

    return {"data": rows, "total": total}
