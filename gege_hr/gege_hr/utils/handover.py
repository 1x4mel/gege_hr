"""
Leave handover task helpers — plan v5 §11.5 / doctype-design §32 (Post-MVP).

A **VN Leave Handover Task** is created when an approved Leave Application
requires a work handover: the departing employee (``from_employee``) hands
their in-flight work to ``to_employee`` with a description and optional
attachment. The receiver drives the task through the lifecycle
``Pending → In Progress → Completed`` (or ``Cancelled``).

Design (mirrors the ``utils/notify.py`` / ``utils/leave.py`` convention):

* Pure helpers (``handover_payload`` / ``handover_row`` /
  ``can_transition``) are side-effect free and bench-free → unit-tested
  outside a Frappe site.
* The ``api/handover.py`` layer performs the only frappe-aware work
  (insert / save / list), guarded so a missing table degrades gracefully.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover - outside bench
    frappe = None


# --------------------------------------------------------------------------- #
# Vocabulary (matches VN Leave Handover Task.status options)
# --------------------------------------------------------------------------- #
HANDOVER_STATUSES = ("Pending", "In Progress", "Completed", "Cancelled")

# Permissive forward-only lifecycle: terminal states may be re-opened back to
# Pending (e.g. a Cancelled handover that needs redoing). The same-state
# transition is always allowed (idempotent no-op).
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "Pending": ("Pending", "In Progress", "Completed", "Cancelled"),
    "In Progress": ("In Progress", "Completed", "Cancelled", "Pending"),
    "Completed": ("Completed", "Pending", "Cancelled"),
    "Cancelled": ("Cancelled", "Pending"),
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def can_transition(
    current: str | None, target: str | None, docstatus: int | None = None
) -> bool:
    """Whether a handover may move from ``current`` → ``target`` status.

    ``docstatus`` (optional — plan-handover-deskfree H1) tightens the table
    for the submittable lifecycle:

    * ``docstatus == 1`` (submitted / Completed): only the idempotent
      same-state transition or a move to ``Cancelled`` (``doc.cancel()``).
    * ``docstatus == 2`` (cancelled): only the idempotent same-state no-op —
      a redo goes through "Tạo lại" (a brand-new doc), never a transition.
    * ``docstatus in (0, None)``: the legacy permissive table below.

    The two-argument form is unchanged (existing pins keep passing).
    """
    if not target or target not in HANDOVER_STATUSES:
        return False
    if docstatus == 1:
        return target == current or target == "Cancelled"
    if docstatus == 2:
        return target == current
    if not current:
        # A brand-new handover can be created in any valid status.
        return True
    if current not in ALLOWED_TRANSITIONS:
        return False
    return target in ALLOWED_TRANSITIONS[current]


def _coerce_date(value: Any) -> str | None:
    """Normalise a date/datetime/ISO-str into a ``YYYY-MM-DD`` string."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Accept datetime-like strings; keep only the date portion.
    if "T" in text or " " in text:
        text = text.replace("T", " ").split(" ")[0]
    return text


def handover_payload(
    *,
    leave_application: str,
    from_employee: str,
    to_employee: str,
    handover_date: Any,
    description: str,
    attachment: str | None = None,
    note: str | None = None,
    status: str = "Pending",
) -> dict:
    """Assemble the field dict for ``frappe.get_doc({...}).insert()``.

    Strips blanks, coerces the date, defaults ``status`` to Pending, and
    enforces the from≠to rule + required fields. Raises ``ValueError`` for a
    caller that ignores required fields (the api layer translates this into a
    ``frappe.throw``).
    """
    if not leave_application:
        raise ValueError("leave_application is required")
    if not from_employee:
        raise ValueError("from_employee is required")
    if not to_employee:
        raise ValueError("to_employee is required")
    if from_employee == to_employee:
        raise ValueError("from_employee and to_employee must differ")
    if not (description or "").strip():
        raise ValueError("description is required")
    coerced_date = _coerce_date(handover_date)
    if not coerced_date:
        raise ValueError("handover_date is required")
    if not can_transition(None, status):
        status = "Pending"
    doc: dict[str, Any] = {
        "doctype": "VN Leave Handover Task",
        "leave_application": str(leave_application).strip(),
        "from_employee": str(from_employee).strip(),
        "to_employee": str(to_employee).strip(),
        "handover_date": coerced_date,
        "description": str(description).strip(),
        "status": status,
    }
    if attachment:
        doc["attachment"] = str(attachment).strip()
    if note and str(note).strip():
        doc["note"] = str(note).strip()
    return doc


