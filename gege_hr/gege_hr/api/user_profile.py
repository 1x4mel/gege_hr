"""User 360° profile API — /hr/users parity (plans/plan-user-frontend-parity.md §2.1, §2.3, §2.5, §2.7).

Endpoints powering the SPA ``User360Drawer`` so an HR admin never has to open
the Frappe Desk to operate a User account:

* :func:`get_user_360` — one-call 360° payload: scrubbed profile + roles +
  linked Employee (with a "Left but still enabled" warning) + merged activity
  timeline (standard ``Comment`` + ``Version`` + ``VN Audit Event``) + recent
  logins (``Activity Log``).
* :func:`add_user_comment` — HR note stored as a standard ``Comment`` row
  referenced to the User.
* :func:`logout_all_sessions` — wrap ``frappe.sessions.clear_sessions`` (the
  Desk "Logout From All Devices" button), guarded so HR can never kill their
  own session.
* :func:`update_user_security` — whitelist-edit the standard User security
  knobs (``login_before`` / ``login_after`` / ``simultaneous_sessions``;
  ``restrict_ip`` System Manager only).
* :func:`list_user_permissions` / :func:`add_user_permission` /
  :func:`remove_user_permission` — data-scope rows via the standard
  ``User Permission`` doctype (``frappe.permissions`` helpers), allow-listed to
  safe masters so the portal can never escalate privileges.
* :func:`get_role_profile_options` / :func:`set_user_role_profile` — assign a
  Role Profile bundle; Frappe's ``User.validate`` replaces the roles table
  from the profile on save.
* :func:`bulk_assign_roles` / :func:`bulk_set_users_enabled` — partial-safe
  bulk mutations (cap 100, per-row try/except — same posture as
  ``admin.bulk_end_shift_assignments``).

Self-contained on purpose: the shared audit / gate helpers are imported lazily
inside the endpoints (pattern ``employee_profile.py``), and the pure helpers
(:func:`left_employee_warning`, :func:`summarize_logins`,
:func:`validate_security`) carry the logic so the bench-free stub-frappe tests
in ``tests/test_user_profile.py`` stay simple. Timeline scrubbing reuses the
pure helpers of :mod:`gege_hr.gege_hr.api.employee_profile`.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.employee_profile import (
    SENSITIVE_FIELD_RE,
    _existing_fields,
    _safe_rows,
    merge_activity,
)

USER_DOCTYPE = "User"

HR_ADMIN_ROLES = ["HR Manager", "System Manager"]

# Whitelist of standard User security fields the portal may edit (plan §2.3.2).
# B0.1 findings: ``login_before`` / ``login_after`` are Int hours (0-24) on the
# User doctype; ``block_user`` does NOT exist in this Frappe version — account
# locking is the existing ``enabled`` toggle (admin.set_user_enabled).
_USER_SECURITY_EDITABLE = ["login_before", "login_after", "simultaneous_sessions"]

# System Manager-only knob (plan risk R2 — a wrong restrict_ip locks HR out).
_RESTRICT_IP_FIELD = "restrict_ip"

# User Permission ``allow`` whitelist (plan §2.5.1, risk R1). Data-scope masters
# only — never arbitrary doctypes, never ``User`` itself.
_ALLOWED_PERM_DOCTYPES = [
    "Company",
    "Branch",
    "Department",
    "VN Work Location",
    "Employment Type",
    "Designation",
]

# Cap for the bulk endpoints (plan risk R6).
_BULK_CAP = 100


def _require_hr_admin() -> None:
    """Gate: same admin roles as ``admin._require_hr_admin`` (lazy import)."""
    from gege_hr.gege_hr.api.admin import _require_hr_admin as _gate

    _gate()


def _audit(description: str, *, user: str, new_value=None) -> None:
    """Best-effort VN Audit Event row (lazy ``admin._audit_admin``)."""
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin

        _audit_admin(
            description,
            reference_doctype=USER_DOCTYPE,
            reference_name=user,
            new_value=new_value,
        )
    except Exception:
        frappe.log_error(title="user_profile audit failed")


def _require_user(user: str) -> str:
    """Trim + existence-check a User name (Vietnamese error on failure)."""
    user = (user or "").strip()
    if not user or not frappe.db.exists(USER_DOCTYPE, user):
        frappe.throw(_("Người dùng không tồn tại."))
    return user


# --------------------------------------------------------------------------- #
# Pure helpers — no frappe I/O (bench-free unit tests, BE-UP-01/02/11/12)
# --------------------------------------------------------------------------- #
def left_employee_warning(user_enabled, employee_status, employee_name=None) -> str | None:
    """ "Employee Left but account still enabled" warning (BE-UP-02).

    Only the dangerous combination returns a message: the Employee is Left AND
    the account is still enabled. Everything else (no employee, active
    employee, already-disabled account) stays quiet.
    """
    if not user_enabled:
        return None
    if (employee_status or "") != "Left":
        return None
    who = f" {employee_name}" if employee_name else ""
    return f"Nhân viên{who} đã nghỉ việc nhưng tài khoản vẫn đang hoạt động."


def summarize_logins(rows, limit: int = 10) -> list[dict]:
    """Activity Log rows → newest-first slice (BE-UP-01).

    Missing/blank ``creation`` sorts last; never raises on odd payloads.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("creation") or ""), reverse=True)
    return rows[: max(0, int(limit))]


