"""FIX-2 (I-2, hr-gap-audit.md) — VN Employee Onboarding (custom gege_hr module).

Decision (hr-fix-plan.md §QUYẾT ĐỊNH #4): a self-contained onboarding process —
**do NOT reuse hrms Employee Onboarding**, so the portal keeps full control and
stays in lock-step with the trader-ui DNA. HR picks a ``VN Onboarding Template``,
starts a ``VN Employee Onboarding`` for a new hire, and ticks the task checklist
to completion. Pure helpers (progress / status / due-date instantiation) are kept
separate from the frappe I/O so they can be unit-tested without a bench.
"""

from __future__ import annotations

import json
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
_TASK_FIELDS = [
    "task_name",
    "assignee",
    "due_in_days",
    "due_date",
    "status",
    "completed_at",
    "completed_by",
    "note",
]


def _require_hr() -> None:
    frappe.only_for(["HR Manager", "HR User", "System Manager"])


def _require_hr_manager() -> None:
    """Tighter gate for destructive ops (delete onboarding / template)."""
    frappe.only_for(["HR Manager", "System Manager"])


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


def is_overdue(due_date, status: str | None, today: str | None = None) -> bool:
    """Pure: an Open task whose due_date has passed (plan-onboarding §2.0)."""
    if (status or "Open") != "Open" or not due_date:
        return False
    return str(due_date)[:10] < (today or date.today().isoformat())


def derive_can(doc_status: str | None) -> dict:
    """Pure capability matrix for the SPA drawer (plan-onboarding §2.0)."""
    s = doc_status or ""
    return {
        "edit_tasks": s in ("Open", "In Progress"),
        "cancel": s in ("Open", "In Progress"),
        "delete": s in ("Open", "Cancelled"),
    }


def _task_row(t) -> dict:
    """Serialise a child task + its row PK (``row_name``) + ``is_overdue``."""
    row = {f: _t(t, f) for f in _TASK_FIELDS}
    row["row_name"] = _t(t, "name")
    row["is_overdue"] = is_overdue(row.get("due_date"), row.get("status") or "Open")
    return row


def _find_task(doc, row_name=None, task_name=None):
    """Locate an open task by row PK (preferred) or task_name (back-compat)."""
    for t in doc.tasks or []:
        if _t(t, "status") in ("Done", "Skipped"):
            continue
        if row_name:
            if (_t(t, "name") or "") == row_name:
                return t
        elif task_name is not None and (_t(t, "task_name") or "") == (task_name or ""):
            return t
    return None


