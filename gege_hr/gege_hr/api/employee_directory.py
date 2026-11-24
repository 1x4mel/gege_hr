"""Danh bạ nhân viên rút gọn — cho HR thực tập ("HR Intern") (24/11/2026).

HR Intern KHÔNG có quyền đọc ``Employee`` (hồ sơ chứa lương / ngân hàng / CCCD /
địa chỉ / SĐT cá nhân) và vẫn giữ User Permission "Employee = chính mình". Trang
``/hr/directory`` đọc qua endpoint này: chỉ các cột công việc trong
:data:`DIRECTORY_FIELDS`, đọc bằng ``frappe.get_all`` (đã gate role ở đầu hàm).
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import pagination
from gege_hr.gege_hr.utils.approval import HR_INTERN_ROLE

DIRECTORY_ROLES = ["HR Manager", "HR User", "System Manager", HR_INTERN_ROLE]

# Chỉ thông tin công việc. KHÔNG thêm: ctc / salary_* / bank_* / iban /
# passport / CCCD / date_of_birth / gender / cell_number / personal_email /
# địa chỉ / payroll_group (test_employee_directory khoá danh sách cấm).
DIRECTORY_FIELDS = [
    "name",
    "employee_name",
    "vn_employee_code",
    "status",
    "department",
    "branch",
    "designation",
    "employment_type",
    "date_of_joining",
    "reports_to",
    "company_email",
    "user_id",
]

_SEARCH_FIELDS = [
    "employee_name",
    "vn_employee_code",
    "department",
    "designation",
    "company_email",
    "user_id",
]
_EXACT_FILTERS = ["status", "department", "branch", "designation"]


def _safe_fields(fields: list[str]) -> list[str]:
    """Bỏ cột chưa có trên bench (custom field chưa migrate)."""
    try:
        meta = frappe.get_meta("Employee")
        return [f for f in fields if f == "name" or meta.has_field(f)]
    except Exception:
        return ["name", "employee_name"]


@frappe.whitelist()
def list_directory(
    search: str | None = None,
    status: str | None = "Active",
    department: str | None = None,
    branch: str | None = None,
    designation: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """``{"data": [...], "total": int, "departments": [...]}`` — chỉ đọc."""
    frappe.only_for(DIRECTORY_ROLES)
    fields = _safe_fields(DIRECTORY_FIELDS)
    valid = set(fields)
    filters: list = []
    exact = {"status": status, "department": department, "branch": branch, "designation": designation}
    for field in _EXACT_FILTERS:
        val = exact.get(field)
        if val not in (None, "") and field in valid:
            filters.append([field, "=", val])
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [[c, "like", like] for c in _SEARCH_FIELDS if c in valid] or None

    page = max(1, pagination.as_int(page, 1))
    page_size = pagination.clamp_limit(page_size, default=50)
    total = pagination.count_all("Employee", filters=filters or None, or_filters=or_filters)
    rows = (
        frappe.get_all(
            "Employee",
            fields=fields,
            filters=filters or None,
            or_filters=or_filters,
            order_by="employee_name asc",
            limit_start=(page - 1) * page_size,
            limit_page_length=page_size,
        )
        or []
    )
    # Tên người quản lý trực tiếp (reports_to là mã nhân viên).
    managers = {r.get("reports_to") for r in rows if r.get("reports_to")}
    names = (
        dict(
            frappe.get_all(
                "Employee",
                filters={"name": ["in", list(managers)]},
                fields=["name", "employee_name"],
                as_list=1,
            )
        )
        if managers
        else {}
    )
    for r in rows:
        r["reports_to_name"] = names.get(r.get("reports_to")) or ""
    departments = sorted(
        {d for d in frappe.get_all("Employee", filters={"status": "Active"}, pluck="department") if d}
    )
    return {"data": rows, "total": total, "departments": departments}