def validate_security(values: dict) -> list[str]:
    """Pure validation of ``update_user_security`` values (BE-UP-11/12).

    B0.1: ``login_before`` / ``login_after`` are Int hours 0-24. A same-day
    window is required — ``login_after < login_before`` (after=8, before=18
    means 08:00–18:00). ``simultaneous_sessions`` must be an int ≥ 1.
    Returns a list of Vietnamese error strings (empty = valid).
    """
    values = values or {}
    errors: list[str] = []

    def _hour(key: str):
        raw = values.get(key)
        if raw in (None, ""):
            return None
        try:
            v = int(raw)
        except (TypeError, ValueError):
            errors.append("Khung giờ đăng nhập phải là số giờ (0-24).")
            return None
        if not 0 <= v <= 24:
            errors.append("Khung giờ đăng nhập phải nằm trong 0-24.")
            return None
        return v

    after = _hour("login_after")
    before = _hour("login_before")
    if after is not None and before is not None and after >= before:
        errors.append("Khung giờ cho phép đăng nhập không hợp lệ — giờ bắt đầu phải nhỏ hơn giờ kết thúc.")

    sessions = values.get("simultaneous_sessions")
    if sessions not in (None, ""):
        try:
            s = int(sessions)
        except (TypeError, ValueError):
            s = None
        if s is None:
            errors.append("Số phiên đồng thời phải là số nguyên.")
        elif s < 1:
            errors.append("Số phiên đồng thời tối thiểu là 1.")

    ip = values.get(_RESTRICT_IP_FIELD)
    if ip is not None and not str(ip).strip():
        errors.append("Giới hạn IP không được để trống — bỏ trống nếu không giới hạn.")
    return errors


# --------------------------------------------------------------------------- #
# Frappe I/O helpers
# --------------------------------------------------------------------------- #
def _version_ref_filters(user: str) -> dict:
    """Version link filters, column-name aware (v15: ``ref_doctype``)."""
    if "ref_doctype" in _existing_fields("Version", ["ref_doctype"]):
        return {"ref_doctype": USER_DOCTYPE, "ref_name": user}
    return {"ref_type": USER_DOCTYPE, "ref_name": user}


def _user_roles(user: str) -> list[str]:
    """Every role on the User (Has Role child rows)."""
    try:
        return frappe.db.get_all("Has Role", filters={"parent": user}, pluck="role") or []
    except Exception:
        frappe.log_error(title="user_profile._user_roles failed")
        return []


