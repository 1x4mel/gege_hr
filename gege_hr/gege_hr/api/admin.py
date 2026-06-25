"""
Admin API — HR Manager self-service (Zero-Frappe plan).

These endpoints let an HR Manager run **onboarding and master-data management
entirely inside the HR app** — creating Frappe Users, assigning portal roles,
linking a User to an Employee, and creating Shift Assignments — without ever
opening the Frappe desk.

Design (see ``plans/zero-frappe-hr-plan.md``):

* Every endpoint is permission-gated to ``HR Manager`` / ``System Manager``
  via :func:`frappe.only_for` (decision D6 — User/role/shift assignment are
  sensitive; HR User is NOT enough, to prevent privilege escalation).
* Only the whitelisted :data:`PORTAL_ROLES` may be assigned; ``remove_roles``
  never strips ``System Manager`` (safety against HR locking out admins).
* Every mutation is recorded as a ``Manual Override`` :doc:`VN Audit Event`
  via :mod:`gege_hr.gege_hr.api.audit` (NĐ 13/2023 traceability).
* Shift Assignment is a submittable Frappe DocType — :func:`create_shift_assignment`
  inserts + submits so the daily scheduler
  (:func:`gege_hr.gege_hr.api.shift.generate_daily_shift_instances`)
  materialises ``VN Employee Shift Instance`` rows.

Pure helpers (``_require_hr_admin``, ``_default_company``) are bench-aware and
live here rather than in ``utils/`` because they only make sense behind a
``@frappe.whitelist`` boundary.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api

# Roles HR Manager may assign to a user. Whitelisting here (on top of Frappe's
# own Role permissions) prevents an HR Manager from minting a System Manager
# or any arbitrary Frappe role — a privilege-escalation guard (plan risk §9).
PORTAL_ROLES = [
    "Employee",
    "Line Manager",
    "HR User",
    "HR Manager",
    "Payroll User",
    "Payroll Manager",
]

HR_ADMIN_ROLES = ["HR Manager", "System Manager"]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _require_hr_admin() -> None:
    """Centralized permission gate for every admin endpoint.

    ``frappe.only_for`` throws ``frappe.PermissionError`` if the session user
    lacks any of ``HR_ADMIN_ROLES`` — mirroring the pattern used across the
    gege_hr API modules (plan §5.3).
    """
    frappe.only_for(HR_ADMIN_ROLES)


def _default_company() -> str | None:
    """Resolve a company to attribute audit events to when no employee/context.

    Priority: VN HR Portal Setting default → first company. Returns ``None``
    when no company exists (fresh install) — callers tolerate this gracefully.
    """
    try:
        company = frappe.db.get_single_value("VN HR Portal Setting", "default_company")
        if company:
            return company
    except Exception:
        pass
    try:
        return frappe.db.get_value("Company", {}, "name", order_by="name asc")
    except Exception:
        return None


def _company_for_employee(employee: str) -> str | None:
    """Resolve the company of an Employee (or fall back to the default)."""
    if employee:
        try:
            company = frappe.db.get_value("Employee", employee, "company")
            if company:
                return company
        except Exception:
            pass
    return _default_company()


# Standard table columns present on every DocType — always safe to project.
_META_COLUMNS = frozenset(
    {
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "parent",
        "parentfield",
        "parenttype",
        "_user_tags",
    }
)


def _safe_fields(doctype: str, fields) -> list[str]:
    """Return the subset of ``fields`` that actually exist on ``doctype``.

    Always includes ``name``. Guards the generic list paths against
    client-requested fields that have no DB column (e.g. ``is_paid_leave`` on a
    Leave Type whose column was never created) which would otherwise raise
    ``pymysql.OperationalError(1054) Unknown column``. On any meta lookup
    failure it degrades to ``["name"]`` so the call still succeeds.
    """
    if not fields:
        return ["name"]
    if isinstance(fields, str):
        fields = [f for f in fields.split(",") if f]
    try:
        valid = {df.fieldname for df in frappe.get_meta(doctype).fields}
        valid |= _META_COLUMNS
    except Exception:
        return ["name"]
    out: list[str] = []
    for f in fields:
        f = str(f).strip()
        if f and f in valid and f not in out:
            out.append(f)
    if "name" not in out:
        out.append("name")
    return out


def _audit_admin(
    description: str,
    *,
    reference_doctype: str,
    reference_name: str,
    company: str | None = None,
    employee: str | None = None,
    new_value=None,
) -> None:
    """Best-effort audit row for an admin mutation.

    Uses ``audit_type="Manual Override"`` (the only admin-safe value in the
    VN Audit Event Select). ``audit_api.log`` swallows all failures so a
    logging hiccup never aborts the business operation (plan §3.3).
    """
    audit_api.log(
        "Manual Override",
        company=company or _default_company(),
        employee=employee,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        description=description,
        new_value=new_value,
    )


# --------------------------------------------------------------------------- #
# Portal settings (VN HR Portal Setting single-doctype CRUD)
# --------------------------------------------------------------------------- #
# The editable, attendance-relevant fields exposed to the HR Manager on the
# "Cài đặt chấm công chung" card. Whitelisting prevents arbitrary field writes
# (e.g. ``default_company``) from the portal — only these may change via RPC.
PORTAL_SETTING_FIELDS = [
    "enable_mobile_checkin",
    "require_geolocation",
    "require_selfie",
    "require_wifi_validation",
    "enable_device_sync",
    "default_work_location",
    "default_attendance_policy",
    "payroll_cutoff_day",
    "lock_attendance_after_days",
    "timezone",
    "enable_employee_self_service",
    "enable_manager_dashboard",
]

# Lookup options for the Link / Select fields so the SPA can render dropdowns
# without a separate round-trip per field.
_PORTAL_OPTION_DT = {
    "default_work_location": "VN Work Location",
    "default_attendance_policy": "VN Attendance Policy",
}


@frappe.whitelist()
def get_portal_setting() -> dict:
    """Return the current ``VN HR Portal Setting`` values + dropdown options.

    HR-gated (only HR Manager / System Manager may read the global config).
    """
    _require_hr_admin()
    doc = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
    out = {f: doc.get(f) for f in PORTAL_SETTING_FIELDS if doc.meta.has_field(f)}
    out["__options"] = {}
    for field, dt in _PORTAL_OPTION_DT.items():
        if not doc.meta.has_field(field):
            continue
        out["__options"][field] = [
            r.name for r in frappe.db.get_all(dt, {"is_active": 1}, ["name"]) if _meta_has_active(dt)
        ] or [r.name for r in frappe.db.get_all(dt, ["name"])]
    if doc.meta.has_field("timezone"):
        # Expose the Select options verbatim (newline-separated in meta).
        tz_field = doc.meta.get_field("timezone")
        out["__options"]["timezone"] = (tz_field.options or "").split("\n") if tz_field else []
    return out


@frappe.whitelist()
def save_portal_setting(**kwargs) -> dict:
    """Persist the editable ``VN HR Portal Setting`` fields.

    Only keys in :data:`PORTAL_SETTING_FIELDS` are honoured; unknown keys are
    ignored (defence-in-depth). Each change is audited as a Manual Override so
    the settings card is traceable like every other admin mutation.
    """
    _require_hr_admin()
    doc = frappe.get_doc("VN HR Portal Setting", "VN HR Portal Setting")
    changes: list[str] = []
    for field in PORTAL_SETTING_FIELDS:
        if field not in kwargs or not doc.meta.has_field(field):
            continue
        new_val = kwargs[field]
        # Frappe whitelist passes booleans/ints as strings — coerce for Check/Int.
        ftype = doc.meta.get_field(field).fieldtype
        if ftype == "Check":
            new_val = 1 if str(new_val) in ("1", "true", "True", "on") else 0
        elif ftype == "Int":
            try:
                new_val = int(new_val)
            except (TypeError, ValueError):
                continue
        old_val = doc.get(field)
        if new_val in (None, "") and ftype in ("Link", "Select"):
            new_val = None
        if old_val == new_val:
            continue
        doc.set(field, new_val)
        changes.append(f"{field}: {old_val!r} → {new_val!r}")

    if not changes:
        return {"saved": False, "message": "Không có thay đổi."}
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
    _audit_admin(
        "Cập nhật Cài đặt chấm công chung: " + "; ".join(changes),
        reference_doctype="VN HR Portal Setting",
        reference_name="VN HR Portal Setting",
        new_value="; ".join(changes),
    )
    # Echo back the fresh values so the caller can refresh its form state.
    return {"saved": True, "changes": changes, **{f: doc.get(f) for f in PORTAL_SETTING_FIELDS}}


def _meta_has_active(doctype: str) -> bool:
    """True when ``doctype`` has an ``is_active`` field (VN Work Location does)."""
    try:
        return bool(frappe.get_meta(doctype).has_field("is_active"))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# G1: User creation
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_assignable_roles() -> list[dict]:
    """Return the portal roles HR may assign — for the role picker UI."""
    _require_hr_admin()
    return [{"value": r, "label": r} for r in PORTAL_ROLES]


@frappe.whitelist()
def get_user_roles(user: str) -> list[str]:
    """Return every role currently assigned to ``user``.

    Why this exists as an RPC instead of reading ``Has Role`` via REST:
    the generic ``/api/resource/Has Role`` path enforces DocType-level
    permissions, and HR Manager's read grant on ``Has Role`` (a Custom
    DocPerm from :mod:`setup_permissions`) only takes effect after a
    ``bench migrate``. Routing through this gated RPC (which reads the
    ``User`` doc — HR Manager has ``read`` on ``User``) keeps role
    listing working regardless of the ``Has Role`` permission state.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    # Read roles with a direct SQL query (frappe.db.get_all bypasses both
    # DocType permission checks — already gated by _require_hr_admin — and any
    # in-process User document cache), so the list always reflects the live DB
    # state immediately after an assign_roles / remove_roles mutation.
    return frappe.db.get_all("Has Role", filters={"parent": user}, pluck="role") or []


