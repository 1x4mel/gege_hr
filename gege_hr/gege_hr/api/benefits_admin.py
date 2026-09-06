"""NEW-6 (hr-gap-audit 🟥) — Benefits + Gratuity + Promotion API.

Reuses Frappe HR's ``Employee Benefit Application``, ``Employee Benefit Claim``,
``Gratuity``, ``Employee Promotion`` DocTypes. Mirrors ``api/expense.py``.

plan-benefits-desk-free (P1): chuẩn hoá Benefit Application theo đúng HRMS —
``benefit_context`` (hạn mức tự tính + component eligible + kỳ lương active),
``save_benefit_application`` (rows ``employee_benefits`` bắt buộc), lifecycle
``set_benefit_application_action`` (submit/cancel/amend — Employee soạn draft,
HR duyệt, đúng native permission hrms). Endpoint cũ ``submit_benefit_application``
đã XOÁ: nó không set ``payroll_period`` + ``employee_benefits`` (field reqd)
nên không thể tạo doc hợp lệ — plan §2.2.

Lưu ý schema: ``Employee Benefit Application`` / ``Employee Promotion`` không có
cột ``status`` — chỉ có ``docstatus``; list endpoints map
``docstatus → Draft/Submitted/Cancelled`` và filter status qua docstatus.
"""

from __future__ import annotations

import json

import frappe

from gege_hr.gege_hr.utils import pagination

BENEFIT_DOCTYPE = "Employee Benefit Application"
CLAIM_DOCTYPE = "Employee Benefit Claim"
GRATUITY_DOCTYPE = "Gratuity"
GRATUITY_RULE_DOCTYPE = "Gratuity Rule"
PROMOTION_DOCTYPE = "Employee Promotion"

_BENEFIT_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "date",
    "payroll_period",
    "max_benefits",
    "total_amount",
    "docstatus",
]
_GRATUITY_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "gratuity_rule",
    "amount",
    "status",
    "posting_date",
    "pay_via_salary_slip",
]
_PROMOTION_FIELDS = ["name", "employee", "employee_name", "promotion_date", "docstatus"]

_DOCSTATUS_LABELS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
_STATUS_TO_DOCSTATUS = {"Draft": 0, "Submitted": 1, "Cancelled": 2}

_HRMS_REMAINING = (
    "hrms.payroll.doctype.employee_benefit_application."
    "employee_benefit_application.get_max_benefits_remaining"
)

# Thông báo validate tiếng Anh của HRMS → tiếng Việt ngắn (plan §3.1 `_friendly`).
_FRIENDLY_MAP = [
    ("already submitted an application", "Nhân viên đã có bản đăng ký phúc lợi trong kỳ lương này."),
    ("cannot apply for benefits", "Theo cấu trúc lương hiện tại, nhân viên chưa thể đăng ký phúc lợi."),
    ("maximum benefit amount of component", "Vượt hạn mức tối đa của khoản phúc lợi."),
    ("maximum benefit amount of employee", "Tổng đăng ký vượt hạn mức phúc lợi của nhân viên."),
    ("maximum benefit of employee", "Tổng đăng ký vượt hạn mức phúc lợi của nhân viên."),
    ("not in a valid payroll period", "Ngày không nằm trong kỳ lương (Payroll Period) hợp lệ."),
    (
        "payroll date can not be greater",
        "Ngày chi trả (payroll date) không được sau ngày nghỉ việc của nhân viên.",
    ),
    ("please set relieving date", "Nhân viên chưa có ngày nghỉ việc (Relieving Date)."),
    ("mandatory value missing", "Thiếu thông tin bắt buộc."),
    ("should be in the application as pro-rata component", "Phần còn lại phải được phân bổ vào component pro-rata."),
]


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if employee != own:
        frappe.throw("Bạn chỉ xem được của chính mình.")


def _friendly(err) -> str:
    """Map thông báo lỗi (HRMS tiếng Anh) sang tiếng Việt; không map được thì
    trả nguyên văn để không nuốt thông tin."""
    msg = str(err or "").strip()
    low = msg.lower()
    for key, vn in _FRIENDLY_MAP:
        if key in low:
            return vn
    return msg or "Không thao tác được. Vui lòng thử lại."


def _flag(value) -> int:
    return 1 if str(value) in ("1", "True", "true") else 0


