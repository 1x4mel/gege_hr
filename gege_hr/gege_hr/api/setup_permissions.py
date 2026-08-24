"""
setup_permissions — grant the real Frappe role permissions the HR portal needs.

WHY THIS EXISTS
---------------
The HR portal's admin RPCs (``api/catalog_master.py``, ``api/admin.py``)
previously wrote with ``ignore_permissions=True`` because the ``HR Manager``
role has no DocType-level grant on the *core* Frappe masters (Department,
Designation, Branch, Employment Type, Leave Type) nor on ``User`` / ``Has Role``
by default. Bypassing role permissions in code is the wrong principle: it
defeats Frappe's permission engine, hides the real access model and makes the
app un-auditable.

This module grants those role permissions the *proper* way — as ``Custom
DocPerm`` rows (the very records Frappe's own *Role Permissions Manager*
writes). They survive app reinstalls, are merged with each DocType's standard
permissions at runtime, and are re-applied on every ``bench migrate`` via the
``after_migrate`` hook in ``hooks.py``.

Once granted, the RPCs drop ``ignore_permissions=True`` and honour the real
role permissions; ``frappe.only_for(HR_ADMIN_ROLES)`` stays as an extra
app-level gate on top (defense in depth — *not* a bypass).

Apply now (idempotent)::

    bench execute gege_hr.gege_hr.api.setup_permissions.grant_hr_permissions

Verify / inspect::

    bench execute gege_hr.gege_hr.api.setup_permissions.diagnose_permissions

Provision the full-access test account + run the end-to-end self-test::

    bench execute gege_hr.gege_hr.api.setup_permissions.apply_all
"""

from __future__ import annotations

import frappe
from frappe import _

HR_MANAGER = "HR Manager"
HR_USER = "HR User"
EMPLOYEE = "Employee"