def _linked_employee(user: str) -> dict | None:
    """The Employee whose ``user_id`` is this account (or None)."""
    try:
        rows = frappe.get_all(
            "Employee",
            filters={"user_id": user},
            fields=_existing_fields("Employee", ["name", "employee_name", "status", "image"]),
            limit_page_length=1,
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _scrub_payload(payload: dict) -> dict:
    """Drop sensitive keys (password/api_key/…) — BE-UP-03."""
    return {k: v for k, v in (payload or {}).items() if not SENSITIVE_FIELD_RE.search(str(k))}


# --------------------------------------------------------------------------- #
# P0 — User 360° (plan §2.1)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_user_360(user: str, activity_limit: int = 20) -> dict:
    """One-call 360° payload for the SPA drawer (plan §2.1.1, BE-UP-03/04/05)."""
    _require_hr_admin()
    user = _require_user(user)
    activity_limit = max(1, min(int(activity_limit or 20), 50))

    doc = frappe.get_doc(USER_DOCTYPE, user)
    profile_fields = _existing_fields(
        USER_DOCTYPE,
        [
            "name",
            "email",
            "full_name",
            "username",
            "enabled",
            "user_type",
            "user_image",
            "mobile_no",
            "phone",
            "language",
            "time_zone",
            "gender",
            "birth_date",
            "last_login",
            "last_active",
            "creation",
        ],
    )
    profile = {f: doc.get(f) for f in profile_fields}
    security = {}
    for f in _USER_SECURITY_EDITABLE + [_RESTRICT_IP_FIELD]:
        if f in _existing_fields(USER_DOCTYPE, [f]):
            security[f] = doc.get(f)
    profile["security"] = security
    profile["can_edit_restrict_ip"] = "System Manager" in (frappe.get_roles() or [])
    profile["role_profile_name"] = getattr(doc, "role_profile_name", None)

    roles = _user_roles(user)

    linked = _linked_employee(user)
    warning = left_employee_warning(
        bool(doc.get("enabled")), (linked or {}).get("status"), (linked or {}).get("employee_name")
    )

    comments = _safe_rows(
        "Comment",
        {"reference_doctype": USER_DOCTYPE, "reference_name": user},
        ["owner", "comment_email", "creation", "content"],
        "creation desc",
        20,
    )
    versions = _safe_rows(
        "Version",
        _version_ref_filters(user),
        ["owner", "creation", "data"],
        "creation desc",
        20,
    )
    audits = _safe_rows(
        "VN Audit Event",
        {"reference_doctype": USER_DOCTYPE, "reference_name": user},
        ["audit_type", "actor", "created_at", "description"],
        "created_at desc",
        20,
    )
    logins_raw = _safe_rows(
        "Activity Log",
        {"user": user},
        ["operation", "status", "ip_address", "creation"],
        "creation desc",
        10,
    )

    return _scrub_payload(
        {
            "user": profile,
            "roles": roles,
            "linked_employee": linked,
            "left_employee_warning": warning,
            "activity": merge_activity(comments, versions, audits, activity_limit),
            "logins": summarize_logins(logins_raw, 10),
            "counts": {
                "comments": len(comments or []),
                "versions": len(versions or []),
                "audits": len(audits or []),
                "logins": len(logins_raw or []),
            },
        }
    )


@frappe.whitelist()
def add_user_comment(user: str, comment: str) -> dict:
    """HR note → standard ``Comment`` row on the User (plan §2.1.2, BE-UP-06/07)."""
    _require_hr_admin()
    user = _require_user(user)
    text = (comment or "").strip()
    if len(text) < 2:
        frappe.throw(_("Nội dung ghi chú quá ngắn."))
    doc = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": USER_DOCTYPE,
            "reference_name": user,
            "comment_email": frappe.session.user,
            "content": text,
        }
    )
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "creation": doc.creation, "owner": doc.owner}


# --------------------------------------------------------------------------- #
# P1 — Session & security (plan §2.3)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def logout_all_sessions(user: str) -> dict:
    """Force-logout every device of ``user`` (plan §2.3.1, BE-UP-08/09/10).

    Mirrors the Desk "Logout From All Devices" button via
    ``frappe.sessions.clear_sessions``. HR can never kill their own session —
    they would lock themselves out of the portal mid-operation.
    """
    _require_hr_admin()
    user = _require_user(user)
    if user == frappe.session.user:
        frappe.throw(_("Không thể tự đăng xuất tài khoản đang dùng."))
    from frappe.sessions import clear_sessions

    # Installed Frappe v15 signature is (user, keep_current, force) — the old
    # ``reason=`` kwarg raised TypeError on the real bench (plan-profile §8).
    clear_sessions(user=user, keep_current=False, force=True)
    _audit(_("Buộc đăng xuất mọi thiết bị"), user=user)
    return {"name": user, "ok": True}


@frappe.whitelist()
def update_user_security(user: str, **values) -> dict:
    """Edit the standard User security knobs (plan §2.3.2, BE-UP-11..14).

    Only :data:`_USER_SECURITY_EDITABLE` keys are honoured;
    ``restrict_ip`` additionally requires System Manager (risk R2). Validated
    by :func:`validate_security` before a real ``save()`` so Frappe's own
    hooks (login window enforcement) stay in charge.
    """
    _require_hr_admin()
    user = _require_user(user)
    values = values or {}

    if _RESTRICT_IP_FIELD in values:
        frappe.only_for("System Manager")

    incoming = {k: values.get(k) for k in _USER_SECURITY_EDITABLE if k in values}
    if _RESTRICT_IP_FIELD in values:
        incoming[_RESTRICT_IP_FIELD] = values.get(_RESTRICT_IP_FIELD)

    errors = validate_security(incoming)
    if errors:
        frappe.throw("\n".join(errors))

    target = frappe.get_doc(USER_DOCTYPE, user)
    updated = []
    for field, new_val in incoming.items():
        if field not in _existing_fields(USER_DOCTYPE, [field]):
            continue
        if new_val in (None, ""):
            new_val = None
        old_val = target.get(field)
        if old_val == new_val:
            continue
        target.set(field, new_val)
        updated.append(f"{field}: {old_val!r} → {new_val!r}")
    if updated:
        target.save(ignore_permissions=True)
        _audit(
            _("Cập nhật cài đặt bảo mật"),
            user=user,
            new_value={"updated": updated},
        )
    return {"name": user, "updated": updated}