def _rowval(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _decorate_docstatus(rows) -> list:
    """Gắn nhãn ``status`` từ ``docstatus`` cho các doctype không có cột status."""
    for r in rows or []:
        try:
            ds = int(r.get("docstatus") or 0)
        except (TypeError, ValueError):
            ds = 0
        r["status"] = _DOCSTATUS_LABELS.get(ds, "Draft")
    return rows or []


def _list(
    doctype,
    fields,
    filters,
    status,
    search,
    page,
    page_size,
    date_field=None,
    date_from=None,
    date_to=None,
    amount_field=None,
    amount_min=None,
    amount_max=None,
    numeric_fields=None,
) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    # Date range — two list conditions (NOT "between" — DNA §6.6 B).
    if date_field:
        if date_from:
            flt.append([date_field, ">=", date_from])
        if date_to:
            flt.append([date_field, "<=", date_to])
    # Numeric range — two list conditions on the same field (DNA §6.6 B).
    if amount_field:
        if amount_min not in (None, ""):
            try:
                flt.append([amount_field, ">=", float(amount_min)])
            except (TypeError, ValueError):
                pass
        if amount_max not in (None, ""):
            try:
                flt.append([amount_field, "<=", float(amount_max)])
            except (TypeError, ValueError):
                pass
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        of = [["employee_name", "like", like], ["name", "like", like]]
        # Numeric columns also join the broad search (DNA §6.6 A — typing a
        # number must match max_benefits / amount columns too).
        for nf in numeric_fields or []:
            of.append([nf, "like", like])
        or_filters = of
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=fields,
                order_by="creation desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"benefits_admin.list {doctype} failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


# ── Shared domain helpers (plan §3.1) ────────────────────────────────────────


def _assigned_structure(employee, on_date):
    """Salary Structure assigned tại ``on_date`` (SAL assignment đã submit,
    from_date gần nhất) — tương đương ``get_assigned_salary_structure`` của
    HRMS nhưng query trực tiếp để chạy được bench-free."""
    if not employee or not on_date:
        return None
    rows = (
        frappe.get_all(
            "Salary Structure Assignment",
            filters=[["employee", "=", employee], ["from_date", "<=", on_date], ["docstatus", "=", 1]],
            fields=["salary_structure"],
            order_by="from_date desc",
            limit_page_length=1,
        )
        or []
    )
    return rows[0].get("salary_structure") if rows else None


def _active_payroll_period(company=None):
    today = frappe.utils.today()
    flt = [["start_date", "<=", today], ["end_date", ">=", today]]
    if company:
        flt.append(["company", "=", company])
    rows = (
        frappe.get_all(
            "Payroll Period",
            filters=flt,
            fields=["name", "start_date", "end_date"],
            order_by="start_date desc",
            limit_page_length=1,
        )
        or []
    )
    return rows[0] if rows else None


def _component_flags(component: str):
    cap = frappe.db.get_value("Salary Component", component, "max_benefit_amount") or 0
    claim = _flag(frappe.db.get_value("Salary Component", component, "pay_against_benefit_claim"))
    return float(cap or 0), claim


def _benefit_components(structure):
    """Earnings flexi của structure assigned — nguồn options cho rows editor."""
    if not structure:
        return []
    try:
        rows = (
            frappe.get_all(
                "Salary Detail",
                filters=[["parent", "=", structure], ["parentfield", "=", "earnings"], ["is_flexible_benefit", "=", 1]],
                fields=["salary_component"],
                limit_page_length=50,
            )
            or []
        )
    except Exception:
        rows = []
    seen: dict = {}
    for r in rows:
        comp = r.get("salary_component")
        if not comp or comp in seen:
            continue
        cap, claim = _component_flags(comp)
        depends = _flag(frappe.db.get_value("Salary Component", comp, "depends_on_payment_days"))
        seen[comp] = {
            "name": comp,
            "max_benefit_amount": cap,
            "pay_against_benefit_claim": claim,
            "depends_on_payment_days": depends,
        }
    return list(seen.values())


def _existing_application(employee, period):
    """Bản application mới nhất của employee trong kỳ (draft/submitted ưu
    tiên; fallback cancelled để bật nút Amend)."""
    if not period:
        return None
    base = [["employee", "=", employee], ["payroll_period", "=", period.get("name")]]
    rows = (
        frappe.get_all(
            BENEFIT_DOCTYPE,
            filters=base + [["docstatus", "!=", 2]],
            fields=["name", "docstatus"],
            order_by="creation desc",
            limit_page_length=1,
        )
        or []
    )
    if not rows:
        rows = (
            frappe.get_all(
                BENEFIT_DOCTYPE,
                filters=base + [["docstatus", "=", 2]],
                fields=["name", "docstatus"],
                order_by="creation desc",
                limit_page_length=1,
            )
            or []
        )
    if not rows:
        return None
    return {"name": rows[0].get("name"), "docstatus": int(rows[0].get("docstatus") or 0)}


def _doc_can(doc) -> dict:
    """Ma trận action theo docstatus + role (plan §3.1 `_doc_can`)."""
    try:
        ds = int(getattr(doc, "docstatus", 0) or 0)
    except (TypeError, ValueError):
        ds = 0
    mgr = _is_manager()
    return {
        "edit": ds == 0,
        "submit": ds == 0 and mgr,
        "cancel": ds == 1 and mgr,
        "amend": ds == 2 and mgr,
    }


def _doc_action(doctype, name, action, allow_amend=True):
    """Dispatcher submit/cancel/amend — HR-only, controller HRMS tự validate
    (mandatory/cap/duplicate), lỗi được map tiếng Việt qua ``_friendly``."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager thực hiện thao tác này.")
    if action not in ("submit", "cancel", "amend"):
        frappe.throw("Thao tác không hợp lệ.")
    doc = frappe.get_doc(doctype, name)
    try:
        if action == "submit":
            if int(getattr(doc, "docstatus", 0) or 0) != 0:
                frappe.throw("Chỉ duyệt được bản nháp (Draft).")
            doc.submit()
        elif action == "cancel":
            if int(getattr(doc, "docstatus", 0) or 0) != 1:
                frappe.throw("Chỉ huỷ được bản đã duyệt (Submitted).")
            doc.cancel()
        else:
            if not allow_amend:
                frappe.throw("Thao tác không hỗ trợ cho loại tài liệu này.")
            if int(getattr(doc, "docstatus", 0) or 0) != 2:
                frappe.throw("Chỉ tạo lại từ bản đã huỷ (Cancelled).")
            new_doc = frappe.copy_doc(doc)
            new_doc.amended_from = doc.name
            new_doc.insert()
            doc = new_doc
    except Exception as e:
        msg = str(e)
        if msg:
            frappe.throw(_friendly(msg))
        raise
    return doc


def _payload_dict(payload):
    """Whitelisted dict arg: Frappe tự parse JSON chuỗi; chấp nhận cả dict."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


# ── Benefits: list (self-service + HR) ───────────────────────────────────────


@frappe.whitelist()
def my_benefit_applications(employee=None, status=None, search=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    flt = [["employee", "=", emp]]
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt.append(["docstatus", "=", ds])
    res = _list(BENEFIT_DOCTYPE, _BENEFIT_FIELDS, flt, None, search, page, page_size)
    _decorate_docstatus(res.get("data"))
    return res


@frappe.whitelist()
def all_benefit_applications(status=None, search=None, date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    flt = None
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt = [["docstatus", "=", ds]]
    res = _list(
        BENEFIT_DOCTYPE,
        _BENEFIT_FIELDS,
        flt,
        None,
        search,
        page,
        page_size,
        date_field="date",
        date_from=date_from,
        date_to=date_to,
        numeric_fields=["max_benefits", "total_amount"],
    )
    _decorate_docstatus(res.get("data"))
    return res


# ── Benefits: context + save/get/action (plan §2.1–2.2) ──────────────────────


@frappe.whitelist()
def benefit_context(employee=None):
    """Budget + component eligible + kỳ lương active cho form đăng ký.

    Self-service: nhân viên xem của mình; HR xem bất kỳ (truyền employee).
    ``remaining`` bọc ``get_max_benefits_remaining`` của HRMS qua dotted-path
    ``frappe.call`` (bench-free friendly, production dùng thật).
    """
    emp = _resolve(employee)
    _assert_own(emp)
    today = frappe.utils.today()
    company = frappe.db.get_value("Employee", emp, "company")
    structure = _assigned_structure(emp, today)
    period = _active_payroll_period(company)
    components = _benefit_components(structure)

    max_benefits = 0.0
    if structure:
        try:
            max_benefits = float(frappe.db.get_value("Salary Structure", structure, "max_benefits") or 0)
        except (TypeError, ValueError):
            max_benefits = 0.0

    remaining = max_benefits
    if max_benefits and period:
        try:
            remaining = float(
                frappe.call(
                    _HRMS_REMAINING,
                    employee=emp,
                    on_date=today,
                    payroll_period=period.get("name"),
                )
                or 0
            )
        except Exception:
            remaining = max_benefits

    existing = _existing_application(emp, period)
    can = {
        "create": (not existing or existing["docstatus"] == 2) and bool(period) and max_benefits > 0,
        "edit": bool(existing and existing["docstatus"] == 0),
        "submit": bool(existing and existing["docstatus"] == 0 and _is_manager()),
        "cancel": bool(existing and existing["docstatus"] == 1 and _is_manager()),
        "amend": bool(existing and existing["docstatus"] == 2 and _is_manager()),
    }
    return {
        "employee": emp,
        "date": today,
        "salary_structure": structure,
        "payroll_period": period,
        "components": components,
        "max_benefits": max_benefits,
        "remaining": remaining,
        "existing_application": existing,
        "can": can,
        "is_manager": _is_manager(),
    }


@frappe.whitelist()
def save_benefit_application(payload=None):
    """Tạo/cập nhật draft Employee Benefit Application với rows bắt buộc.

    Validate nghiệp vụ (cap, duplicate kỳ) do controller HRMS lo khi
    insert/save; endpoint chỉ normalize payload + guard tiếng Việt.
    """
    payload = _payload_dict(payload)
    employee = (payload.get("employee") or "").strip()
    if not employee:
        frappe.throw("Cần nhân viên.")
    _assert_own(employee)
    payroll_period = (payload.get("payroll_period") or "").strip()
    if not payroll_period:
        frappe.throw("Cần chọn kỳ lương (Payroll Period).")
    benefits = payload.get("benefits") or []
    if not isinstance(benefits, list) or not benefits:
        frappe.throw("Cần ít nhất một khoản phúc lợi.")

    rows = []
    total = 0.0
    for item in benefits:
        if not isinstance(item, dict):
            frappe.throw("Dòng phúc lợi không hợp lệ.")
        comp = (item.get("earning_component") or "").strip()
        if not comp:
            frappe.throw("Thiếu khoản phúc lợi ở một dòng.")
        try:
            amount = float(item.get("amount") or 0)
        except (TypeError, ValueError):
            frappe.throw(f"Số tiền không hợp lệ ở dòng {comp}.")
        if amount <= 0:
            frappe.throw(f"Số tiền phải lớn hơn 0 ở dòng {comp}.")
        cap, claim = _component_flags(comp)
        rows.append(
            {
                "earning_component": comp,
                "amount": amount,
                "max_benefit_amount": cap,
                "pay_against_benefit_claim": claim,
            }
        )
        total += amount

    name = (payload.get("name") or "").strip()
    date = (payload.get("date") or "").strip() or None
    try:
        if name:
            doc = frappe.get_doc(BENEFIT_DOCTYPE, name)
            if int(getattr(doc, "docstatus", 0) or 0) != 0:
                frappe.throw("Chỉ sửa được bản nháp (Draft).")
            _assert_own(getattr(doc, "employee", None) or "")
            doc.date = date or getattr(doc, "date", None) or frappe.utils.today()
            doc.payroll_period = payroll_period
            doc.employee_benefits = []
            for row in rows:
                doc.append("employee_benefits", row)
            doc.save()
        else:
            doc = frappe.new_doc(BENEFIT_DOCTYPE)
            doc.employee = employee
            doc.date = date or frappe.utils.today()
            doc.payroll_period = payroll_period
            for row in rows:
                doc.append("employee_benefits", row)
            doc.insert()
    except Exception as e:
        msg = str(e)
        if msg:
            frappe.throw(_friendly(msg))
        raise
    return {
        "name": doc.name,
        "docstatus": int(getattr(doc, "docstatus", 0) or 0),
        "status": _DOCSTATUS_LABELS.get(int(getattr(doc, "docstatus", 0) or 0), "Draft"),
        "total_amount": total,
    }


@frappe.whitelist()
def get_benefit_application(name):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = frappe.get_doc(BENEFIT_DOCTYPE, name)
    _assert_own(getattr(doc, "employee", None) or "")
    rows = []
    for r in getattr(doc, "employee_benefits", None) or []:
        rows.append(
            {
                "earning_component": _rowval(r, "earning_component"),
                "amount": _rowval(r, "amount"),
                "max_benefit_amount": _rowval(r, "max_benefit_amount"),
                "pay_against_benefit_claim": _rowval(r, "pay_against_benefit_claim"),
            }
        )
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "employee": getattr(doc, "employee", None),
        "employee_name": getattr(doc, "employee_name", None),
        "date": getattr(doc, "date", None),
        "payroll_period": getattr(doc, "payroll_period", None),
        "max_benefits": getattr(doc, "max_benefits", None),
        "remaining_benefit": getattr(doc, "remaining_benefit", None),
        "total_amount": getattr(doc, "total_amount", None),
        "pro_rata_dispensed_amount": getattr(doc, "pro_rata_dispensed_amount", None),
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "benefits": rows,
        "can": _doc_can(doc),
    }


@frappe.whitelist()
def set_benefit_application_action(name, action):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = _doc_action(BENEFIT_DOCTYPE, name, action)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "amended_from": getattr(doc, "amended_from", None),
    }


# ── Claims: list + save/get/action (P2 — plan §2.3) ──────────────────────────

_CLAIM_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "claim_date",
    "earning_component",
    "max_amount_eligible",
    "claimed_amount",
    "salary_slip",
    "docstatus",
]