# DocType-level grants at permlevel 0. Only the ptypes each flow truly needs —
# least-privilege by design (e.g. no ``delete`` on ``User``).
#
# Notes:
# * ``VN Work Location`` already grants HR Manager full + HR User create/read/
#   write in its own JSON (app-owned doctype) — so it is NOT listed here.
# * Core Frappe / HRMS DocTypes (Department, User, Shift Type, …) receive their
#   HR Manager grants here as ``Custom DocPerm``.
PERMISSION_MATRIX: dict[str, dict[str, dict[str, int]]] = {
    # --- Catalog masters (api/catalog_master.py: list / get / save / delete) --
    "Department": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
    },
    "Designation": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
    },
    "Branch": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
    },
    "Employment Type": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
    },
    "Leave Type": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
    },
    "VN Attendance Policy": {  # read-only browse on the settings screen
        HR_MANAGER: {"read": 1},
        HR_USER: {"read": 1},
    },
    "Holiday List": {  # holiday_master.py: HR Manager CRUD (child-table master)
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
    },
    # --- User & role management (api/admin.py) -------------------------------
    "User": {
        # create_user (create), assign/remove_roles + enable/disable (write),
        # loadUsers / loadUserRoles (read). No delete — least privilege.
        HR_MANAGER: {"read": 1, "write": 1, "create": 1},
    },
    "Has Role": {  # loadUserRoles() lists Has Role rows directly via REST
        HR_MANAGER: {"read": 1},
    },
    # --- Shift management (api/admin.py + useAdmin shift helpers) ------------
    "Shift Type": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1, "write": 1, "create": 1},
    },
    "Shift Assignment": {
        # create_shift_assignment (create + submit), end_shift_assignment
        # (write + cancel), list_shift_assignments (read).
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    # --- Leave (core Frappe HR) — leave.py self-service + HR approval --------
    # HR Manager also gets ``share`` so the unified-inbox approval flow can grant
    # the approving manager per-doc access via DocShare (the same mechanism HRMS
    # uses in ``share_doc_with_approver``) before ``doc.submit()`` — proper Frappe,
    # submit still runs validate + on_submit (Leave Ledger + audit logs).
    "Leave Application": {
        # F6: Employee previously held submit/cancel/delete — a self-submit
        # bypassed the approval matrix entirely (HRMS still writes the ledger,
        # so leave balance deducted with no approval).
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
        HR_MANAGER: {
            "read": 1,
            "write": 1,
            "create": 1,
            "delete": 1,
            "submit": 1,
            "cancel": 1,
            "amend": 1,
            "share": 1,
        },
        HR_USER: {"read": 1},
        "Line Manager": {"read": 1},
    },
    # --- Mobile / device check-in (core Frappe) — attendance.py -------------
    "Employee Checkin": {
        EMPLOYEE: {"read": 1, "create": 1},
        HR_USER: {"read": 1, "create": 1},
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
    },
    # --- Org / attendance masters the HR screens list directly via REST -------
    # getList('/api/resource/<doctype>') enforces DocType role perms; these core
    # masters are NOT granted to the gege_hr HR roles by default → 403. Granting
    # read here (Custom DocPerm, least-privilege) is the production-correct fix
    # (no ignore_permissions anywhere).
    "Company": {
        HR_MANAGER: {"read": 1},
        HR_USER: {"read": 1},
    },
    "VN Attendance Work Session": {  # HrAttendanceAdminView session search
        HR_MANAGER: {"read": 1},
        HR_USER: {"read": 1},
    },
    "Attendance": {  # team_attendance / team_daily_status reads Attendance rows
        HR_MANAGER: {"read": 1},
        HR_USER: {"read": 1},
        "Line Manager": {"read": 1},
    },
    # --- Payroll masters (payroll_master.py: HR Manager admin) --------------
    "Salary Structure": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
    },
    "Salary Structure Assignment": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    "Leave Period": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1},
    },
    "Leave Policy": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
    },
    "Leave Policy Assignment": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    # --- Payroll closing (payroll.py: HR / Payroll Manager) ------------------
    "Salary Slip": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        "Payroll Manager": {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    "Additional Salary": {
        # submit included: HRMS Leave Encashment on_submit inserts the
        # Additional Salary directly at docstatus=1 (encashment payout).
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        "Payroll Manager": {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    # --- VN request doctypes: HR Manager full lifecycle so the approval inbox can
    # read + advance state (approve_request / reject_request) on every request
    # type. Without read/write here an HR Manager gets 403 when approving an
    # Attendance Correction or Overtime request.
    "VN Salary Advance Request": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1, "submit": 1, "cancel": 1, "amend": 1},
    },
    "VN Attendance Correction Request": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        HR_USER: {"read": 1},
    },
    "VN Overtime Request": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        HR_USER: {"read": 1},
        # Employee self-service: create / write (read) on their own OT requests.
        # Ownership is enforced at the app layer (overtime._assert_own) — every
        # employee is scoped to their own docs; HR can grant/revoke this from the
        # HR UI via overtime_settings.enable_employee_ot_submission.
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    # --- Leave-cancellation + encashment/comp-off (inbox + manager approve) -----
    "VN Leave Cancellation Request": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        # Employee needs ``share`` so the owner (filing the request) can share it
        # with the approver (HRMS DocShare pattern) → the approver can then act.
        EMPLOYEE: {"read": 1, "write": 1, "create": 1, "share": 1, "delete": 1},
    },
    "Leave Encashment": {  # HRMS — leave_extra.approve_leave_encashment (.submit)
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    "Compensatory Leave Request": {  # HRMS — leave_extra.approve_comp_off (.submit)
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    # --- Expense / grievance / travel / onboarding (manager approve + HR admin) -
    "Expense Claim": {  # HRMS — expense.approve_expense_claim / reject
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    "Employee Grievance": {  # HRMS — employee_services.resolve_grievance
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    "Travel Request": {  # HRMS — employee_services.approve_travel_request
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
        EMPLOYEE: {"read": 1, "write": 1, "create": 1},
    },
    "VN Employee Onboarding": {  # onboarding.py HR admin lifecycle
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "delete": 1},
    },
    "VN Employee Onboarding Template": {  # onboarding.save_template
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
    },
    # --- Payroll review period lifecycle (approve / generate / publish) ---------
    "VN Payroll Review Period": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1},
    },
    "VN Payroll Review Line": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1},
    },
    # --- Employee master: HR admin edit (admin.save_employee) ------------------
    "Employee": {
        HR_MANAGER: {"read": 1, "write": 1, "create": 1, "delete": 1},
        HR_USER: {"read": 1},
        EMPLOYEE: {"read": 1},
    },
}

# The portal roles a full-access test account should hold. Provisioning a test
# user with EXACTLY these (and not ``System Manager``) validates that the real
# HR Manager permission path works end-to-end — ``System Manager`` implicitly
# bypasses every DocType check and would mask any permission gap.
PORTAL_ROLES = [
    "Employee",
    "Line Manager",
    "HR User",
    "HR Manager",
    "Payroll User",
    "Payroll Manager",
]


def _ensure_role(role: str) -> None:
    """Create a Role row if it does not exist (so a grant never silently no-ops)."""
    if not frappe.db.exists("Role", role):
        try:
            frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
        except Exception:  # noqa: BLE001 - best-effort; role may exist via fixture
            frappe.db.rollback()


