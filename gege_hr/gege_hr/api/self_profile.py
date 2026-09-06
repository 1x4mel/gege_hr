"""Employee self-profile desk-free API — plans/plan-profile-desk-free.md §2.1.

Endpoints powering the SPA ``/hr/profile`` so an employee never has to open
the Frappe Desk to operate their own profile:

* :func:`get_my_profile` — one-call identity payload (scrubbed Employee
  fields + User block + runtime allow-lists). Never returns raw meta.
* :func:`update_my_contact` — allow-listed self-edit of the Employee contact
  block. Ownership gate FIRST, ``doc.save(ignore_permissions=True)`` after
  (the Employee role has no write perm on Employee — precedent
  ``leave.delete_draft``; plan §0 F2).
* :func:`request_profile_change` / :func:`my_profile_requests` /
  :func:`cancel_profile_request` — locked fields (DOB, gender…) flow through
  a standard ``ToDo`` for every HR Manager + a ``Comment`` on the Employee.
* :func:`set_my_avatar` — ``Employee.image`` + ``User.user_image`` from a
  base64 data URL (≤1 MB, jpeg/png/webp; public file — plan R9).
* :func:`my_activity` — merged ``Comment``/``Version``/``VN Audit Event``
  timeline, self-scoped (reuses the employee_profile helpers).
* :func:`change_my_password` — thin Vietnamese wrapper delegating to Frappe's
  stock ``update_password`` (old-password re-auth included there — plan F3).
* :func:`my_sessions` / :func:`logout_my_sessions` — ``Sessions`` listing +
  ``clear_sessions``. NOTE (plan §0 F4): unlike the admin variant in
  ``user_profile.logout_all_sessions``, the self variant MAY kill the current
  session when ``everywhere=1`` — that is the point of the button.
* :func:`my_documents` / :func:`upload_my_document` /
  :func:`delete_my_document` — private ``File`` attachments on the employee's
  own Employee record; employees may only delete files they uploaded.

Self-contained on purpose (pattern ``employee_profile.py``): the shared
helpers are imported lazily inside the endpoints so bench-free stub-frappe
tests in ``tests/test_self_profile.py`` stay simple. Pure helpers
(:func:`diff_contact`, :func:`decode_data_url`, :func:`safe_file_name`,
:func:`short_sid`) carry the logic and are unit-testable without a bench.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

import frappe
from frappe import _

from gege_hr.gege_hr.api.employee_profile import (
    _existing_fields,
    _safe_rows,
    _version_ref_filters,
    merge_activity,
)

EMPLOYEE_DOCTYPE = "Employee"
USER_DOCTYPE = "User"

SELF_REALTIME_EVENT = "profile_updated"
CHANGE_REQUEST_EVENT = "profile_change_requested"

# --------------------------------------------------------------------------- #
# Whitelists (plan §0 F1 / B0.2 — verified against erpnext Employee meta)
# --------------------------------------------------------------------------- #
# Contact block the employee may self-edit. Intersected at runtime with the
# fields that actually exist on the site's Employee meta (partial installs,
# older migrations) so a missing field degrades to "hidden", never "crash".
_SELF_EDIT_CANDIDATES = (
    "cell_number",
    "personal_email",
    "prefered_email",
    "current_address",
    "permanent_address",
    "person_to_be_contacted",
    "relation",
    "marital_status",
    "blood_group",
)

# Display-only fields surfaced in the payload so the SPA can offer the
# "Đề nghị sửa" flow instead of an editable input (plan §1.2 G3).
_LOCKED_CANDIDATES = ("salutation", "employee_number", "gender", "date_of_birth")

_LOCKED_LABELS = {
    "salutation": "Cách xưng hô",
    "employee_number": "Mã nhân viên",
    "gender": "Giới tính",
    "date_of_birth": "Ngày sinh",
}

# Employee fields projected by get_my_profile (identity + contact display).
_PROFILE_FIELDS = (
    "name",
    "employee_name",
    "employee_number",
    "salutation",
    "company",
    "department",
    "branch",
    "designation",
    "employment_type",
    "status",
    "reports_to",
    "gender",
    "date_of_birth",
    "date_of_joining",
    "cell_number",
    "personal_email",
    "prefered_email",
    "company_email",
    "current_address",
    "permanent_address",
    "person_to_be_contacted",
    "relation",
    "marital_status",
    "blood_group",
    "image",
    "user_id",
)

# --------------------------------------------------------------------------- #
# Upload limits (plan §2.1.5 / §2.1.9 — B0.5)
# --------------------------------------------------------------------------- #
_AVATAR_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_AVATAR_MAX_BYTES = 1 * 1024 * 1024  # 1 MB

_DOC_MIME = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
_DOC_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

_CHANGE_REQUEST_MIN_REASON = 10
_TODO_FIELD_PREFIX = "[Hồ sơ]"
_HR_MANAGER_ROLE = "HR Manager"
_HR_MANAGER_CAP = 10  # safety cap — sites with more HR Managers get the first 10

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<payload>.+)$", re.DOTALL)
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


# --------------------------------------------------------------------------- #
# Pure helpers — no frappe I/O (bench-free unit tests, plan PF-01/06/11)
# --------------------------------------------------------------------------- #
def _norm(value) -> str:
    """None-safe stringify + strip (empty and None compare equal)."""
    return ("" if value is None else str(value)).strip()


def diff_contact(current, values, allowed) -> dict:
    """Compute the self-edit diff of an Employee contact block (PF-01).

    Only keys listed in ``allowed`` may change; anything else raises
    ``ValueError`` with a Vietnamese message (never silently dropped — the
    SPA contract must stay explicit). Keys whose normalised value is
    unchanged (including None ↔ "") are skipped so an empty diff is a no-op.
    """
    get = current.get if hasattr(current, "get") else (lambda k, d=None: getattr(current, k, d))
    allowed_set = {str(a) for a in allowed or []}
    diff: dict[str, str] = {}
    for raw_key, raw_val in (values or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if key not in allowed_set:
            raise ValueError(f"Trường không được phép sửa: {key}")
        if _norm(get(key)) != _norm(raw_val):
            diff[key] = _norm(raw_val)
    return diff


def decode_data_url(data_url, allowed_mime: dict, max_bytes: int) -> tuple[str, bytes]:
    """``data:<mime>;base64,<payload>`` → ``(mime, bytes)`` (PF-06/PF-11).

    Vietnamese ``ValueError`` on: malformed data URL, unsupported mime,
    broken base64 or size over ``max_bytes``.
    """
    m = _DATA_URL_RE.match(str(data_url or "").strip())
    if not m:
        raise ValueError("Dữ liệu tệp không hợp lệ (thiếu data URL base64).")
    mime = m.group("mime").lower()
    if mime not in (allowed_mime or {}):
        raise ValueError(f"Định dạng tệp không được hỗ trợ: {mime or 'không xác định'}.")
    try:
        payload = base64.b64decode(m.group("payload"), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("Dữ liệu tệp không hợp lệ (base64 hỏng).") from e
    if len(payload) > int(max_bytes):
        mb = max_bytes / (1024 * 1024)
        raise ValueError(f"Tệp vượt quá giới hạn {mb:.0f} MB.")
    return mime, payload


def safe_file_name(filename: str, fallback: str = "tep-dinh-kem") -> str:
    """Strip path components + unsafe chars from a client-provided filename."""
    name = str(filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_FILENAME_RE.sub("_", name).strip(" .")
    return name[:120] or fallback


def short_sid(sid: str, keep: int = 8) -> str:
    """Truncate a session id for display — never leak the full sid."""
    return str(sid or "")[: max(1, int(keep))]


# --------------------------------------------------------------------------- #
# Frappe I/O helpers
# --------------------------------------------------------------------------- #
def _current_user() -> str:
    user = getattr(frappe.session, "user", None)
    if not user or user == "Guest":
        frappe.throw(_("Vui lòng đăng nhập để sử dụng tính năng này."), frappe.PermissionError)
    return user


def _own_employee() -> str:
    """Employee name linked to the session user (raises VN error if none)."""
    user = _current_user()
    employee = frappe.db.get_value(EMPLOYEE_DOCTYPE, {"user_id": user, "status": "Active"})
    if not employee:
        frappe.throw(
            _("Tài khoản của bạn không liên kết với bản ghi Employee."),
            frappe.PermissionError,
        )
    return employee


def _publish(event: str, message: dict | None = None) -> None:
    """Best-effort realtime tickle targeted at the session user only."""
    try:
        frappe.publish_realtime(event=event, message=message or {}, user=frappe.session.user)
    except Exception:
        frappe.log_error(title="self_profile.publish_realtime failed")


def _audit(
    description: str, *, employee: str | None = None, reference_doctype=None, reference_name=None
) -> None:
    """Best-effort VN Audit Event row (lazy ``admin._audit_admin`` pattern)."""
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin, _company_for_employee

        _audit_admin(
            description,
            reference_doctype=reference_doctype or EMPLOYEE_DOCTYPE,
            reference_name=reference_name or employee,
            company=_company_for_employee(employee) if employee else None,
            employee=employee,
        )
    except Exception:
        frappe.log_error(title="self_profile audit failed")


def _hr_manager_users() -> list[str]:
    """Distinct User names holding the HR Manager role (cap 10, plan R7)."""
    try:
        users = (
            frappe.get_all(
                "Has Role",
                filters={"role": _HR_MANAGER_ROLE, "parenttype": USER_DOCTYPE},
                pluck="parent",
                limit_page_length=50,
            )
            or []
        )
    except Exception:
        return []
    seen: list[str] = []
    for u in users:
        if u and u not in seen:
            seen.append(u)
        if len(seen) >= _HR_MANAGER_CAP:
            break
    return seen


def _notify_managers(title: str, message: str, employee: str) -> int:
    """Bell notification to every HR Manager employee (best-effort)."""
    sent = 0
    try:
        from gege_hr.gege_hr.utils import notify

        for user in _hr_manager_users():
            manager_emp = frappe.db.get_value(EMPLOYEE_DOCTYPE, {"user_id": user, "status": "Active"})
            if not manager_emp:
                continue
            name = notify.push_notification(
                employee=manager_emp,
                notification_type="Alert",
                title=title,
                message=message,
                user=user,
                reference_doctype=EMPLOYEE_DOCTYPE,
                reference_name=employee,
                action_url="/hr/employees",
            )
            if name:
                sent += 1
    except Exception:
        frappe.log_error(title="self_profile notify managers failed")
    return sent


def _insert_file(*, filename: str, content: bytes, doctype: str, docname: str, is_private: int) -> str:
    """Create a ``File`` row from raw bytes (plan §0 F10).

    ``content`` + ``decode=False`` is the stock File controller path — the
    doc writes itself to disk on insert and generates ``file_url``.
    """
    file_doc = frappe.new_doc("File")
    file_doc.file_name = filename
    file_doc.attached_to_doctype = doctype
    file_doc.attached_to_name = docname
    file_doc.is_private = is_private
    # ``content``/``decode`` must land in the docfields dict (``doc.set``) —
    # plain setattr is invisible to File.validate → get_content → nothing is
    # written to disk (E2E P8/P6 caught it live — F-PF6).
    file_doc.set("content", content)
    file_doc.set("decode", False)
    file_doc.insert(ignore_permissions=True)
    return getattr(file_doc, "file_url", None) or (f"{'/private' if is_private else ''}/files/{filename}")


# --------------------------------------------------------------------------- #
# Endpoints — profile payload & contact self-edit
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_my_profile() -> dict:
    """Identity payload for the SPA profile page (plan §2.1.1, PF-14)."""
    user = _current_user()
    employee = frappe.db.get_value(EMPLOYEE_DOCTYPE, {"user_id": user, "status": "Active"})
    if not employee:
        # Non-employee users (Admin/IT) still get the user/security block so
        # the SPA renders the Security tab instead of a dead end (G14).
        employee = None

    existing = set(_existing_fields(EMPLOYEE_DOCTYPE, list(_PROFILE_FIELDS)))
    emp_fields = [f for f in _PROFILE_FIELDS if f in existing] or ["name"]
    emp = None
    if employee:
        emp = frappe.db.get_value(EMPLOYEE_DOCTYPE, employee, emp_fields, as_dict=True)

    user_block = None
    try:
        user_block = frappe.db.get_value(
            USER_DOCTYPE,
            user,
            ["name", "full_name", "language", "time_zone", "user_image", "last_active", "enabled"],
            as_dict=True,
        )
    except Exception:
        user_block = None

    self_edit = [f for f in _SELF_EDIT_CANDIDATES if f in existing]
    locked = [f for f in _LOCKED_CANDIDATES if f in existing]
    return {
        "employee": emp,
        "self_edit_fields": self_edit,
        "locked_fields": locked,
        "locked_labels": {f: _LOCKED_LABELS.get(f, f) for f in locked},
        "user": user_block,
        "flags": {
            "avatar_max_mb": 1,
            "document_max_mb": 5,
            "document_mimes": sorted(_DOC_MIME),
        },
    }


@frappe.whitelist()
def update_my_contact(values=None) -> dict:
    """Self-edit the allow-listed contact block (plan §2.1.2, PF-01→03).

    The Employee doc is derived from the session — a ``values`` payload about
    anyone else's fields is impossible by construction (IDOR-proof).
    """
    employee = _own_employee()
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError):
            frappe.throw(_("Dữ liệu gửi lên không hợp lệ."))
    if not isinstance(values, dict):
        frappe.throw(_("Dữ liệu gửi lên không hợp lệ."))

    existing = set(_existing_fields(EMPLOYEE_DOCTYPE, list(_SELF_EDIT_CANDIDATES)))
    allowed = [f for f in _SELF_EDIT_CANDIDATES if f in existing]
    current = frappe.db.get_value(EMPLOYEE_DOCTYPE, employee, allowed or ["name"], as_dict=True) or {}
    try:
        diff = diff_contact(current, values, allowed)
    except ValueError as e:
        frappe.throw(_(str(e)), frappe.PermissionError)

    changed = sorted(diff)
    if diff:
        doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
        for field, value in diff.items():
            setattr(doc, field, value)
        doc.save(ignore_permissions=True)  # AFTER ownership gate (plan F2)
        _audit(
            _("Tự sửa thông tin liên hệ: {0}").format(", ".join(changed)),
            employee=employee,
        )
        _publish(SELF_REALTIME_EVENT, {"employee": employee, "changed": changed})

    refreshed = frappe.db.get_value(EMPLOYEE_DOCTYPE, employee, allowed or ["name"], as_dict=True) or {}
    return {"changed": changed, "employee": refreshed}


# --------------------------------------------------------------------------- #
# Change requests for locked fields (ToDo + Comment — plan §0 F9)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def request_profile_change(field: str, requested_value: str, reason: str) -> dict:
    """Ask HR to fix a locked field — creates ToDo(s) + Comment (PF-04)."""
    employee = _own_employee()
    field = str(field or "").strip()
    value = _norm(requested_value)
    why = _norm(reason)
    label = _LOCKED_LABELS.get(field, field)
    if field not in _LOCKED_CANDIDATES:
        frappe.throw(_("Trường này không cần đề nghị sửa — vui lòng dùng form chỉnh sửa."))
    if not value:
        frappe.throw(_("Vui lòng nhập giá trị mới."))
    if len(why) < _CHANGE_REQUEST_MIN_REASON:
        frappe.throw(_("Lý do cần ít nhất {0} ký tự.").format(_CHANGE_REQUEST_MIN_REASON))

    user = frappe.session.user
    description = f"{_TODO_FIELD_PREFIX} {user} đề nghị sửa “{label}” → “{value}”. Lý do: {why}"

    managers = _hr_manager_users()
    todo_names: list[str] = []
    for manager in managers:
        todo = frappe.new_doc("ToDo")
        todo.allocated_to = manager
        todo.assigned_by = user
        todo.description = description
        todo.reference_type = EMPLOYEE_DOCTYPE
        todo.reference_name = employee
        todo.priority = "High"
        todo.status = "Open"
        todo.insert(ignore_permissions=True)
        todo_names.append(todo.name)

    comment = frappe.new_doc("Comment")
    comment.comment_type = "Comment"
    comment.reference_doctype = EMPLOYEE_DOCTYPE
    comment.reference_name = employee
    comment.comment_email = user
    comment.content = description
    comment.insert(ignore_permissions=True)

    notified = _notify_managers(
        _("Yêu cầu sửa hồ sơ"),
        _("{0} đề nghị sửa {1}.").format(user, label),
        employee,
    )
    _publish(CHANGE_REQUEST_EVENT, {"employee": employee, "field": field})
    _audit(_("Đề nghị sửa hồ sơ: {0}").format(label), employee=employee)
    return {
        "todos": todo_names,
        "todo": todo_names[0] if todo_names else None,
        "comment": comment.name,
        "notified": notified,
    }


@frappe.whitelist()
def my_profile_requests() -> dict:
    """List the session user's own change-request ToDos (newest first)."""
    employee = _own_employee()
    user = frappe.session.user
    rows = (
        frappe.get_all(
            "ToDo",
            filters={
                "owner": user,
                "reference_type": EMPLOYEE_DOCTYPE,
                "reference_name": employee,
            },
            fields=[
                "name",
                "description",
                "status",
                "priority",
                "allocated_to",
                "owner",
                "creation",
                "modified",
            ],
            order_by="creation desc",
            limit_page_length=50,
        )
        or []
    )
    return {"total": len(rows), "data": rows}


