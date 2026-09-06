"""Leave blackout period API — plan v5 §11.4 / doctype-design §31 + desk-free
parity (``plans/plan-blackout-desk-free.md``).

HR-only CRUD for **VN Leave Blackout Period** + read endpoints the leave
preview engine consults. Pure date/range math lives in
``utils/leave_blackout.py`` (bench-free).

SPA contract (plan blackout desk-free §3):

  * ``blackout_periods``         → list (sort/filter/pagination + summary)
  * ``blackout_distinct``        → filter-select options feed
  * ``blackout_versions``        → Version timeline (track_changes parity)
  * ``blackout_impact``          → leave applications overlapped by a rule
  * ``create_blackout``          → mint a rule (dup-name + overlap guards)
  * ``update_blackout``          → edit fields (HR Manager only)
  * ``delete_blackout``          → remove (HR Manager only)
  * ``bulk_update_blackouts``    → multi-select edit (HR Manager only)
  * ``bulk_delete_blackouts``    → multi-select remove (HR Manager only)
  * ``export_blackout_csv``      → filtered CSV (+ audit stamp)
  * ``evaluate_leave_blackout``  → decide block/require-approval/warn
  * ``on_doc_event``             → doc_events hook → realtime ``blackout_rule_changed``

Read endpoints gate on ``{HR User, HR Manager, System Manager}`` (permission
matrix parity — DocType gives HR User read). The employee leave flow calls the
internal ``_list_blackouts`` helper (no gate) via ``evaluate_leave_blackout``,
so applying for leave never depends on HR roles.
"""

from __future__ import annotations

import json
from datetime import date

import frappe
from frappe import _

from gege_hr.gege_hr.utils import leave_blackout as blackout_utils, pagination

DOCTYPE = "VN Leave Blackout Period"
_LIST_FIELDS = [
    "name",
    "blackout_name",
    "company",
    "branch",
    "department",
    "from_date",
    "to_date",
    "applies_to_leave_type",
    "is_active",
    "action",
    "reason",
    "modified",
    "modified_by",
    "owner",
]

# Bulk endpoints process at most this many documents per call.
BULK_MAX_NAMES = 50

# Export ceiling (parity audit EXPORT_MAX_ROWS; a blackout list is small but a
# runaway export must stay bounded).
EXPORT_MAX_ROWS = 1000

_REALTIME_EVENT = "blackout_rule_changed"


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "System Manager"})


