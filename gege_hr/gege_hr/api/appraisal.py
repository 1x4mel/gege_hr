"""NEW-2 (hr-gap-audit 🟥) — Appraisal / Goals API.

Reuses Frappe HR's ``Appraisal Cycle`` / ``Appraisal`` / ``Goal`` / ``KRA``
DocTypes (no duplicate doctype) and exposes a DNA-compliant surface to the
portal: an employee lists/creates/tracks their Goals (progress → auto status) and
reads their Appraisals; an HR/Manager lists cycles + everyone's goals/appraisals.
Pure helpers are split out for unit testing (no frappe).

Desk-free extensions (plans/goals-frontend-crud.md): full Goal CRUD —
``get_goal`` (detail + ``can`` matrix), ``update_goal``, ``delete_goal``,
``set_goal_status`` (bulk Archived/Closed/Completed/Unarchive/Reopen) and a
``update_goal_progress`` FIX that now saves via ``doc.save()`` so the native
NestedSet hooks run (parent roll-up + Appraisal goal score). ``submit_goal``
grows ``is_group`` / ``parent_goal`` / cycle start-date fallback per hrms rules.
"""

from __future__ import annotations

import json

import frappe

from gege_hr.gege_hr.utils import pagination

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
    "is_group",
    "parent_goal",
]
_GOAL_DETAIL_FIELDS = _GOAL_FIELDS + [
    "description",
    "company",
    "user",
    "created_by",
    "modified",
    "modified_by",
    "owner",
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
        progress_sum += _num(r.get("progress") if isinstance(r, dict) else getattr(r, "progress", None))
    completion = round(progress_sum / total, 1) if total else 0.0
    return {
        "total": total,
        "completed": counts["Completed"],
        "in_progress": counts["In Progress"],
        "pending": counts["Pending"],
        "completion": completion,
    }


def _to_bool(value) -> bool:
    """Truthy coercion for HTTP form params (``1`` / ``"1"`` / ``true``)."""
    return value in (True, 1, "1", "true", "True", "on")


def _assert_cycle_open(cycle: str | None) -> None:
    """A goal inside a Completed/Cancelled cycle is frozen (hrms standard)."""
    if not cycle:
        return
    cyc_status = frappe.db.get_value(CYCLE_DOCTYPE, cycle, "status")
    if cyc_status in ("Completed", "Cancelled"):
        frappe.throw("Chu kỳ đánh giá đã đóng — không thể cập nhật mục tiêu.")


def _goal_doc_or_throw(name: str | None):
    if not name or not frappe.db.exists(GOAL_DOCTYPE, name):
        frappe.throw("Mục tiêu không tồn tại.")
    return frappe.get_doc(GOAL_DOCTYPE, name)


def goal_can(status: str | None, is_group, children_total: int = 0) -> dict:
    """Pure ``can`` action matrix for a Goal (plans/goals-frontend-crud.md §2.1).

    Mirrors hrms rules: progress is read-only for group goals & sticky states
    (Archived/Closed); a group with children cannot be deleted.
    """
    sticky = status in ("Archived", "Closed")
    group = _to_bool(is_group)
    return {
        "edit": True,
        "delete": not children_total,
        "set_progress": (not group) and not sticky,
        "archive": not sticky,
        "unarchive": status == "Archived",
        "close": not sticky,
        "reopen": status == "Closed",
    }


def _friendly_goal_error(exc: Exception) -> str | None:
    """Map the most common native hrms Goal validation messages to Vietnamese."""
    lowered = str(exc).lower()
    if "from date" in lowered or "to date" in lowered:
        return "Ngày kết thúc không được trước ngày bắt đầu."
    if "same employee" in lowered:
        return "Mục tiêu con phải cùng nhân viên với mục tiêu cha."
    if "same kra" in lowered or "aligned with the same kra" in lowered:
        return "Mục tiêu phải cùng KRA với mục tiêu cha."
    if "same appraisal cycle" in lowered:
        return "Mục tiêu phải cùng Kỳ đánh giá với mục tiêu cha."
    if "progress percentage cannot be more than 100" in lowered:
        return "Tiến độ không được vượt quá 100%."
    return None


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
        like = f"%{pagination.escape_like(q)}%"
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
    is_group=False,
    parent_goal: str | None = None,
) -> dict:
    emp = _resolve(employee)
    _assert_own(emp)
    if not (goal_name or "").strip():
        frappe.throw("Cần tên mục tiêu.")

    group_flag = _to_bool(is_group)
    parent = None
    if parent_goal and not group_flag:  # a group is always a root — ignore any parent
        parent = _goal_doc_or_throw(parent_goal)
        if getattr(parent, "employee", None) != emp:
            frappe.throw("Mục tiêu con phải cùng nhân viên với mục tiêu cha.")
        # hrms fetch_from: child inherits KRA + cycle from the parent goal
        if kra and parent.kra and kra != parent.kra:
            frappe.throw("Mục tiêu phải cùng KRA với mục tiêu cha.")
        kra = kra or getattr(parent, "kra", None) or None
        cycle = getattr(parent, "appraisal_cycle", None) or None
    elif cycle and not (kra or "").strip():
        # goal.json: KRA is mandatory when a cycle is linked and there is no parent
        frappe.throw("Mục tiêu gắn kỳ đánh giá cần chọn KRA.")

    _assert_cycle_open(cycle)

    if not start_date and cycle:
        # fetch_if_empty: default the window from the appraisal cycle
        start_date = frappe.db.get_value(CYCLE_DOCTYPE, cycle, "start_date")

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
    doc.is_group = 1 if group_flag else 0
    doc.parent_goal = parent_goal if not group_flag else None
    doc.progress = 0
    doc.status = status_for_progress(0)
    doc.insert(ignore_permissions=True)
    return {
        "name": doc.name,
        "status": doc.status,
        "is_group": getattr(doc, "is_group", 0),
        "parent_goal": getattr(doc, "parent_goal", None),
    }