def _claim_component_ok(employee: str, on_date: str, component: str) -> bool:
    """Component phải là flexi + claim-based trong structure assigned."""
    for c in _benefit_components(_assigned_structure(employee, on_date)):
        if c.get("name") == component:
            return bool(c.get("pay_against_benefit_claim"))
    return False


@frappe.whitelist()
def my_benefit_claims(employee=None, status=None, component=None, search=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    flt = [["employee", "=", emp]]
    if component:
        flt.append(["earning_component", "=", component])
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt.append(["docstatus", "=", ds])
    res = _list(
        CLAIM_DOCTYPE, _CLAIM_FIELDS, flt, None, search, page, page_size, numeric_fields=["claimed_amount"]
    )
    _decorate_docstatus(res.get("data"))
    return res


@frappe.whitelist()
def all_benefit_claims(
    status=None,
    search=None,
    date_from=None,
    date_to=None,
    component=None,
    amount_min=None,
    amount_max=None,
    page=1,
    page_size=20,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    flt = []
    if component:
        flt.append(["earning_component", "=", component])
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt.append(["docstatus", "=", ds])
    res = _list(
        CLAIM_DOCTYPE,
        _CLAIM_FIELDS,
        flt or None,
        None,
        search,
        page,
        page_size,
        date_field="claim_date",
        date_from=date_from,
        date_to=date_to,
        amount_field="claimed_amount",
        amount_min=amount_min,
        amount_max=amount_max,
        numeric_fields=["claimed_amount"],
    )
    _decorate_docstatus(res.get("data"))
    return res


@frappe.whitelist()
def save_benefit_claim(payload=None):
    """Tạo draft Employee Benefit Claim — hoá đơn đính kèm sau qua
    ``/api/method/uploadfile`` chuẩn Frappe (FE 2 bước: save → upload)."""
    payload = _payload_dict(payload)
    employee = (payload.get("employee") or "").strip()
    if not employee:
        frappe.throw("Cần nhân viên.")
    _assert_own(employee)
    component = (payload.get("earning_component") or "").strip()
    if not component:
        frappe.throw("Cần chọn khoản phúc lợi để hoàn Từ.")
    claim_date = (payload.get("claim_date") or "").strip() or frappe.utils.today()
    try:
        amount = float(payload.get("claimed_amount") or 0)
    except (TypeError, ValueError):
        frappe.throw("Số tiền không hợp lệ.")
    if amount <= 0:
        frappe.throw("Số tiền phải lớn hơn 0.")
    if not _claim_component_ok(employee, claim_date, component):
        frappe.throw(f"Khoản {component} không hỗ trợ hoàn Từ (claim-based).")
    cap, _claim_flag = _component_flags(component)
    try:
        doc = frappe.new_doc(CLAIM_DOCTYPE)
        doc.employee = employee
        doc.claim_date = claim_date
        doc.earning_component = component
        doc.claimed_amount = amount
        doc.max_amount_eligible = cap
        doc.pay_against_benefit_claim = 1
        doc.insert()
    except Exception as e:
        msg = str(e)
        if msg:
            frappe.throw(_friendly(msg))
        raise
    return {"name": doc.name, "max_amount_eligible": cap, "status": "Draft"}


@frappe.whitelist()
def get_benefit_claim(name):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = frappe.get_doc(CLAIM_DOCTYPE, name)
    _assert_own(getattr(doc, "employee", None) or "")
    try:
        attachments = (
            frappe.get_all(
                "File",
                filters=[["attached_to_doctype", "=", CLAIM_DOCTYPE], ["attached_to_name", "=", name]],
                fields=["name", "file_name", "file_url"],
                limit_page_length=20,
            )
            or []
        )
    except Exception:
        attachments = []
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "employee": getattr(doc, "employee", None),
        "employee_name": getattr(doc, "employee_name", None),
        "claim_date": getattr(doc, "claim_date", None),
        "earning_component": getattr(doc, "earning_component", None),
        "max_amount_eligible": getattr(doc, "max_amount_eligible", None),
        "claimed_amount": getattr(doc, "claimed_amount", None),
        "salary_slip": getattr(doc, "salary_slip", None),
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "attachments": attachments,
        "can": _doc_can(doc),
    }


@frappe.whitelist()
def set_benefit_claim_action(name, action):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = _doc_action(CLAIM_DOCTYPE, name, action)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "amended_from": getattr(doc, "amended_from", None),
    }