def _sync_todo(doc_name: str, row, action: str) -> None:
    """Best-effort ToDo add/remove for a task assignee (Frappe ``assign_to``).

    Fail-tolerant by design: a missing ``frappe.desk.form`` (stub tests) or an
    assign_to error must never abort the onboarding mutation itself.
    """
    assignee = _t(row, "assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return
    assignee = assignee.strip()
    try:
        from frappe.desk.form import assign_to  # lazy: stub-safe

        if action == "add":
            assign_to.add(
                {
                    "doctype": ONBOARDING_DOCTYPE,
                    "name": doc_name,
                    "assign": [assignee],
                    "description": f"Onboarding task: {_t(row, 'task_name') or ''}",
                }
            )
        else:
            assign_to.remove(ONBOARDING_DOCTYPE, doc_name, assignee)
    except Exception:
        frappe.log_error(title=f"onboarding._sync_todo {action} failed")


def _close_all_todos(doc) -> None:
    for t in doc.tasks or []:
        _sync_todo(doc.name, t, "remove")


def _publish_onboarding(doc) -> None:
    """Best-effort realtime ping so an open list tab can offer a refresh."""
    pub = getattr(frappe, "publish_realtime", None)
    if pub is None:
        return
    try:
        pub(
            "onboarding_updated",
            {"name": doc.name, "status": doc.status, "progress": doc.progress},
        )
    except Exception:
        frappe.log_error(title="onboarding._publish_onboarding failed")


def _audit(doctype: str, name: str, audit_type: str, description: str, company=None, employee=None) -> None:
    """Best-effort VN Audit Event row for destructive ops (never aborts)."""
    try:
        from gege_hr.gege_hr.api import audit as audit_api  # lazy: stub-safe

        audit_api.record(
            audit_type,
            company or "",
            reference_doctype=doctype,
            reference_name=name,
            description=description,
            employee=employee,
        )
    except Exception:
        frappe.log_error(title="onboarding._audit failed")


def _safe_rows(
    doctype: str, filters, fields: list, order_by: str | None = "creation desc", limit: int = 20
) -> list:
    """Fail-tolerant get_all for the detail-drawer sources (Comment/Version/File)."""
    try:
        return (
            frappe.get_all(
                doctype, filters=filters, fields=fields, order_by=order_by, limit_page_length=limit
            )
            or []
        )
    except Exception:
        return []


def _version_summary(data_json) -> str:
    """Version ``data`` JSON → ``"status: Open → In Progress"`` (pure, tolerant)."""
    try:
        changed = (json.loads(data_json) or {}).get("changed") or []
        parts = [f"{c[0]}: {c[1]} → {c[2]}" for c in changed if isinstance(c, (list, tuple)) and len(c) >= 3]
        return "; ".join(parts) or "Cập nhật hồ sơ"
    except Exception:
        return "Cập nhật hồ sơ"


def _merge_activity(comments, versions, limit: int = 20) -> list[dict]:
    """Merge Comment + Version rows into one newest-first timeline (pure)."""
    items = []
    for c in comments or []:
        items.append(
            {
                "type": "comment",
                "content": _t(c, "content"),
                "owner": _t(c, "owner"),
                "creation": str(_t(c, "creation") or ""),
            }
        )
    for v in versions or []:
        items.append(
            {
                "type": "version",
                "content": _version_summary(_t(v, "data")),
                "owner": _t(v, "owner"),
                "creation": str(_t(v, "creation") or ""),
            }
        )
    items.sort(key=lambda x: x["creation"], reverse=True)
    return items[:limit]


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


@frappe.whitelist()
def get_template(name: str) -> dict:
    """Template + its task rows, for the SPA template editor (plan §2.4)."""
    _require_hr()
    doc = frappe.get_doc(TEMPLATE_DOCTYPE, name)
    return {
        "name": doc.name,
        "template_name": doc.template_name,
        "company": getattr(doc, "company", None),
        "is_active": _to_bool(getattr(doc, "is_active", 0)),
        "tasks": [
            {
                "task_name": _t(t, "task_name"),
                "assignee": _t(t, "assignee"),
                "due_in_days": _to_int(_t(t, "due_in_days")),
            }
            for t in (doc.tasks or [])
        ],
    }


@frappe.whitelist()
def delete_template(name: str) -> dict:
    """HR-Manager-only template delete; blocked while referenced (plan §2.4)."""
    _require_hr_manager()
    refs = frappe.get_all(
        ONBOARDING_DOCTYPE, filters={"template": name}, fields=["name"], limit_page_length=1
    )
    if refs:
        frappe.throw("Template đang được dùng bởi onboarding — không xoá được.")
    frappe.delete_doc(TEMPLATE_DOCTYPE, name)
    _audit(TEMPLATE_DOCTYPE, name, "onboarding_template_deleted", "Xoá template onboarding")
    return {"name": name, "deleted": True}


@frappe.whitelist()
def duplicate_template(name: str, new_name: str | None = None) -> dict:
    """Copy a template (inactive by default) with all its tasks (plan §2.4)."""
    _require_hr()
    src = frappe.get_doc(TEMPLATE_DOCTYPE, name)
    doc = frappe.new_doc(TEMPLATE_DOCTYPE)
    doc.template_name = (new_name or f"{src.template_name} (copy)").strip()
    doc.company = getattr(src, "company", None)
    doc.is_active = 0
    for t in src.tasks or []:
        doc.append(
            "tasks",
            {
                "task_name": _t(t, "task_name"),
                "assignee": _t(t, "assignee"),
                "due_in_days": _to_int(_t(t, "due_in_days")),
            },
        )
    doc.insert()
    return {"name": doc.name, "template_name": doc.template_name, "task_count": len(doc.tasks or [])}


@frappe.whitelist()
def list_assignees() -> list[dict]:
    """Enabled System Users for the SPA assignee picker (plan §2.4)."""
    _require_hr()
    rows = (
        frappe.get_all(
            "User",
            filters={"enabled": 1, "user_type": "System User"},
            fields=["name", "full_name"],
            limit_page_length=200,
        )
        or []
    )
    return [{"value": r.get("name"), "label": r.get("full_name") or r.get("name")} for r in rows]


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
            for t in tpl.tasks or []:
                doc.append("tasks", instantiate_task(t, boarding_date))
        except Exception:
            frappe.log_error(title="onboarding.start_onboarding template load failed")
    for t in tasks or []:
        doc.append("tasks", instantiate_task(t, boarding_date))

    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.insert()  # proper (HR Manager grant via setup_permissions)
    for t in doc.tasks or []:
        _sync_todo(doc.name, t, "add")
    _publish_onboarding(doc)
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
    return [_task_row(t) for t in (doc.tasks or [])]


@frappe.whitelist()
def complete_task(
    name: str,
    task_name: str | None = None,
    row_name: str | None = None,
    status: str = "Done",
    note: str | None = None,
) -> dict:
    """Mark a task Done/Skipped. Match by child-row PK (``row_name``) first,
    falling back to ``task_name`` for back-compat (plan-onboarding §2.1)."""
    _require_hr()
    if status not in ("Done", "Skipped"):
        frappe.throw("status chỉ nhận Done hoặc Skipped.")
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if doc.status == "Cancelled":
        frappe.throw("Onboarding đã huỷ — không thể đánh dấu task.")
    target = _find_task(doc, row_name=row_name, task_name=task_name)
    if target is None:
        frappe.throw("Không tìm thấy task mở khớp yêu cầu.")
    target.status = status
    if status in ("Done", "Skipped"):
        target.completed_at = frappe.utils.now_datetime()
        target.completed_by = frappe.session.user
    if note is not None:
        target.note = note
    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.save()  # proper (HR Manager grant via setup_permissions)
    _sync_todo(doc.name, target, "remove")
    _publish_onboarding(doc)
    return {"name": doc.name, "status": doc.status, "progress": doc.progress}


@frappe.whitelist()
def cancel_onboarding(name: str, reason: str | None = None) -> dict:
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    _close_all_todos(doc)
    doc.status = "Cancelled"
    if reason:
        doc.add_comment("Comment", reason) if hasattr(doc, "add_comment") else None
    doc.save()  # proper (HR Manager grant via setup_permissions)
    _audit(
        ONBOARDING_DOCTYPE,
        name,
        "onboarding_cancelled",
        f"Huỷ onboarding{' — ' + reason if reason else ''}",
        company=getattr(doc, "company", None),
        employee=getattr(doc, "employee", None),
    )
    _publish_onboarding(doc)
    return {"name": doc.name, "status": "Cancelled"}


@frappe.whitelist()
def add_task(
    name: str,
    task_name: str,
    assignee: str | None = None,
    due_date: str | None = None,
    due_in_days: int | None = None,
    note: str | None = None,
) -> dict:
    """Append an ad-hoc task to a running onboarding (plan-onboarding §2.2)."""
    _require_hr()
    task_name = (task_name or "").strip()
    if not task_name:
        frappe.throw("Cần tên task.")
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if doc.status in ("Completed", "Cancelled"):
        frappe.throw("Onboarding đã đóng — không thể thêm task.")
    row = instantiate_task(
        {"task_name": task_name, "assignee": assignee, "due_in_days": due_in_days or 0},
        getattr(doc, "boarding_date", None),
    )
    if due_date:
        row["due_date"] = due_date
    if note:
        row["note"] = note
    doc.append("tasks", row)
    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.save()  # proper (HR Manager grant via setup_permissions)
    _sync_todo(doc.name, doc.tasks[-1], "add")
    _publish_onboarding(doc)
    return {
        "name": doc.name,
        "status": doc.status,
        "progress": doc.progress,
        "task_count": len(doc.tasks or []),
    }


@frappe.whitelist()
def update_task(
    name: str,
    row_name: str,
    task_name: str | None = None,
    assignee: str | None = None,
    due_date: str | None = None,
    note: str | None = None,
) -> dict:
    """Patch a task (rename / reassign / reschedule). Status stays untouched —
    a closed task must be reopened first (plan-onboarding §2.2)."""
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if doc.status in ("Completed", "Cancelled"):
        frappe.throw("Onboarding đã đóng — không thể sửa task.")
    target = None
    for t in doc.tasks or []:
        if (_t(t, "name") or "") == (row_name or ""):
            target = t
            break
    if target is None:
        frappe.throw("Không tìm thấy task.")
    old_assignee = _t(target, "assignee")
    if task_name is not None and str(task_name).strip():
        target.task_name = str(task_name).strip()
    if assignee is not None:
        target.assignee = assignee or None
    if due_date is not None:
        target.due_date = due_date or None
    if note is not None:
        target.note = note
    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.save()  # proper (HR Manager grant via setup_permissions)
    if assignee is not None and (assignee or "") != (old_assignee or ""):
        _sync_todo(doc.name, {"assignee": old_assignee}, "remove")
        _sync_todo(doc.name, target, "add")
    _publish_onboarding(doc)
    return {"name": doc.name, "status": doc.status, "progress": doc.progress}


@frappe.whitelist()
def reopen_task(name: str, row_name: str, note: str | None = None) -> dict:
    """Done/Skipped → Open (undo). The doc may drop back to In Progress."""
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if doc.status == "Cancelled":
        frappe.throw("Onboarding đã huỷ — không thể mở lại task.")
    target = None
    for t in doc.tasks or []:
        if (_t(t, "name") or "") == (row_name or ""):
            target = t
            break
    if target is None:
        frappe.throw("Không tìm thấy task.")
    if _t(target, "status") not in ("Done", "Skipped"):
        frappe.throw("Chỉ task Done/Skipped mới mở lại được.")
    target.status = "Open"
    target.completed_at = None
    target.completed_by = None
    if note is not None and str(note).strip():
        target.note = note
    doc.progress = compute_progress(doc.tasks)
    doc.status = derive_status(doc.tasks, doc.status)
    doc.save()  # proper (HR Manager grant via setup_permissions)
    _sync_todo(doc.name, target, "add")
    _publish_onboarding(doc)
    return {"name": doc.name, "status": doc.status, "progress": doc.progress}


@frappe.whitelist()
def delete_onboarding(name: str) -> dict:
    """Hard-delete — HR Manager only, Open/Cancelled only (plan-onboarding §2.2)."""
    _require_hr_manager()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    if not derive_can(doc.status)["delete"]:
        frappe.throw("Chỉ xoá được onboarding ở trạng thái Open hoặc Cancelled.")
    employee = getattr(doc, "employee", None)
    company = getattr(doc, "company", None)
    _close_all_todos(doc)
    frappe.delete_doc(ONBOARDING_DOCTYPE, name)
    _audit(
        ONBOARDING_DOCTYPE,
        name,
        "onboarding_deleted",
        "Xoá onboarding",
        company=company,
        employee=employee,
    )
    return {"name": name, "deleted": True}


@frappe.whitelist()
def get_onboarding(name: str) -> dict:
    """One-call detail payload for the SPA drawer (plan-onboarding §2.2)."""
    _require_hr()
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    comments = _safe_rows(
        "Comment",
        {"reference_doctype": ONBOARDING_DOCTYPE, "reference_name": name},
        ["name", "content", "owner", "creation"],
    )
    versions = _safe_rows(
        "Version",
        {"ref_doctype": ONBOARDING_DOCTYPE, "docname": name},
        ["name", "data", "owner", "creation"],
    )
    attachments = _safe_rows(
        "File",
        {"attached_to_doctype": ONBOARDING_DOCTYPE, "attached_to_name": name},
        ["name", "file_name", "file_url", "is_private", "file_size", "owner", "creation"],
    )
    payload = {f: getattr(doc, f, None) for f in ONBOARDING_FIELDS}
    payload.update(
        {
            "tasks": [_task_row(t) for t in (doc.tasks or [])],
            "activity": _merge_activity(comments, versions),
            "attachments": attachments,
            "payroll": _payroll_profile(getattr(doc, "employee", None)),
            "can": derive_can(doc.status),
        }
    )
    return payload


@frappe.whitelist()
def onboarding_summary() -> dict:
    """Site-wide counts for the SPA summary cards + overdue chips (plan §2.2).

    Client-side card counts were page-local (wrong under pagination); this is
    the server-side truth. Overdue = Open child task past its due_date whose
    parent process is still active.
    """
    _require_hr()
    rows = _safe_rows(ONBOARDING_DOCTYPE, None, ["name", "status"], order_by=None, limit=0)
    by_status: dict = {}
    active_parents = set()
    for r in rows:
        s = _t(r, "status") or "Open"
        by_status[s] = by_status.get(s, 0) + 1
        if s in ("Open", "In Progress"):
            active_parents.add(_t(r, "name"))
    today = date.today().isoformat()
    open_tasks = _safe_rows(
        "VN Onboarding Task",
        {"parenttype": ONBOARDING_DOCTYPE, "status": "Open"},
        ["name", "parent", "due_date"],
        order_by=None,
        limit=0,
    )
    overdue_tasks = 0
    overdue_parents = set()
    for t in open_tasks:
        parent = _t(t, "parent")
        if parent in active_parents and is_overdue(_t(t, "due_date"), "Open", today):
            overdue_tasks += 1
            overdue_parents.add(parent)
    return {
        "total": len(rows),
        "by_status": by_status,
        "overdue_tasks": overdue_tasks,
        "overdue_processes": len(overdue_parents),
    }


@frappe.whitelist()
def add_onboarding_comment(name: str, comment: str) -> dict:
    """HR note → standard ``Comment`` row on the onboarding (plan §2.3)."""
    _require_hr()
    text = (comment or "").strip()
    if not text:
        frappe.throw("Cần nội dung bình luận.")
    doc = frappe.get_doc(ONBOARDING_DOCTYPE, name)
    doc.add_comment("Comment", text)
    _publish_onboarding(doc)
    return {"name": name, "ok": True}


@frappe.whitelist()
def delete_onboarding_attachment(file_name: str, onboarding: str | None = None) -> dict:
    """Guarded ``File`` delete from the drawer (plan §2.3)."""
    _require_hr()
    file = frappe.get_doc("File", file_name)
    if (getattr(file, "attached_to_doctype", None) or "") != ONBOARDING_DOCTYPE:
        frappe.throw("Tệp này không đính kèm với onboarding.")
    owner_doc = (onboarding or "").strip()
    if owner_doc and (getattr(file, "attached_to_name", None) or "") != owner_doc:
        frappe.throw("Tệp không thuộc về onboarding này.")
    company = None
    employee = None
    if owner_doc:
        d = frappe.get_doc(ONBOARDING_DOCTYPE, owner_doc)
        company = getattr(d, "company", None)
        employee = getattr(d, "employee", None)
    frappe.delete_doc("File", file_name)
    _audit(
        "File",
        file_name,
        "onboarding_attachment_deleted",
        f"Xoá tệp đính kèm onboarding {owner_doc or ''}".strip(),
        company=company,
        employee=employee,
    )
    return {"name": file_name, "deleted": True}


@frappe.whitelist()
def onboarding_list(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    company: str | None = None,
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
    if company:
        filters.append(["company", "=", company])
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


# --------------------------------------------------------------------------- #
# WP5 — payroll profile (SSA + bank) completion + nudges
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def complete_payroll_profile(
    employee: str,
    salary_structure: str | None = None,
    base: float | None = None,
    bank_ac_no: str | None = None,
    bank_name: str | None = None,
    bank_branch: str | None = None,
    from_date: str | None = None,
) -> dict:
    """Create/refresh the employee's Salary Structure Assignment + bank fields.

    Idempotent (OB1/OB4): an existing submitted SSA is left untouched unless a
    new ``salary_structure``/``base`` is given; bank fields are updated in
    place. HR-gated. Kills the ``missing account_no`` napas-export failures and
    the WP1 ``failed_lines`` (no-SSA) slip errors at their onboarding source.
    """
    _require_hr()
    if not employee:
        frappe.throw("Cần employee.")
    employee = str(employee).strip()

    emp = frappe.get_doc("Employee", employee)
    changed = []

    # --- Bank fields -------------------------------------------------------- #
    if bank_ac_no:
        emp.bank_ac_no = bank_ac_no
        changed.append("bank_ac_no")
    if bank_name:
        emp.bank_name = bank_name
        changed.append("bank_name")
    try:
        if bank_branch and hasattr(emp, "bank_branch"):
            emp.bank_branch = bank_branch
            changed.append("bank_branch")
    except Exception:
        pass
    if changed:
        emp.save(ignore_permissions=True)

    # --- SSA ---------------------------------------------------------------- #
    ssa_name = None
    existing_ssa = frappe.db.get_value(
        "Salary Structure Assignment",
        {"employee": employee, "docstatus": 1},
        "name",
    )
    if not existing_ssa:
        if not (salary_structure and base):
            frappe.throw("Nhân viên chưa có SSA — cần salary_structure + base để tạo mới.")
        company = emp.company or frappe.db.get_value("Employee", employee, "company")
        ssa = frappe.get_doc(
            {
                "doctype": "Salary Structure Assignment",
                "employee": employee,
                "salary_structure": salary_structure,
                "company": company,
                "base": float(base or 0),
                "from_date": from_date or emp.date_of_joining or frappe.utils.today(),
            }
        )
        ssa.insert(ignore_permissions=True)
        try:
            ssa.submit()
        except Exception:
            frappe.log_error(title=f"SSA submit failed {employee}")
        ssa_name = ssa.name
        changed.append("ssa")

    return {
        "ok": True,
        "employee": employee,
        "ssa": ssa_name or existing_ssa,
        "changed": changed,
        "complete": _payroll_profile_complete(employee),
    }


def _payroll_profile(employee: str) -> dict:
    """SSA + bank snapshot for the drawer payroll card (plan-onboarding §2.2)."""
    try:
        ssa = frappe.db.get_value(
            "Salary Structure Assignment", {"employee": employee, "docstatus": 1}, "name"
        )
    except Exception:
        ssa = None
    try:
        bank = frappe.db.get_value("Employee", employee, "bank_ac_no")
    except Exception:
        bank = None
    return {"ssa": ssa, "has_bank": bool(bank), "complete": bool(ssa) and bool(bank)}


def _payroll_profile_complete(employee: str) -> bool:
    """SSA submitted AND bank account present."""
    return _payroll_profile(employee)["complete"]


@frappe.whitelist()
def missing_payroll_profiles() -> list[dict]:
    """Active employees onboarded > 3 days ago still missing SSA or bank.

    Feeds the dashboard "Cần bổ sung hồ sơ" card (OB2) and the daily nudge.
    """
    _require_hr()
    try:
        from datetime import date as _d, timedelta as _td

        cutoff = (_d.today() - _td(days=3)).isoformat()
        rows = (
            frappe.db.get_all(
                "Employee",
                filters={"status": "Active", "date_of_joining": ["<=", cutoff]},
                fields=["name", "employee_name", "company", "date_of_joining", "bank_ac_no"],
            )
            or []
        )
    except Exception:
        return []
    out = []
    for r in rows:
        missing = []
        if not r.get("bank_ac_no"):
            missing.append("bank")
        try:
            has_ssa = frappe.db.get_value(
                "Salary Structure Assignment",
                {"employee": r["name"], "docstatus": 1},
                "name",
            )
        except Exception:
            has_ssa = None
        if not has_ssa:
            missing.append("ssa")
        if missing:
            out.append(
                {
                    "employee": r["name"],
                    "employee_name": r.get("employee_name"),
                    "company": r.get("company"),
                    "date_of_joining": r.get("date_of_joining"),
                    "missing": missing,
                }
            )
    return out


def notify_missing_payroll_profile() -> dict:
    """Daily cron — ONE grouped notification to HR Managers (OB3).

    Deduped per day: if today's notification already exists, skip.
    """
    if frappe is None:
        return {"notified": 0}
    try:
        rows = missing_payroll_profiles()
    except Exception:
        rows = []
    if not rows:
        return {"notified": 0}

    today = frappe.utils.today()
    subject = f"[GeGe HR] {len(rows)} NV thiếu hồ sơ lương ({today})"
    # Dedupe: 1 notification/day (grouped, never spam).
    try:
        already = frappe.db.exists("Notification Log", {"subject": subject})
    except Exception:
        already = None
    if already:
        return {"notified": 0, "deduped": True}

    from gege_hr.gege_hr.utils import health as _health

    lines = [f"• {r['employee_name'] or r['employee']} — thiếu: {', '.join(r['missing'])}" for r in rows]
    _health._notify_users(
        _health._hr_manager_users(),
        subject,
        "Các nhân viên Active sau thiếu SSA / tài khoản ngân hàng:\n" + "\n".join(lines),
    )
    return {"notified": len(rows)}
