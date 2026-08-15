"""
Naming utilities — auto-naming for transaction DocTypes.

Format: ``PREFIX-YYMMDD-XXXXXX`` (plan v5 §5.2), generated in ``before_insert``.
"""

from __future__ import annotations

from datetime import datetime

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None

# DocType → prefix map (plan v5 §5.2).
PREFIXES = {
    "VN Employee Shift Instance": "SI",
    "VN Attendance Raw Log": "RL",
    "VN Mobile Checkin Attempt": "MC",
    "VN Attendance Work Session": "WS",
    "VN Attendance Correction Request": "CR",
    "VN Overtime Request": "OR",
    "VN Monthly Attendance Period": "MAP",
    "VN Payroll Review Period": "PRP",
    "VN Salary Advance Request": "SAR",
    "VN Audit Event": "AE",
    "VN Attendance Calculation Run": "CRUN",
    "VN Attendance Exception": "EXC",
}


# Reverse of PREFIXES — the fallback counter must count the RIGHT doctype.
_PREFIX_TO_DOCTYPE = {v: k for k, v in PREFIXES.items()}


def _next_sequence(prefix: str, stamp: str) -> int:
    """Deterministic-ish 6-digit counter from a Redis INCR per prefix+day."""
    try:
        n = frappe.cache().incr(f"gege_hr:seq:{prefix}:{stamp}")
    except Exception:
        # Redis unavailable: the OLD fallback counted "VN Attendance Raw Log"
        # for EVERY prefix — a meaningless number that produced existing names
        # (DuplicateEntryError) on WS/CR/SAR/... inserts. Count the prefix's
        # own doctype + the number already used with today's stamp instead.
        doctype = _PREFIX_TO_DOCTYPE.get(prefix)
        if not doctype:
            return 1
        try:
            total = frappe.db.count(doctype) or 0
            used_today = frappe.db.count(doctype, {"name": ["like", f"{prefix}-{stamp}-%"]})
            n = max(total, used_today) + 1
        except Exception:
            return 1
    return int(n)


def set_yymmdd_name(doc, method: str | None = None) -> None:
    """Assign ``PREFIX-YYMMDD-XXXXXX`` to a transaction doc in before_insert."""
    if frappe is None or doc is None:
        return
    if getattr(doc, "name", None) and not str(doc.name).startswith("__"):
        # Some doctypes already named — leave as is.
        if getattr(doc, "_is_new", True) is False:
            return
    prefix = PREFIXES.get(doc.doctype)
    if not prefix:
        return
    stamp = datetime.utcnow().strftime("%y%m%d")
    seq = _next_sequence(prefix, stamp)
    doc.name = f"{prefix}-{stamp}-{seq:06d}"


def generate_unique_id(prefix: str) -> str:
    """Public helper to mint an id outside the doc hook context."""
    stamp = datetime.utcnow().strftime("%y%m%d")
    seq = _next_sequence(prefix, stamp)
    return f"{prefix}-{stamp}-{seq:06d}"