@frappe.whitelist()
def get_goal(name: str | None = None) -> dict:
    """Goal detail + children stats + server-side ``can`` action matrix."""
    doc = _goal_doc_or_throw(name)
    _assert_own(doc.employee)

    children_rows = (
        frappe.get_all(GOAL_DOCTYPE, filters=[["parent_goal", "=", name]], fields=["name", "status"]) or []
    )
    total = len(children_rows)
    counts = {"Completed": 0, "In Progress": 0, "Pending": 0}
    for row in children_rows:
        st = row.get("status")
        if st in counts:
            counts[st] += 1

    out = {f: getattr(doc, f, None) for f in _GOAL_DETAIL_FIELDS}
    out["parent_goal_name"] = (
        frappe.db.get_value(GOAL_DOCTYPE, doc.parent_goal, "goal_name") if doc.parent_goal else None
    )
    out["children"] = {
        "total": total,
        "completed": counts["Completed"],
        "in_progress": counts["In Progress"],
        "pending": counts["Pending"],
    }
    out["completion_count"] = (
        f"{counts['Completed']}/{total} hoàn thành" if _to_bool(doc.is_group) and total else ""
    )
    out["can"] = goal_can(doc.status, getattr(doc, "is_group", 0), total)
    return out


@frappe.whitelist()
def update_goal(
    name: str | None = None,
    goal_name: str | None = None,
    kra: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    description: str | None = None,
    parent_goal: str | None = None,
) -> dict:
    """Edit the non-set_only_once fields. Runs the FULL hrms controller
    (validate + on_update roll-up) via ``doc.save()`` — never ``db.set_value``.

    ``employee`` / ``is_group`` / ``appraisal_cycle`` / ``progress`` / ``status``
    are intentionally NOT accepted (set_only_once or dedicated endpoints).
    """
    doc = _goal_doc_or_throw(name)
    _assert_own(doc.employee)
    _assert_cycle_open(getattr(doc, "appraisal_cycle", None))

    if (goal_name or "").strip():
        doc.goal_name = goal_name.strip()
    if kra is not None:
        doc.kra = kra or None
    if start_date:
        doc.start_date = start_date
    if end_date is not None:
        doc.end_date = end_date or None
    if description is not None:
        doc.description = description or ""

    if getattr(doc, "appraisal_cycle", None) and not getattr(doc, "parent_goal", None) and not doc.kra:
        frappe.throw("Mục tiêu gắn kỳ đánh giá cần chọn KRA.")

    if parent_goal is not None:
        # NestedSet move: set old_parent from DB truth before saving
        # (mirrors frappe.desk.treeview) so on_update relocates the node.
        doc.old_parent = frappe.db.get_value(GOAL_DOCTYPE, name, "parent_goal") or ""
        doc.parent_goal = parent_goal or None

    try:
        doc.save()
    except Exception as exc:  # noqa: BLE001 — map native messages to Vietnamese
        friendly = _friendly_goal_error(exc)
        if friendly:
            frappe.throw(friendly)
        raise

    return {
        "name": name,
        "goal_name": getattr(doc, "goal_name", None),
        "status": getattr(doc, "status", None),
        "progress": getattr(doc, "progress", None),
        "modified": getattr(doc, "modified", None),
    }


@frappe.whitelist()
def delete_goal(name: str | None = None) -> dict:
    _delete_one_goal(name)
    return {"name": name}


def _delete_one_goal(name: str | None) -> None:
    """Shared delete path (single + bulk): ownership gate + children guard."""
    doc = _goal_doc_or_throw(name)
    _assert_own(doc.employee)
    child_count = frappe.db.count(GOAL_DOCTYPE, [["parent_goal", "=", name]]) or 0
    if child_count:
        frappe.throw("Mục tiêu nhóm còn mục tiêu con — hãy xoá hoặc di chuyển các mục tiêu con trước.")
    try:
        frappe.delete_doc(GOAL_DOCTYPE, name)
    except Exception as exc:  # noqa: BLE001 — LinkExistsError etc → Vietnamese
        lowered = str(exc).lower()
        if "linkexists" in lowered or "linked with" in lowered:
            frappe.throw("Mục tiêu đang được liên kết — không xoá được.")
        raise