@frappe.whitelist()
def cancel_profile_request(todo: str) -> dict:
    """Close one of MY open change requests (PF-05, IDOR-guarded)."""
    employee = _own_employee()
    user = frappe.session.user
    todo = str(todo or "").strip()
    if not todo:
        frappe.throw(_("Thiếu mã yêu cầu."))
    doc = frappe.get_doc("ToDo", todo)
    if not doc:
        frappe.throw(_("Yêu cầu không tồn tại."))
    if (
        getattr(doc, "reference_type", None) != EMPLOYEE_DOCTYPE
        or getattr(doc, "reference_name", None) != employee
        or getattr(doc, "owner", None) != user
    ):
        frappe.throw(
            _("Bạn không có quyền thao tác yêu cầu này."),
            frappe.PermissionError,
        )
    if getattr(doc, "status", "Open") != "Open":
        frappe.throw(_("Yêu cầu này đã đóng, không thể hủy."))

    doc.status = "Closed"
    doc.save(ignore_permissions=True)

    comment = frappe.new_doc("Comment")
    comment.comment_type = "Comment"
    comment.reference_doctype = EMPLOYEE_DOCTYPE
    comment.reference_name = employee
    comment.comment_email = user
    comment.content = _("Đã hủy yêu cầu sửa hồ sơ.")
    comment.insert(ignore_permissions=True)

    _audit(_("Hủy yêu cầu sửa hồ sơ"), employee=employee, reference_doctype="ToDo", reference_name=todo)
    _publish(CHANGE_REQUEST_EVENT, {"employee": employee, "cancelled": todo})
    return {"name": todo, "status": "Closed"}


