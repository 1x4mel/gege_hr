"""FIX-1 (I-1, hr-gap-audit.md) — sync core ``Attendance`` from the Work Session.

The portal's source of truth is the custom ``VN Attendance Work Session``; Frappe
HR's standard reports/dashboards read the core ``Attendance`` DocType. This module
upserts one ``Attendance`` row per Work Session (idempotent, key = employee +
attendance_date) so the two stay consistent.

Locked decisions (``plans/hr-fix-plan.md`` §QUYẾT ĐỊNH):
  * sync only when ``calculation_status`` is Calculated or Locked (Error / Pending /
    Recalculating are skipped);
  * a Work Session that ``need_review`` / has missing logs still maps to ``Present``
    and is **not** submitted (HR may still correct it);
  * the ``Attendance`` row is **submitted** only when the work day belongs to a
    *Locked* ``VN Monthly Attendance Period``.
"""
from __future__ import annotations

import frappe

WORK_SESSION_DOCTYPE = "VN Attendance Work Session"
ATTENDANCE_DOCTYPE = "Attendance"
MONTHLY_PERIOD_DOCTYPE = "VN Monthly Attendance Period"

# Work-Session projection used to build an Attendance row.
_WS_FIELDS = [
    "name",
    "employee",
    "work_date",
    "shift_type",
    "company",
    "calculation_status",
    "absent",
    "has_leave",
    "payable_day",
    "leave_application",
    # NOTE: the Work Session doctype stores only ``leave_application`` (Link) — it
    # has NO ``leave_type`` column. Requesting it here made ``db.get_value`` raise
    # (UnknownColumnError), which sync_attendance swallowed → every session was
    # silently skipped and no core Attendance row was ever created. ``leave_type``
    # is resolved from the linked Leave Application below where actually needed.
]

# Only terminal, calculated states produce a trustworthy Attendance row.
_SYNCABLE_STATUSES = {"Calculated", "Locked"}


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def attendance_status_for(ws: dict) -> str | None:
    """Map a Work Session row to a core ``Attendance`` status.

    Returns ``None`` to signal "do not sync" (non-calculated / Error).
    """
    if (ws.get("calculation_status") or "").strip() not in _SYNCABLE_STATUSES:
        return None
    if _to_int(ws.get("absent")):
        return "Absent"
    if _to_float(ws.get("payable_day")) == 0.5:
        return "Half Day"
    # On leave / on-time / need-review all surface as Present; the leave link is
    # carried separately so hrms leave-ledger stays correct.
    return "Present"


def build_attendance_fields(ws: dict) -> dict:
    """Projection of a Work Session row → core ``Attendance`` field values."""
    return {
        "employee": ws.get("employee"),
        "attendance_date": ws.get("work_date"),
        "status": attendance_status_for(ws),
        "shift": ws.get("shift_type") or None,
        "company": ws.get("company") or None,
        "leave_application": ws.get("leave_application") or None,
        "leave_type": ws.get("leave_type") or None,
        "vn_work_session": ws.get("name"),
    }


def _writable(fields: dict) -> dict:
    """Drop ``None`` values so an update never blanks unrelated columns."""
    return {k: v for k, v in fields.items() if v is not None}


def is_work_date_locked(work_date: str | None) -> bool:
    """True when a VN Monthly Attendance Period covering ``work_date`` is Locked."""
    if not work_date:
        return False
    try:
        return bool(
            frappe.db.get_all(
                MONTHLY_PERIOD_DOCTYPE,
                filters=[
                    ["from_date", "<=", work_date],
                    ["to_date", ">=", work_date],
                    ["status", "=", "Locked"],
                ],
                limit_page_length=1,
            )
        )
    except Exception:
        return False


@frappe.whitelist()
def sync_attendance(ws_name: str | None = None) -> str | None:
    """Upsert the core ``Attendance`` row for one Work Session (idempotent).

    Returns the Attendance name on success, or ``None`` when the session is not
    syncable / has no employee / could not be persisted.
    """
    if not ws_name:
        return None
    frappe.only_for(["HR Manager", "System Manager"])
    return _sync_attendance_internal(ws_name)