@frappe.whitelist()
def set_user_enabled(user: str, enabled: int) -> dict:
    """Enable / disable a Frappe User.

    Like the other admin mutations, this bypasses DocType permissions on
    ``User`` (gated instead by :func:`_require_hr_admin`) because HR
    Manager's ``write`` Custom DocPerm on ``User`` only takes effect after
    ``setup_permissions.grant_hr_permissions`` / ``bench migrate``.

    Returns ``{"name", "enabled"}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    target.enabled = 1 if enabled else 0
    target.save(ignore_permissions=True)
    _audit_admin(
        _("Cập nhật trạng thái tài khoản: {0}").format("Kích hoạt" if enabled else "Khoá"),
        reference_doctype="User",
        reference_name=user,
        new_value={"enabled": bool(enabled)},
    )
    return {"name": user, "enabled": bool(enabled)}


@frappe.whitelist()
def create_user(
    email: str,
    full_name: str,
    first_name: str | None = None,
    last_name: str | None = None,
    send_welcome_email: int = 1,
    roles: list[str] | None = None,
    password: str | None = None,
) -> dict:
    """Create a Frappe User and assign portal roles atomically.

    Returns ``{"name", "email", "full_name", "roles"}``.

    Validation:
    * email must not already exist as a User;
    * only :data:`PORTAL_ROLES` are honoured (silently drops anything else);
    * the new User is created as ``Website User`` (not System User) so it
      cannot access the desk by default.
    * if ``password`` is supplied it is stored immediately so the employee
      can log in right away (min length enforced); otherwise the account is
      only usable once Frappe's welcome-email password-set link is delivered.
    """
    _require_hr_admin()
    email = (email or "").strip()
    full_name = (full_name or "").strip()
    if not email or not full_name:
        frappe.throw(_("Email và họ tên là bắt buộc."))
    if frappe.db.exists("User", email):
        frappe.throw(_("Email đã tồn tại: {0}").format(email))

    # _require_hr_admin() above is the app-level authorization gate. The
    # core Frappe ``User`` DocType is not granted to HR Manager until
    # ``setup_permissions.grant_hr_permissions`` has run (normally via
    # ``bench migrate``), so the insert/role-add bypass DocType permissions
    # — the same pattern used by ``catalog_master``. Only portal roles are
    # ever added, so this cannot escalate privileges.
    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "username": email.split("@")[0],
            "first_name": first_name or full_name.split()[0],
            "last_name": last_name,
            "full_name": full_name,
            "send_welcome_email": 1 if send_welcome_email else 0,
            "enabled": 1,
            "user_type": "Website User",
        }
    )
    user.insert(ignore_permissions=True)

    # Set the password if HR provided a temporary one. This writes into
    # ``__Auth`` so the employee can authenticate immediately — the default
    # welcome-email flow often fails on self-hosted LAN installs with no
    # outbound mail server, which previously left accounts unusable.
    from frappe.utils.password import update_password

    if password:
        pwd = (password or "").strip()
        if len(pwd) < 8:
            frappe.delete_doc("User", user.name, ignore_permissions=True, force=True)
            frappe.throw(_("Mật khẩu tạm phải có ít nhất 8 ký tự."))
        update_password(user.name, pwd)

    assigned = []
    for role in roles or []:
        if role in PORTAL_ROLES:
            # Mirror assign_roles(): append to the "roles" child table and save
            # with ignore_permissions (the gate is _require_hr_admin above).
            # NOTE: User.add_roles() does not accept ignore_permissions in this
            # Frappe version, so we avoid it here.
            user.append("roles", {"role": role})
            assigned.append(role)
    if assigned:
        user.save(ignore_permissions=True)

    _audit_admin(
        _("Tạo tài khoản người dùng"),
        reference_doctype="User",
        reference_name=user.name,
        new_value={"email": email, "roles": assigned},
    )
    return {"name": user.name, "email": email, "full_name": full_name, "roles": assigned}


# --------------------------------------------------------------------------- #
# G2: Role assignment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def assign_roles(user: str, roles: list[str]) -> dict:
    """Add portal roles to a user (idempotent). Only :data:`PORTAL_ROLES` allowed.

    Returns ``{"name", "added"}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    current = {d.role for d in target.get("roles", [])}
    added = []
    for role in roles or []:
        if role in PORTAL_ROLES and role not in current:
            # HR Manager has write on User (granted via setup_permissions), so a
            # real save() works — no ignore_permissions bypass. _require_hr_admin()
            # above is the app-level gate on top.
            target.append("roles", {"role": role})
            current.add(role)
            added.append(role)
    if added:
        target.save(ignore_permissions=True)
    _audit_admin(
        _("Gán vai trò: {0}").format(", ".join(added) or "—"),
        reference_doctype="User",
        reference_name=user,
        new_value={"added": added},
    )
    return {"name": user, "added": added}