# --------------------------------------------------------------------------- #
# Avatar (plan §2.1.5, F6 — set BOTH Employee.image and User.user_image)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def set_my_avatar(data_url: str = None, file_url: str = None) -> dict:
    """Upload (base64) or point to an avatar, then set both image fields (PF-06)."""
    employee = _own_employee()
    user = frappe.session.user
    url = _norm(file_url)
    if not url:
        try:
            mime, payload = decode_data_url(data_url, _AVATAR_MIME, _AVATAR_MAX_BYTES)
        except ValueError as e:
            frappe.throw(_(str(e)))
        ext = _AVATAR_MIME[mime]
        import time

        url = _insert_file(
            filename=f"avatar-{employee}-{int(time.time())}.{ext}",
            content=payload,
            doctype=EMPLOYEE_DOCTYPE,
            docname=employee,
            is_private=0,  # avatars must render in <img> without auth headers
        )
    elif not (url.startswith("/files/") or url.startswith("/private/files/") or url.startswith("https://")):
        frappe.throw(_("Đường dẫn ảnh không hợp lệ."))

    emp_doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
    emp_doc.image = url
    emp_doc.save(ignore_permissions=True)

    user_doc = frappe.get_doc(USER_DOCTYPE, user)
    if user_doc:
        user_doc.user_image = url
        user_doc.save(ignore_permissions=True)

    _audit(_("Đổi ảnh đại diện"), employee=employee)
    _publish(SELF_REALTIME_EVENT, {"employee": employee, "avatar": url})
    return {"image": url}