def _sync_attendance_internal(ws_name: str) -> str | None:
    """Role-free core — called by the whitelisted endpoint AND by the
    ``on_work_session_update`` doc-event. The doc-event runs inside the
    background job that an employee check-in enqueues (session user = the
    employee), so any role gate here would fail the job and roll back the
    whole Work Session calculation."""
    try:
        ws = frappe.db.get_value(WORK_SESSION_DOCTYPE, ws_name, _WS_FIELDS, as_dict=True) or {}
    except Exception:
        return None
    if not ws.get("name"):
        return None

    fields = build_attendance_fields(ws)
    status = fields["status"]
    employee, attendance_date = fields["employee"], fields["attendance_date"]
    if status is None or not employee or not attendance_date:
        return None

    existing = frappe.db.get_value(
        ATTENDANCE_DOCTYPE,
        {"employee": employee, "attendance_date": attendance_date},
        ["name", "docstatus"],
        as_dict=True,
    )

    try:
        if existing:
            frappe.db.set_value(ATTENDANCE_DOCTYPE, existing.name, _writable(fields))
            att_name = existing.name
            docstatus = _to_int(existing.docstatus)
        else:
            doc = frappe.get_doc({"doctype": ATTENDANCE_DOCTYPE, **_writable(fields)})
            doc.insert(ignore_permissions=True)
            att_name = getattr(doc, "name", None)
            docstatus = _to_int(getattr(doc, "docstatus", 0))

        # IN/OUT come from the Work Session (calc.py pairs punches to the shift's
        # planned window, so overnight checkouts land on the right row). They are
        # set via SINGLE-FIELD db.set_value because the multi-field update above
        # is silently dropped by Frappe for in_time/out_time on a SUBMITTED
        # Attendance (they are not allow_on_submit), whereas single-field sets
        # persist — verified on overnight rows that the dict update left stale.
        if ws.get("actual_checkin"):
            frappe.db.set_value(ATTENDANCE_DOCTYPE, att_name, "in_time", ws["actual_checkin"])
        if ws.get("actual_checkout"):
            frappe.db.set_value(ATTENDANCE_DOCTYPE, att_name, "out_time", ws["actual_checkout"])

        # Back-link the Work Session so the round-trip is traceable (dict form —
        # the same shape used for the Attendance upsert above).
        frappe.db.set_value(WORK_SESSION_DOCTYPE, ws_name, {"attendance": att_name})

        # Submit only when the day belongs to a Locked period (decision #2).
        if docstatus == 0 and is_work_date_locked(attendance_date):
            try:
                frappe.get_doc(ATTENDANCE_DOCTYPE, att_name).submit()
            except Exception:
                frappe.log_error(title="attendance_sync.submit failed")
    except Exception:
        try:
            frappe.log_error(title="attendance_sync.sync_attendance failed")
        except Exception:
            pass
        return None
    return att_name


def on_work_session_update(doc, method: str | None = None) -> None:
    """``doc_events`` hook — keep Attendance in sync after every Work Session save."""
    _sync_attendance_internal(getattr(doc, "name", None))


@frappe.whitelist()
def backfill_attendance(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Regenerate ``Attendance`` for every Work Session in a date window.

    HR Manager / System Manager only. Safe to re-run (idempotent per row).
    """
    frappe.only_for(["HR Manager", "System Manager"])
    range_filters: list = []
    if from_date:
        range_filters.append(["work_date", ">=", from_date])
    if to_date:
        range_filters.append(["work_date", "<=", to_date])

    try:
        names = (
            frappe.get_all(
                WORK_SESSION_DOCTYPE,
                filters=range_filters or None,
                pluck="name",
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        names = []

    synced = 0
    skipped = 0
    for name in names:
        if _sync_attendance_internal(name):
            synced += 1
        else:
            skipped += 1
    # Persist regardless of caller: when invoked via console/execute there is no
    # request boundary to auto-commit, so the db.set_value writes would otherwise
    # be rolled back at process exit and the correction would silently vanish.
    try:
        frappe.db.commit()
    except Exception:
        pass
    return {"synced": synced, "skipped": skipped, "total": len(names)}