def _upsert_perm(doctype: str, role: str, ptypes: dict[str, int]) -> None:
    """Create or update a ``Custom DocPerm`` granting ``ptypes`` to ``role``.

    Idempotent. Mirrors what Frappe's Role Permissions Manager writes (a
    ``Custom DocPerm`` child of the DocType, merged with the standard perms at
    runtime by taking the most permissive value).
    """
    if not frappe.db.exists("DocType", doctype):
        print(f"  skip (doctype missing): {doctype}")
        return
    _ensure_role(role)

    name = frappe.db.get_value(
        "Custom DocPerm",
        {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0},
    )
    if name:
        doc = frappe.get_doc("Custom DocPerm", name)
    else:
        doc = frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": doctype,
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": role,
                "permlevel": 0,
            }
        )

    # Reset every standard ptype, then apply the granted ones (least-privilege).
    for ptype in ("read", "write", "create", "delete", "submit", "cancel", "amend"):
        doc.set(ptype, ptypes.get(ptype, 0))
    doc.flags.ignore_permissions = True
    doc.save()
    granted = {k: v for k, v in ptypes.items() if v}
    print(f"  grant {doctype} / {role} -> {granted}")


def grant_hr_permissions() -> dict:
    """Apply every grant in :data:`PERMISSION_MATRIX` (idempotent)."""
    print("=== grant_hr_permissions ===")
    for doctype, roles in PERMISSION_MATRIX.items():
        for role, ptypes in roles.items():
            _upsert_perm(doctype, role, ptypes)
    frappe.db.commit()
    frappe.clear_cache()
    print("=== grant_hr_permissions done ===")
    return {"granted": list(PERMISSION_MATRIX.keys())}


def diagnose_permissions() -> dict:
    """Print + return the current ``Custom DocPerm`` rows for managed doctypes."""
    print("=== diagnose_permissions (Custom DocPerm) ===")
    out: dict[str, list] = {}
    for dt in PERMISSION_MATRIX:
        rows = frappe.db.get_all(
            "Custom DocPerm",
            {"parent": dt},
            ["role", "permlevel", "read", "write", "create", "delete", "submit", "cancel"],
            order_by="role",
        )
        out[dt] = rows
        print(f"{dt}: {rows}")
    return out


def provision_test_user(user: str = "1x4mel@gmail.com") -> dict:
    """Set ``user`` to exactly :data:`PORTAL_ROLES`.

    Drops ``System Manager`` so testing exercises the *real* HR Manager
    permission path (``System Manager`` implicitly bypasses every DocType
    check). Re-add it later from the desk / bench if needed.
    """
    print(f"=== provision_test_user {user} ===")
    if not frappe.db.exists("User", user):
        frappe.throw(_("User {0} không tồn tại.").format(user))
    doc = frappe.get_doc("User", user)
    doc.set("roles", [{"role": r} for r in PORTAL_ROLES])
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    frappe.clear_cache()  # drop stale role cache so only_for/get_roles resolve fresh
    actual = sorted(frappe.get_roles(user))
    print(f"roles -> {actual}")
    return {"user": user, "roles": actual}