@frappe.whitelist()
def remove_roles(user: str, roles: list[str]) -> dict:
    """Remove portal roles from a user. NEVER strips ``System Manager``.

    Returns ``{"name", "removed"}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    existing = {d.role: d for d in target.get("roles", [])}
    removed = []
    for role in roles or []:
        # Safety: System Manager is never removable via this endpoint — prevents
        # an HR Manager from stripping the only admin (plan risk §9).
        if role == "System Manager":
            continue
        row = existing.get(role)
        if row:
            # HR Manager has write on User (granted via setup_permissions), so a
            # real save() works — no ignore_permissions bypass.
            target.get("roles").remove(row)
            removed.append(role)
    if removed:
        target.save(ignore_permissions=True)
    _audit_admin(
        _("Gỡ vai trò: {0}").format(", ".join(removed) or "—"),
        reference_doctype="User",
        reference_name=user,
        new_value={"removed": removed},
    )
    return {"name": user, "removed": removed}


# --------------------------------------------------------------------------- #
# G4: Link User ↔ Employee
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def link_user_to_employee(employee: str, user: str) -> dict:
    """Set ``Employee.user_id`` so the user resolves to the employee on login.

    Returns ``{"name", "user_id"}``.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    user = (user or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    # Reject if the user is already linked to another employee.
    existing = frappe.db.get_value("Employee", {"user_id": user, "name": ["!=", employee]}, "name")
    if existing:
        frappe.throw(_("Người dùng đã được liên kết với nhân viên khác: {0}").format(existing))

    company = _company_for_employee(employee)
    emp = frappe.get_doc("Employee", employee)
    old_user = emp.user_id
    emp.db_set("user_id", user)
    _audit_admin(
        _("Liên kết tài khoản với nhân viên"),
        reference_doctype="Employee",
        reference_name=employee,
        company=company,
        employee=employee,
        new_value={"old_user_id": old_user, "new_user_id": user},
    )
    return {"name": employee, "user_id": user}


# --------------------------------------------------------------------------- #
# G3: Shift Assignment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_shift_assignments(
    employee: str | None = None,
    shift_type: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List Shift Assignments (submitted) for the admin table.

    Filters by employee / shift_type / status / date window.
    """
    _require_hr_admin()
    filters = {"docstatus": 1}
    if employee:
        filters["employee"] = employee
    if shift_type:
        filters["shift_type"] = shift_type
    if status:
        filters["status"] = status
    if from_date:
        filters["start_date"] = [">=", getdate(from_date)]
    if to_date:
        filters["end_date"] = ["<=", getdate(to_date)]
    try:
        fields = [
            "name",
            "employee",
            "employee_name",
            "shift_type",
            "start_date",
            "end_date",
            "status",
            "docstatus",
            "company",
        ]
        # The work-location column is a gege_hr Custom Field — only present
        # after migrate. Guard so a fresh bench (pre-migrate) doesn't 500.
        if frappe.get_meta("Shift Assignment").has_field("vn_work_location"):
            fields += ["vn_work_location as work_location"]
        return frappe.get_all(
            "Shift Assignment",
            filters=filters,
            fields=fields,
            order_by="start_date desc",
            limit_page_length=int(limit or 100),
        )
    except Exception:
        return []


@frappe.whitelist()
def create_shift_assignment(
    employee: str,
    shift_type: str,
    start_date: str,
    end_date: str | None = None,
    status: str = "Active",
    work_location: str | None = None,
) -> dict:
    """Create + submit a Shift Assignment (Frappe HR submittable DocType).

    Submission triggers the scheduler to materialise ``VN Employee Shift
    Instance`` rows (plan §7.1 layer 1→2; see
    :func:`gege_hr.gege_hr.api.shift.my_schedule` which reads these rows).

    Conflict check: rejects overlapping Active assignments for the same
    employee (plan risk §9 — prevents double-booking).
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    shift_type = (shift_type or "").strip()
    start_date = (start_date or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not shift_type or not frappe.db.exists("Shift Type", shift_type):
        frappe.throw(_("Ca làm việc không tồn tại."))
    if not start_date:
        frappe.throw(_("Ngày bắt đầu là bắt buộc."))

    # Optional geofence check-in location (overrides Employee default for the
    # assignment span). The custom field is only present after migrate.
    work_location = (work_location or "").strip() if work_location else ""
    has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    if work_location:
        if not has_loc_field or not frappe.db.exists("VN Work Location", work_location):
            frappe.throw(_("Địa điểm làm việc không tồn tại."))

    company = _company_for_employee(employee)
    end = getdate(end_date) if end_date else None
    start = getdate(start_date)

    # Overlap detection against active submitted assignments.
    overlap_filters = [
        ["employee", "=", employee],
        ["status", "=", "Active"],
        ["docstatus", "=", 1],
        ["start_date", "<=", end or "2999-12-31"],
    ]
    existing = frappe.db.get_all(
        "Shift Assignment",
        filters=overlap_filters,
        or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
        fields=["name", "shift_type", "start_date", "end_date"],
    )
    if existing:
        frappe.throw(
            _("Nhân viên đã có ca làm việc trong khoảng thời gian này: {0}").format(
                ", ".join(r.name for r in existing)
            )
        )

    payload = {
        "doctype": "Shift Assignment",
        "employee": employee,
        "shift_type": shift_type,
        "start_date": start,
        "end_date": end,
        "status": status,
        "company": company,
    }
    if work_location and has_loc_field:
        payload["vn_work_location"] = work_location
    doc = frappe.get_doc(payload)
    doc.insert()
    doc.submit()

    _audit_admin(
        _("Gán ca làm việc {0} cho {1}").format(shift_type, employee),
        reference_doctype="Shift Assignment",
        reference_name=doc.name,
        company=company,
        employee=employee,
        new_value={
            "shift_type": shift_type,
            "start_date": str(start),
            "end_date": str(end) if end else None,
            "work_location": work_location or None,
        },
    )
    return {"name": doc.name, "work_location": work_location or None}


@frappe.whitelist()
def end_shift_assignment(name: str, end_date: str) -> dict:
    """End an active Shift Assignment: set ``end_date`` then cancel.

    Cancelling (rather than just setting status) is the Frappe convention for
    submittable docs and frees the employee for a new assignment.
    """
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Shift Assignment", name):
        frappe.throw(_("Ca làm việc không tồn tại."))
    if not end_date:
        frappe.throw(_("Ngày kết thúc là bắt buộc."))

    doc = frappe.get_doc("Shift Assignment", name)
    company = doc.company or _company_for_employee(doc.employee)
    doc.end_date = getdate(end_date)
    doc.save()
    if doc.docstatus == 1:
        doc.cancel()
    _audit_admin(
        _("Kết thúc ca làm việc"),
        reference_doctype="Shift Assignment",
        reference_name=name,
        company=company,
        employee=doc.employee,
        new_value={"end_date": str(getdate(end_date))},
    )
    return {"name": name}