# Fields selected from the DB row → normalised into the SPA contract.
HANDOVER_ROW_FIELDS = (
    "name",
    "leave_application",
    "from_employee",
    "to_employee",
    "handover_date",
    "status",
    "description",
    "attachment",
    "completed_at",
    "completed_by",
    "note",
    "docstatus",
    "owner",
    "modified",
)


def handover_row(row: Any) -> dict:
    """Normalise a DB row / dict into the SPA handover shape.

    Defaults missing keys, coerces datetimes to ISO strings, and drops any
    rogue keys not in the contract.
    """
    if not isinstance(row, dict):
        return {}
    out: dict[str, Any] = {key: row.get(key) for key in HANDOVER_ROW_FIELDS}
    # Datetime → ISO string for JSON safety.
    for key in ("completed_at", "modified"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


# --------------------------------------------------------------------------- #
# Receiver suggestions (auto-suggest to_employee from reports_to / team)
# --------------------------------------------------------------------------- #
# Why a colleague is suggested — surfaced to the FE as a small caption so the
# user understands *why* this person is recommended (peer vs. report vs. mgr).
RECEIVER_REASON = {
    "direct_report": "Cấp dưới trực tiếp",
    "colleague": "Cùng phòng ban",
    "manager": "Quản lý trực tiếp",
}

# Rank order: direct reports first (hand down the work), then peers (same
# department), then the manager (escalate). Lower number ranks earlier.
_REASON_RANK = {"direct_report": 0, "colleague": 1, "manager": 2}


def _receiver_name(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    name = row.get("name")
    return str(name).strip() if name else None


def suggest_receivers(
    employee: str | None,
    reports_to: str | None,
    department: str | None,
    colleague_rows: Any,
    *,
    limit: int = 8,
) -> list[dict]:
    """Rank candidate receivers for a departing ``employee``.

    Given the employee's own ``reports_to`` / ``department`` and a pool of
    colleague rows (each ``{name, employee_name, reports_to, department}``),
    return a de-duplicated, ranked suggestion list **excluding the employee
    themselves**. Each entry is ``{name, label, reason}`` where ``reason`` maps
    to ``RECEIVER_REASON`` so the FE can caption the relationship.

    Ranking: direct reports (``reports_to == employee``) → same-department peers
    → the employee's own manager. Non-dict rows and blanks are dropped. Pure &
    bench-free so it can be unit-tested without a Frappe site.
    """
    if not employee:
        return []
    employee = str(employee).strip()
    reports_to = str(reports_to).strip() if reports_to else None
    department = str(department).strip() if department else None

    ranked: list[tuple[int, str, dict]] = []
    seen: set[str] = set()

    def push(name: str, label: str, reason: str) -> None:
        key = str(name).strip()
        if not key or key == employee or key in seen:
            return
        seen.add(key)
        ranked.append((_REASON_RANK.get(reason, 9), label, {"name": key, "label": label, "reason": reason}))

    for row in colleague_rows or []:
        name = _receiver_name(row)
        if not name or name == employee:
            continue
        label = str(row.get("employee_name") or name).strip()
        row_reports_to = str(row.get("reports_to") or "").strip()
        row_department = str(row.get("department") or "").strip()
        if row_reports_to == employee:
            push(name, label, "direct_report")
        elif department and row_department and row_department == department:
            push(name, label, "colleague")

    # The manager is only relevant if it is a real, distinct person.
    if reports_to and reports_to != employee and reports_to not in seen:
        # We do not have the manager's display name in the pool necessarily;
        # fall back to the employee name.
        mgr_label = reports_to
        for row in colleague_rows or []:
            if _receiver_name(row) == reports_to:
                mgr_label = str(row.get("employee_name") or reports_to).strip()
                break
        push(reports_to, mgr_label, "manager")

    ranked.sort(key=lambda item: (item[0], item[1].lower()))
    out = (
        [entry for _, _, entry in ranked[: max(0, int(limit or 0))]]
        if limit
        else [entry for _, _, entry in ranked]
    )
    return out
