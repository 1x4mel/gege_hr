"""Per-employee salary plan — Phase-1 seed (plan per-employee-salary §4.6).

One-time, IDEMPOTENT migration so the day this ships the payroll numbers do
NOT change:

1. Ensures the new custom fields exist (sync_custom_fields is idempotent and
   also runs from after_migrate — the patch re-runs it because patches
   execute BEFORE the after_migrate hooks).
2. Every ACTIVE employee with a blank ``vn_payroll_mode`` → ``Hourly``
   (the mode every employee was effectively on before this plan).
3. Every ACTIVE employee with a blank/0 ``vn_hourly_rate`` → seeded with
   their effective legacy rate (department rate as of today → portal
   default). After the seed, ``resolve_hourly_rate_detail`` returns exactly
   what it would have returned before the deploy — same tier, same number.

Run via ``bench --site <site> migrate`` (patches.txt) — safe to re-run:
only BLANK fields are filled, never overwritten.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    # 1) Fields first — patches run before after_migrate's sync hook.
    from gege_hr.hooks import sync_custom_fields

    sync_custom_fields()

    from gege_hr.gege_hr.utils.payroll import resolve_hourly_rate_detail

    try:
        employees = frappe.get_all(
            "Employee",
            filters={"status": "Active"},
            fields=["name", "department", "vn_payroll_mode", "vn_hourly_rate"],
        )
    except Exception:
        # Field projection failed (partial deploy) — retry minimal projection.
        employees = frappe.get_all(
            "Employee", filters={"status": "Active"}, fields=["name", "department"]
        )
        for e in employees:
            e["vn_payroll_mode"] = None
            e["vn_hourly_rate"] = None

    seeded_mode = 0
    seeded_rate = 0
    for emp in employees:
        updates: dict = {}
        if not (emp.get("vn_payroll_mode") or "").strip():
            updates["vn_payroll_mode"] = "Hourly"
        try:
            current_rate = float(emp.get("vn_hourly_rate") or 0)
        except (TypeError, ValueError):
            current_rate = 0.0
        if current_rate <= 0:
            # The effective LEGACY rate: dept → portal default (employee
            # tier is empty by definition here).
            effective, _source = resolve_hourly_rate_detail(emp.get("name"))
            if effective and effective > 0:
                updates["vn_hourly_rate"] = effective
        if updates:
            frappe.db.set_value("Employee", emp.get("name"), updates, update_modified=False)
            seeded_mode += 1 if "vn_payroll_mode" in updates else 0
            seeded_rate += 1 if "vn_hourly_rate" in updates else 0

    frappe.db.commit()
    print(
        f"[per_employee_salary_phase1] {len(employees)} active employees — "
        f"seeded mode={seeded_mode}, hourly_rate={seeded_rate} "
        "(idempotent: only blank fields filled)."
    )


def seed_e2e_structure(company: str, name: str = "E2E ST") -> str:
    """E2E helper (bench execute) — ensure a SUBMITTED Salary Structure exists.

    Local benches usually have none, and HRMS refuses SSA submit against a
    draft structure — the UI flow (G1 gap) can't submit one yet, so the e2e
    suite calls this directly. Idempotent.
    """
    if not frappe.db.exists("Salary Component", "E2E Basic"):
        frappe.get_doc(
            {
                "doctype": "Salary Component",
                "salary_component": "E2E Basic",
                "salary_component_name": "E2E Basic",
                "type": "Earning",
            }
        ).insert(ignore_permissions=True)
    if not frappe.db.exists("Salary Structure", name):
        doc = frappe.get_doc(
            {
                "doctype": "Salary Structure",
                "salary_structure": name,
                "name": name,  # field-name doc: the label must double as name
                "company": company,
                "is_active": "Yes",
                "currency": "VND",
                "earnings": [
                    {"salary_component": "E2E Basic", "amount": 2_000_000, "amount_based_on_formula": 0}
                ],
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()
    frappe.db.commit()
    return name


def cleanup_e2e_employees() -> int:
    """E2E helper (bench execute) — remove leftover 'E2E …' employees.

    Aborted e2e runs leave test employees behind; their VN Audit Event rows
    link the Employee and block deletion (LinkExists) unless removed first.
    """
    rows = frappe.get_all("Employee", filters={"employee_name": ["like", "E2E%"]}, pluck="name")
    removed = 0
    for emp in rows:
        for doctype, flt in (
            ("VN Audit Event", {"employee": emp}),
            ("VN Payroll Review Line", {"employee": emp}),
            ("Salary Structure Assignment", {"employee": emp}),
            ("User Permission", {"for_value": emp}),
        ):
            try:
                frappe.db.delete(doctype, flt)
            except Exception:
                pass
        try:
            frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
            removed += 1
        except Exception:
            pass
    frappe.db.commit()
    return removed


def make_e2e_employee(first_name: str, company: str) -> str:
    """E2E helper (bench execute) — insert one clean test Employee, return name.

    Deterministic JSON-printable return for the e2e harness (parsing
    frappe.client.insert's full-doc output proved fragile).
    """
    doc = frappe.get_doc(
        {
            "doctype": "Employee",
            "first_name": first_name,
            "last_name": "E2E",
            "company": company,
            "gender": "Other",
            "date_of_birth": "1990-01-01",
            "date_of_joining": "2026-01-01",
        }
    ).insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name
