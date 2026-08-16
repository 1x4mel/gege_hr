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

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import pagination

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
    username: str | None = None,
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

    # Resolve the username: honour an explicit value, otherwise derive it from
    # the email local-part. Reject collisions up-front so the SPA shows a clear
    # Vietnamese error instead of Frappe's raw "Username already exists" when two
    # employees share an email prefix (e.g. two ``nam@…`` across domains).
    username = (username or "").strip() or email.split("@")[0]
    if frappe.db.exists("User", {"username": username}):
        frappe.throw(_("Tên đăng nhập đã tồn tại: {0}").format(username))

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
            "username": username,
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
    return {
        "name": user.name,
        "email": email,
        "full_name": full_name,
        "username": username,
        "roles": assigned,
    }


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
# G1b: User profile edit / password reset / delete (zero-desk parity)
# --------------------------------------------------------------------------- #
# Whitelist of User fields an HR Manager may edit through the portal. ``email``
# (the docname) and ``user_type`` (a security-sensitive privilege lever) are
# intentionally NOT editable here — the same defensive posture as PORTAL_ROLES.
_USER_EDITABLE = [
    "full_name",
    "first_name",
    "last_name",
    "username",
    "gender",
    "birth_date",
    "mobile_no",
    "phone",
    "language",
    "time_zone",
    "user_image",
]


@frappe.whitelist()
def get_user_detail(user: str) -> dict:
    """Return the editable profile + all roles + linked Employee for the SPA.

    Combines :data:`_USER_EDITABLE` fields, the full role list and the Employee
    bound via ``user_id`` so the "Sửa người dùng" modal can render in a single
    round-trip — no Desk visit required.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    doc = frappe.get_cached_doc("User", user)
    out = {f: doc.get(f) for f in _USER_EDITABLE if doc.meta.has_field(f)}
    out["name"] = doc.name
    out["email"] = doc.email
    out["enabled"] = bool(doc.enabled)
    out["user_type"] = doc.user_type
    out["roles"] = frappe.db.get_all("Has Role", filters={"parent": user}, pluck="role") or []
    linked = frappe.db.get_value(
        "Employee", {"user_id": user}, ["name", "employee_name"], as_dict=True
    )
    out["linked_employee"] = linked.name if linked else None
    out["linked_employee_name"] = linked.employee_name if linked else None
    return out


@frappe.whitelist()
def update_user(user: str, **values) -> dict:
    """Edit whitelisted profile fields of an existing User.

    Only :data:`_USER_EDITABLE` keys are honoured; everything else is ignored
    (defence-in-depth — never lets the portal flip ``user_type`` / ``api_key``).
    Returns ``{"name", "updated": [...]}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    updated = []
    for field in _USER_EDITABLE:
        if field not in values or not target.meta.has_field(field):
            continue
        new_val = values[field]
        # Normalise empty Link / Select / Date strings to None (Frappe convention).
        ftype = target.meta.get_field(field).fieldtype
        if new_val in (None, "") and ftype in ("Link", "Select", "Date"):
            new_val = None
        old_val = target.get(field)
        if old_val == new_val:
            continue
        # Username collision guard (mirrors create_user).
        if (
            field == "username"
            and new_val
            and frappe.db.exists("User", {"username": new_val, "name": ["!=", user]})
        ):
            frappe.throw(_("Tên đăng nhập đã tồn tại: {0}").format(new_val))
        target.set(field, new_val)
        updated.append(f"{field}: {old_val!r} → {new_val!r}")
    if updated:
        target.save(ignore_permissions=True)
        _audit_admin(
            _("Cập nhật hồ sơ người dùng"),
            reference_doctype="User",
            reference_name=user,
            new_value={"updated": updated},
        )
    return {"name": user, "updated": updated}


@frappe.whitelist()
def reset_user_password(
    user: str, new_password: str | None = None, send_email: int = 1
) -> dict:
    """Reset a user's password without leaving the portal.

    Two modes:

    * ``new_password`` provided (>=8 chars): stored immediately via
      ``frappe.utils.password.update_password`` so the employee can log in at
      once — essential on LAN installs without an outbound mail server. The
      account is also re-enabled so the password actually works.
    * otherwise + ``send_email``: Frappe's native reset-link email is sent
      (requires a configured email account; on failure a friendly Vietnamese
      error nudges the manager to set a password directly).

    Only the hash is ever persisted; the audit row records ``{"reset": True}``
    and never the plaintext.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    pwd = (new_password or "").strip()
    if pwd:
        if len(pwd) < 8:
            frappe.throw(_("Mật khẩu mới phải có ít nhất 8 ký tự."))
        from frappe.utils.password import update_password

        update_password(user, pwd)
        # Re-enable on reset so a previously locked account becomes usable.
        if not target.enabled:
            target.enabled = 1
            target.save(ignore_permissions=True)
    elif send_email:
        try:
            target._reset_password(send_email=True)
        except frappe.OutgoingEmailError:
            frappe.clear_messages()
            frappe.throw(_("Chưa cấu hình máy chủ email — hãy đặt mật khẩu trực tiếp."))
    _audit_admin(
        _("Đặt lại mật khẩu người dùng"),
        reference_doctype="User",
        reference_name=user,
        new_value={"reset": True},
    )
    return {"name": user, "ok": True}


@frappe.whitelist()
def delete_user(user: str) -> dict:
    """Delete a Frappe User (zero-desk parity).

    Safety rails (plan risk §9): never delete the acting session user and never
    delete a System Manager — prevents HR from orphaning the only admin.
    Returns ``{"name", "deleted": True}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    if user == frappe.session.user:
        frappe.throw(_("Không thể xoá tài khoản đang đăng nhập."))
    roles = frappe.db.get_all("Has Role", filters={"parent": user}, pluck="role") or []
    if "System Manager" in roles:
        frappe.throw(_("Không thể xoá quản trị viên hệ thống (System Manager)."))
    # Audit BEFORE the doc is destroyed so the reference still resolves.
    _audit_admin(
        _("Xoá tài khoản người dùng"),
        reference_doctype="User",
        reference_name=user,
        new_value={"deleted": True},
    )
    frappe.delete_doc("User", user, ignore_permissions=True, force=True)
    return {"name": user, "deleted": True}


