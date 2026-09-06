"""Attendance Policy API — desk-free editor for ``VN Attendance Policy``.

Plan: plans/plan-hr-settings-desk-free.md §2.2 (gap G2). The settings tab
"Chính sách chấm công" previously rendered read-only chips; this module gives
the SPA the full lifecycle while staying backward compatible with the
pre-existing "Phạt đi muộn" tab in HrSalaryStructureView (which calls
``save_policy({name, penalty_rules})`` on the ACTIVE policy — direct editing of
an active policy therefore stays allowed; only a LOCKED policy refuses edits):

* ``list_policies`` / ``get_policy`` — projection + a ``can.*`` action matrix,
  plus an ``is_engine_pick`` flag (the OT engine uses the *most recently
  modified active* policy per ``overtime_settings._active_policy_name``).
* ``save_policy`` — transactional create-or-update with server-side validation
  and a *replace* strategy for the ``penalty_rules`` child table. Accepts the
  legacy kwargs style (``save_policy(name=…, penalty_rules=…)``) and the
  editor style (``save_policy(values={…}, penalty_rules=[…])``).
* ``clone_policy`` — the schema's versioning pattern: a new draft with
  ``version + 1`` and ``cloned_from_policy`` set, inactive + unlocked.
* ``activate_policy`` / ``deactivate_policy`` / ``lock_policy`` — guarded
  transitions (freezing what the engine uses requires deactivating first).
* ``policy_options`` — dropdown options read from the doctype meta.

Every mutation is gated by ``_require_hr_admin`` and audited through
``_audit_admin`` (convention identical to catalog_master / holiday_master).
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import _audit_admin, _default_company, _require_hr_admin

DOCTYPE = "VN Attendance Policy"
CHILD = "VN Attendance Penalty Rule"

PENALTY_TYPES = frozenset({"Fixed Amount", "Per Minute", "Percentage", "Half Day", "Full Day"})

# Parent fields the SPA may write through save_policy. ``is_active`` /
# ``employee_grade`` are meta-guarded (has_field) so older schemas are safe.
# Engine-managed fields (version, is_locked, cloned_from_policy,
# policy_snapshot_json) are only touched by the dedicated endpoints below.
EDITABLE_FIELDS = frozenset(
    {
        "policy_name",
        "company",
        "apply_to",
        "branch",
        "department",
        "employee_grade",
        "is_active",
        "effective_from",
        "effective_to",
        # Grace & thresholds
        "grace_late_minutes",
        "grace_early_leave_minutes",
        "min_working_hours_full_day",
        "min_working_hours_half_day",
        "multiple_logs_strategy",
        "min_overtime_minutes",
        "max_overtime_hours_per_shift",
        "max_total_work_hours_per_shift",
        # OT settings
        "allow_pre_shift_overtime",
        "allow_post_shift_overtime",
        "require_overtime_approval",
        "overtime_rounding_method",
        "overtime_rounding_minutes",
        "allow_ot_compensate_late",
        "allow_ot_compensate_early_leave",
        "max_overtime_hours_per_day",
        "minimum_rest_hours_between_shifts",
        # Night & missing-log
        "night_start_time",
        "night_end_time",
        "missing_checkin_action",
        "missing_checkout_action",
        "auto_mark_absent",
    }
)

PENALTY_FIELDS = [
    "from_minutes",
    "to_minutes",
    "penalty_type",
    "penalty_value",
    "salary_component",
]

_LIST_FIELDS = [
    "name",
    "policy_name",
    "company",
    "apply_to",
    "branch",
    "department",
    "is_active",
    "is_locked",
    "version",
    "grace_late_minutes",
    "effective_from",
    "effective_to",
    "modified",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _num(value, cast=float, default=None):
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return default


def _get_policy(name: str):
    name = (name or "").strip()
    if not name or not frappe.db.exists(DOCTYPE, name):
        frappe.throw(_('Chính sách chấm công "{0}" không tồn tại.').format(name))
    return frappe.get_doc(DOCTYPE, name)


def _penalty_rows(doc) -> list[dict]:
    return [
        {f: row.get(f) for f in PENALTY_FIELDS} | {"name": getattr(row, "name", "")}
        for row in (doc.get("penalty_rules") or [])
    ]


def _clean_penalty_rules(rows) -> list[dict]:
    """Normalise + validate the SPA ``penalty_rules`` payload (plan §2.2)."""
    if isinstance(rows, str):
        try:
            import json

            rows = json.loads(rows)
        except Exception:
            frappe.throw(_("Danh sách quy tắc phạt không hợp lệ."))
    if not isinstance(rows, list):
        frappe.throw(_("Danh sách quy tắc phạt không hợp lệ."))
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if all((row.get(k) in (None, "")) for k in ("from_minutes", "to_minutes", "penalty_value")):
            continue  # blank row — drop
        frm = _num(row.get("from_minutes"), int)
        to = _num(row.get("to_minutes"), int)
        ptype = (row.get("penalty_type") or "").strip()
        value = _num(row.get("penalty_value"))
        if frm is None:
            frappe.throw(_("Quy tắc phạt thiếu “từ phút”."))
        if ptype not in PENALTY_TYPES:
            frappe.throw(_("Loại phạt “{0}” không hợp lệ.").format(ptype))
        if value is None or value < 0:
            frappe.throw(_("Mức phạt phải ≥ 0."))
        if to is not None and to <= frm:
            frappe.throw(_("Khoảng phút phạt không hợp lệ: “đến” phải lớn hơn “từ”."))
        clean.append(
            {
                "from_minutes": frm,
                "to_minutes": to,
                "penalty_type": ptype,
                "penalty_value": value,
                "salary_component": (row.get("salary_component") or "").strip() or None,
            }
        )
    return clean


def _validate_policy(doc) -> None:
    """Cross-field validation shared by create + update (plan §2.2)."""
    half = _num(doc.get("min_working_hours_half_day"))
    full = _num(doc.get("min_working_hours_full_day"))
    if half is not None and full is not None and half > full:
        frappe.throw(_("Số giờ tối thiểu cho nửa ngày không được lớn hơn cho nguyên ngày."))

    apply_to = (doc.get("apply_to") or "All").strip()
    if apply_to == "Branch" and not doc.get("branch"):
        frappe.throw(_("Chính sách áp dụng theo Chi nhánh phải chọn chi nhánh."))
    if apply_to == "Department" and not doc.get("department"):
        frappe.throw(_("Chính sách áp dụng theo Phòng ban phải chọn phòng ban."))

    rounding = (doc.get("overtime_rounding_method") or "No Rounding").strip()
    minutes = _num(doc.get("overtime_rounding_minutes"), int)
    if rounding != "No Rounding" and minutes not in (15, 30, 60):
        frappe.throw(_("Số phút làm tròn OT phải là 15, 30 hoặc 60."))

    if doc.get("effective_from") and doc.get("effective_to") and doc.effective_to < doc.effective_from:
        frappe.throw(_("Ngày hiệu lực đến phải sau ngày hiệu lực từ."))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_policies(q: str = "", company: str | None = None, limit: int = 200) -> list[dict]:
    """Policy projection for the settings list, newest-modified first.

    ``is_engine_pick`` marks the most-recently-modified active policy per
    company — the one the OT engine (``overtime_settings``) will actually use.
    """
    _require_hr_admin()
    filters = []
    q = (q or "").strip()
    if q:
        filters.append(["policy_name", "like", f"%{q}%"])
    company = (company or "").strip()
    if company:
        filters.append(["company", "=", company])
    rows = frappe.get_all(
        DOCTYPE,
        fields=_LIST_FIELDS,
        filters=filters or None,
        order_by="modified desc",
        limit_page_length=int(limit or 200),
    )
    seen_companies: set[str] = set()
    for row in rows:
        row["is_engine_pick"] = 0
        if row.get("is_active") and row.get("company") not in seen_companies:
            row["is_engine_pick"] = 1  # first active row = newest modified
            seen_companies.add(row["company"])
    return rows


@frappe.whitelist()
def get_policy(name: str) -> dict:
    """One policy (all fields + clean penalty rows) + the ``can`` matrix."""
    _require_hr_admin()
    doc = _get_policy(name)
    out = doc.as_dict()
    out["penalty_rules"] = _penalty_rows(doc)
    out["can"] = {
        # Active policies stay directly editable (the "Phạt đi muộn" tab of
        # HrSalaryStructureView relies on it) — only a locked one refuses.
        "edit": not doc.get("is_locked"),
        "activate": not doc.get("is_active"),
        "deactivate": bool(doc.get("is_active")),
        "lock": not doc.get("is_locked") and not doc.get("is_active"),
        "unlock": bool(doc.get("is_locked")) and not doc.get("is_active"),
        "clone": True,
        "delete": not doc.get("is_active"),
    }
    return out


@frappe.whitelist()
def save_policy(values: dict | None = None, penalty_rules=None, **kwargs) -> dict:
    """Create or update one policy + its penalty rules in one transaction.

    Accepts both call styles:
    * editor — ``save_policy(values={…}, penalty_rules=[…])``;
    * legacy kwargs — ``save_policy(name=…, penalty_rules=[…], **fields)``
      (HrSalaryStructureView's late-penalty tab).

    A LOCKED policy can never be edited here (clone instead). ``penalty_rules``
    may be ``None`` (keep the existing child rows) — the legacy call always
    passes a list, which replaces the table (replace strategy).
    """
    _require_hr_admin()
    payload: dict = dict(values or {}) or dict(kwargs or {})
    if not isinstance(payload, dict):
        payload = {}

    rules = penalty_rules if penalty_rules is not None else payload.get("penalty_rules")
    payload = {k: v for k, v in payload.items() if k in EDITABLE_FIELDS and k != "penalty_rules"}
    name = ""
    # The legacy style passes `name` as a kwarg (not part of EDITABLE_FIELDS).
    values_dict = values if isinstance(values, dict) else {}
    name = (values_dict.get("name") or kwargs.get("name") or "").strip()

    created = not name
    if created:
        doc = frappe.new_doc(DOCTYPE)
    else:
        doc = _get_policy(name)
        if doc.get("is_locked"):
            frappe.throw(_("Chính sách đã khoá. Hãy Nhân bản thành bản mới (version+1) để sửa."))
        payload.pop("policy_name", None)  # autoname field — never rename on edit

    if created and not (payload.get("policy_name") or "").strip():
        frappe.throw(_("Tên chính sách là bắt buộc."))
    if not (payload.get("company") or (not created and doc.get("company"))):
        payload["company"] = _default_company()

    for key, val in payload.items():
        if doc.meta.has_field(key):
            doc.set(key, val)
    _validate_policy(doc)

    if rules is not None:
        doc.set("penalty_rules", [])
        for row in _clean_penalty_rules(rules):
            child = doc.append("penalty_rules", {})
            for f, v in row.items():
                child.set(f, v)

    if created:
        doc.insert(ignore_permissions=True)
        action = _("Tạo")
    else:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
        action = _("Cập nhật")

    _audit_admin(
        _("{0} chính sách chấm công: {1}").format(action, doc.get("policy_name")),
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        company=doc.get("company") or _default_company(),
        new_value={"is_new": created, "rules": len(doc.get("penalty_rules") or [])},
    )
    frappe.db.commit()
    return {
        "name": doc.name,
        "policy_name": doc.get("policy_name"),
        "version": doc.get("version"),
        "is_active": doc.get("is_active"),
        "is_locked": doc.get("is_locked"),
    }


@frappe.whitelist()
def activate_policy(name: str, deactivate_others: int = 0) -> dict:
    """Mark a policy active. Multiple active policies are legal (``apply_to``
    scoping) but the caller gets ``warnings`` listing the others of the same
    company; ``deactivate_others=1`` turns them off first (each audited)."""
    _require_hr_admin()
    doc = _get_policy(name)
    others = frappe.get_all(
        DOCTYPE,
        filters={"is_active": 1, "company": doc.get("company"), "name": ["!=", doc.name]},
        fields=["name"],
    )
    if int(deactivate_others or 0) and others:
        for row in others:
            other = frappe.get_doc(DOCTYPE, row.name)
            other.set("is_active", 0)
            other.flags.ignore_permissions = True
            other.save(ignore_permissions=True)
            _audit_admin(
                _('Ngừng kích hoạt chính sách "{0}"').format(row.name),
                reference_doctype=DOCTYPE,
                reference_name=row.name,
                company=doc.get("company") or _default_company(),
            )
        others = []

    doc.set("is_active", 1)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _audit_admin(
        _('Kích hoạt chính sách "{0}"').format(doc.name),
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        company=doc.get("company") or _default_company(),
    )
    frappe.db.commit()
    return {
        "name": doc.name,
        "is_active": 1,
        "warnings": [row.name for row in others],
    }


@frappe.whitelist()
def deactivate_policy(name: str) -> dict:
    """Turn a policy inactive (the direct counterpart of activate_policy)."""
    _require_hr_admin()
    doc = _get_policy(name)
    doc.set("is_active", 0)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    _audit_admin(
        _('Ngừng kích hoạt chính sách "{0}"').format(doc.name),
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        company=doc.get("company") or _default_company(),
    )
    frappe.db.commit()
    return {"name": doc.name, "is_active": 0}


@frappe.whitelist()
def clone_policy(name: str, new_policy_name: str = "") -> dict:
    """Copy a policy into a fresh inactive + unlocked draft with version+1.

    This is the schema's effective-dating mechanism (``cloned_from_policy`` /
    ``version``) — editing a locked policy always goes through here.
    """
    _require_hr_admin()
    doc = _get_policy(name)
    new_name = (new_policy_name or "").strip() or f"{doc.get('policy_name')} v{(doc.get('version') or 1) + 1}"

    new = frappe.copy_doc(doc)
    new.set("policy_name", new_name)
    new.set("version", (doc.get("version") or 1) + 1)
    new.set("cloned_from_policy", doc.name)
    new.set("is_active", 0)
    new.set("is_locked", 0)
    new.insert(ignore_permissions=True)

    _audit_admin(
        _('Nhân bản chính sách "{0}" → "{1}"').format(doc.name, new.name),
        reference_doctype=DOCTYPE,
        reference_name=new.name,
        company=new.get("company") or _default_company(),
        new_value={"cloned_from_policy": doc.name, "version": new.get("version")},
    )
    frappe.db.commit()
    return {"name": new.name, "version": new.get("version"), "cloned_from_policy": doc.name}


@frappe.whitelist()
def lock_policy(name: str, locked: int = 1) -> dict:
    """Freeze (or unfreeze) a policy. An active policy must be deactivated
    first — freezing what the engine is using would be a silent behaviour
    change nobody asked for."""
    _require_hr_admin()
    doc = _get_policy(name)
    target = 1 if int(locked or 0) else 0
    if target and doc.get("is_active"):
        frappe.throw(_("Ngừng kích hoạt chính sách trước khi khoá."))
    doc.set("is_locked", target)
    doc.flags.ignore_permissions = True
    doc.save(ignore_permissions=True)
    action = _('Khoá chính sách "{0}"') if target else _('Mở khoá chính sách "{0}"')
    _audit_admin(
        action.format(doc.name),
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
        company=doc.get("company") or _default_company(),
    )
    frappe.db.commit()
    return {"name": doc.name, "is_locked": target}


@frappe.whitelist()
def delete_policy(name: str) -> dict:
    """Permanently remove a policy — refused while active (deactivate first).

    Returns the legacy ``{"deleted": bool, "name"}`` shape.
    """
    _require_hr_admin()
    if not frappe.db.exists(DOCTYPE, name):
        return {"deleted": False, "message": "Không tồn tại."}
    doc = _get_policy(name)
    if doc.get("is_active"):
        frappe.throw(_("Chính sách đang áp dụng — ngừng kích hoạt trước khi xoá."))
    try:
        frappe.delete_doc(DOCTYPE, name, ignore_permissions=True)
    except frappe.LinkExistsError:
        frappe.throw(_("Chính sách đang được tham chiếu (phiên chấm công / kỳ lương), không xoá được."))
    _audit_admin(
        _('Xoá chính sách "{0}"').format(name),
        reference_doctype=DOCTYPE,
        reference_name=name,
        company=doc.get("company") or _default_company(),
    )
    frappe.db.commit()
    return {"deleted": True, "name": name}


@frappe.whitelist()
def policy_options() -> dict:
    """Dropdown/select options for the editor (read from the meta)."""
    _require_hr_admin()
    meta = frappe.get_meta(DOCTYPE)

    def _opts(fieldname):
        f = meta.get_field(fieldname)
        return [o.strip() for o in (f.options or "").split("\n") if o.strip()] if f else []

    return {
        "companies": [r.name for r in frappe.db.get_all("Company", ["name"])],
        "apply_to_options": _opts("apply_to"),
        "multiple_logs_strategy_options": _opts("multiple_logs_strategy"),
        "overtime_rounding_method_options": _opts("overtime_rounding_method"),
        "missing_checkin_action_options": _opts("missing_checkin_action"),
        "missing_checkout_action_options": _opts("missing_checkout_action"),
        "penalty_type_options": _opts("penalty_type") or sorted(PENALTY_TYPES),
    }