# ── Gratuity (HR read) ───────────────────────────────────────────────────────
@frappe.whitelist()
def list_gratuities(
    status=None,
    search=None,
    date_from=None,
    date_to=None,
    amount_min=None,
    amount_max=None,
    page=1,
    page_size=20,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    return _list(
        GRATUITY_DOCTYPE,
        _GRATUITY_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        date_field="posting_date",
        date_from=date_from,
        date_to=date_to,
        amount_field="amount",
        amount_min=amount_min,
        amount_max=amount_max,
        numeric_fields=["amount"],
    )


# ── Gratuity: rules + preview + create + action (P3 — plan §2.4) ────────────

_GRATUITY_RULE_FIELDS = [
    "name",
    "work_experience_calculation_function",
    "total_working_days_per_year",
    "minimum_year_for_gratuity",
    "calculate_gratuity_amount_based_on",
]


@frappe.whitelist()
def list_gratuity_rules(search=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [["name", "like", like]]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                GRATUITY_RULE_DOCTYPE,
                or_filters=or_filters,
                fields=_GRATUITY_RULE_FIELDS,
                order_by="modified desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                GRATUITY_RULE_DOCTYPE, or_filters=or_filters, fields=["name"], limit_page_length=0
            )
            or []
        )
    except Exception:
        frappe.log_error(title="benefits_admin.list_gratuity_rules failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


@frappe.whitelist()
def get_gratuity_rule(name):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    if not name:
        frappe.throw("Cần mã quy tắc.")
    doc = frappe.get_doc(GRATUITY_RULE_DOCTYPE, name)
    slabs = []
    for s in getattr(doc, "gratuity_rule_slabs", None) or []:
        slabs.append(
            {
                "from_year": _rowval(s, "from_year"),
                "to_year": _rowval(s, "to_year"),
                "fraction_of_applicable_earnings": _rowval(s, "fraction_of_applicable_earnings"),
            }
        )
    components = []
    for c in getattr(doc, "applicable_earnings_component", None) or []:
        components.append(_rowval(c, "salary_component") or _rowval(c, "name"))
    return {
        "name": doc.name,
        "work_experience_calculation_function": getattr(doc, "work_experience_calculation_function", None),
        "total_working_days_per_year": getattr(doc, "total_working_days_per_year", None),
        "minimum_year_for_gratuity": getattr(doc, "minimum_year_for_gratuity", None),
        "calculate_gratuity_amount_based_on": getattr(doc, "calculate_gratuity_amount_based_on", None),
        "slabs": slabs,
        "applicable_components": components,
    }


@frappe.whitelist()
def preview_gratuity(payload=None):
    """Tính trước số gratuity KHÔNG lưu doc — bọc docmethod
    ``calculate_work_experience_and_amount`` của HRMS."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    payload = _payload_dict(payload)
    employee = (payload.get("employee") or "").strip()
    if not employee:
        frappe.throw("Cần nhân viên.")
    rule = (payload.get("gratuity_rule") or "").strip()
    if not rule:
        frappe.throw("Cần chọn quy tắc trợ cấp.")
    relieving = frappe.db.get_value("Employee", employee, "relieving_date")
    if not relieving:
        frappe.throw("Nhân viên chưa có ngày nghỉ việc (Relieving Date).")
    doc = frappe.new_doc(GRATUITY_DOCTYPE)
    doc.employee = employee
    doc.gratuity_rule = rule
    doc.posting_date = (payload.get("posting_date") or "").strip() or frappe.utils.today()
    calc = getattr(doc, "calculate_work_experience_and_amount", None)
    try:
        out = calc() if callable(calc) else {}
    except Exception as e:
        frappe.throw(_friendly(e))
    out = out or {}
    return {
        "current_work_experience": out.get("current_work_experience") or 0,
        "amount": out.get("amount") or 0,
    }


@frappe.whitelist()
def create_gratuity(payload=None):
    """Tạo draft Gratuity — validate kênh thanh toán theo ``mandatory_depends_on``
    của doctype; submit (set_gratuity_action) để controller tự sinh Additional
    Salary (pay_via_salary_slip) hoặc GL Entries."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    payload = _payload_dict(payload)
    employee = (payload.get("employee") or "").strip()
    if not employee:
        frappe.throw("Cần nhân viên.")
    rule = (payload.get("gratuity_rule") or "").strip()
    if not rule:
        frappe.throw("Cần chọn quy tắc trợ cấp.")
    relieving = frappe.db.get_value("Employee", employee, "relieving_date")
    if not relieving:
        frappe.throw("Nhân viên chưa có ngày nghỉ việc (Relieving Date).")
    pay_via = _flag(payload.get("pay_via_salary_slip", 1))
    fields = {
        "employee": employee,
        "gratuity_rule": rule,
        "posting_date": (payload.get("posting_date") or "").strip() or frappe.utils.today(),
        "pay_via_salary_slip": pay_via,
    }
    if pay_via:
        payroll_date = (payload.get("payroll_date") or "").strip()
        salary_component = (payload.get("salary_component") or "").strip()
        if not payroll_date or not salary_component:
            frappe.throw("Kênh trả qua phiếu lương cần ngày chi trả và thành phần lương.")
        fields["payroll_date"] = payroll_date
        fields["salary_component"] = salary_component
    else:
        expense_account = (payload.get("expense_account") or "").strip()
        payable_account = (payload.get("payable_account") or "").strip()
        if not expense_account or not payable_account:
            frappe.throw("Kênh trả qua kế toán cần tài khoản chi phí và tài khoản phải trả.")
        fields["expense_account"] = expense_account
        fields["payable_account"] = payable_account
        for key in ("mode_of_payment", "cost_center"):
            val = (payload.get(key) or "").strip()
            if val:
                fields[key] = val
    try:
        doc = frappe.new_doc(GRATUITY_DOCTYPE)
        for key, val in fields.items():
            setattr(doc, key, val)
        doc.insert()
    except Exception as e:
        msg = str(e)
        if msg:
            frappe.throw(_friendly(msg))
        raise
    return {
        "name": doc.name,
        "amount": getattr(doc, "amount", None),
        "current_work_experience": getattr(doc, "current_work_experience", None),
        "status": getattr(doc, "status", None) or "Draft",
    }


@frappe.whitelist()
def set_gratuity_action(name, action):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = _doc_action(GRATUITY_DOCTYPE, name, action, allow_amend=False)
    return {
        "name": doc.name,
        "docstatus": int(getattr(doc, "docstatus", 0) or 0),
        "status": getattr(doc, "status", None),
    }


# ── Promotion ────────────────────────────────────────────────────────────────
@frappe.whitelist()
def my_promotions(employee=None, status=None, page=1, page_size=20):
    emp = _resolve(employee)
    _assert_own(emp)
    flt = [["employee", "=", emp]]
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt.append(["docstatus", "=", ds])
    res = _list(PROMOTION_DOCTYPE, _PROMOTION_FIELDS, flt, None, None, page, page_size)
    _decorate_docstatus(res.get("data"))
    return res


@frappe.whitelist()
def all_promotions(status=None, search=None, date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager.")
    flt = None
    if status:
        ds = _STATUS_TO_DOCSTATUS.get(status)
        if ds is not None:
            flt = [["docstatus", "=", ds]]
    res = _list(
        PROMOTION_DOCTYPE,
        _PROMOTION_FIELDS,
        flt,
        None,
        search,
        page,
        page_size,
        date_field="promotion_date",
        date_from=date_from,
        date_to=date_to,
    )
    _decorate_docstatus(res.get("data"))
    return res


# ── Options (gear popover — DNA §6.3) ────────────────────────────────────────
@frappe.whitelist()
def get_benefit_filter_options():
    """Distinct + hard-code options cho gear popover (DNA §6.3 —
    SearchableSelect không bao giờ rỗng). Nới gate cho mọi user đã đăng nhập:
    chỉ là nhãn/tên master, tab "Của tôi" của nhân viên cũng cần filter."""
    _DOCSTATUS_LABELS_LIST = ["Draft", "Submitted", "Cancelled"]

    def _distinct(doctype, field="status"):
        try:
            rows = frappe.get_all(doctype, fields=[field], distinct=True, limit_page_length=0) or []
        except Exception:
            return []
        seen, out = set(), []
        for r in rows:
            v = (r or {}).get(field)
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _merge(base, extra):
        seen, out = set(), []
        for v in list(base) + list(extra):
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    components: list = []
    try:
        components = frappe.get_all(
            "Salary Component", filters={"is_flexible_benefit": 1}, pluck="name", limit_page_length=100
        )
    except Exception:
        components = []
    periods: list = []
    try:
        periods = frappe.get_all("Payroll Period", fields=["name", "start_date", "end_date"], limit_page_length=100)
    except Exception:
        periods = []
    rules: list = []
    try:
        rules = frappe.get_all("Gratuity Rule", pluck="name", limit_page_length=100)
    except Exception:
        rules = []
    gratuity_statuses = _merge(
        ["Draft", "Submitted", "Unpaid", "Paid", "Cancelled"], _distinct(GRATUITY_DOCTYPE)
    )
    return {
        # legacy keys — FE hiện đang dùng, giữ để không break.
        "benefits": _DOCSTATUS_LABELS_LIST,
        "gratuity": gratuity_statuses,
        "promotion": _DOCSTATUS_LABELS_LIST,
        # plan §2.1 — keys mới.
        "application_statuses": _DOCSTATUS_LABELS_LIST,
        "claim_statuses": _DOCSTATUS_LABELS_LIST,
        "gratuity_statuses": gratuity_statuses,
        "promotion_statuses": _DOCSTATUS_LABELS_LIST,
        "components": components,
        "payroll_periods": periods,
        "gratuity_rules": rules,
    }


# ── Promotion: details + CTC + lifecycle (P4 — plan §2.5) ───────────────────
# Endpoint cũ ``create_promotion`` (bản rỗng không có promotion_details) đã xoá.

_PROMOTION_EDITABLE_FIELDS = [
    ("designation", "Chức danh"),
    ("department", "Phòng ban"),
    ("branch", "Chi nhánh"),
    ("employment_type", "Loại hình làm việc"),
    ("grade", "Cấp bậc"),
]


@frappe.whitelist()
def save_promotion(payload=None):
    """Tạo draft Employee Promotion với rows ``promotion_details`` (Employee
    Property History: property/current/new). Submit cập nhật Employee master
    (controller HRMS)."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager tạo thăng cấp.")
    payload = _payload_dict(payload)
    employee = (payload.get("employee") or "").strip()
    if not employee:
        frappe.throw("Cần nhân viên.")
    details = payload.get("details") or []
    if not isinstance(details, list) or not details:
        frappe.throw("Cần ít nhất một thuộc tính thay đổi.")
    known = {f for f, _label in _PROMOTION_EDITABLE_FIELDS}
    labels = dict(_PROMOTION_EDITABLE_FIELDS)
    rows = []
    for item in details:
        if not isinstance(item, dict):
            frappe.throw("Dòng thuộc tính không hợp lệ.")
        fieldname = (item.get("fieldname") or "").strip()
        if fieldname not in known:
            frappe.throw(f"Thuộc tính {fieldname or '(trống)'} không hỗ trợ.")
        new_value = str(item.get("new_value") or "").strip()
        if not new_value:
            frappe.throw(f"Cần giá trị mới cho {labels.get(fieldname, fieldname)}.")
        previous = frappe.db.get_value("Employee", employee, fieldname) or ""
        rows.append(
            {
                "fieldname": fieldname,
                "property": labels.get(fieldname, fieldname),
                "current": str(previous or ""),
                "new": new_value,
            }
        )
    current_ctc = payload.get("current_ctc")
    revised_ctc = payload.get("revised_ctc")
    if current_ctc in (None, "") and revised_ctc not in (None, ""):
        current_ctc = frappe.db.get_value("Employee", employee, "ctc")
    try:
        doc = frappe.new_doc(PROMOTION_DOCTYPE)
        doc.employee = employee
        doc.promotion_date = (payload.get("promotion_date") or "").strip() or frappe.utils.today()
        for row in rows:
            doc.append("promotion_details", row)
        if current_ctc not in (None, ""):
            doc.current_ctc = current_ctc
        if revised_ctc not in (None, ""):
            doc.revised_ctc = revised_ctc
        doc.insert()
    except Exception as e:
        msg = str(e)
        if msg:
            frappe.throw(_friendly(msg))
        raise
    return {"name": doc.name, "status": "Draft"}


@frappe.whitelist()
def get_promotion(name):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = frappe.get_doc(PROMOTION_DOCTYPE, name)
    _assert_own(getattr(doc, "employee", None) or "")
    details = []
    for r in getattr(doc, "promotion_details", None) or []:
        details.append(
            {
                "fieldname": _rowval(r, "fieldname"),
                "property": _rowval(r, "property"),
                "current": _rowval(r, "current"),
                "new": _rowval(r, "new"),
            }
        )
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "employee": getattr(doc, "employee", None),
        "employee_name": getattr(doc, "employee_name", None),
        "promotion_date": getattr(doc, "promotion_date", None),
        "current_ctc": getattr(doc, "current_ctc", None),
        "revised_ctc": getattr(doc, "revised_ctc", None),
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "details": details,
        "can": _doc_can(doc),
    }


@frappe.whitelist()
def set_promotion_action(name, action):
    if not name:
        frappe.throw("Cần mã tài liệu.")
    doc = _doc_action(PROMOTION_DOCTYPE, name, action)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    return {
        "name": doc.name,
        "docstatus": ds,
        "status": _DOCSTATUS_LABELS.get(ds, "Draft"),
        "amended_from": getattr(doc, "amended_from", None),
    }