# --------------------------------------------------------------------------- #
# Directory lists — employees / users (DNA §6.6 A server-side pagination)
# --------------------------------------------------------------------------- #
# These endpoints replace the SPA's standard-Frappe ``getList`` (REST
# ``/api/resource``) reads of ``Employee`` / ``User`` for the HR directory
# views. ``getList`` returns only ``{data}`` — no row count — and the DNA §6.6
# count path (``frappe.db.get_all(...).len``, honouring ``or_filters``) cannot
# be reproduced from the frontend (``frappe.client.get_count`` rejects
# ``or_filters``). Routing the directory reads through these whitelisted
# endpoints yields the ``{"data","total","summary"}`` envelope the paginated
# shell needs (DNA §6.6 A), identical to ``list_devices`` /
# ``list_shift_assignments``.

_EMPLOYEE_LIST_FIELDS = [
    "name",
    "employee_name",
    "status",
    "company",
    "department",
    "branch",
    "designation",
    "employment_type",
    "gender",
    "date_of_birth",
    "date_of_joining",
    "cell_number",
    "reports_to",
    "user_id",
    "vn_employee_code",
    "line_manager",
    "default_work_location",
    "default_attendance_policy",
    "allow_mobile_checkin",
    "allow_remote_checkin",
    "payroll_group",
]
_EMPLOYEE_SUMMARY_FIELDS = ["name", "status"]

# Exact-match filters the /hr/employees gear popover exposes (DNA §6.2 / Law #2).
# Custom VN columns (vn_employee_code, line_manager, …) may be absent on an
# unmigrated bench — every consumer guards them through ``_safe_fields``.
_EMPLOYEE_EXACT_FILTERS = [
    "status",
    "department",
    "branch",
    "designation",
    "employment_type",
    "company",
    "line_manager",
]

# Columns covered by the broad free-text search box (DNA §6.6 A / Law #3).
# Numeric/datetime fields are intentionally excluded (no ``like`` on them).
_EMPLOYEE_SEARCH_FIELDS = [
    "employee_name",
    "vn_employee_code",
    "cell_number",
    "user_id",
    "department",
    "designation",
    "company",
]


def _employee_summary(light_rows) -> dict:
    """Status-bucket counts over the full filtered set (SPA summary tiles)."""
    buckets = pagination.bucket_counts(light_rows, "status")
    return {
        "total": len(light_rows or []),
        "active": buckets.get("Active", 0),
        "inactive": buckets.get("Inactive", 0),
        "left": buckets.get("Left", 0),
    }


