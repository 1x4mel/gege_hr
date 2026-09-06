"""Audit event API — plan v5 §13.5 / doctype-design §26.

The **VN Audit Event** trail is append-only: rows are written programmatically
by :func:`record` (called from the sensitive flows — leave/OT/correction/
advance submit/approve, work-session recalc, monthly lock/unlock, payroll
calculate/approve/publish, manual overrides) and read via :func:`audit_events`.
Pure payload/row/vocabulary helpers live in ``utils/audit.py`` (bench-free).

SPA contract:

  * ``audit_events`` → HR/System read with filters (type/category/employee/date)
  * ``audit_categories`` → coarse type grouping for the filter UI
  * ``record`` → internal helper used by the domain flows (not for end users)
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.utils import audit as audit_utils, pagination

DOCTYPE = "VN Audit Event"
_LIST_FIELDS = [
    "name",
    "audit_type",
    "company",
    "employee",
    "work_date",
    "actor",
    "actor_ip",
    "reference_doctype",
    "reference_name",
    "description",
    "old_value",
    "new_value",
    "created_at",
    "owner",
]


def _is_hr() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _require_hr() -> None:
    if not _is_hr():
        frappe.throw(_("Nhật ký kiểm toán chỉ dành cho HR."), frappe.PermissionError)


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def audit_categories() -> dict:
    """Return the coarse category → types map for the audit filter UI."""
    return {
        "categories": dict(audit_utils.AUDIT_CATEGORIES),
        "types": list(audit_utils.AUDIT_TYPES),
    }


# Broad-search fields for the audit trail (DNA §6.6 D — OR-combined free text).
_AUDIT_SEARCH_FIELDS = (
    "name",
    "audit_type",
    "description",
    "actor",
    "employee",
    "reference_name",
    "reference_doctype",
)


def _audit_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text audit search, or None."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [[field, "like", like] for field in _AUDIT_SEARCH_FIELDS]


# Sortable columns (Desk list-view parity, plan audit-center B1). ``order_by``
# is whitelisted here — a raw client string must never reach ``order_by``
# (SQL injection surface).
_SORTABLE_FIELDS = {"created_at", "audit_type", "actor", "employee", "work_date", "company"}


def _order_clause(order_by: str | None, order_dir: str | None) -> str:
    """Safe ``order_by`` clause built from whitelisted (field, direction)."""
    field = order_by if order_by in _SORTABLE_FIELDS else "created_at"
    direction = "asc" if str(order_dir or "").strip().lower() == "asc" else "desc"
    return f"{field} {direction}, name desc"


def _build_filters(
    *,
    company: str | None = None,
    employee: str | None = None,
    audit_type: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    actor: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
) -> dict:
    """Shared filter dict for the audit list / export / stats read paths.

    Single source of truth so the three endpoints can never drift
    (plan audit-center B1/B3/B5). Reference filters double as the
    "audit for one document" reader (parity checkout-miss timeline).
    """
    filters: dict = {}
    if company:
        filters["company"] = company
    if employee:
        filters["employee"] = employee
    if actor:
        filters["actor"] = actor
    if reference_doctype:
        filters["reference_doctype"] = reference_doctype
    if reference_name:
        filters["reference_name"] = reference_name
    # Category expands to its member types.
    if category and not audit_type:
        types = audit_utils.AUDIT_CATEGORIES.get(category)
        if types:
            filters["audit_type"] = ("in", list(types))
    elif audit_type:
        filters["audit_type"] = audit_type
    if from_date or to_date:
        window = _date_window(from_date, to_date)
        if window:
            filters["work_date"] = window
    return filters


# WP11 — CSV export ceiling: 10k rows keeps the response bounded even for a
# noisy month (a full audit export can be re-run per-month window).
EXPORT_MAX_ROWS = 10000


def _stamp_export(*, company, first_company, filters: dict, rows: int, truncated: bool) -> None:
    """Best-effort audit row for the export itself (Access Log parity, plan
    audit-center B5).

    Uses ``Manual Override`` — the 21-value vocabulary stays untouched.
    ``record()`` requires a company; fall back to the first exported row's
    company so single-company sites without a filter still get stamped.
    """
    try:
        record(
            audit_type="Manual Override",
            company=company or first_company,
            description=_("Xuất CSV nhật ký kiểm toán: {0} dòng (truncated={1})").format(
                rows, truncated
            ),
            new_value={
                "filters": {k: v for k, v in (filters or {}).items()},
                "rows": rows,
                "truncated": truncated,
            },
        )
    except Exception:
        frappe.log_error(title="audit.export stamp failed")


@frappe.whitelist()
def export_audit_csv(
    company: str | None = None,
    employee: str | None = None,
    audit_type: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    actor: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    download: int = 0,
) -> dict:
    """WP11 — export filtered audit events as an Excel-safe CSV.

    Same filter contract as :func:`audit_events` (type/category/employee/date
    window/free-text). Returns ``{filename, content, rows, truncated}``; with
    ``download=1`` the response is switched to a binary file download. Capped
    at :data:`EXPORT_MAX_ROWS` (``truncated`` flags the cut).
    """
    _require_hr()
    empty = {"filename": None, "content": None, "rows": 0, "truncated": False}
    if not _table_ready():
        return empty

    filters = _build_filters(
        company=company,
        employee=employee,
        audit_type=audit_type,
        category=category,
        from_date=from_date,
        to_date=to_date,
        actor=actor,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
    )
    or_filters = _audit_search_or_filters(search)

    rows = (
        frappe.get_all(
            DOCTYPE,
            filters=filters or None,
            or_filters=or_filters or None,
            fields=list(_LIST_FIELDS),
            order_by="created_at desc",
            limit_page_length=EXPORT_MAX_ROWS + 1,  # +1 detects the truncation
        )
        or []
    )
    truncated = len(rows) > EXPORT_MAX_ROWS
    rows = rows[:EXPORT_MAX_ROWS]

    # Access-Log parity: the export itself is a sensitive read — stamp it
    # (best-effort; never blocks the file response).
    _stamp_export(
        company=company,
        first_company=(rows[0].get("company") if rows else None),
        filters=filters,
        rows=len(rows),
        truncated=truncated,
    )

    csv_text = audit_utils.build_audit_csv([audit_utils.audit_row(r) for r in rows])

    if download:
        frappe.response.filename = f"audit_export_{getdate().isoformat()}.csv"
        frappe.response.filecontent = csv_text.encode("utf-8")
        frappe.response.type = "binary"
        return {"rows": len(rows), "truncated": truncated}

    return {
        "filename": f"audit_export_{getdate().isoformat()}.csv",
        "content": csv_text,
        "rows": len(rows),
        "truncated": truncated,
    }


# Lightweight fields needed to compute the SPA summary tiles server-side (the
# full set, not just the current page) — DNA §6.6 A.
_AUDIT_SUMMARY_FIELDS = ["name", "employee", "actor", "created_at"]


def _audit_summary(light_rows) -> dict:
    """Aggregate ``total`` + today/distinct counts for the SPA summary tiles."""
    today = getdate()
    today_count = 0
    employees: set = set()
    actors: set = set()
    for r in light_rows or []:
        created = r.get("created_at")
        if created:
            try:
                if getdate(created) == today:
                    today_count += 1
            except Exception:
                pass
        if r.get("employee"):
            employees.add(r["employee"])
        if r.get("actor"):
            actors.add(r["actor"])
    return {
        "total": len(light_rows or []),
        "today": today_count,
        "distinct_employees": len(employees),
        "distinct_actors": len(actors),
    }


@frappe.whitelist()
def audit_events(
    company: str | None = None,
    employee: str | None = None,
    audit_type: str | None = None,
    category: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    actor: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    order_by: str | None = None,
    order_dir: str | None = None,
    limit: int = 200,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """HR/System read. Filter by type, coarse category, employee, actor,
    reference doc, date window, or a free-text ``search`` (OR-matched across
    the row's text fields). Sort via the whitelisted ``order_by``/``order_dir``
    pair (default ``created_at desc`` — unchanged for legacy callers).

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates the *full* filtered set so the
    SPA summary tiles stay correct under pagination. Without ``page_size`` the
    legacy bare-list return is preserved (internal callers + bench tests).
    """
    _require_hr()
    if not _table_ready():
        if page_size:
            return {"data": [], "total": 0, "summary": _audit_summary([])}
        return []
    filters = _build_filters(
        company=company,
        employee=employee,
        audit_type=audit_type,
        category=category,
        from_date=from_date,
        to_date=to_date,
        actor=actor,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
    )
    or_filters = _audit_search_or_filters(search)
    order_clause = _order_clause(order_by, order_dir)

    if page_size:
        # Server-side summary over the full filtered set (not just the page).
        summary = _audit_summary(
            pagination.all_rows(
                DOCTYPE,
                fields=_AUDIT_SUMMARY_FIELDS,
                filters=filters,
                or_filters=or_filters,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = pagination.clamp_limit(page_size, default=20)
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.db.get_all(
                    DOCTYPE,
                    filters=filters,
                    or_filters=or_filters,
                    fields=_LIST_FIELDS,
                    order_by=order_clause,
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="audit.audit_events failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {
            "data": [audit_utils.audit_row(r) for r in rows],
            "total": summary["total"],
            "summary": summary,
        }

    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            or_filters=or_filters,
            fields=_LIST_FIELDS,
            order_by=order_clause,
            limit_page_length=pagination.clamp_limit(limit, default=200),
        )
    except Exception:
        frappe.log_error(title="audit.audit_events failed")
        return []
    return [audit_utils.audit_row(r) for r in rows]


@frappe.whitelist()
def approval_logs(
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    actor: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read-only trail of every approval action from ``VN Approval Log``.

    Surfaced in HrAuditView so HR can trace who approved/rejected/delegated each
    request (the VN Audit Event captures the *what*, this captures the granular
    approval workflow transitions). HR-gated, degrades gracefully when the
    DocType or table isn't installed.
    """
    _require_hr()
    if not frappe.db.table_exists("VN Approval Log"):
        return []
    filters: dict = {}
    if reference_doctype:
        filters["reference_doctype"] = reference_doctype
    if reference_name:
        filters["reference_name"] = reference_name
    if actor:
        filters["actor"] = actor
    try:
        rows = frappe.db.get_all(
            "VN Approval Log",
            filters=filters,
            fields=[
                "name",
                "reference_doctype",
                "reference_name",
                "action",
                "from_state",
                "to_state",
                "actor",
                "actor_employee",
                "comment",
                "action_at",
            ],
            order_by="action_at desc, name desc",
            limit_page_length=pagination.clamp_limit(limit, default=200),
        )
    except Exception:
        frappe.log_error(title="audit.approval_logs failed")
        return []
    return rows


# Whitelisted values for audit_distinct / audit_stats group_by (plan
# audit-center B2/B3). Unknown values are thrown out — never interpolated
# into a query.
_DISTINCT_FIELDS = ("actor", "company", "employee")
_STATS_GROUP_BY = ("audit_type", "actor", "employee", "company")


@frappe.whitelist()
def audit_distinct(
    field: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Distinct values of one audit column (``actor`` / ``company`` /
    ``employee``) ranked by frequency desc.

    Feeds the SPA filter selects (Desk link-field filter parity). Read via
    ``pagination.all_rows`` — pure get_all, no raw SQL.
    """
    _require_hr()
    field = (field or "").strip()
    if field not in _DISTINCT_FIELDS:
        frappe.throw(_("Trường lọc không hợp lệ."), frappe.PermissionError)
    if not _table_ready():
        return []
    rows = pagination.all_rows(DOCTYPE, fields=[field]) or []
    q = (search or "").strip().lower()
    counter: dict = {}
    for r in rows:
        value = r.get(field)
        if value in (None, ""):
            continue
        value = str(value).strip()
        if not value:
            continue
        if q and q not in value.lower():
            continue
        counter[value] = counter.get(value, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    cap = pagination.clamp_limit(limit, default=100)
    return [{"value": value, "count": count} for value, count in ranked[:cap]]


@frappe.whitelist()
def audit_stats(
    company: str | None = None,
    employee: str | None = None,
    audit_type: str | None = None,
    category: str | None = None,
    actor: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    group_by: str | None = "audit_type",
    days: int = 30,
) -> dict:
    """Aggregate view over the audit trail (Report Builder parity, B3).

    Same filter contract as :func:`audit_events` (minus reference filters —
    they make little sense for aggregates). Returns ``{"total", "groups"
    (top 20 by count desc), "series" (dense per-day counts for the last
    ``days`` days, clamped to [7, 90])}``. Aggregation is Python-side over
    light rows — parity :func:`_audit_summary`, no raw SQL.
    """
    _require_hr()
    key = (group_by or "audit_type").strip()
    if key not in _STATS_GROUP_BY:
        frappe.throw(_("Kiểu thống kê không hợp lệ."), frappe.PermissionError)
    if not _table_ready():
        return {"total": 0, "groups": [], "series": []}
    window = max(7, min(pagination.as_int(days, 30), 90))
    filters = _build_filters(
        company=company,
        employee=employee,
        audit_type=audit_type,
        category=category,
        from_date=from_date,
        to_date=to_date,
        actor=actor,
    )
    or_filters = _audit_search_or_filters(search)
    rows = (
        pagination.all_rows(
            DOCTYPE,
            fields=[key, "created_at"],
            filters=filters,
            or_filters=or_filters,
        )
        or []
    )

    group_counter: dict = {}
    day_counter: dict = {}
    for r in rows:
        label = r.get(key) or "—"
        group_counter[label] = group_counter.get(label, 0) + 1
        created = r.get("created_at")
        if created:
            try:
                day = getdate(created).isoformat()
                day_counter[day] = day_counter.get(day, 0) + 1
            except Exception:
                pass

    groups = [
        {"label": label, "count": count}
        for label, count in sorted(group_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:20]

    today = getdate()
    series = []
    for i in range(window - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        series.append({"date": day, "count": day_counter.get(day, 0)})
    return {"total": len(rows), "groups": groups, "series": series}


def _date_window(from_date: str | None, to_date: str | None):
    frm = getdate(from_date) if from_date else None
    to = getdate(to_date) if to_date else None
    if frm and to:
        return ("between", [frm, to])
    if frm:
        return (">=", frm)
    if to:
        return ("<=", to)
    return None


# --------------------------------------------------------------------------- #
# Write endpoint — internal, used by domain flows
# --------------------------------------------------------------------------- #
# NOT whitelisted on purpose: the audit trail is legal evidence (NĐ 13/2023).
# A whitelisted record() let any logged-in user forge audit events with an
# arbitrary ``actor``; internal callers use log()/record() directly in code.
def _publish_created(
    name: str | None,
    *,
    audit_type: str | None,
    company: str | None,
    actor: str | None,
    employee: str | None = None,
) -> None:
    """Best-effort realtime ping so an open /hr/audit tab can offer a refresh
    (plan audit-center B4). Payload is deliberately light — no old/new values.

    Broadcast pattern mirrors ``checkout_miss._publish``; only the HR-gated
    audit view subscribes.
    """
    try:
        frappe.publish_realtime(
            "audit_event_created",
            {
                "name": name,
                "audit_type": audit_type,
                "company": company,
                "actor": actor,
                "employee": employee,
            },
        )
    except Exception:
        frappe.log_error(title="audit.publish_realtime failed")


def record(
    audit_type: str,
    company: str,
    actor: str | None = None,
    employee: str | None = None,
    work_date=None,
    actor_ip: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str | None = None,
    old_value=None,
    new_value=None,
) -> str | None:
    """Mint one append-only audit row. Best-effort: never aborts the caller.

    Returns the new row name on success, ``None`` on any failure (the caller's
    business transition must not depend on auditing succeeding).
    """
    if not _table_ready():
        return None
    try:
        payload = audit_utils.audit_payload(
            audit_type=audit_type,
            company=company,
            actor=actor or frappe.session.user,
            employee=employee,
            work_date=work_date,
            actor_ip=actor_ip or _client_ip(),
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except ValueError:
        return None
    try:
        doc = frappe.get_doc(payload)
        # Privileged system audit trail — must always persist regardless of the
        # acting user's role; an audit write must never block a business op.
        doc.insert(ignore_permissions=True)
        _publish_created(
            doc.name,
            audit_type=audit_type,
            company=company,
            actor=payload.get("actor") or frappe.session.user,
            employee=employee,
        )
        return doc.name
    except Exception:
        frappe.log_error(title="audit.record failed")
        return None


# Map of transaction type → audit_type for the matrix-driven approval inbox.
# Leave Application has no dedicated "approve" vocab (its submit/cancel are
# logged at the leave endpoints), so only the three request DocTypes map here.
APPROVE_AUDIT_TYPE = {
    "Overtime Request": "OT Approve",
    "Correction Request": "Correction Approve",
    "Salary Advance Request": "Advance Approve",
}


def log(
    audit_type: str,
    *,
    doc: dict | None = None,
    company: str | None = None,
    employee: str | None = None,
    work_date=None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str | None = None,
    old_value=None,
    new_value=None,
) -> str | None:
    """Doc-aware convenience wrapper around :func:`record` for the domain flows.

    Resolves ``company`` / ``employee`` / ``work_date`` / reference from ``doc``
    when not supplied explicitly (the usual case — the flow already holds the
    document). Fully best-effort: any failure is swallowed so the caller's
    business transition never depends on auditing succeeding. Returns the new
    row name on success, ``None`` otherwise.

    Mirrors :func:`utils.notify.push_notification`'s swallow-and-return contract.
    """
    try:
        d = dict(doc or {})
        company = company or d.get("company")
        if not company:
            return None  # nothing safe to attribute the event to
        employee = employee or d.get("employee")
        if work_date in (None, ""):
            work_date = d.get("work_date") or d.get("posting_date")
        if reference_doctype is None:
            reference_doctype = d.get("doctype")
        if reference_name is None:
            reference_name = d.get("name")
        return record(
            audit_type=audit_type,
            company=company,
            employee=employee,
            work_date=work_date,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except Exception:
        return None


def _client_ip() -> str | None:
    try:
        return frappe.local.request_ip or None
    except Exception:
        return None