# --------------------------------------------------------------------------- #
# Activity timeline (reuse employee_profile merger, self-scoped — F7)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_activity(limit: int = 20) -> dict:
    """Merged Comment/Version/Audit timeline of MY Employee (PF-07)."""
    employee = _own_employee()
    limit = max(1, min(int(limit or 20), 50))

    comments = _safe_rows(
        "Comment",
        {"reference_doctype": EMPLOYEE_DOCTYPE, "reference_name": employee},
        ["owner", "comment_email", "creation", "content"],
        "creation desc",
        limit,
    )
    versions = _safe_rows(
        "Version",
        _version_ref_filters(employee),
        ["owner", "creation", "data"],
        "creation desc",
        limit,
    )
    audits = _safe_rows(
        "VN Audit Event",
        {"employee": employee},
        ["audit_type", "actor", "created_at", "description"],
        "created_at desc",
        limit,
    )
    return {"merged": merge_activity(comments, versions, audits, limit)}


# --------------------------------------------------------------------------- #
# Password & sessions (Frampe core delegation — plan §0 F3/F4/F5)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def change_my_password(old_password: str, new_password: str, logout_all_sessions: int = 1) -> dict:
    """Self password change using stock Frappe primitives (PF-08, F-PF7).

    ``frappe.core.doctype.user.user.update_password`` CANNOT be delegated to
    outside an HTTP request (it touches ``frappe.local.login_manager`` and
    fails with the literal "login_manager" ValidationError — E2E P9 caught it
    live). Instead: verify the old password via
    :func:`frappe.utils.password.check_password`, then save through the User
    controller so strength policy + hashing run natively, and finally clear
    sessions exactly like the stock flow.
    """
    user = _current_user()
    old = str(old_password or "")
    new = str(new_password or "")
    if not old or not new:
        frappe.throw(_("Vui lòng nhập đủ mật khẩu hiện tại và mật khẩu mới."))
    if old == new:
        frappe.throw(_("Mật khẩu mới phải khác mật khẩu hiện tại."))

    from frappe.utils.password import check_password

    try:
        check_password(user, old)
    except Exception:
        frappe.throw(_("Mật khẩu hiện tại không đúng."))

    doc = frappe.get_doc(USER_DOCTYPE, user)
    if not doc:
        frappe.throw(_("Tài khoản không tồn tại."))
    doc.new_password = new
    doc.save(ignore_permissions=True)

    if int(logout_all_sessions or 0):
        from frappe.sessions import clear_sessions

        clear_sessions(user=user, keep_current=False, force=True)

    _audit(
        _("Đổi mật khẩu (tự phục vụ)"),
        employee=frappe.db.get_value(EMPLOYEE_DOCTYPE, {"user_id": user, "status": "Active"}),
        reference_doctype=USER_DOCTYPE,
        reference_name=user,
    )
    return {"message": _("Đã đổi mật khẩu thành công.")}


