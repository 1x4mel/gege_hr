"""
Holiday List Master API — Zero-Frappe P2 remainder (plan §4.5, gap G7-bis).

Lets the HR Manager build and maintain Holiday Lists — including the ``Holiday``
child table that carries each festive / weekly-off date — entirely from inside
the HR app, without ever touching Frappe Desk.

Holiday List carries a child table (``holidays``) and therefore gets dedicated
RPC endpoints here rather than the generic ``frappe.client.*`` path used for
simple masters (P1), mirroring ``api/payroll_master.py``.

Permission + audit conventions are identical to ``api/admin.py``: every call is
gated by ``frappe.only_for(HR_ADMIN_ROLES)`` and emits a ``VN Audit Event``
through ``admin._audit_admin`` (audit_type ``"Manual Override"``).
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api.admin import (
    _audit_admin,
    _default_company,
    _require_hr_admin,
)

# Holiday child-row keys we recognise from the frontend payload.
_HOLIDAY_KEYS = (
    "holiday_date",
    "description",
    "weekly_off",
)


def _coerce_bool(v):
    """Accept JS-style booleans (true/false) or 0/1 from the SPA payload."""
    if isinstance(v, bool):
        return v
    if v in (1, "1", "true", "True", "Yes", "yes"):
        return True
    return False


def _clean_holidays(rows):
    """Normalise the ``holidays`` child-table payload from the SPA.

    Drops blank rows and Frappe meta keys; coerces ``weekly_off`` to a real
    boolean and normalises the ``holiday_date`` to a ``YYYY-MM-DD`` string so
    the backend never stores a JS-style date where Frappe expects a Date.
    """
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        date = row.get("holiday_date")
        if isinstance(date, str):
            date = date.strip()
        if not date:
            continue
        # Normalise JS Date object → ISO string if the SPA sent one.
        if hasattr(date, "isoformat"):
            date = date.isoformat()[:10]
        else:
            date = str(date)[:10]
        desc = (row.get("description") or "").strip()
        clean = {k: row[k] for k in _HOLIDAY_KEYS if k in row}
        clean["holiday_date"] = date
        clean["description"] = desc
        clean["weekly_off"] = _coerce_bool(clean.get("weekly_off"))
        out.append(clean)
    return out


@frappe.whitelist()
def list_holiday_lists(limit: int = 200) -> list[dict]:
    """Return Holiday Lists (projection for the admin table)."""
    _require_hr_admin()
    rows = frappe.get_all(
        "Holiday List",
        fields=["name", "from_date", "to_date"],
        limit_page_length=limit,
        order_by="name asc",
    )
    return rows


@frappe.whitelist()
def get_holiday_list(name: str) -> dict:
    """Return a full Holiday List including its ``holidays`` child rows."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Holiday List", name):
        frappe.throw(_("Danh sách ngày lễ không tồn tại."))
    doc = frappe.get_doc("Holiday List", name)
    return {
        "name": doc.name,
        "holiday_list_name": doc.holiday_list_name,
        "from_date": doc.from_date,
        "to_date": doc.to_date,
        "holidays": [
            {k: r.get(k) for k in _HOLIDAY_KEYS if r.get(k) is not None} for r in (doc.holidays or [])
        ],
    }


@frappe.whitelist()
def save_holiday_list(
    name: str = "",
    holiday_list_name: str = "",
    from_date: str = "",
    to_date: str = "",
    holidays: list | None = None,
) -> dict:
    """Create or update a Holiday List (Draft — kept editable).

    The list is intentionally left ``docstatus=0`` so HR can revise the
    festive dates freely. Leave allocations reference it by name and are not
    affected by later edits.
    """
    _require_hr_admin()
    label = (holiday_list_name or "").strip()
    if not label:
        frappe.throw(_("Tên danh sách ngày lễ là bắt buộc."))

    holidays = _clean_holidays(holidays)
    if not holidays:
        frappe.throw(_("Danh sách ngày lễ phải có ít nhất một ngày."))

    payload = {
        "doctype": "Holiday List",
        "holiday_list_name": label,
        "from_date": from_date or "",
        "to_date": to_date or "",
        "holidays": holidays,
    }

    name = (name or "").strip()
    is_new = not name
    if is_new:
        doc = frappe.get_doc(payload)
        doc.insert()
        ref = doc.name
    else:
        if not frappe.db.exists("Holiday List", name):
            frappe.throw(_("Danh sách ngày lễ không tồn tại."))
        doc = frappe.get_doc("Holiday List", name)
        doc.holiday_list_name = label
        doc.from_date = from_date or ""
        doc.to_date = to_date or ""
        doc.set("holidays", holidays)
        doc.save()
        ref = doc.name

    _audit_admin(
        _("Cập nhật danh sách ngày lễ {0}").format(ref),
        reference_doctype="Holiday List",
        reference_name=ref,
        company=_default_company(),
        new_value={
            "holidays": len(holidays),
        },
    )
    return {"name": ref}


@frappe.whitelist()
def delete_holiday_list(name: str) -> dict:
    """Delete a Holiday List (best-effort; never throws on missing rows)."""
    _require_hr_admin()
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Tên danh sách ngày lễ là bắt buộc."))
    if not frappe.db.exists("Holiday List", name):
        frappe.throw(_("Danh sách ngày lễ không tồn tại."))
    frappe.delete_doc("Holiday List", name, force=True)

    _audit_admin(
        _("Xoá danh sách ngày lễ {0}").format(name),
        reference_doctype="Holiday List",
        reference_name=name,
        company=_default_company(),
    )
    return {"name": name, "deleted": True}
