"""Low-level DB guards — shared by the concurrency/claim call sites.

Frappe v15's ``frappe.db.sql`` returns ``()`` for UPDATE/DELETE statements (no
result set), so ``bool(frappe.db.sql("UPDATE ..."))`` is ALWAYS False — every
claim written that way had its logic inverted (the "winner" looked like the
loser). :func:`guarded_update` executes the UPDATE and returns the true number
of changed rows via ``ROW_COUNT()`` — verified semantics on MariaDB: 1 when a
row matched AND the value actually changed, 0 when the WHERE matched nothing
(or the value was already equal).
"""

from __future__ import annotations

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None


def guarded_update(sql: str, params: dict | None = None) -> int:
    """Run an UPDATE (or DELETE) and return the number of changed rows.

    Use for claim/CAS transitions::

        if guarded_update(
            "UPDATE `tabX` SET status = 'Claimed'"
            " WHERE name = %(name)s AND status = 'Draft'",
            {"name": name},
        ):
            ...  # we won the claim
    """
    return guarded_update_tuple(sql, params)


def guarded_update_tuple(sql: str, params) -> int:
    """Positional-parameter variant of :func:`guarded_update` (``%s`` binds)."""
    if frappe is None:
        return 0
    frappe.db.sql(sql, params or ())
    rc = frappe.db.sql("SELECT ROW_COUNT()")
    try:
        return int(rc[0][0])
    except (TypeError, ValueError, IndexError):
        return 0