@frappe.whitelist()
def my_sessions() -> dict:
    """List MY live sessions — sid truncated, current one flagged (PF-10)."""
    user = _current_user()
    current_sid = str(getattr(frappe.session, "sid", "") or "")
    try:
        rows = (
            frappe.get_all(
                "Sessions",
                filters={"user": user},
                fields=["sid", "user", "lastupdate", "status"],
                order_by="lastupdate desc",
                limit_page_length=50,
            )
            or []
        )
    except Exception:
        try:
            rows = frappe.get_all("Sessions", filters={"user": user}, limit_page_length=50) or []
        except Exception:
            rows = []

    sessions = [
        {
            "sid": short_sid(r.get("sid")),
            "lastupdate": r.get("lastupdate"),
            "status": r.get("status"),
            "current": bool(current_sid) and str(r.get("sid") or "") == current_sid,
        }
        for r in rows
    ]
    return {"sessions": sessions, "total": len(sessions)}


@frappe.whitelist()
def logout_my_sessions(everywhere: int = 0) -> dict:
    """Logout other devices (default) or EVERY device incl. this one (PF-09).

    ``clear_sessions(user, keep_current, force)`` — the installed Frappe v15
    signature (no ``reason`` kwarg; plan §0 F4 + fix note for user_profile).
    """
    user = _current_user()
    from frappe.sessions import clear_sessions

    keep_current = not int(everywhere or 0)
    clear_sessions(user=user, keep_current=keep_current, force=True)
    _audit(
        _("Đăng xuất thiết bị khác") if keep_current else _("Đăng xuất mọi thiết bị"),
        reference_doctype=USER_DOCTYPE,
        reference_name=user,
    )
    return {
        "message": (
            _("Đã đăng xuất các thiết bị khác.")
            if keep_current
            else _("Đã đăng xuất mọi thiết bị — vui lòng đăng nhập lại.")
        )
    }