@frappe.whitelist()
def list_employees(
    search: str | None = None,
    status: str | None = None,
    department: str | None = None,
    branch: str | None = None,
    designation: str | None = None,
    employment_type: str | None = None,
    company: str | None = None,
    line_manager: str | None = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """HR directory "Nhân viên" — Employee rows (HR-gated).

    Broad ``search`` OR-LIKE-matches every text/identifier column
    (``employee_name`` / ``vn_employee_code`` / ``cell_number`` / ``user_id`` /
    ``department`` / ``designation`` / ``company``) and each exact-match filter
    (``status`` / ``department`` / ``branch`` / ``designation`` /
    ``employment_type`` / ``company`` / ``line_manager``) narrows server-side
    (DNA §6.6 A/D — Law #2 & #3). Resolves HR-BL-01.

    ``or_filters`` columns are intersected with the doctype's real columns via
    :func:`_safe_fields` so a missing custom column on an unmigrated bench
    cannot raise ``Unknown column``.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates status buckets over the *full*
    filtered set so the SPA summary tiles stay correct under pagination.
    Without ``page_size`` the legacy bare-list return is preserved.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    # ``_safe_fields`` keeps the custom VN columns (vn_employee_code, …) from
    # raising "Unknown column" on a bench that has not yet run ``migrate``.
    # The resulting set is reused as a membership guard for both the exact
    # ``filters`` and the broad ``or_filters`` (DNA §6.6 A).
    fields = _safe_fields("Employee", _EMPLOYEE_LIST_FIELDS)
    valid = set(fields)
    filters: list = []
    exact = {
        "status": status,
        "department": department,
        "branch": branch,
        "designation": designation,
        "employment_type": employment_type,
        "company": company,
        "line_manager": line_manager,
    }
    for field, val in exact.items():
        if val not in (None, "") and field in valid:
            filters.append([field, "=", val])
    or_filters = None
    _q = (search or "").strip()
    if _q:
        _like = f"%{pagination.escape_like(_q)}%"
        or_filters = [[c, "like", _like] for c in _EMPLOYEE_SEARCH_FIELDS if c in valid]
        if not or_filters:
            or_filters = None
    limit = pagination.clamp_limit(limit, default=100)

    if page_size:
        summary = _employee_summary(
            pagination.all_rows(
                "Employee",
                fields=_EMPLOYEE_SUMMARY_FIELDS,
                filters=filters or None,
                or_filters=or_filters or None,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    "Employee",
                    fields=fields,
                    filters=filters or None,
                    or_filters=or_filters or None,
                    order_by="employee_name asc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="admin.list_employees failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {"data": rows, "total": summary["total"], "summary": summary}

    try:
        return frappe.get_all(
            "Employee",
            fields=fields,
            filters=filters or None,
            or_filters=or_filters or None,
            order_by="employee_name asc",
            limit_page_length=limit,
        )
    except Exception:
        return []


@frappe.whitelist()
def get_employee_filter_options() -> dict:
    """Distinct values for the /hr/employees gear popover (DNA §6.3).

    Returns ``{ field: [{value, label}, ...] }`` for each dynamic exact-match
    filter so the SPA ``SearchableSelect`` dropdowns are never empty (auto-fetch
    pattern — DNA §6.3). HR-gated (same gate as :func:`list_employees`).
    ``status`` is a fixed enum and stays client-side; the dynamic fields are
    ``department`` / ``branch`` / ``designation`` / ``employment_type`` /
    ``company`` / ``line_manager``.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    valid = set(_safe_fields("Employee", _EMPLOYEE_LIST_FIELDS))
    out: dict[str, list[dict]] = {}
    for field in (
        "department",
        "branch",
        "designation",
        "employment_type",
        "company",
        "line_manager",
        "default_work_location",
    ):
        if field not in valid:
            out[field] = []
            continue
        # Query master tables directly so dropdowns are never empty even when no
        # employee has been assigned a value yet.
        _MASTER_MAP = {
            "department": ("Department", "department_name", {"is_group": 0}),
            "designation": ("Designation", "designation_name", {}),
            "branch": ("Branch", "branch", {}),
            "employment_type": ("Employment Type", "employee_type_name", {}),
            # gege_hr custom Link on Employee → VN Work Location master table.
            # Queried directly so the dropdown is populated even before any
            # employee has been assigned a location (DNA §6.3 auto-fetch).
            "default_work_location": ("VN Work Location", "location_name", {"is_active": 1}),
        }
        if field in _MASTER_MAP:
            master_dt, label_field, extra_filters = _MASTER_MAP[field]
            try:
                rows = frappe.db.get_all(
                    master_dt,
                    filters=extra_filters,
                    fields=["name", label_field],
                    order_by="name asc",
                    limit_page_length=500,
                ) or []
                out[field] = [
                    {"value": r["name"], "label": r.get(label_field) or r["name"]}
                    for r in rows
                ]
            except Exception:
                out[field] = []
            continue

        try:
            rows = (
                frappe.db.get_all(
                    "Employee",
                    filters=[[field, "is", "set"]],
                    fields=[field],
                    distinct=1,
                    order_by=f"{field} asc",
                    limit_page_length=500,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="admin.get_employee_filter_options failed")
            rows = []
        seen: list[str] = []
        for row in rows:
            v = (row.get(field) or "").strip()
            if v and v not in seen:
                seen.append(v)
        out[field] = [{"value": v, "label": v} for v in seen]
    return out


# Whitelist of Employee columns an HR admin may quick-edit from the
# /hr/employees modal. The save runs the FULL ``Document.save()`` validation
# (mandatory + business rules) — the modal surfaces every mandatory field
# (first_name / gender / date_of_birth / date_of_joining / status / company)
# so an HR admin can satisfy them directly. Only the role check is lifted
# (``ignore_permissions=True``); this is NOT a mandatory bypass (DNA §6.7).
_EMPLOYEE_EDITABLE_FIELDS = (
    "first_name",
    "last_name",
    "middle_name",
    "status",
    "company",
    "department",
    "branch",
    "designation",
    "employment_type",
    "gender",
    "date_of_birth",
    "date_of_joining",
    "cell_number",
    "reports_to",
    "user_id",
    "vn_employee_code",
    "line_manager",
    "default_work_location",
    "default_attendance_policy",
    "allow_mobile_checkin",
    "allow_remote_checkin",
    "payroll_group",
)


@frappe.whitelist()
def save_employee(employee: str, **kwargs) -> dict:
    """Quick-edit an Employee's HR fields (DNA §6.7).

    Loads the doc, applies the supplied whitelisted fields, then runs the full
    ``Document.save(ignore_permissions=True)`` — so Frappe's mandatory + business
    validation still applies. The edit modal exposes every mandatory field
    (e.g. ``date_of_birth``) so an HR admin can fill them in; an empty
    mandatory field correctly raises a validation error. Only the *role* check
    is lifted (HR admin tool); this is NOT a mandatory bypass.
    Returns the refreshed doc.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    doc = frappe.get_doc("Employee", employee)
    for field, value in kwargs.items():
        if field in _EMPLOYEE_EDITABLE_FIELDS:
            doc.set(field, value)
    doc.save(ignore_permissions=True)
    return doc.as_dict()


_USER_LIST_FIELDS = ["name", "email", "full_name", "enabled", "user_type", "last_active"]
_USER_SUMMARY_FIELDS = ["name", "enabled"]
_SYSTEM_USERS = ["Guest", "Administrator"]


def _user_summary(light_rows) -> dict:
    """Enabled-bucket counts over the full filtered set (SPA summary tiles)."""
    active = sum(1 for r in light_rows or [] if int(r.get("enabled") or 0) == 1)
    total = len(light_rows or [])
    return {"total": total, "active": active, "locked": max(0, total - active)}


def _attach_linked_employees(rows: list[dict]) -> None:
    """Attach ``employee`` / ``employee_name`` to each row in one pass.

    Maps ``User.name`` → the Employee whose ``user_id`` equals it, so the
    directory can show which account is bound to which Employee — a frequent
    manager question that previously forced a Desk visit. Defensive: any lookup
    failure leaves the rows untouched.
    """
    if not rows:
        return
    names = [r.get("name") for r in rows if r.get("name")]
    if not names:
        return
    mapping: dict[str, dict] = {}
    try:
        for r in frappe.get_all(
            "Employee",
            fields=["name", "employee_name", "user_id"],
            filters={"user_id": ["in", names]},
        ):
            if r.get("user_id"):
                mapping[r["user_id"]] = r
    except Exception:
        return
    for row in rows:
        linked = mapping.get(row.get("name"))
        if linked:
            row["employee"] = linked.get("name")
            row["employee_name"] = linked.get("employee_name")


@frappe.whitelist()
def list_users(
    search: str | None = None,
    enabled: str | None = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """HR directory "Người dùng" — Frappe User rows (HR-gated).

    ``search`` OR-matches ``full_name`` / ``email`` (HR-BL-01) and ``enabled``
    narrows by account state (``"1"``/``"true"`` → active, else locked).
    System accounts (Guest / Administrator) are excluded **server-side** so the
    reported ``total`` matches the rows the SPA used to hide client-side. Both
    filters are applied server-side (DNA §6.6 D).

    Pagination is **opt-in** (DNA §6.6 A) — see :func:`list_employees`.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    filters = [["name", "not in", _SYSTEM_USERS]]
    if enabled not in (None, ""):
        filters.append(["enabled", "=", 1 if str(enabled) in ("1", "true", "True") else 0])
    or_filters = None
    _q = (search or "").strip()
    if _q:
        _like = f"%{pagination.escape_like(_q)}%"
        or_filters = [
            ["full_name", "like", _like],
            ["email", "like", _like],
        ]
    limit = pagination.clamp_limit(limit, default=100)
    fields = _safe_fields("User", _USER_LIST_FIELDS)

    if page_size:
        summary = _user_summary(
            pagination.all_rows(
                "User",
                fields=_USER_SUMMARY_FIELDS,
                filters=filters or None,
                or_filters=or_filters or None,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    "User",
                    fields=fields,
                    filters=filters or None,
                    or_filters=or_filters or None,
                    order_by="full_name asc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="admin.list_users failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        _attach_linked_employees(rows)
        return {"data": rows, "total": summary["total"], "summary": summary}

    try:
        rows = (
            frappe.get_all(
                "User",
                fields=fields,
                filters=filters or None,
                or_filters=or_filters or None,
                order_by="full_name asc",
                limit_page_length=limit,
            )
            or []
        )
        _attach_linked_employees(rows)
        return rows
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# G3: Shift Assignment
# --------------------------------------------------------------------------- #
def _shift_display_status(row) -> str:
    """Map a Shift Assignment row to its display lifecycle (parity G3/G4).

    Frappe's ``Shift Assignment.status`` is only ``Active``/``Inactive``; the
    legacy "Completed" value never occurs natively. Real lifecycle:

    * ``Active``    — docstatus 1, status ``Active`` (currently assigned).
    * ``Expired``   — docstatus 1, status ``Inactive`` (scheduler marked it
                      past ``end_date``).
    * ``Cancelled`` — docstatus 2 (HR ended it via ``end_shift_assignment``).
    """
    if not row:
        return ""
    docstatus = row.get("docstatus")
    status = (row.get("status") or "").strip()
    if docstatus == 2 or status == "Cancelled":
        return "Cancelled"
    if docstatus == 1 and status == "Inactive":
        return "Expired"
    return "Active"


def _shift_assignment_summary(light_rows) -> dict:
    """Display-bucket counts over the full filtered set (SPA summary tiles)."""
    active = expired = cancelled = 0
    for r in light_rows or []:
        ds = _shift_display_status(r)
        if ds == "Active":
            active += 1
        elif ds == "Expired":
            expired += 1
        elif ds == "Cancelled":
            cancelled += 1
    return {
        "total": len(light_rows or []),
        "active": active,
        "expired": expired,
        "cancelled": cancelled,
    }


def _shift_assignment_row(row) -> dict:
    """Augment a raw DB row with display_status + is_cancelled + work_location."""
    row["display_status"] = _shift_display_status(row)
    row["is_cancelled"] = row.get("docstatus") == 2
    if "work_location" not in row:
        row["work_location"] = row.get("vn_work_location") or ""
    return row


def _has_work_location_field() -> bool:
    """True when the gege_hr ``vn_work_location`` Custom Field is migrated."""
    try:
        return bool(frappe.get_meta("Shift Assignment").has_field("vn_work_location"))
    except Exception:
        return False


def _shifts_have_overlapping_timings(shift_1: str, shift_2: str) -> bool:
    """Mirror ``hrms...shift_assignment.has_overlapping_timings`` (parity G5).

    Two shifts on the same date are allowed unless their clock windows overlap
    (so a morning + an evening shift can coexist, matching native semantics).
    """
    s1 = frappe.db.get_value("Shift Type", shift_1, ["start_time", "end_time"], as_dict=True)
    s2 = frappe.db.get_value("Shift Type", shift_2, ["start_time", "end_time"], as_dict=True)
    if not s1 or not s2 or not s1.end_time or not s2.end_time:
        return False
    for d in (s1, s2):
        if d.end_time <= d.start_time:  # overnight shift → roll end to next day
            d.end_time = d.end_time + timedelta(days=1)
    return s1.end_time > s2.start_time and s1.start_time < s2.end_time


def _assert_shift_assignment_cancel_safe(doc) -> None:
    """Block cancelling a Shift Assignment linked to Checkin/Attendance (G6).

    Mirrors hrms ``ShiftAssignment.on_cancel`` so HR gets a friendly Vietnamese
    message *before* the native validator would reject the cancel.
    """
    end = doc.end_date or doc.start_date
    linked_checkin = frappe.db.get_all(
        "Employee Checkin",
        filters={
            "employee": doc.employee,
            "shift": doc.shift_type,
            "time": ["between", [doc.start_date, end]],
        },
        pluck="name",
        limit=1,
    )
    if linked_checkin:
        frappe.throw(
            _("Không thể kết thúc ca: đã có lượt chấm công {0} liên kết.").format(
                linked_checkin[0]
            )
        )
    linked_attendance = frappe.db.get_all(
        "Attendance",
        filters={
            "employee": doc.employee,
            "shift": doc.shift_type,
            "attendance_date": ["between", [doc.start_date, end]],
        },
        pluck="name",
        limit=1,
    )
    if linked_attendance:
        frappe.throw(
            _("Không thể kết thúc ca: đã có bản chấm công {0} liên kết.").format(
                linked_attendance[0]
            )
        )


@frappe.whitelist()
def list_shift_assignments(
    employee: str | None = None,
    shift_type: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    work_location: str | None = None,
    department: str | None = None,
    company: str | None = None,
    display_status: str | None = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """List Shift Assignments (submitted) for the admin table.

    Filters by employee / shift_type / status / date window, or a free-text
    ``search`` (OR-matched across employee / employee_name / shift_type —
    DNA §6.6 D, HR-BL-10) so the SPA broad-search box no longer filters an
    already-loaded list client-side.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates status buckets over the *full*
    filtered set so the SPA summary tiles stay correct under pagination.
    Without ``page_size`` the legacy bare-list return is preserved.
    """
    _require_hr_admin()
    has_loc = _has_work_location_field()
    # Parity G3/G4: show submitted + cancelled by default so ended assignments
    # stay visible (history). A ``display_status`` filter narrows the bucket.
    filters = []
    ds = (display_status or "").strip()
    if ds == "Active":
        filters += [["docstatus", "=", 1], ["status", "=", "Active"]]
    elif ds == "Expired":
        filters += [["docstatus", "=", 1], ["status", "=", "Inactive"]]
    elif ds == "Cancelled":
        filters.append(["docstatus", "=", 2])
    else:
        filters.append(["docstatus", "in", [1, 2]])
        if status:  # legacy Active/Inactive filter kept for back-compat
            filters.append(["status", "=", status])
    if employee:
        filters.append(["employee", "=", employee])
    if shift_type:
        filters.append(["shift_type", "=", shift_type])
    if department:
        filters.append(["department", "=", department])
    if company:
        filters.append(["company", "=", company])
    if from_date:
        filters.append(["start_date", ">=", getdate(from_date)])
    if to_date:
        filters.append(["end_date", "<=", getdate(to_date)])
    if work_location and has_loc:
        filters.append(["vn_work_location", "=", work_location])
    # Broad search (DNA §6.6 D) — OR-match across the assignment's text fields.
    or_filters = None
    _q = (search or "").strip()
    if _q:
        _like = f"%{pagination.escape_like(_q)}%"
        or_filters = [
            ["employee", "like", _like],
            ["employee_name", "like", _like],
            ["shift_type", "like", _like],
            ["department", "like", _like],
        ]
        if has_loc:
            or_filters.append(["vn_work_location", "like", _like])
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
        "department",
    ]
    # The work-location column is a gege_hr Custom Field — only present
    # after migrate. Guard so a fresh bench (pre-migrate) doesn't 500.
    if has_loc:
        fields.append("vn_work_location as work_location")

    if page_size:
        summary = _shift_assignment_summary(
            pagination.all_rows(
                "Shift Assignment",
                fields=["name", "status", "docstatus"],
                filters=filters,
                or_filters=or_filters,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    "Shift Assignment",
                    filters=filters,
                    or_filters=or_filters,
                    fields=fields,
                    order_by="start_date desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="admin.list_shift_assignments failed")
            rows = []
        return {
            "data": [_shift_assignment_row(r) for r in rows],
            "total": summary["total"],
            "summary": summary,
        }

    try:
        rows = (
            frappe.get_all(
                "Shift Assignment",
                filters=filters,
                or_filters=or_filters,
                fields=fields,
                order_by="start_date desc",
                limit_page_length=pagination.clamp_limit(limit, default=100),
            )
            or []
        )
    except Exception:
        rows = []
    return [_shift_assignment_row(r) for r in rows]


@frappe.whitelist()
def get_shift_assignment_options() -> dict:
    """Dropdown options for the Shift Assignment gear popover (DNA §6.3).

    Auto-fetched so the popover never shows an empty dropdown. ``display_status``
    replaces the dead native "Completed" with the real Active/Expired/Cancelled
    lifecycle (parity G3/G4).
    """
    _require_hr_admin()
    has_loc = _has_work_location_field()

    def _opts(doctype, field="name"):
        try:
            return [
                {"value": v, "label": v}
                for v in frappe.db.get_all(doctype, pluck=field, order_by=f"{field} asc")
            ]
        except Exception:
            return []

    statuses = [
        {"value": "Active", "label": "Đang gán"},
        {"value": "Expired", "label": "Hết hạn"},
        {"value": "Cancelled", "label": "Đã huỷ"},
    ]
    return {
        "statuses": statuses,
        "shift_types": _opts("Shift Type"),
        "departments": _opts("Department"),
        "companies": _opts("Company"),
        "work_locations": _opts("VN Work Location") if has_loc else [],
    }


@frappe.whitelist()
def create_shift_assignment(
    employee: str,
    shift_type: str,
    start_date: str,
    end_date: str | None = None,
    status: str = "Active",
    work_location: str | None = None,
    shift_request: str | None = None,
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

    # Overlap detection (parity G5) — matches native Shift Assignment semantics:
    # honour HR Settings "Allow Multiple Shift Assignments for Same Date" and only
    # block when the clock timings actually overlap (morning + evening = OK).
    allow_multi = False
    try:
        allow_multi = bool(
            frappe.db.get_single_value("HR Settings", "allow_multiple_shift_assignments")
        )
    except Exception:
        allow_multi = False
    if not allow_multi:
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
        conflicts = [
            d for d in existing if _shifts_have_overlapping_timings(shift_type, d.shift_type)
        ]
        if conflicts:
            frappe.throw(
                _("Nhân viên đã có ca làm việc trùng giờ trong khoảng này: {0}").format(
                    ", ".join(d.name for d in conflicts)
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
    if shift_request:
        payload["shift_request"] = shift_request  # back-link (parity G9)
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
        _assert_shift_assignment_cancel_safe(doc)
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


@frappe.whitelist()
def bulk_create_shift_assignments(
    employees,
    shift_type,
    start_date,
    end_date: str | None = None,
    work_location: str | None = None,
) -> dict:
    """Assign one shift to many employees at once (parity G7 — Shift Assignment Tool).

    Partial-safe: each employee is created independently via
    :func:`create_shift_assignment`, so a conflict (overlap / safeguard) on one
    does NOT abort the batch. ``created``/``failed`` are reported back so the SPA
    can show a per-employee result; per-assignment audit rows are written by
    ``create_shift_assignment`` (granular traceability).
    """
    import json

    _require_hr_admin()
    # Frappe passes whitelist args as strings under the standard form POST, but
    # the SPA's JSON frappeCall may deliver a real list — accept both.
    if isinstance(employees, str):
        try:
            employees = json.loads(employees)
        except Exception:
            employees = [employees]
    employees = [e for e in (employees or []) if e]
    shift_type = (shift_type or "").strip()
    start_date = (start_date or "").strip()
    if not employees:
        frappe.throw(_("Chọn ít nhất một nhân viên."))
    if not shift_type:
        frappe.throw(_("Chọn ca làm việc."))
    if not start_date:
        frappe.throw(_("Ngày bắt đầu là bắt buộc."))

    created, failed = [], []
    for emp in employees:
        try:
            doc = create_shift_assignment(
                employee=emp,
                shift_type=shift_type,
                start_date=start_date,
                end_date=end_date,
                work_location=work_location,
            )
            created.append(doc.get("name") if isinstance(doc, dict) else doc)
        except Exception as exc:
            failed.append({"employee": emp, "reason": str(exc)})
    return {"created": created, "failed": failed}


# --------------------------------------------------------------------------- #
# G9 — Shift Request (parity: reuse native hrms "Shift Request" submittable
# doctype). Employee requests a shift; HR/manager approves → a Shift Assignment
# is created + linked back; reject → no assignment.
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_shift_requests(
    employee: str | None = None,
    shift_type: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """List Shift Requests (Draft / Approved / Rejected) — HR view."""
    _require_hr_admin()
    filters = []
    if employee:
        filters.append(["employee", "=", employee])
    if shift_type:
        filters.append(["shift_type", "=", shift_type])
    if status:
        filters.append(["status", "=", status])
    if from_date:
        filters.append(["from_date", ">=", getdate(from_date)])
    if to_date:
        filters.append(["to_date", "<=", getdate(to_date)])
    or_filters = None
    _q = (search or "").strip()
    if _q:
        _like = f"%{pagination.escape_like(_q)}%"
        or_filters = [
            ["employee_name", "like", _like],
            ["shift_type", "like", _like],
            ["employee", "like", _like],
        ]
    fields = [
        "name",
        "employee",
        "employee_name",
        "shift_type",
        "from_date",
        "to_date",
        "status",
        "approver",
        "company",
        "department",
        "docstatus",
    ]
    if page_size:
        total = pagination.count_all("Shift Request", filters=filters, or_filters=or_filters)
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    "Shift Request",
                    filters=filters,
                    or_filters=or_filters,
                    fields=fields,
                    order_by="modified desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="admin.list_shift_requests failed")
            rows = []
        return {"data": rows, "total": total}
    try:
        return frappe.get_all(
            "Shift Request",
            filters=filters,
            or_filters=or_filters,
            fields=fields,
            order_by="modified desc",
            limit_page_length=pagination.clamp_limit(limit, default=100),
        )
    except Exception:
        return []


@frappe.whitelist()
def create_shift_request(
    employee: str,
    shift_type: str,
    from_date: str,
    to_date: str | None = None,
    approver: str | None = None,
    company: str | None = None,
) -> dict:
    """Submit a Shift Request (draft, docstatus 0). Native doctype is submittable."""
    _require_hr_admin()
    employee = (employee or "").strip()
    shift_type = (shift_type or "").strip()
    from_date = (from_date or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not shift_type or not frappe.db.exists("Shift Type", shift_type):
        frappe.throw(_("Ca làm việc không tồn tại."))
    if not from_date:
        frappe.throw(_("Ngày bắt đầu là bắt buộc."))
    approver = (approver or "").strip() or frappe.session.user
    company = company or _company_for_employee(employee)
    doc = frappe.get_doc(
        {
            "doctype": "Shift Request",
            "employee": employee,
            "shift_type": shift_type,
            "from_date": getdate(from_date),
            "to_date": getdate(to_date) if to_date else None,
            "approver": approver,
            "company": company,
            "status": "Draft",
        }
    )
    doc.insert()
    _audit_admin(
        _("Tạo yêu cầu đổi ca {0} cho {1}").format(shift_type, employee),
        reference_doctype="Shift Request",
        reference_name=doc.name,
        company=company,
        employee=employee,
    )
    return {"name": doc.name}


@frappe.whitelist()
def approve_shift_request(name: str) -> dict:
    """Approve a Shift Request → create + submit a Shift Assignment linked back
    to the request (overlap-aware via create_shift_assignment), then mark Approved.
    """
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Shift Request", name):
        frappe.throw(_("Yêu cầu đổi ca không tồn tại."))
    req = frappe.get_doc("Shift Request", name)
    if req.status == "Approved":
        frappe.throw(_("Yêu cầu này đã được duyệt."))
    assignment = create_shift_assignment(
        employee=req.employee,
        shift_type=req.shift_type,
        start_date=str(req.from_date),
        end_date=str(req.to_date) if req.to_date else None,
        shift_request=name,
    )
    req.status = "Approved"
    req.save()
    sa_name = assignment.get("name") if isinstance(assignment, dict) else assignment
    _audit_admin(
        _("Duyệt yêu cầu đổi ca {0}").format(name),
        reference_doctype="Shift Request",
        reference_name=name,
        company=req.company or _company_for_employee(req.employee),
        employee=req.employee,
        new_value={"shift_assignment": sa_name},
    )
    return {"name": name, "shift_assignment": sa_name}


@frappe.whitelist()
def reject_shift_request(name: str, reason: str | None = None) -> dict:
    """Reject a Shift Request (no Shift Assignment is created)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Shift Request", name):
        frappe.throw(_("Yêu cầu đổi ca không tồn tại."))
    req = frappe.get_doc("Shift Request", name)
    req.status = "Rejected"
    req.save()
    _audit_admin(
        _("Từ chối yêu cầu đổi ca {0}").format(name),
        reference_doctype="Shift Request",
        reference_name=name,
        company=req.company or _company_for_employee(req.employee),
        employee=req.employee,
        new_value={"reason": reason or ""},
    )
    return {"name": name}


# --------------------------------------------------------------------------- #
# G8 — Recurring shift schedule (parity). Native ``Shift Assignment`` already
# applies a shift to EVERY day in [start_date, end_date], so a continuous
# schedule is one assignment. A ``weekdays`` subset (Mon=0..Sun=6) creates one
# assignment per maximal run of consecutive matching days (e.g. only Sat-Sun, or
# a rotating Mon/Wed/Fri). Reuses create_shift_assignment (overlap-aware,
# audited); partial-safe across employees. (The native ``Shift Assignment
# Schedule`` doctype is absent in this hrms version, so this is self-contained.)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def generate_recurring_shift_assignments(
    employees,
    shift_type,
    from_date,
    to_date,
    weekdays=None,
) -> dict:
    """Generate recurring Shift Assignments over a date range (parity G8)."""
    import json
    from datetime import timedelta

    _require_hr_admin()
    if isinstance(employees, str):
        try:
            employees = json.loads(employees)
        except Exception:
            employees = [employees]
    employees = [e for e in (employees or []) if e]
    shift_type = (shift_type or "").strip()
    if not employees:
        frappe.throw(_("Chọn ít nhất một nhân viên."))
    if not shift_type:
        frappe.throw(_("Chọn ca làm việc."))
    if not from_date or not to_date:
        frappe.throw(_("Khoảng ngày là bắt buộc."))
    start = getdate(from_date)
    end = getdate(to_date)
    if end < start:
        frappe.throw(_("Ngày kết thúc phải sau ngày bắt đầu."))

    # weekdays: None/empty = every day; else a subset of 0..6 (Mon=0).
    wd = None
    if weekdays:
        if isinstance(weekdays, str):
            try:
                weekdays = json.loads(weekdays)
            except Exception:
                weekdays = [int(x) for x in weekdays.split(",") if str(x).strip() != ""]
        wd = {int(x) for x in weekdays if str(x).strip() != ""}
        if not wd:
            wd = None

    # Build maximal runs of consecutive matching days.
    runs = []
    cur_start = None
    day = start
    one_day = timedelta(days=1)
    while day <= end:
        match = wd is None or (day.weekday() in wd)
        if match and cur_start is None:
            cur_start = day
        elif not match and cur_start is not None:
            runs.append((cur_start, day - one_day))
            cur_start = None
        day += one_day
    if cur_start is not None:
        runs.append((cur_start, end))
    if not runs:
        frappe.throw(_("Không có ngày nào trong khoảng khớp tuần đã chọn."))

    created, failed = [], []
    for emp in employees:
        for run_start, run_end in runs:
            try:
                doc = create_shift_assignment(
                    employee=emp,
                    shift_type=shift_type,
                    start_date=str(run_start),
                    end_date=str(run_end),
                )
                created.append(doc.get("name") if isinstance(doc, dict) else doc)
            except Exception as exc:
                failed.append(
                    {
                        "employee": emp,
                        "from_date": str(run_start),
                        "to_date": str(run_end),
                        "reason": str(exc),
                    }
                )
    return {
        "created": created,
        "failed": failed,
        "runs": [{"from_date": str(s), "to_date": str(e)} for s, e in runs],
    }


@frappe.whitelist()
def set_shift_assignment_status(name: str, status: str) -> dict:
    """Toggle a submitted Shift Assignment's status (Active/Inactive) — reversible.

    Unlike :func:`end_shift_assignment` (which cancels → docstatus 2), this keeps
    the document submitted and just flips ``status`` (allow_on_submit), matching
    the native "Inactive" action. Inactive stops the shift applying; setting back
    to Active re-validates overlap.
    """
    _require_hr_admin()
    name = (name or "").strip()
    status = (status or "").strip()
    if status not in ("Active", "Inactive"):
        frappe.throw(_("Trạng thái không hợp lệ (chỉ Active/Inactive)."))
    if not name or not frappe.db.exists("Shift Assignment", name):
        frappe.throw(_("Ca làm việc không tồn tại."))
    doc = frappe.get_doc("Shift Assignment", name)
    if doc.docstatus != 1:
        frappe.throw(_("Chỉ ca đã xác nhận mới đổi trạng thái được."))
    doc.status = status
    doc.save()  # allow_on_submit; native re-validates overlap on Active
    _audit_admin(
        _("Đổi trạng thái ca {0} → {1}").format(name, status),
        reference_doctype="Shift Assignment",
        reference_name=name,
        company=doc.company or _company_for_employee(doc.employee),
        employee=doc.employee,
        new_value={"status": status},
    )
    return {"name": name, "status": status}


# --------------------------------------------------------------------------- #
# Manual check-in override — team-attendance matrix (checkin-manager.txt parity)
# --------------------------------------------------------------------------- #
# Lets an HR/Line Manager fix an employee's IN/OUT punch straight from the
# /hr/team/attendance grid. ``time_in``/``time_out`` arrive as portal-local
# strings ("YYYY-MM-DD HH:mm"); they are converted to the UTC storage convention
# used by ``Employee Checkin.time``. Existing rows are updated in place; missing
# rows are inserted. Each affected day's Work Session is recalculated so the grid
# reflects the change immediately.
ATTENDANCE_EDITOR_ROLES = ["HR Manager", "System Manager", "HR User", "Line Manager"]


def _require_attendance_editor_for(employee: str) -> None:
    """Permission gate for :func:`admin_custom_checkin`.

    HR Manager / System Manager / HR User may edit anyone (HR User kept because
    the company roster is its scope). ``Line Manager`` may only edit employees
    whose ``reports_to`` is them — preventing cross-team privilege escalation
    (plan §6 risk R4). Raises ``PermissionError`` otherwise.
    """
    roles = set(frappe.get_roles())
    if roles & {"System Manager", "HR Manager", "HR User"}:
        return
    if roles & {"Line Manager"}:
        from gege_hr.gege_hr.utils import employee as emp_utils

        me = emp_utils.get_employee_for_user()
        if me:
            reports_to = frappe.db.get_value("Employee", employee, "reports_to")
            if reports_to == me:
                return
        frappe.throw(
            _("Bạn chỉ được sửa chấm công của nhân viên trong team của mình."),
            frappe.PermissionError,
        )
    frappe.throw(_("Bạn không có quyền thực hiện thao tác này."), frappe.PermissionError)


def _parse_portal_dt(value):
    """Parse a portal-local datetime string ("YYYY-MM-DD HH:mm[:ss]" / ISO) →
    a naive ``datetime``. Returns ``None`` for empty/unparseable input."""
    from datetime import datetime

    if not value:
        return None
    v = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _portal_local_to_utc_str(value) -> str | None:
    """Convert a portal-local datetime string to the MySQL-safe UTC
    "YYYY-MM-DD HH:MM:SS" stored in ``Employee Checkin.time`` (plan §2.7).

    The input is interpreted in the configured portal timezone
    (Asia/Ho_Chi_Minh), mirroring :func:`utils.tz.now_in_portal`.
    """
    from zoneinfo import ZoneInfo

    from gege_hr.gege_hr.utils import tz as tz_utils

    dt = _parse_portal_dt(value)
    if dt is None:
        return None
    dt = dt.replace(tzinfo=tz_utils.get_tzinfo())
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def _find_existing_checkin(employee: str, log_type: str, utc_time_str: str) -> str | None:
    """Find an existing ``Employee Checkin`` of ``log_type`` on the same portal
    day as ``utc_time_str``. Used when the caller passes no docname (the team
    grid does not carry checkin doc names) so an edit updates the right row
    instead of inserting a duplicate. Returns the docname or ``None``.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from frappe.utils import get_datetime

    from gege_hr.gege_hr.utils import tz as tz_utils

    check_dt = get_datetime(utc_time_str)
    portal_day = tz_utils.to_portal(check_dt).date()
    tz = tz_utils.get_tzinfo()
    start_local = datetime.combine(portal_day, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
    order = "time asc" if log_type == "IN" else "time desc"
    rows = frappe.db.get_all(
        "Employee Checkin",
        filters={
            "employee": employee,
            "log_type": log_type,
            "time": ["between", [start_utc, end_utc]],
        },
        pluck="name",
        order_by=order,
        limit=1,
    )
    return rows[0] if rows else None


def _upsert_employee_checkin(employee: str, docname: str | None, log_type: str, utc_time_str: str) -> str:
    """Insert or update an ``Employee Checkin`` row and recalc its Work Session.

    When ``docname`` is empty (the team grid does not send checkin doc names),
    we look up the existing row of this ``log_type`` on the same portal day and
    update it — preventing duplicate punches on repeated edits. The update path
    does NOT fire the ``after_insert`` recalc hook, so we re-run the Work-Session
    recalculation explicitly via the same public entry point the hook uses
    (:func:`attendance.on_employee_checkin_create`). On the insert path that
    hook fires automatically.
    """
    from gege_hr.gege_hr.api import attendance as att_api

    if not docname:
        docname = _find_existing_checkin(employee, log_type, utc_time_str)
    if docname and frappe.db.exists("Employee Checkin", docname):
        frappe.db.set_value(
            "Employee Checkin", docname, "time", utc_time_str, update_modified=False
        )
        try:
            doc = frappe.get_doc("Employee Checkin", docname)
            att_api.on_employee_checkin_create(doc)
        except Exception:
            frappe.log_error(
                title="admin_custom_checkin: recalc failed",
                message=f"checkin={docname} employee={employee}",
            )
        return docname

    doc = frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": employee,
            "log_type": log_type,
            "time": utc_time_str,
        }
    )
    doc.insert(ignore_permissions=True)
    # ``after_insert`` hook fires recalc automatically on insert.
    return doc.name


@frappe.whitelist()
def admin_custom_checkin(
    employee: str,
    in_id: str | None = None,
    time_in: str | None = None,
    out_id: str | None = None,
    time_out: str | None = None,
) -> dict:
    """Manual override of an employee's IN/OUT ``Employee Checkin`` for a day.

    Parity with ``checkin-manager.txt`` ``admin_custom_checkin``. The manager
    types portal-local times ("dd/MM/yyyy HH:mm" or "YYYY-MM-DD HH:mm"); they
    are converted to the UTC storage convention. Existing rows (in_id/out_id)
    are updated in place; missing rows are inserted. Each affected day's Work
    Session is recalculated so the team-attendance grid reflects the change.

    Returns ``{"status": "ok", "msg": ..., "touched": [...]}`` or raises a
    Vietnamese error.
    """
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."), frappe.DoesNotExistError)

    _require_attendance_editor_for(employee)

    utc_in = _portal_local_to_utc_str(time_in)
    utc_out = _portal_local_to_utc_str(time_out)

    if not utc_in and not utc_out:
        frappe.throw(_("Cần điền tối thiểu Thời gian Vào hoặc Thời gian Ra."))

    # Lock guard: hand-edited checkins must not land inside a Locked monthly
    # period — the attendance its lines were computed from has to stay frozen
    # (the sanctioned flow is delete payroll review → unlock → edit → re-lock).
    # Resolved defensively so bench-free stubs (unit tests) skip the guard.
    from gege_hr.gege_hr.api import attendance as _att_api

    _is_locked = getattr(_att_api, "_is_date_locked", None)
    for _label, _raw_val in (("Vào", time_in), ("Ra", time_out)):
        if not _raw_val or _is_locked is None:
            continue
        _dt = _parse_portal_dt(_raw_val)
        if _dt and _is_locked(_dt.date().isoformat()):
            frappe.throw(
                _("Ngày {0} (giờ {1}) thuộc kỳ công đã khoá — không thể sửa chấm công. Hãy mở khóa kỳ công trước.")
                .format(_dt.date().isoformat(), _label)
            )

    # Reject an obvious inversion (Ra trước Vào). We compare the manager's raw
    # portal-local inputs so overnight shifts (OUT on the next calendar day,
    # typed in explicitly) are not falsely rejected.
    if utc_in and utc_out:
        tin = _parse_portal_dt(time_in)
        tout = _parse_portal_dt(time_out)
        if tin and tout and tout < tin:
            frappe.throw(_("Thời gian Ra không được trước Thời gian Vào."))

    touched = []
    if utc_in:
        touched.append(_upsert_employee_checkin(employee, in_id, "IN", utc_in))
    if utc_out:
        touched.append(_upsert_employee_checkin(employee, out_id, "OUT", utc_out))

    frappe.db.commit()

    _audit_admin(
        _("Sửa chấm công thủ công {0}").format(employee),
        reference_doctype="Employee Checkin",
        reference_name=touched[0] if touched else "",
        company=_company_for_employee(employee),
        employee=employee,
        new_value={
            "in_id": in_id,
            "time_in": time_in,
            "out_id": out_id,
            "time_out": time_out,
        },
    )

    return {"status": "ok", "msg": "Đã cập nhật chấm công.", "touched": touched}
