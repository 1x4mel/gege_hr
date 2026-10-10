# Copyright (c) 2026, GEGE HR and contributors
# For license information, see LICENSE in this repo root.
"""Backfill ``company`` on pre-existing ``VN Attendance Exception`` rows.

A ``company`` Link field was added to ``VN Attendance Exception`` (the dashboard's
``_count_open_exceptions`` filters by it). The controller now auto-populates it
from the linked Employee on save, but rows created before the field existed are
blank — this patch stamps them so the company-scoped count query returns correct
numbers instead of treating every pre-existing exception as "no company".

Idempotent: rows that already carry a company are left untouched. Safe to re-run.
"""

from __future__ import annotations

import frappe

DOCTYPE = "VN Attendance Exception"


def execute() -> None:
    """Stamp ``company`` on legacy exception rows from their linked Employee."""
    # Only proceed once the column actually exists (runs after schema migration).
    if not frappe.db.has_column(DOCTYPE, "company"):
        return

    rows = frappe.db.get_all(
        DOCTYPE,
        filters={"company": ["in", [None, ""]]},
        fields=["name", "employee"],
    )
    if not rows:
        return

    # Resolve employee → company once to avoid N queries.
    emp_names = {r["employee"] for r in rows if r.get("employee")}
    emp_company: dict[str, str | None] = {}
    if emp_names:
        for emp, company in frappe.db.get_all(
            "Employee",
            filters={"name": ["in", list(emp_names)]},
            fields=["name", "company"],
        ):
            emp_company[emp] = company

    for r in rows:
        employee = r.get("employee")
        if not employee:
            continue
        company = emp_company.get(employee)
        if not company:
            continue
        try:
            frappe.db.set_value(
                DOCTYPE,
                r["name"],
                "company",
                company,
                update_modified=False,
            )
        except Exception:  # noqa: BLE001 - never block the patch loop
            frappe.log_error(
                f"backfill_exception_company: failed for {r['name']}",
                frappe.get_traceback(),
            )
            continue

    frappe.db.commit()