# --------------------------------------------------------------------------- #
# Documents (private File attachments — plan §2.1.9, F10)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_documents() -> dict:
    """List Files attached to MY Employee record (can_delete = own upload)."""
    employee = _own_employee()
    user = frappe.session.user
    rows = _safe_rows(
        "File",
        {"attached_to_doctype": EMPLOYEE_DOCTYPE, "attached_to_name": employee},
        ["name", "file_name", "file_url", "is_private", "file_size", "owner", "creation"],
        "creation desc",
        100,
    )
    data = []
    for r in rows or []:
        r = dict(r)
        r["can_delete"] = (r.get("owner") or "") == user
        data.append(r)
    return {"total": len(data), "data": data}


@frappe.whitelist()
def upload_my_document(filename: str, data_url: str) -> dict:
    """Upload a private document onto MY Employee record (PF-11)."""
    employee = _own_employee()
    try:
        mime, payload = decode_data_url(data_url, _DOC_MIME, _DOC_MAX_BYTES)
    except ValueError as e:
        frappe.throw(_(str(e)))
    name = safe_file_name(filename)
    ext = _DOC_MIME[mime]
    if "." not in name.rsplit("/", 1)[-1]:
        name = f"{name}.{ext}"

    file_url = _insert_file(
        filename=name,
        content=payload,
        doctype=EMPLOYEE_DOCTYPE,
        docname=employee,
        is_private=1,
    )
    _audit(_("Tải lên giấy tờ: {0}").format(name), employee=employee, reference_doctype="File")
    _publish(SELF_REALTIME_EVENT, {"employee": employee, "document": name})
    return {"file_name": name, "file_url": file_url}