def self_test(user: str = "1x4mel@gmail.com") -> dict:
    """End-to-end check: acting *as* ``user``, exercise read + write through the
    very RPCs the UI uses — proving they honour (not bypass) role permissions.

    * ``Department`` list        -> read perm on a core master.
    * ``Employment Type`` round-trip (create + delete) -> create / delete perms.
    * remove + re-add ``Line Manager`` on the user     -> write perm on ``User``.

    Every side effect is reverted, so the test leaves no trace.
    """
    print(f"=== self_test (as {user}) ===")
    from gege_hr.gege_hr.api import admin, catalog_master

    results: dict = {}
    prev = frappe.session.user
    created_name = None
    holiday_name = None
    try:
        frappe.set_user(user)

        # 1) read — Department (a core master HR Manager previously could not see)
        rows = catalog_master.list_catalog_masters("Department", fields=["name"], limit=5)
        results["list_department"] = {"ok": True, "count": len(rows)}

        # 2) create + delete round-trip — Employment Type (single required field)
        label = f"ZZ-PERMTEST-{frappe.generate_hash(length=6)}"
        created = catalog_master.save_catalog_master(
            "Employment Type", {"employee_type_name": label}, is_new=1
        )
        created_name = created.get("name") or label
        results["create_employment_type"] = {"ok": True, "name": created_name}
        catalog_master.delete_catalog_master("Employment Type", created_name)
        results["delete_employment_type"] = {"ok": True}
        created_name = None  # already cleaned up

        # 3) User write — remove then re-add a portal role (net-zero change).
        # Removing Line Manager (not HR Manager) keeps the admin gate satisfied.
        admin.remove_roles(user, ["Line Manager"])
        admin.assign_roles(user, ["Line Manager"])
        results["user_role_roundtrip"] = {"ok": True}

        # 4) Holiday List create + delete round-trip (HR Manager CRUD on a core
        #    Frappe master with a child table).
        from gege_hr.gege_hr.api import holiday_master

        hl_label = f"ZZ-PERMTEST-HL-{frappe.generate_hash(length=6)}"
        hl = holiday_master.save_holiday_list(
            holiday_list_name=hl_label,
            from_date="2026-01-01",
            to_date="2026-12-31",
            holidays=[{"holiday_date": "2026-01-01", "description": "PermTest"}],
        )
        holiday_name = hl.get("name")
        results["create_holiday_list"] = {"ok": True, "name": holiday_name}
        holiday_master.delete_holiday_list(holiday_name)
        results["delete_holiday_list"] = {"ok": True}
        holiday_name = None  # already cleaned up

        # 5) Permission sweep — assert the granted ptypes resolve True for ``user``
        #    across every core DocType the self-service / admin flows touch.
        expected = {
            "Leave Application": ["read", "write", "create", "submit", "cancel"],
            "Employee Checkin": ["read", "create"],
            "Holiday List": ["read", "write", "create", "delete"],
            "Salary Structure": ["read", "write", "create", "delete"],
            "Salary Structure Assignment": ["read", "create", "submit"],
            "Leave Period": ["read", "create"],
            "Leave Policy": ["read", "write", "create"],
            "Leave Policy Assignment": ["read", "create", "submit"],
            "Salary Slip": ["read", "create"],
            "Additional Salary": ["read", "create"],
            "VN Salary Advance Request": ["read", "write", "create"],
        }
        perm_report: dict = {}
        for dt, ptypes in expected.items():
            for pt in ptypes:
                try:
                    perm_report[f"{dt}.{pt}"] = bool(frappe.has_permission(dt, pt, user=user))
                except Exception as pe:  # noqa: BLE001
                    perm_report[f"{dt}.{pt}"] = f"ERR:{pe}"
        results["perm_sweep"] = perm_report
        results["perm_sweep_missing"] = [k for k, v in perm_report.items() if v is not True]
    except Exception as e:  # noqa: BLE001 - report any failure verbosely
        import traceback

        results["error"] = f"{type(e).__name__}: {e}"
        results["traceback"] = traceback.format_exc()
    finally:
        # Defensive cleanup of any half-created test rows.
        if created_name or holiday_name:
            try:
                frappe.set_user("Administrator")
                if created_name and frappe.db.exists("Employment Type", created_name):
                    frappe.delete_doc("Employment Type", created_name, ignore_permissions=True)
                if holiday_name and frappe.db.exists("Holiday List", holiday_name):
                    frappe.delete_doc("Holiday List", holiday_name, ignore_permissions=True)
                frappe.db.commit()
            except Exception:  # noqa: BLE001
                pass
        frappe.set_user(prev)

    print(f"self_test results -> {results}")
    return results


def apply_all(user: str = "1x4mel@gmail.com") -> dict:
    """Grant permissions, provision the test user, and run the end-to-end test.

    Single entry point for ``bench execute gege_hr.gege_hr.api.setup_permissions.apply_all``.
    """
    grant_hr_permissions()
    provision_test_user(user)
    self_test(user)
    diagnose_permissions()
    return {"done": True}