def _require_hr() -> None:
    """Read gate — parity the DocType permission matrix (HR User has read)."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not (roles & {"HR User", "HR Manager", "System Manager"}):
        frappe.throw(_("Yêu cầu quyền HR."), frappe.PermissionError)


def _require_hr_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not (roles & {"HR Manager", "System Manager"}):
        frappe.throw(_("Yêu cầu quyền HR Manager."), frappe.PermissionError)


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


def _truthy(value) -> bool:
    """Lenient truth for args Frappe may pass as "1"/"0"/bool/None."""
    if value is None or value == "":
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _coerce_names(names) -> list[str]:
    """Normalise a ``names`` arg (list / JSON string) into a list of strings."""
    if isinstance(names, str):
        try:
            names = json.loads(names)
        except (TypeError, ValueError):
            names = [n for n in (p.strip() for p in names.split(",")) if n]
    if not isinstance(names, (list, tuple)):
        frappe.throw(_("Danh sách tên không hợp lệ."), frappe.ValidationError)
    out = [str(n).strip() for n in names if str(n or "").strip()]
    if not out:
        frappe.throw(_("Danh sách tên không được để trống."), frappe.ValidationError)
    if len(out) > BULK_MAX_NAMES:
        frappe.throw(
            _("Chỉ xử lý tối đa {0} kỳ cấm mỗi lần.").format(BULK_MAX_NAMES),
            frappe.ValidationError,
        )
    return out


def _coerce_active(value) -> int | None:
    """``is_active`` → 1/0 filter value; omitted/blank → ``None`` (scope 'all').

    The SPA passes ``isActive: null`` for the "Tất cả" scope (JSON null →
    Python None); previously that fell through to an implicit active-only
    filter, silently hiding paused rules from the admin table.
    """
    if value is None or value == "" or value == "None":
        return None
    try:
        return 1 if int(value) else 0
    except (TypeError, ValueError):
        return None


# Broad-search fields for the blackout list (DNA §6.6 D — OR-combined free text).
_BLACKOUT_SEARCH_FIELDS = (
    "name",
    "blackout_name",
    "company",
    "branch",
    "department",
    "applies_to_leave_type",
    "reason",
)


def _blackout_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text blackout search, or None."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [[field, "like", like] for field in _BLACKOUT_SEARCH_FIELDS]


# Sortable columns (Desk list-view parity, plan blackout §B1). ``order_by`` is
# whitelisted here — a raw client string must never reach ``order_by`` (SQL
# injection surface), parity audit ``_order_clause``.
_SORTABLE = {"from_date", "to_date", "blackout_name", "company", "action", "modified"}


def _order_clause(order_by: str | None, order_dir: str | None) -> str:
    """Safe ``order_by`` clause built from whitelisted (field, direction)."""
    field = order_by if order_by in _SORTABLE else "from_date"
    direction = "asc" if str(order_dir or "").strip().lower() == "asc" else "desc"
    return f"{field} {direction}, name desc"


def _blackout_filters(
    *,
    company: str | None = None,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active=None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list:
    """Shared list-form filter builder for the list / export read paths.

    List form (DNA §6.6 B) — a dict cannot hold the date-window overlap (two
    conditions touching different date columns), and ``between`` renders broken
    SQL for the same field twice.
    """
    filters: list = []
    if company:
        filters.append(["company", "=", company])
    if branch:
        filters.append(["branch", "=", branch])
    if department:
        filters.append(["department", "=", department])
    if applies_to_leave_type:
        filters.append(["applies_to_leave_type", "=", applies_to_leave_type])
    active = _coerce_active(is_active)
    if active is not None:
        filters.append(["is_active", "=", active])
    if action:
        filters.append(["action", "=", action])
    # Date-window overlap (rule window ∩ selected window). NOT `between`
    # (DNA §6.6 B): two conditions on different date columns.
    if from_date:
        filters.append(["to_date", ">=", from_date])
    if to_date:
        filters.append(["from_date", "<=", to_date])
    return filters


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
_BLACKOUT_SUMMARY_FIELDS = ["name", "is_active", "action"]


def _blackout_summary(light_rows) -> dict:
    """Per-bucket counts over the full filtered set (SPA summary tiles)."""
    actions = pagination.bucket_counts(light_rows, "action")
    active = sum(1 for r in light_rows or [] if r.get("is_active"))
    return {
        "total": len(light_rows or []),
        "active": active,
        "block": actions.get("Block", 0),
        "require_approval": actions.get("Require HR Approval", 0),
    }


def _list_blackouts(
    company: str | None = None,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active=None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    order_by: str | None = None,
    order_dir: str | None = None,
    limit: int = 200,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Internal rule loader — NO role gate.

    ``evaluate_leave_blackout`` (the employee leave preview/apply flow) calls
    this directly, so leave applications never depend on HR roles (plan §1
    bảo toàn luồng employee). The whitelisted :func:`blackout_periods` wraps it
    with the read gate.
    """
    if not _table_ready():
        if page_size:
            return {"data": [], "total": 0, "summary": _blackout_summary([])}
        return []
    filters = _blackout_filters(
        company=company,
        branch=branch,
        department=department,
        applies_to_leave_type=applies_to_leave_type,
        is_active=is_active,
        action=action,
        from_date=from_date,
        to_date=to_date,
    )
    or_filters = _blackout_search_or_filters(search)
    order = _order_clause(order_by, order_dir)

    if page_size:
        summary = _blackout_summary(
            pagination.all_rows(
                DOCTYPE,
                fields=_BLACKOUT_SUMMARY_FIELDS,
                filters=filters,
                or_filters=or_filters,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.db.get_all(
                    DOCTYPE,
                    filters=filters,
                    or_filters=or_filters,
                    fields=_LIST_FIELDS,
                    order_by=order,
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="blackout.blackout_periods failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {
            "data": [blackout_utils.blackout_row(r) for r in rows],
            "total": summary["total"],
            "summary": summary,
        }

    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            or_filters=or_filters,
            fields=_LIST_FIELDS,
            order_by=order,
            limit_page_length=pagination.clamp_limit(limit, default=200),
        )
    except Exception:
        frappe.log_error(title="blackout.blackout_periods failed")
        return []
    return [blackout_utils.blackout_row(r) for r in rows]


@frappe.whitelist()
def blackout_periods(
    company: str | None = None,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active=None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    order_by: str | None = None,
    order_dir: str | None = None,
    limit: int = 200,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """List blackout rules (HR-gated read — plan blackout §B1 / decision D2).

    ``search`` performs a server-side broad LIKE across the rule's text fields
    (DNA §6.6 D, HR-BL blackout). Per-column popover filters (DNA Law #2) are
    all server-side: ``company`` / ``branch`` / ``department`` /
    ``applies_to_leave_type`` / ``action`` (exact), and a date-window
    ``from_date``–``to_date`` intersection. ``order_by`` / ``order_dir`` sort
    on a whitelist (Desk sortable-column parity).

    ``is_active`` omitted → no active filter (scope "Tất cả"); pass ``1``/``0``
    to narrow.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    (``total`` + ``summary`` aggregate the *full* filtered set so the SPA
    summary tiles stay correct under pagination). Without ``page_size`` the
    legacy bare-list return is preserved.
    """
    _require_hr()
    return _list_blackouts(
        company=company,
        branch=branch,
        department=department,
        applies_to_leave_type=applies_to_leave_type,
        is_active=is_active,
        action=action,
        from_date=from_date,
        to_date=to_date,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        page=page,
        page_size=page_size,
    )


_DISTINCT_FIELDS = ("company", "branch", "department", "applies_to_leave_type", "action")


@frappe.whitelist()
def blackout_distinct(field: str | None = None, search: str = "", limit=100) -> list[dict]:
    """Distinct non-empty values of one column → filter-select options feed.

    Desk link-field filter parity (plan blackout §B2). Values are ranked by
    frequency desc then alphabetically, capped at 100.
    """
    _require_hr()
    field = (field or "").strip()
    if field not in _DISTINCT_FIELDS:
        frappe.throw(_("Trường lọc không hợp lệ."), frappe.ValidationError)
    if not _table_ready():
        return []
    filters = None
    q = (search or "").strip()
    if q:
        filters = [[field, "like", f"%{pagination.escape_like(q)}%"]]
    rows = pagination.all_rows(DOCTYPE, fields=[field], filters=filters)
    counts: dict = {}
    for r in rows or []:
        value = r.get(field)
        if value is None or str(value).strip() == "":
            continue
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    cap = pagination.clamp_limit(limit, default=100, maximum=100)
    return [{"value": value} for value, _count in ranked[:cap]]


@frappe.whitelist()
def blackout_versions(name: str) -> dict:
    """Version timeline for one rule — Desk Form-View timeline parity (§B3).

    Reads the ``Version`` docs Frappe mints because the DocType sets
    ``track_changes: 1``. ``data`` is best-effort parsed per entry: a corrupt
    payload drops that entry instead of failing the endpoint.
    """
    _require_hr()
    if not _table_ready():
        return {"name": name, "versions": []}
    if not frappe.db.exists(DOCTYPE, name):
        frappe.throw(_("Kỳ cấm nghỉ {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    try:
        rows = (
            frappe.db.get_all(
                "Version",
                filters={"ref_doctype": DOCTYPE, "ref_name": name},
                fields=["name", "owner", "creation", "modified", "data"],
                order_by="creation desc",
                limit_page_length=50,
            )
            or []
        )
    except Exception:
        frappe.log_error(title="blackout.blackout_versions failed")
        return {"name": name, "versions": []}
    versions: list[dict] = []
    for row in rows:
        try:
            data = json.loads(row.get("data") or "{}")
        except (TypeError, ValueError):
            continue
        changed = data.get("changed") or []
        changes = [
            {"field": str(c[0]), "old": c[1], "new": c[2]}
            for c in changed
            if isinstance(c, (list, tuple)) and len(c) >= 3
        ]
        if not changes:
            continue
        versions.append(
            {
                "name": row.get("name"),
                "at": str(row.get("creation") or ""),
                "actor": row.get("owner"),
                "changes": changes,
            }
        )
    return {"name": name, "versions": versions}


def _impact_fields() -> list[str]:
    """Leave Application list fields, plus the ``vn_`` custom fields when the
    migrated fields exist on this site (parity ``_application_fields``)."""
    fields = [
        "name",
        "employee",
        "employee_name",
        "leave_type",
        "from_date",
        "to_date",
        "status",
        "posting_date",
        "company",
    ]
    try:
        meta = frappe.get_meta("Leave Application")
        for extra in ("vn_requires_blackout_approval", "vn_blackout_decision"):
            if meta and meta.has_field(extra):
                fields.append(extra)
    except Exception:
        pass
    return fields


@frappe.whitelist()
def blackout_impact(name: str) -> dict:
    """Leave applications overlapped by one rule — drill-down parity (§B4).

    Matches non-cancelled applications of the rule's company whose leave window
    intersects the rule window. Blackout-flagged applications (stamped at apply
    time) surface first, then newest ``posting_date`` (parity
    ``sort_pending_approvals``). Capped at 200.
    """
    _require_hr()
    if not _table_ready():
        return {"rule": {}, "applications": [], "total": 0}
    if not frappe.db.exists(DOCTYPE, name):
        frappe.throw(_("Kỳ cấm nghỉ {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    rule = frappe.db.get_value(
        DOCTYPE,
        name,
        ["name", "blackout_name", "company", "from_date", "to_date", "action", "is_active"],
        as_dict=True,
    )
    if not rule:
        return {"rule": {}, "applications": [], "total": 0}
    try:
        rows = (
            frappe.db.get_all(
                "Leave Application",
                filters=[
                    ["company", "=", rule.get("company")],
                    ["docstatus", "<", 2],
                    ["status", "!=", "Cancelled"],
                    ["from_date", "<=", str(rule.get("to_date"))],
                    ["to_date", ">=", str(rule.get("from_date"))],
                ],
                fields=_impact_fields(),
                order_by="posting_date desc",
                limit_page_length=201,
            )
            or []
        )
    except Exception:
        frappe.log_error(title="blackout.blackout_impact failed")
        rows = []
    total = len(rows)
    rows = rows[:200]
    flagged = [r for r in rows if r.get("vn_requires_blackout_approval")]
    plain = [r for r in rows if not r.get("vn_requires_blackout_approval")]
    rows = flagged + plain
    return {
        "rule": blackout_utils.blackout_row(rule),
        "applications": rows,
        "total": total,
    }


@frappe.whitelist()
def evaluate_leave_blackout(
    from_date: str,
    to_date: str,
    leave_type: str | None = None,
    company: str | None = None,
    employee: str | None = None,
) -> dict:
    """Decision endpoint for the leave preview/apply flow.

    Loads the active blackout rules (optionally company-scoped) and delegates
    to :func:`leave_blackout.evaluate_blackout`. When ``company`` is omitted but
    ``employee`` is supplied, the company is resolved from the Employee record so
    the SPA leave form — which knows the employee, not the company — still gets a
    correctly scoped decision (and rules from other companies do not leak in).

    Uses the internal ``_list_blackouts`` loader (no role gate): employees apply
    for leave without HR roles (plan §1 bảo toàn luồng employee).
    """
    if not company and employee:
        try:
            company = frappe.db.get_value("Employee", employee, "company") or None
        except Exception:
            company = None
    rules = _list_blackouts(company=company, is_active=1)
    # DB rows already normalised; pass back the dict shape evaluate expects.
    return blackout_utils.evaluate_blackout(
        from_date=from_date,
        to_date=to_date,
        leave_type=leave_type,
        rules=rules,
    )


# --------------------------------------------------------------------------- #
# Write endpoints (HR Manager)
# --------------------------------------------------------------------------- #
def _throw_duplicate(name: str) -> None:
    frappe.throw(
        _("Tên kỳ cấm '{0}' đã tồn tại.").format(name),
        frappe.ValidationError,
    )


def _guard_overlap(
    *,
    company: str | None,
    from_date,
    to_date,
    branch=None,
    department=None,
    leave_type=None,
    exclude: str | None = None,
    force=None,
) -> None:
    """Throw (ValidationError) when an active same-scope rule overlaps the
    window and the caller did not ``force`` the override (plan §B7 / D3)."""
    if _truthy(force):
        return
    rules = _list_blackouts(company=company, is_active=1)
    clashes = blackout_utils.overlapping_rules(
        rules,
        from_date=from_date,
        to_date=to_date,
        branch=branch,
        department=department,
        leave_type=leave_type,
        exclude=exclude,
    )
    if clashes:
        names = ", ".join(
            str(r.get("blackout_name") or r.get("name") or "") for r in clashes
        )
        frappe.throw(
            _("Chồng lấp với kỳ cấm: {0}. Gửi lại với force=1 để ghi đè.").format(names),
            frappe.ValidationError,
        )


@frappe.whitelist()
def create_blackout(
    blackout_name: str,
    company: str,
    from_date: str,
    to_date: str,
    reason: str,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active: bool = True,
    action: str = "Warning",
    force=None,
) -> dict:
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    try:
        payload = blackout_utils.blackout_payload(
            blackout_name=blackout_name,
            company=company,
            from_date=from_date,
            to_date=to_date,
            reason=reason,
            branch=branch,
            department=department,
            applies_to_leave_type=applies_to_leave_type,
            is_active=is_active,
            action=action,
        )
    except ValueError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    # Friendly duplicate-name guard (autoname field:blackout_name + unique=1
    # would otherwise surface a raw DuplicateEntryError).
    new_name = str(payload.get("blackout_name") or "").strip()
    if frappe.db.exists(DOCTYPE, {"blackout_name": new_name}):
        _throw_duplicate(new_name)
    _guard_overlap(
        company=payload.get("company"),
        from_date=payload.get("from_date"),
        to_date=payload.get("to_date"),
        branch=payload.get("branch"),
        department=payload.get("department"),
        leave_type=payload.get("applies_to_leave_type"),
        force=force,
    )
    doc = frappe.get_doc(payload)
    dup_error = getattr(frappe, "DuplicateEntryError", None)
    try:
        doc.insert()
    except Exception as exc:  # race: two HR minting the same name concurrently
        if dup_error is not None and isinstance(exc, dup_error):
            _throw_duplicate(new_name)
        raise
    return {"name": doc.name, "action": doc.action, "message": _("Đã tạo kỳ cấm nghỉ.")}


_BULK_UPDATE_ALLOWED = {"is_active", "action"}


@frappe.whitelist()
def update_blackout(name: str, force=None, **fields) -> dict:
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    doc = frappe.get_doc(DOCTYPE, name)
    allowed = {
        "blackout_name",
        "from_date",
        "to_date",
        "reason",
        "branch",
        "department",
        "applies_to_leave_type",
        "is_active",
        "action",
    }
    for key, value in fields.items():
        if key in allowed:
            doc.set(key, value)
    # Re-validate the date window before saving.
    if not blackout_utils.is_valid_date_range(doc.from_date, doc.to_date):
        frappe.throw(_("from_date phải trước hoặc bằng to_date."), frappe.ValidationError)
    # Friendly duplicate-name guard (renaming onto an existing rule).
    new_name = str(doc.get("blackout_name") or "").strip()
    existing = frappe.db.exists(DOCTYPE, {"blackout_name": new_name})
    if existing and existing != doc.name:
        _throw_duplicate(new_name)
    _guard_overlap(
        company=doc.company,
        from_date=doc.from_date,
        to_date=doc.to_date,
        branch=doc.branch,
        department=doc.department,
        leave_type=doc.applies_to_leave_type,
        exclude=doc.name,
        force=force,
    )
    doc.save()
    return {"name": doc.name, "message": _("Đã cập nhật.")}


@frappe.whitelist()
def delete_blackout(name: str) -> dict:
    _require_hr_manager()
    if not _table_ready():
        return {"name": name, "deleted": False}
    try:
        frappe.delete_doc(DOCTYPE, name)
    except Exception:
        frappe.log_error(title="blackout.delete_blackout failed")
        return {"name": name, "deleted": False}
    return {"name": name, "deleted": True, "message": _("Đã xóa.")}


@frappe.whitelist()
def bulk_update_blackouts(names, fields=None) -> dict:
    """Multi-select edit (Desk bulk parity, plan §B5). One failing document
    does not abort the batch — failures are reported per name."""
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    names = _coerce_names(names)
    if isinstance(fields, str):
        try:
            fields = json.loads(fields or "{}")
        except (TypeError, ValueError):
            fields = {}
    if not isinstance(fields, dict) or not fields:
        frappe.throw(_("Không có trường nào để cập nhật."), frappe.ValidationError)
    unsupported = set(fields) - _BULK_UPDATE_ALLOWED
    if unsupported:
        frappe.throw(
            _("Chỉ cho phép cập nhật hàng loạt: {0}.").format(
                ", ".join(sorted(_BULK_UPDATE_ALLOWED))
            ),
            frappe.ValidationError,
        )
    updated: list[str] = []
    failed: list[dict] = []
    for name in names:
        try:
            doc = frappe.get_doc(DOCTYPE, name)
            if "is_active" in fields:
                doc.set("is_active", 1 if _truthy(fields.get("is_active")) else 0)
            if "action" in fields:
                action = str(fields.get("action") or "").strip()
                if action not in blackout_utils.BLACKOUT_ACTIONS:
                    frappe.throw(_("Hành động không hợp lệ."), frappe.ValidationError)
                doc.set("action", action)
            doc.save()
            updated.append(name)
        except Exception as exc:
            frappe.log_error(title=f"blackout.bulk_update({name}) failed")
            failed.append({"name": name, "error": str(exc)})
    return {"updated": updated, "failed": failed}


@frappe.whitelist()
def bulk_delete_blackouts(names) -> dict:
    """Multi-select remove (Desk bulk parity, plan §B5)."""
    _require_hr_manager()
    if not _table_ready():
        frappe.throw(_("Tính năng cấm nghỉ chưa được cài đặt."), frappe.ValidationError)
    names = _coerce_names(names)
    deleted: list[str] = []
    failed: list[dict] = []
    for name in names:
        try:
            frappe.delete_doc(DOCTYPE, name)
            deleted.append(name)
        except Exception as exc:
            frappe.log_error(title=f"blackout.bulk_delete({name}) failed")
            failed.append({"name": name, "error": str(exc)})
    return {"deleted": deleted, "failed": failed}


def _stamp_export_audit(*, company, description, new_value) -> None:
    """Best-effort audit row for the export itself (Access-Log parity, §B6/D1).

    ``Manual Override`` keeps the 21-value vocabulary untouched (parity audit
    ``_stamp_export``). The lazy import keeps the bench-free test harness free
    of the audit module's own frappe surface; a stamping failure never blocks
    the export response.
    """
    try:
        from gege_hr.gege_hr.api.audit import record as _audit_record

        _audit_record(
            audit_type="Manual Override",
            company=company,
            description=description,
            new_value=new_value,
        )
    except Exception:
        frappe.log_error(title="blackout.export stamp failed")


# --------------------------------------------------------------------------- #
# Export (Desk Data-Export parity, plan §B6 + decision D1)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def export_blackout_csv(
    company: str | None = None,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active=None,
    action: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    download: int = 0,
) -> dict:
    """Export the filtered blackout list as an Excel-safe CSV.

    Same filter contract as :func:`blackout_periods`. Returns
    ``{filename, content, rows, truncated}``; with ``download=1`` the response
    switches to a binary file download. Each export stamps one audit row
    (``Manual Override`` — the 21-value vocabulary stays untouched, decision D1).
    """
    _require_hr()
    empty = {"filename": None, "content": None, "rows": 0, "truncated": False}
    if not _table_ready():
        return empty
    filters = _blackout_filters(
        company=company,
        branch=branch,
        department=department,
        applies_to_leave_type=applies_to_leave_type,
        is_active=is_active,
        action=action,
        from_date=from_date,
        to_date=to_date,
    )
    or_filters = _blackout_search_or_filters(search)
    try:
        rows = (
            frappe.db.get_all(
                DOCTYPE,
                filters=filters,
                or_filters=or_filters,
                fields=_LIST_FIELDS,
                order_by=_order_clause(None, None),
                limit_page_length=EXPORT_MAX_ROWS + 1,  # +1 detects the truncation
            )
            or []
        )
    except Exception:
        frappe.log_error(title="blackout.export_blackout_csv failed")
        return empty
    truncated = len(rows) > EXPORT_MAX_ROWS
    rows = [blackout_utils.blackout_row(r) for r in rows[:EXPORT_MAX_ROWS]]

    # Access-Log parity (decision D1): the export is a sensitive read — stamp
    # it best-effort; never blocks the file response. 0 rows + no company
    # filter → nothing to attribute the export to → skip.
    if rows:
        export_filters = {
            "company": company,
            "branch": branch,
            "department": department,
            "applies_to_leave_type": applies_to_leave_type,
            "action": action,
            "from_date": from_date,
            "to_date": to_date,
            "search": search,
        }
        _stamp_export_audit(
            company=company or rows[0].get("company"),
            description=_("Xuất CSV kỳ cấm nghỉ: {0} dòng (truncated={1})").format(
                len(rows), truncated
            ),
            new_value={
                "filters": {k: v for k, v in export_filters.items() if v},
                "rows": len(rows),
                "truncated": truncated,
            },
        )

    csv_text = blackout_utils.build_blackout_csv(rows)
    filename = f"blackout_export_{date.today().isoformat()}.csv"

    if _truthy(download):
        frappe.response.filename = filename
        frappe.response.filecontent = csv_text.encode("utf-8")
        frappe.response.type = "binary"
        return {"filename": None, "content": None, "rows": len(rows), "truncated": truncated}

    return {
        "filename": filename,
        "content": csv_text,
        "rows": len(rows),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- #
# Realtime hook (doc_events → plan blackout §B8). NOT whitelisted.
# --------------------------------------------------------------------------- #
def on_doc_event(doc, method=None) -> None:
    """Best-effort broadcast so the SPA list shows the "N quy tắc thay đổi"
    pill — parity ``audit_event_created`` (plan audit-center §B4).

    Registered for ``after_insert`` / ``on_update`` / ``on_trash`` in
    ``hooks.py``. Payload is intentionally light (no old/new values).
    """
    try:
        frappe.publish_realtime(
            _REALTIME_EVENT,
            {
                "name": getattr(doc, "name", None),
                "event": str(method or ""),
                "company": getattr(doc, "company", None),
            },
        )
    except Exception:
        frappe.log_error(title="blackout.publish_realtime failed")