# --------------------------------------------------------------------------- #
# P2 — User Permission / Role Profile / bulk roles (plan §2.5)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_user_permissions(user: str) -> list[dict]:
    """Data-scope rows of ``user`` (plan §2.5.1, BE-UP-16)."""
    _require_hr_admin()
    user = _require_user(user)
    rows = _safe_rows(
        "User Permission",
        {"user": user},
        ["name", "allow", "for_value", "is_default", "applicable_for", "creation"],
        "creation desc",
        100,
    )
    return [
        {
            "name": r.get("name"),
            "allow": r.get("allow"),
            "value": r.get("for_value"),
            "is_default": r.get("is_default"),
            "applicable_for": r.get("applicable_for"),
            "creation": r.get("creation"),
        }
        for r in rows or []
    ]


@frappe.whitelist()
def add_user_permission(user: str, allow: str, value: str, is_default: int = 0) -> dict:
    """Add a data-scope row via the standard core helper (plan §2.5.1, BE-UP-17).

    ``allow`` is restricted to :data:`_ALLOWED_PERM_DOCTYPES` — a
    privilege-escalation guard (risk R1). Self-scoping (HR restricting their
    own account) is rejected so nobody locks themselves out of the data.
    """
    _require_hr_admin()
    user = _require_user(user)
    allow = (allow or "").strip()
    value = (value or "").strip()
    if allow not in _ALLOWED_PERM_DOCTYPES:
        frappe.throw(_("Loại phạm vi không được phép: {0}.").format(allow))
    if not value:
        frappe.throw(_("Vui lòng chọn giá trị phạm vi."))
    if user == frappe.session.user:
        frappe.throw(_("Không thể đặt phạm vi dữ liệu cho chính tài khoản đang dùng."))
    from frappe.permissions import add_user_permission as core_add

    core_add(allow, value, user, ignore_permissions=True, is_default=1 if is_default else 0)
    _audit(
        _("Thêm phạm vi dữ liệu: {0} = {1}").format(allow, value),
        user=user,
        new_value={"allow": allow, "value": value, "is_default": bool(is_default)},
    )
    return {"name": user, "allow": allow, "value": value, "ok": True}


@frappe.whitelist()
def remove_user_permission(name: str, user: str) -> dict:
    """Remove one User Permission row — must belong to ``user`` (BE-UP-18)."""
    _require_hr_admin()
    user = _require_user(user)
    name = (name or "").strip()
    row = None
    try:
        row = frappe.db.get_value(
            "User Permission", name, ["name", "user", "allow", "for_value"], as_dict=True
        )
    except Exception:
        row = None
    if not row:
        frappe.throw(_("Bản ghi phạm vi không tồn tại."))
    if (row.get("user") if isinstance(row, dict) else row.user) != user:
        frappe.throw(_("Bản ghi không thuộc người dùng này."))
    frappe.delete_doc("User Permission", name, ignore_permissions=True)
    _audit(
        _("Xoá phạm vi dữ liệu: {0} = {1}").format(
            row.get("allow") if isinstance(row, dict) else row.allow,
            row.get("for_value") if isinstance(row, dict) else row.for_value,
        ),
        user=user,
    )
    return {"name": name, "deleted": True}


@frappe.whitelist()
def get_role_profile_options() -> list[dict]:
    """Role Profile bundles for the SPA picker (plan §2.5.2)."""
    _require_hr_admin()
    out: list[dict] = []
    try:
        profiles = (
            frappe.get_all("Role Profile", fields=["name"], order_by="name asc", limit_page_length=50) or []
        )
    except Exception:
        frappe.log_error(title="user_profile.get_role_profile_options failed")
        return []
    for p in profiles:
        name = p.get("name")
        try:
            roles = (
                frappe.get_all(
                    "Has Role",
                    filters={"parent": name, "parenttype": "Role Profile"},
                    pluck="role",
                )
                or []
            )
        except Exception:
            roles = []
        out.append({"name": name, "roles": roles})
    return out