# --------------------------------------------------------------------------- #
# P1 — Work & salary (read-only, masked) + preferences (plan §2.2)
# --------------------------------------------------------------------------- #
def _row_get(row, key, default=None):
    get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
    return get(key, default)


@frappe.whitelist()
def my_job_history() -> dict:
    """Internal work history + HRMS Transfer/Promotion → merged timeline (PF-15).

    Read-only, self-scoped (mirrors ``get_lifecycle_history`` queries but
    derived from the session — no ``employee`` param to forge).
    """
    employee = _own_employee()

    internal: list[dict] = []
    try:
        doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
        for row in getattr(doc, "internal_work_history", None) or []:
            internal.append(
                {
                    "date": _row_get(row, "from_date"),
                    "branch": _row_get(row, "branch"),
                    "department": _row_get(row, "department"),
                    "designation": _row_get(row, "designation"),
                }
            )
    except Exception:
        frappe.log_error(title="self_profile.my_job_history internal failed")

    transfers = _safe_rows(
        "Employee Transfer",
        {"employee": employee},
        ["name", "transfer_date", "new_department", "new_designation", "new_branch", "docstatus"],
        "transfer_date desc",
        20,
    )
    promotions = _safe_rows(
        "Employee Promotion",
        {"employee": employee},
        ["name", "promotion_date", "new_designation", "new_branch", "docstatus"],
        "promotion_date desc",
        20,
    )

    merged: list[dict] = []
    for r in internal:
        merged.append({"type": "internal", "date": r.get("date"), "text": _history_text(r, "internal")})
    for r in transfers or []:
        merged.append(
            {
                "type": "transfer",
                "date": r.get("transfer_date"),
                "text": _history_text(r, "transfer"),
                "docstatus": r.get("docstatus"),
            }
        )
    for r in promotions or []:
        merged.append(
            {
                "type": "promotion",
                "date": r.get("promotion_date"),
                "text": _history_text(r, "promotion"),
                "docstatus": r.get("docstatus"),
            }
        )
    merged.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    return {
        "internal": internal,
        "transfers": transfers or [],
        "promotions": promotions or [],
        "merged": merged,
    }