@frappe.whitelist()
def bulk_delete_goals(names) -> dict:
    """P1 (plans/goals-frontend-crud.md §1.7): manager-only bulk delete, partial-safe."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xoá được hàng loạt mục tiêu.")
    if isinstance(names, str):
        try:
            parsed = json.loads(names)
        except (TypeError, ValueError):
            parsed = [names]
        names = parsed
    deleted: list[str] = []
    failed: list[dict] = []
    for goal_name in [n for n in (names or []) if n]:
        try:
            _delete_one_goal(goal_name)
            deleted.append(goal_name)
        except Exception as exc:  # noqa: BLE001 — per-row failures never kill the batch
            failed.append({"name": goal_name, "reason": str(exc)})
    return {"deleted": deleted, "failed": failed}


@frappe.whitelist()
def list_goal_children(name: str | None = None) -> dict:
    """P1: direct children of a group goal (hrms tree semantics — Archived hidden).

    Each row carries ``has_children`` so the UI can render expandable nodes.
    """
    doc = _goal_doc_or_throw(name)
    _assert_own(doc.employee)

    rows = (
        frappe.get_all(
            GOAL_DOCTYPE,
            filters=[["parent_goal", "=", name], ["status", "!=", "Archived"]],
            fields=["name", "goal_name", "status", "progress", "kra", "end_date", "is_group"],
            order_by="creation asc",
        )
        or []
    )
    children = []
    for row in rows:
        child = dict(row)
        child["has_children"] = bool(frappe.db.count(GOAL_DOCTYPE, [["parent_goal", "=", row.get("name")]]))
        children.append(child)
    return {"parent": name, "children": children}


_GOAL_BULK_STATUS = {"Archived", "Closed", "Completed", "Unarchive", "Reopen"}


@frappe.whitelist()
def set_goal_status(names, status: str | None = None) -> dict:
    """Bulk status moves (partial-safe): Archived / Closed / Completed /
    Unarchive / Reopen (the last two recompute the status from progress,
    mirroring hrms set_status semantics for sticky states)."""
    if isinstance(names, str):
        try:
            parsed = json.loads(names)
        except (TypeError, ValueError):
            parsed = [names]
        names = parsed
    names = [n for n in (names or []) if n]

    if status not in _GOAL_BULK_STATUS:
        frappe.throw("Trạng thái không hợp lệ.")

    updated: list[str] = []
    failed: list[dict] = []
    for goal_name in names:
        try:
            doc = _goal_doc_or_throw(goal_name)
            _assert_own(doc.employee)
            _assert_cycle_open(getattr(doc, "appraisal_cycle", None))

            if status in ("Archived", "Closed"):
                doc.status = status
            elif status == "Completed":
                doc.status = "Completed"
                doc.progress = 100
            else:  # Unarchive / Reopen → recompute from progress
                doc.status = status_for_progress(getattr(doc, "progress", 0) or 0)

            doc.flags.ignore_mandatory = True
            doc.save()
            updated.append(goal_name)
        except Exception as exc:  # noqa: BLE001 — per-row failures never kill the batch
            failed.append({"name": goal_name, "reason": str(exc)})
    return {"updated": updated, "failed": failed}


@frappe.whitelist()
def update_goal_progress(name: str | None = None, progress: float = 0) -> dict:
    """FIX (plans/goals-frontend-crud.md §2.5): save via ``doc.save()`` so the
    native hrms controller runs — auto status, parent-group roll-up and the
    Appraisal goal score refresh. The old ``db.set_value`` bypassed all hooks."""
    doc = _goal_doc_or_throw(name)
    _assert_own(doc.employee)
    _assert_cycle_open(getattr(doc, "appraisal_cycle", None))

    if _to_bool(getattr(doc, "is_group", 0)):
        frappe.throw("Mục tiêu nhóm tự tính tiến độ từ các mục tiêu con.")
    if doc.status in ("Archived", "Closed"):
        frappe.throw("Mục tiêu đã lưu trữ/đóng — khôi phục trước khi cập nhật.")

    p = max(0.0, min(100.0, _num(progress)))
    doc.progress = p
    doc.status = status_for_progress(p)  # native validate re-derives this too
    doc.flags.ignore_mandatory = True
    doc.save()
    return {"name": name, "progress": doc.progress, "status": doc.status}


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
    out = {"cycles": [], "kras": [], "employees": []}
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
    if _is_manager():
        try:
            out["employees"] = (
                frappe.get_all(
                    "Employee",
                    filters=[["status", "=", "Active"]],
                    fields=["name", "employee_name"],
                    order_by="employee_name asc",
                )
                or []
            )
        except Exception:
            pass
    return out