@frappe.whitelist()
def set_user_role_profile(user: str, role_profile: str | None = None) -> dict:
    """Assign / clear the User's Role Profile (plan §2.5.2, BE-UP-19).

    B0.4: ``User.validate`` → ``populate_role_profile_roles`` REPLACES the
    whole roles table from the profile — the response returns roles before /
    after so the SPA can surface the diff (roles silently vanish otherwise).
    """
    _require_hr_admin()
    user = _require_user(user)
    role_profile = (role_profile or "").strip() or None
    if role_profile and not frappe.db.exists("Role Profile", role_profile):
        frappe.throw(_("Nhóm vai trò không tồn tại: {0}").format(role_profile))

    target = frappe.get_doc(USER_DOCTYPE, user)
    roles_before = _user_roles(user)
    target.set("role_profile_name", role_profile)
    target.save(ignore_permissions=True)
    roles_after = _user_roles(user)

    _audit(
        _("Đặt nhóm vai trò: {0}").format(role_profile or "—"),
        user=user,
        new_value={
            "role_profile": role_profile,
            "roles_before": roles_before,
            "roles_after": roles_after,
        },
    )
    return {
        "name": user,
        "role_profile": role_profile,
        "roles_before": roles_before,
        "roles_after": roles_after,
    }


@frappe.whitelist()
def bulk_assign_roles(users, role: str) -> dict:
    """Add one PORTAL_ROLES role to many users (plan §2.5.3, BE-UP-20/21).

    Partial-safe: per-row try/except, one summary audit row (cap 100).
    """
    _require_hr_admin()
    from gege_hr.gege_hr.api.admin import PORTAL_ROLES

    role = (role or "").strip()
    if role not in PORTAL_ROLES:
        frappe.throw(_("Vai trò không được phép: {0}").format(role))
    names = []
    for u in users or []:
        u = (u or "").strip()
        if u and u not in names:
            names.append(u)
    names = names[:_BULK_CAP]

    updated: list[str] = []
    failed: list[dict] = []
    for u in names:
        try:
            target = frappe.get_doc(USER_DOCTYPE, u)
            current = {getattr(d, "role", None) for d in target.get("roles", [])}
            if role in current:
                updated.append(u)  # idempotent — counts as success
                continue
            target.append("roles", {"role": role})
            target.save(ignore_permissions=True)
            updated.append(u)
        except Exception as exc:  # partial-safe (risk R6)
            failed.append({"user": u, "error": str(exc)})
    if updated:
        _audit(
            _("Gán vai trò hàng loạt: {0}").format(role),
            user=updated[0],
            new_value={"role": role, "count": len(updated)},
        )
    return {"role": role, "updated": updated, "failed": failed}


# --------------------------------------------------------------------------- #
# P3 — Bulk enable/disable (plan §2.7)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def bulk_set_users_enabled(users, enabled: int = 1) -> dict:
    """Enable / disable many accounts (plan §2.7, partial-safe, cap 100).

    Guards mirror ``set_user_enabled`` / ``delete_user``: never touch the
    acting session user or the built-in Administrator.
    """
    _require_hr_admin()
    enable = 1 if str(enabled) in ("1", "true", "True") else 0
    names = []
    for u in users or []:
        u = (u or "").strip()
        if u and u not in names:
            names.append(u)
    names = names[:_BULK_CAP]

    updated: list[str] = []
    failed: list[dict] = []
    for u in names:
        try:
            if u == frappe.session.user:
                failed.append({"user": u, "error": "Không thể đổi trạng thái tài khoản đang dùng."})
                continue
            if u in ("Administrator", "Guest"):
                failed.append({"user": u, "error": "Không thể đổi trạng thái tài khoản hệ thống."})
                continue
            target = frappe.get_doc(USER_DOCTYPE, u)
            if int(target.get("enabled") or 0) == enable:
                updated.append(u)
                continue
            target.set("enabled", enable)
            target.save(ignore_permissions=True)
            updated.append(u)
        except Exception as exc:
            failed.append({"user": u, "error": str(exc)})
    if updated:
        _audit(
            _("Khoá/mở tài khoản hàng loạt"),
            user=updated[0],
            new_value={"enabled": bool(enable), "count": len(updated)},
        )
    return {"enabled": bool(enable), "updated": updated, "failed": failed}