def _history_text(row: dict, kind: str) -> str:
    dept = row.get("new_department") or row.get("department")
    desig = row.get("new_designation") or row.get("designation")
    branch = row.get("new_branch") or row.get("branch")
    parts = [p for p in (desig, dept, branch) if p]
    label = {"internal": "Vị trí", "transfer": "Điều chuyển", "promotion": "Thăng chức"}[kind]
    return f"{label}: {' · '.join(parts)}" if parts else label


@frappe.whitelist()
def my_salary_summary() -> dict:
    """My Salary Structure Assignments — NAMES ONLY, amounts masked (PF-16).

    Per can-matrix §2.4 an employee may see WHICH structure is assigned and
    since when — never the base/amount columns.
    """
    employee = _own_employee()
    rows = _safe_rows(
        "Salary Structure Assignment",
        {"employee": employee},
        ["name", "salary_structure", "from_date", "to_date", "company", "docstatus"],
        "from_date desc",
        20,
    )
    today = ""
    try:
        from frappe.utils import today as _today

        today = _today()
    except Exception:
        today = ""

    active = None
    for r in rows or []:
        if r.get("docstatus") == 1 and (not r.get("to_date") or str(r.get("to_date")) >= today):
            active = r
            break
    return {"active": active, "history": rows or [], "masked": True}


@frappe.whitelist()
def update_my_preferences(language: str | None = None, time_zone: str | None = None) -> dict:
    """Set MY User.language / User.time_zone (PF-17). Non-employees too (G14)."""
    user = _current_user()
    tz = _norm(time_zone)
    lang = _norm(language)

    if tz:
        tz_ok = False
        try:
            import pytz

            pytz.timezone(tz)
            tz_ok = True
        except Exception:
            tz_ok = False
        if not tz_ok:
            frappe.throw(_("Múi giờ không hợp lệ."))

    if lang:
        try:
            langs = frappe.get_all("Language", pluck="name") or []
        except Exception:
            langs = []
        if lang not in langs:
            frappe.throw(_("Ngôn ngữ không được hỗ trợ."))

    doc = frappe.get_doc(USER_DOCTYPE, user)
    if not doc:
        frappe.throw(_("Tài khoản không tồn tại."))
    if lang:
        doc.language = lang
    if tz:
        doc.time_zone = tz
    doc.save(ignore_permissions=True)

    _audit(
        _("Cập nhật tuỳ chọn cá nhân (ngôn ngữ/múi giờ)"),
        reference_doctype=USER_DOCTYPE,
        reference_name=user,
    )
    _publish(SELF_REALTIME_EVENT, {"employee": None, "preferences": True})
    return {
        "language": getattr(doc, "language", None),
        "time_zone": getattr(doc, "time_zone", None),
    }


@frappe.whitelist()
def delete_my_document(file_name: str) -> dict:
    """Delete a File I uploaded on MY Employee record (PF-12, IDOR-guarded)."""
    employee = _own_employee()
    user = frappe.session.user
    file_name = str(file_name or "").strip()
    if not file_name:
        frappe.throw(_("Thiếu mã tệp."))
    doc = frappe.get_doc("File", file_name)
    if not doc:
        frappe.throw(_("Tệp không tồn tại."))
    if (getattr(doc, "attached_to_doctype", None) or "") != EMPLOYEE_DOCTYPE or (
        getattr(doc, "attached_to_name", None) or ""
    ) != employee:
        frappe.throw(
            _("Tệp không thuộc hồ sơ của bạn."),
            frappe.PermissionError,
        )
    if (getattr(doc, "owner", None) or "") != user:
        frappe.throw(
            _("Chỉ người tải lên mới được xoá tệp này."),
            frappe.PermissionError,
        )

    frappe.delete_doc("File", file_name, ignore_permissions=True)
    _audit(_("Xoá giấy tờ: {0}").format(getattr(doc, "file_name", None) or file_name), employee=employee)
    _publish(SELF_REALTIME_EVENT, {"employee": employee, "document_deleted": file_name})
    return {"name": file_name, "deleted": True}
