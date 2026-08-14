"""
Audit event helpers — plan v5 §13.5 / doctype-design §26.

A **VN Audit Event** is an append-only row minted whenever a sensitive portal
action occurs (leave/OT/correction/advance submit/approve, work-session
recalc, monthly lock/unlock, payroll calculate/approve/publish, manual
override). This module holds the pure payload/row/vocabulary helpers; the
``record`` entry point in ``api/audit.py`` is the only frappe-aware writer.

Design (mirrors ``utils/notify.py``): pure, bench-free builders + a vocabulary
guard, so the audit vocabulary stays in lock-step with the DocType select
options.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover - outside bench
    frappe = None


# --------------------------------------------------------------------------- #
# Vocabulary (matches VN Audit Event.audit_type options — 19 values)
# --------------------------------------------------------------------------- #
AUDIT_TYPES = (
    "Check-in",
    "Check-out",
    "Leave Submit",
    "Leave Cancel",
    "Leave Approve",
    "Leave Reject",
    "OT Submit",
    "OT Approve",
    "Correction Submit",
    "Correction Approve",
    "Advance Submit",
    "Advance Approve",
    "Work Session Recalculate",
    "Monthly Lock",
    "Monthly Unlock",
    "Payroll Calculate",
    "Payroll Approve",
    "Payroll Publish",
    "Manual Override",
    "Checkout Miss Explain",
    "Checkout Miss Resolve",
)

# Coarse category groupings, useful for the audit-list filter UI.
AUDIT_CATEGORIES = {
    "attendance": ("Check-in", "Check-out", "Work Session Recalculate"),
    "checkout_miss": ("Checkout Miss Explain", "Checkout Miss Resolve"),
    "leave": ("Leave Submit", "Leave Cancel", "Leave Approve", "Leave Reject"),
    "overtime": ("OT Submit", "OT Approve"),
    "correction": ("Correction Submit", "Correction Approve"),
    "advance": ("Advance Submit", "Advance Approve"),
    "monthly": ("Monthly Lock", "Monthly Unlock"),
    "payroll": ("Payroll Calculate", "Payroll Approve", "Payroll Publish"),
    "manual": ("Manual Override",),
}


def is_valid_audit_type(value: str | None) -> bool:
    return bool(value) and value in AUDIT_TYPES


def category_for(audit_type: str | None) -> str | None:
    """Map an audit type to its coarse category (None if unknown)."""
    if not audit_type:
        return None
    for category, types in AUDIT_CATEGORIES.items():
        if audit_type in types:
            return category
    return None


def _coerce_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if "T" in text or " " in text:
        text = text.replace("T", " ").split(" ")[0]
    return text


def _jsonable(value: Any) -> str | None:
    """Serialise an old/new value to a JSON string (or None for blanks)."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    import json

    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def audit_payload(
    *,
    audit_type: str,
    company: str,
    actor: str | None = None,
    employee: str | None = None,
    work_date: Any = None,
    actor_ip: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
) -> dict:
    """Assemble the field dict for a VN Audit Event insert.

    Coerces the ``audit_type`` to a valid value (fallback ``Manual Override``),
    serialises old/new values to JSON, strips blanks, and validates the
    required fields. Raises ``ValueError`` when ``company`` is missing.
    """
    if not company:
        raise ValueError("company is required")
    if not is_valid_audit_type(audit_type):
        audit_type = "Manual Override"
    doc: dict[str, Any] = {
        "doctype": "VN Audit Event",
        "audit_type": audit_type,
        "company": str(company).strip(),
    }
    if actor:
        doc["actor"] = str(actor).strip()
    if employee:
        doc["employee"] = str(employee).strip()
    coerced = _coerce_date(work_date)
    if coerced:
        doc["work_date"] = coerced
    if actor_ip:
        doc["actor_ip"] = str(actor_ip).strip()
    if reference_doctype:
        doc["reference_doctype"] = str(reference_doctype).strip()
    if reference_name:
        doc["reference_name"] = str(reference_name).strip()
    if description and str(description).strip():
        doc["description"] = str(description).strip()
    old = _jsonable(old_value)
    new = _jsonable(new_value)
    if old:
        doc["old_value"] = old
    if new:
        doc["new_value"] = new
    return doc


AUDIT_ROW_FIELDS = (
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
)


def audit_row(row: Any) -> dict:
    """Normalise a DB row / dict into the SPA audit shape."""
    if not isinstance(row, dict):
        return {}
    out: dict[str, Any] = {key: row.get(key) for key in AUDIT_ROW_FIELDS}
    value = out.get("created_at")
    if isinstance(value, datetime):
        out["created_at"] = value.isoformat(sep=" ")
    return out