def create_test_employee_user(
    email: str = "nhanvien@gegeteam.xyz",
    password: str = "Gege128",
    full_name: str = "Nhân Viên Test",
    company: str | None = None,
) -> dict:
    """Create a Frappe User (``Employee`` role) + a linked Employee record with a
    known password — ready to log into the portal and exercise the self-service
    screens (leave / OT / advance / correction / check-in).

    Idempotent: re-running only refreshes the password / role / Employee link.

    Run via::

        bench execute gege_hr.gege_hr.api.setup_permissions.create_test_employee_user
    """
    from frappe.utils.password import update_password

    from gege_hr.gege_hr.api.admin import _default_company

    print(f"=== create_test_employee_user {email} ===")
    email = (email or "").strip().lower()
    if not email or not password:
        frappe.throw(_("Email và mật khẩu là bắt buộc."))

    company = company or _default_company()
    if not company:
        frappe.throw(
            _("Không xác định được công ty — thiết lập default_company trước."),
        )

    # 1) User (enabled, no welcome email — we set the password directly).
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        user.enabled = 1
        user.full_name = full_name
    else:
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "username": email.split("@")[0],
                "first_name": full_name,
                "full_name": full_name,
                "enabled": 1,
                "user_type": "Website User",
                "send_welcome_email": 0,
            }
        )
        user.insert(ignore_permissions=True)

    # 2) Ensure the Employee role is assigned.
    if "Employee" not in {d.role for d in user.get("roles", [])}:
        user.append("roles", {"role": "Employee"})
    user.flags.ignore_permissions = True
    user.save()

    # 3) Set the chosen password (bypass policy so the literal test password is
    #    honoured — this is a throwaway test account).
    try:
        frappe.flags.ignore_password_policy = True
        update_password(email, password)
    finally:
        frappe.flags.ignore_password_policy = False

    # 4) Linked Employee record (so self-service screens resolve the user).
    emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if emp_name:
        emp = frappe.get_doc("Employee", emp_name)
    else:
        emp = frappe.get_doc(
            {
                "doctype": "Employee",
                "first_name": full_name,
                "employee_name": full_name,
                "company": company,
                "user_id": email,
                "status": "Active",
                "date_of_joining": frappe.utils.today(),
                "date_of_birth": "1995-01-01",
                "gender": "Other",
            }
        )
        emp.insert(ignore_permissions=True)

    frappe.db.commit()
    frappe.clear_cache()
    out = {
        "user": email,
        "employee": emp.name,
        "company": company,
        "roles": sorted(frappe.get_roles(email)),
        "password_set": True,
    }
    print(f"created -> {out}")
    return out


def provision_test_shift(
    employee: str = "HR-EMP-00003",
    shift_type: str = "Ca Hành Chính",
    start_time: str = "08:00:00",
    end_time: str = "17:00:00",
) -> dict:
    """Create a Shift Type + an Active Shift Assignment (+ today's shift instance)
    so the test employee can exercise the check-in flow end-to-end.

    Idempotent. Run via::

        bench execute gege_hr.gege_hr.api.setup_permissions.provision_test_shift
    """
    from gege_hr.gege_hr.api.admin import _default_company

    print(f"=== provision_test_shift {employee} ===")
    if not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại: {0}").format(employee))
    company = frappe.db.get_value("Employee", employee, "company") or _default_company()
    today = frappe.utils.today()

    # 1) Shift Type (with the gege_hr check-in window custom fields).
    if not frappe.db.exists("Shift Type", shift_type):
        st = frappe.get_doc(
            {
                "doctype": "Shift Type",
                "shift_name": shift_type,
                "start_time": start_time,
                "end_time": end_time,
                "vn_is_overnight_shift": 0,
                "vn_shift_duration_hours": 9,
                "vn_standard_hours_per_day": 8,
                "vn_earliest_checkin_minutes": 120,
                "vn_latest_checkin_minutes": 120,
                "vn_earliest_checkout_minutes": 120,
                "vn_latest_checkout_minutes": 180,
                "vn_max_checkout_after_end_minutes": 360,
            }
        )
        st.name = shift_type  # Shift Type uses prompt-naming here
        st.insert(ignore_permissions=True)
        st_name = st.name
    else:
        st_name = shift_type

    # 2) Active, submitted Shift Assignment covering today (skip if one exists).
    existing = frappe.db.get_value(
        "Shift Assignment",
        {"employee": employee, "shift_type": st_name, "status": "Active", "docstatus": 1},
    )
    if existing:
        sa_name = existing
    else:
        sa = frappe.get_doc(
            {
                "doctype": "Shift Assignment",
                "employee": employee,
                "shift_type": st_name,
                "start_date": today,
                "status": "Active",
                "company": company,
            }
        )
        sa.insert(ignore_permissions=True)
        sa.submit()
        sa_name = sa.name

    # 3) Materialise today's VN Employee Shift Instance (work-session calc).
    try:
        from gege_hr.gege_hr.api import shift as shift_api

        shift_api.generate_daily_shift_instances()
    except Exception as exc:  # noqa: BLE001 - best-effort; check-in still works
        print(f"  shift-instance generation warning: {exc}")

    frappe.db.commit()
    frappe.clear_cache()
    out = {
        "employee": employee,
        "shift_type": st_name,
        "shift_assignment": sa_name,
        "company": company,
    }
    print(f"provisioned -> {out}")
    return out
