"""Payslip acknowledge & QR payout API — plans/payslip-ack-qr-payment-plan.md.

Employee-facing flow (2026-08, user decisions #1–#5):

  * ``my_bank_accounts`` / ``save_bank_account`` / ``delete_bank_account``
    — the employee manages their VND payout accounts (QR-scanned or manual)
    from ``/hr/profile``; one account is the payroll default.
  * ``request_payslip_adjustment`` — "phiếu sai": stamp ``Requested`` + note,
    notify HR; the period stays recalculable so HR can fix & republish.
  * ``confirm_payslip`` — MANDATORY choice of one of the employee's bank
    accounts (decision #3); snapshots BIN/account/holder + a VietQR payload
    (amount = net, memo = REF) onto the slip and flips it to
    ``Awaiting Payment``.

Manager-facing flow:

  * ``pending_payment_slips`` — the accounting payout queue
    (``/hr/payroll/payments``).
  * ``mark_payslip_paid`` — proof image is MANDATORY (decision #4); stamps
    Paid + who/when and notifies the employee.

Lock rule (decision #2): a single slip in ``Awaiting Payment``/``Paid``
freezes the whole period — enforced in api/payroll.calculate/approve/
generate/publish via ``utils.payroll.period_has_confirmed_slips``.
No acknowledgement deadline (decision #5): an unconfirmed slip simply waits.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, vietqr

BANK_DOCTYPE = "VN Employee Bank Account"
SLIP_DOCTYPE = "Salary Slip"

ACK_REQUESTED = "Requested"
ACK_AWAITING = "Awaiting Payment"
ACK_PAID = "Paid"

_BANK_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "bank_bin",
    "bank_name",
    "account_no",
    "account_name",
    "is_default",
    "qr_image",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _own_employee() -> str | None:
    return emp_utils.get_employee_for_user()


def _get_slip(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã phiếu lương."))
    slip = frappe.db.get_value(
        SLIP_DOCTYPE,
        name,
        [
            "name",
            "employee",
            "employee_name",
            "company",
            "vn_payroll_review_period",
            "start_date",
            "end_date",
            "net_pay",
            "vn_employee_visible",
            "vn_visible_at",
            "vn_ack_status",
            "vn_ack_note",
            "vn_ack_at",
            "vn_ack_source",
            "vn_ack_rejected_at",
            "vn_ack_rejected_reason",
            "vn_payment_ref",
            "vn_payee_bank_bin",
            "vn_payee_bank_name",
            "vn_payee_account_no",
            "vn_payee_account_name",
            "vn_payee_qr_text",
            "vn_payment_proof",
            "vn_paid_at",
            "vn_paid_by",
        ],
        as_dict=True,
    )
    if not slip:
        frappe.throw(_("Phiếu lương {0} không tồn tại.").format(name))
    return slip


def _assert_own_slip(slip) -> None:
    if _is_manager():
        return
    own = _own_employee()
    if not own or own != getattr(slip, "employee", None):
        frappe.throw(
            _("Bạn không có quyền thao tác phiếu lương của nhân viên khác."),
            frappe.PermissionError,
        )


def _assert_manager() -> None:
    if not _is_manager():
        frappe.throw(
            _("Chỉ HR/Payroll Manager mới được thực hiện thao tác này."),
            frappe.PermissionError,
        )


def _payment_ref(slip) -> str:
    """Deterministic bank-transfer memo: LUONG-{MMYYYY}-{employee code}."""
    start = str(getattr(slip, "start_date", "") or "")
    month, year = "", ""
    if len(start) >= 7:
        year, month = start[:4], start[5:7]
    return f"LUONG-{month}{year}-{getattr(slip, 'employee', '')}"


def _notify_hr_managers(
    company: str | None, title: str, message: str, ref_doctype: str, ref_name: str
) -> None:
    """Push a bell notification to every HR manager employee of the company."""
    try:
        managers = frappe.db.sql(
            """
            SELECT DISTINCT e.name
            FROM `tabEmployee` e
            INNER JOIN `tabUser` u ON u.name = e.user_id
            INNER JOIN `tabHas Role` hr ON hr.parent = u.name
            WHERE e.status = 'Active' AND hr.role IN ('HR Manager', 'Payroll Manager')
              AND (%(company)s IS NULL OR e.company = %(company)s)
            """,
            {"company": company},
            as_dict=True,
        )
        from gege_hr.gege_hr.utils import notify

        for m in managers or []:
            try:
                notify.push_notification(
                    employee=m["name"],
                    notification_type="Payroll",
                    title=title,
                    message=message,
                    reference_doctype=ref_doctype,
                    reference_name=ref_name,
                    action_url="/payroll",
                )
            except Exception:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# M1 — bank accounts (profile)
# --------------------------------------------------------------------------- #
def _publish_payslip_updated(slip) -> None:
    """Payslips desk-free (plan payslips-deskfree-complete §2.7) — best-effort
    realtime ping so open payslip tabs (list + detail) refresh when the ack
    status changes. Room contract mirrors useHrRealtime's ``payroll:{company}``
    channel; the employee's own user room gets a direct ping too."""
    try:
        frappe.publish_realtime(
            "payslip_updated",
            {
                "name": getattr(slip, "name", None),
                "employee": getattr(slip, "employee", None),
                "vn_ack_status": getattr(slip, "vn_ack_status", None) or "",
            },
            room=f"payroll:{getattr(slip, 'company', None) or ''}",
        )
        user_id = frappe.db.get_value("Employee", getattr(slip, "employee", None), "user_id")
        if user_id:
            frappe.publish_realtime(
                "payslip_updated",
                {"name": getattr(slip, "name", None)},
                user=user_id,
            )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_bank_accounts() -> list[dict]:
    """The caller's own VND payout accounts (default first)."""
    emp = _own_employee()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    rows = frappe.get_all(
        BANK_DOCTYPE,
        filters={"employee": emp},
        fields=_BANK_FIELDS,
        order_by="is_default desc, modified desc",
    )
    return rows


@frappe.whitelist()
def save_bank_account(**kwargs) -> dict:
    """Create/update one of MY accounts (QR-scanned values or manual input).

    Accepts ``name`` (edit) plus ``bank_bin`` (6 digits), ``account_no`` (6–19
    digits), ``account_name``, ``bank_name`` (optional), ``is_default`` and
    ``qr_image`` (optional File url of the scanned QR).
    """
    emp = _own_employee()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)

    name = (kwargs.get("name") or "").strip()
    if name:
        doc = frappe.get_doc(BANK_DOCTYPE, name)
        if doc.employee != emp:
            frappe.throw(
                _("Bạn không có quyền sửa tài khoản của nhân viên khác."),
                frappe.PermissionError,
            )
    else:
        doc = frappe.new_doc(BANK_DOCTYPE)
        doc.employee = emp

    doc.bank_bin = kwargs.get("bank_bin")
    doc.bank_name = (kwargs.get("bank_name") or "").strip()
    doc.account_no = kwargs.get("account_no")
    doc.account_name = (kwargs.get("account_name") or "").strip()
    doc.is_default = 1 if kwargs.get("is_default") else 0
    if kwargs.get("qr_image"):
        doc.qr_image = kwargs.get("qr_image")
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "message": _("Đã lưu tài khoản nhận lương.")}


@frappe.whitelist()
def delete_bank_account(name: str | None = None) -> dict:
    """Delete one of MY accounts — refused while a pending slip still
    references it as its payout snapshot (would orphan the queue entry)."""
    emp = _own_employee()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    name = (name or "").strip()
    if not name:
        frappe.throw(_("Thiếu mã tài khoản."))
    doc = frappe.get_doc(BANK_DOCTYPE, name)
    if doc.employee != emp:
        frappe.throw(
            _("Bạn không có quyền xóa tài khoản của nhân viên khác."),
            frappe.PermissionError,
        )
    in_use = frappe.db.get_value(
        SLIP_DOCTYPE,
        {
            "employee": emp,
            "vn_payee_account_no": doc.account_no,
            "vn_ack_status": ["in", (ACK_AWAITING, ACK_PAID)],
        },
        "name",
    )
    if in_use:
        frappe.throw(_("Tài khoản đang được dùng trong phiếu lương {0} — không thể xóa.").format(in_use))
    frappe.delete_doc(BANK_DOCTYPE, name, ignore_permissions=True)
    return {"name": name, "message": _("Đã xóa tài khoản.")}


# --------------------------------------------------------------------------- #
# M2 — adjustment request
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def request_payslip_adjustment(name: str | None = None, reason: str | None = None) -> dict:
    """Employee flags the published slip as wrong → ``Requested``.

    The period is NOT locked (that is the point): HR fixes the line,
    recalculates and republishes; the regenerated draft slip starts fresh.
    """
    slip = _get_slip(name)
    _assert_own_slip(slip)

    if not slip.vn_employee_visible:
        frappe.throw(_("Phiếu lương này chưa được phát hành cho nhân viên."))
    # Decision (2026-08-20): a REJECTED adjustment is FINAL — the employee may
    # only Confirm afterwards; re-requesting would loop forever and delay the
    # whole company's payday. The reject reason is surfaced on the slip banner.
    if getattr(slip, "vn_ack_rejected_at", None):
        frappe.throw(
            _(
                "Yêu cầu điều chỉnh đã bị từ chối — vui lòng xác nhận phiếu lương. "
                "Nếu còn thắc mắc, liên hệ HR trực tiếp."
            )
        )
    if slip.vn_ack_status == ACK_REQUESTED:
        frappe.throw(_("Yêu cầu điều chỉnh đã được gửi — vui lòng chờ HR xử lý."))
    if slip.vn_ack_status in (ACK_AWAITING, ACK_PAID):
        frappe.throw(_("Phiếu đã được xác nhận — không thể yêu cầu điều chỉnh. Liên hệ HR để hỗ trợ."))

    reason = (reason or "").strip()
    if len(reason) < 10:
        frappe.throw(_("Vui lòng mô tả điều cần điều chỉnh (tối thiểu 10 ký tự)."))

    stamp = {
        "vn_ack_status": ACK_REQUESTED,
        "vn_ack_note": reason,
        "vn_ack_at": now_datetime(),
    }
    frappe.db.set_value(SLIP_DOCTYPE, slip.name, stamp)
    audit_api.log(
        "Payslip Adjustment Request",
        doc={"doctype": SLIP_DOCTYPE, "name": slip.name, **stamp},
        work_date=slip.start_date,
        description=f"{slip.employee}: {reason[:140]}",
        old_value="",
        new_value=ACK_REQUESTED,
    )
    _notify_hr_managers(
        slip.company,
        _("Yêu cầu điều chỉnh phiếu lương"),
        _("Nhân viên {0} yêu cầu điều chỉnh phiếu lương {1}.").format(
            slip.employee_name or slip.employee, slip.name
        ),
        SLIP_DOCTYPE,
        slip.name,
    )
    _publish_payslip_updated(slip)
    return {
        "name": slip.name,
        "status": ACK_REQUESTED,
        "message": _("Đã gửi yêu cầu điều chỉnh tới HR."),
    }


# --------------------------------------------------------------------------- #
# M3 — confirm (mandatory bank choice) → Awaiting Payment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def confirm_payslip(name: str | None = None, bank_account: str | None = None, **kwargs) -> dict:
    """Employee confirms the slip is correct and picks the payout account.

    Decision #3: ``bank_account`` (a VN Employee Bank Account of the same
    employee) is MANDATORY. The account details + a VietQR payload (amount =
    net_pay, memo = REF) are SNAPSHOTTED onto the slip so later profile edits
    cannot silently retarget a pending payout. From here the period is locked
    (decision #2).
    """
    slip = _get_slip(name)
    _assert_own_slip(slip)

    if not slip.vn_employee_visible:
        frappe.throw(_("Phiếu lương này chưa được phát hành cho nhân viên."))
    if slip.vn_ack_status == ACK_REQUESTED:
        frappe.throw(_("Phiếu đang chờ điều chỉnh — HR sẽ phát hành lại trước khi bạn xác nhận."))
    if slip.vn_ack_status in (ACK_AWAITING, ACK_PAID):
        frappe.throw(_("Phiếu lương này đã được xác nhận."))

    bank_account = (bank_account or "").strip()
    if not bank_account:
        frappe.throw(_("Vui lòng chọn tài khoản ngân hàng nhận lương."))
    # Auto-confirm path (scheduled job): the caller passes the employee's
    # DEFAULT bank account and source="Auto" — same snapshot semantics.
    source = "Auto" if kwargs.get("source") == "Auto" else "Employee"
    bank = frappe.db.get_value(
        BANK_DOCTYPE,
        bank_account,
        ["name", "bank_bin", "bank_name", "account_no", "account_name"],
        as_dict=True,
    )
    if not bank or not _is_manager():
        own = _own_employee()
        if not bank or (own and frappe.db.get_value(BANK_DOCTYPE, bank_account, "employee") != own):
            frappe.throw(
                _("Tài khoản ngân hàng không hợp lệ hoặc không thuộc về bạn."),
                frappe.PermissionError,
            )

    ref = _payment_ref(slip)
    qr_text = vietqr.encode_vietqr(
        bank.bank_bin,
        bank.account_no,
        amount=float(slip.net_pay or 0),
        description=ref,
    )

    stamp = {
        "vn_ack_status": ACK_AWAITING,
        "vn_ack_note": "",
        "vn_ack_at": now_datetime(),
        "vn_ack_source": source,
        "vn_payment_ref": ref,
        "vn_payee_bank_bin": bank.bank_bin,
        "vn_payee_bank_name": bank.bank_name,
        "vn_payee_account_no": bank.account_no,
        "vn_payee_account_name": bank.account_name,
        "vn_payee_qr_text": qr_text,
    }
    frappe.db.set_value(SLIP_DOCTYPE, slip.name, stamp)
    audit_api.log(
        "Payslip Confirmed",
        doc={"doctype": SLIP_DOCTYPE, "name": slip.name, **stamp},
        work_date=slip.start_date,
        description=f"{slip.employee} → {ref} ({bank.account_no})",
        old_value="",
        new_value=ACK_AWAITING,
    )
    maybe_lock_period_after_confirm(getattr(slip, "vn_payroll_review_period", None))
    _publish_payslip_updated(slip)

    return {
        "name": slip.name,
        "status": ACK_AWAITING,
        "payment_ref": ref,
        "qr_text": qr_text,
        "message": _("Đã xác nhận phiếu lương — chuyển sang hàng chờ thanh toán."),
    }


def maybe_lock_period_after_confirm(period_name: str | None) -> None:
    """Plan v2 auto-lock: when EVERY visible slip of the period is confirmed
    (Awaiting Payment / Paid), submit them all (HRMS Submitted — the standard
    accounting state) and set the period to ``Confirmed`` (locked). Individual
    submit failures are logged and skipped; withheld (non-visible) slips are
    not counted. Best-effort — never raises into the confirm call."""
    try:
        _maybe_lock_period_after_confirm(period_name)
    except Exception:
        pass


def _maybe_lock_period_after_confirm(period_name: str | None) -> None:
    if not period_name:
        return
    rows = frappe.get_all(
        SLIP_DOCTYPE,
        filters={"vn_payroll_review_period": period_name, "docstatus": ["<", 2]},
        fields=["name", "vn_employee_visible", "vn_ack_status"],
    )
    visible = [r for r in rows if int(r.get("vn_employee_visible") or 0) == 1]
    if not visible:
        return
    if any((r.get("vn_ack_status") or "") not in (ACK_AWAITING, ACK_PAID) for r in visible):
        return
    submitted = 0
    for r in visible:
        try:
            doc = frappe.get_doc(SLIP_DOCTYPE, r["name"])
            if doc.docstatus == 0:
                doc.submit()
            submitted += 1
        except Exception:
            frappe.log_error(
                title="VN Payslip auto-lock: submit failed",
                message=f"{period_name} -> {r['name']}",
            )
    frappe.db.set_value("VN Payroll Review Period", period_name, "status", "Confirmed")
    frappe.db.commit()
    _notify_hr_managers(
        None,
        _("Kỳ lương đã được chốt"),
        _(
            "100% nhân viên đã xác nhận — kỳ {0} đã chốt ({1}/{2} phiếu). Chuyển tiền tại /hr/payroll/payments."
        ).format(period_name, submitted, len(visible)),
        "VN Payroll Review Period",
        period_name,
    )
    audit_api.log(
        "Payslip Period Auto-Locked",
        doc={"doctype": "VN Payroll Review Period", "name": period_name},
        description=f"100% confirmed → submitted {submitted}/{len(visible)} slips",
        old_value="",
        new_value="Confirmed",
    )


# --------------------------------------------------------------------------- #
# M4 — accounting payout queue
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def pending_payment_slips(
    company: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Manager-only queue of confirmed slips awaiting the bank transfer."""
    _assert_manager()
    filters = {"vn_ack_status": ACK_AWAITING}
    if company:
        filters["company"] = company
    if from_date or to_date:
        filters["start_date"] = ["between", [from_date or to_date, to_date or from_date]]
    return frappe.get_all(
        SLIP_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "employee",
            "employee_name",
            "company",
            "start_date",
            "end_date",
            "net_pay",
            "vn_payment_ref",
            "vn_payee_bank_bin",
            "vn_payee_bank_name",
            "vn_payee_account_no",
            "vn_payee_account_name",
            "vn_payee_qr_text",
            "vn_ack_at",
        ],
        order_by="start_date asc, employee asc",
        limit_page_length=500,
    )


@frappe.whitelist()
def pending_adjustment_slips(company: str | None = None) -> list[dict]:
    """Manager-only inbox of employee adjustment requests (``Requested``).

    Where the HR manager READS the "phiếu sai" requests: each row carries the
    employee's note; the fix loop is Review (edit line) → Tính lương → Duyệt →
    Tạo phiếu (replaces the draft slip) → Phát hành lại — the regenerated
    slip starts unacknowledged so the employee can confirm the corrected one.
    """
    _assert_manager()
    filters = {"vn_ack_status": ACK_REQUESTED}
    if company:
        filters["company"] = company
    return frappe.get_all(
        SLIP_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "employee",
            "employee_name",
            "company",
            "start_date",
            "end_date",
            "net_pay",
            "vn_ack_note",
            "vn_ack_at",
        ],
        order_by="vn_ack_at asc",
        limit_page_length=500,
    )


# --------------------------------------------------------------------------- #
# Plan v2 — FINAL rejection of an adjustment request (Employee cannot re-request)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def reject_payslip_adjustment(name: str | None = None, reason: str | None = None) -> dict:
    """HR concludes the payslip is CORRECT — the request is closed for good.

    The employee keeps (only) the Confirm action on the slip; re-requesting is
    refused forever (guard in ``request_payslip_adjustment``). No amounts are
    touched, the period is NOT recalculated. Reject reason is shown on the
    employee's banner + notification."""
    _assert_manager()
    slip = _get_slip(name)
    if slip.vn_ack_status != ACK_REQUESTED:
        frappe.throw(_("Chỉ yêu cầu điều chỉnh đang chờ mới có thể bị từ chối."))
    reason = (reason or "").strip()
    if len(reason) < 10:
        frappe.throw(_("Vui lòng nêu lý do từ chối (tối thiểu 10 ký tự)."))

    stamp = {
        "vn_ack_status": "",
        "vn_ack_note": reason,
        "vn_ack_rejected_at": now_datetime(),
        "vn_ack_rejected_reason": reason,
    }
    frappe.db.set_value(SLIP_DOCTYPE, slip.name, stamp)
    frappe.db.commit()
    audit_api.log(
        "Payslip Adjustment Rejected",
        doc={"doctype": SLIP_DOCTYPE, "name": slip.name, **stamp},
        work_date=slip.start_date,
        description=f"{slip.employee}: {reason[:140]}",
        old_value=ACK_REQUESTED,
        new_value="Rejected (final)",
    )
    try:
        from gege_hr.gege_hr.utils import notify

        notify.push_notification(
            employee=slip.employee,
            notification_type="Payroll",
            title=_("Yêu cầu điều chỉnh bị từ chối"),
            message=_(
                "Yêu cầu điều chỉnh phiếu lương {0} bị từ chối. Lý do: {1}. Vui lòng xác nhận phiếu lương."
            ).format(slip.name, reason[:120]),
            reference_doctype=SLIP_DOCTYPE,
            reference_name=slip.name,
            action_url="/payslips",
        )
    except Exception:
        pass
    _publish_payslip_updated(slip)
    return {
        "name": slip.name,
        "status": "",
        "rejected": True,
        "message": _("Đã từ chối yêu cầu — nhân viên chỉ có thể xác nhận phiếu."),
    }


# --------------------------------------------------------------------------- #
# Plan v2 — Option D: deliberate reopen of a confirmed period (HR Manager)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def reopen_confirmed_period(name: str | None = None, reason: str | None = None) -> dict:
    """🔓 "Mở lại để điều chỉnh" — the sanctioned escape hatch.

    Resets the ack state of every not-yet-submitted slip (and cancels already
    submitted ones with audit) so HR can fix → recalculate → regenerate →
    notify. Requires a reason (≥10 chars); every affected employee is
    notified to re-confirm afterwards. HR Manager only."""
    _assert_manager()
    reason = (reason or "").strip()
    if len(reason) < 10:
        frappe.throw(_("Vui lòng nêu lý do mở lại (tối thiểu 10 ký tự)."))
    period = frappe.db.get_value(
        "VN Payroll Review Period", name, ["name", "status", "company"], as_dict=True
    )
    if not period:
        frappe.throw(_("Kỳ lương {0} không tồn tại.").format(name))
    if period.status not in ("Published", "Confirmed", "Slips Generated"):
        frappe.throw(_("Chỉ kỳ đã phát hành/chốt mới được mở lại."))

    slips = frappe.get_all(
        SLIP_DOCTYPE,
        filters={"vn_payroll_review_period": name, "docstatus": ["<", 2]},
        fields=["name", "employee", "docstatus", "vn_ack_status"],
    )
    reset = cancelled = 0
    for s in slips or []:
        try:
            if s.docstatus == 1:
                frappe.get_doc(SLIP_DOCTYPE, s["name"]).cancel()
                cancelled += 1
            frappe.db.set_value(
                SLIP_DOCTYPE,
                s["name"],
                {
                    "vn_ack_status": "",
                    "vn_ack_note": "",
                    "vn_ack_at": None,
                    "vn_ack_source": "",
                    "vn_payment_ref": "",
                    "vn_payee_bank_bin": "",
                    "vn_payee_bank_name": "",
                    "vn_payee_account_no": "",
                    "vn_payee_account_name": "",
                    "vn_payee_qr_text": "",
                    "vn_ack_rejected_at": None,
                    "vn_ack_rejected_reason": "",
                },
            )
            reset += 1
            try:
                from gege_hr.gege_hr.utils import notify

                notify.push_notification(
                    employee=s["employee"],
                    notification_type="Payroll",
                    title=_("Kỳ lương được mở lại điều chỉnh"),
                    message=_(
                        "Kỳ lương của bạn được điều chỉnh — vui lòng xác nhận lại phiếu khi có thông báo."
                    ),
                    reference_doctype=SLIP_DOCTYPE,
                    reference_name=s["name"],
                    action_url="/payslips",
                )
            except Exception:
                pass
        except Exception:
            frappe.log_error(title="VN Payslip reopen: reset failed", message=f"{name} -> {s['name']}")
    frappe.db.set_value("VN Payroll Review Period", name, "status", "Calculated")
    frappe.db.commit()
    audit_api.log(
        "Payslip Period Reopened",
        doc={"doctype": "VN Payroll Review Period", "name": name},
        description=f"{reason[:140]} (reset {reset}, cancelled {cancelled})",
        old_value=period.status,
        new_value="Calculated",
    )
    return {
        "name": name,
        "reset": reset,
        "cancelled": cancelled,
        "message": _("Đã mở lại kỳ — sửa dòng lương rồi Tính lại → Duyệt → Tạo phiếu → Gửi thông báo."),
    }


# --------------------------------------------------------------------------- #
# Plan v2 — auto-confirm scheduled job (daily)
# --------------------------------------------------------------------------- #
AUTOCONFIRM_SETTING = "vn_payslip_autoconfirm_days"


def _autoconfirm_days() -> int:
    try:
        return int(frappe.db.get_single_value("VN HR Portal Setting", AUTOCONFIRM_SETTING) or 3)
    except Exception:
        return 3


@frappe.whitelist()
def run_payslip_autoconfirm() -> dict:
    """Daily job — auto-confirm overdue unacknowledged slips (default 3 days).

    Rules (plan v2 §6.4): deadline anchored at ``vn_visible_at`` (NOT reset on
    reject); ``Requested`` slips are skipped (HR owns them); employees without
    a DEFAULT bank account are collected into an HR digest notification."""
    days = _autoconfirm_days()
    if days <= 0:
        return {"processed": 0, "auto_confirmed": 0, "missing_bank": 0, "days": days}

    try:
        from frappe.utils import add_days, now

        cutoff = add_days(now(), -days)
    except Exception:
        cutoff = None
    if not cutoff:
        return {"processed": 0, "auto_confirmed": 0, "missing_bank": 0, "days": days}

    # Day-2 reminder (plan v2 §6.4): visible_at in [cutoff, cutoff+1d) → the
    # slip is due TOMORROW — nudge the employee before the auto-confirm.
    try:
        from frappe.utils import add_days as _add_days

        remind_until = _add_days(cutoff, 1)
        for r in (
            frappe.get_all(
                SLIP_DOCTYPE,
                filters={
                    "vn_employee_visible": 1,
                    "docstatus": ["<", 2],
                    "vn_ack_status": "",
                    "vn_visible_at": ["between", [cutoff, remind_until]],
                },
                fields=["name", "employee"],
                limit_page_length=1000,
            )
            or []
        ):
            try:
                from gege_hr.gege_hr.utils import notify

                notify.push_notification(
                    employee=r["employee"],
                    notification_type="Payroll",
                    title=_("Sắp tự động xác nhận phiếu lương"),
                    message=_(
                        "Phiếu lương của bạn sẽ được tự động xác nhận trong vòng 24 giờ — xem lại ngay nếu cần điều chỉnh."
                    ),
                    reference_doctype=SLIP_DOCTYPE,
                    reference_name=r["name"],
                    action_url="/payslips",
                )
            except Exception:
                pass
    except Exception:
        pass

    rows = frappe.get_all(
        SLIP_DOCTYPE,
        filters={
            "vn_employee_visible": 1,
            "docstatus": ["<", 2],
            "vn_ack_status": "",
            "vn_visible_at": ["<", cutoff],
        },
        fields=["name", "employee"],
        limit_page_length=1000,
    )
    auto = missing = 0
    missing_names: list[str] = []
    for r in rows or []:
        default_bank = frappe.db.get_value(BANK_DOCTYPE, {"employee": r["employee"], "is_default": 1}, "name")
        if not default_bank:
            missing += 1
            missing_names.append(r["name"])
            continue
        try:
            confirm_payslip(name=r["name"], bank_account=default_bank, source="Auto")
            auto += 1
        except Exception:
            frappe.log_error(title="VN Payslip auto-confirm failed", message=f"{r['name']}")
    if missing_names:
        _notify_hr_managers(
            None,
            _("Phiếu lương đến hạn thiếu tài khoản"),
            _(
                "{0} phiếu đã quá {1} ngày nhưng nhân viên chưa có tài khoản nhận lương mặc định — không thể tự xác nhận."
            ).format(missing, days),
            "Salary Slip",
            missing_names[0],
        )
    return {
        "processed": len(rows or []),
        "auto_confirmed": auto,
        "missing_bank": missing,
        "days": days,
    }


@frappe.whitelist()
def get_period_ack_progress(period: str | None = None) -> dict:
    """Ack progress for the Review 🔒 badge (visible slips only). Manager only."""
    _assert_manager()
    from gege_hr.gege_hr.utils import payroll as calc

    return calc.period_ack_progress((period or "").strip())


@frappe.whitelist()
def mark_payslip_paid(name: str | None = None, proof_file: str | None = None) -> dict:
    """Manager confirms the transfer — proof image is MANDATORY (#4)."""
    _assert_manager()
    slip = _get_slip(name)
    if slip.vn_ack_status != ACK_AWAITING:
        frappe.throw(_("Chỉ phiếu lương ở trạng thái Chờ thanh toán mới được đánh dấu đã chuyển."))

    proof = (proof_file or "").strip()
    if not proof:
        frappe.throw(_("Vui lòng đính kèm ảnh biên lai chuyển khoản."))
    if not str(proof).startswith("/files/") and not str(proof).startswith("/private/files/"):
        frappe.throw(_("Ảnh biên lai không hợp lệ."))

    stamp = {
        "vn_ack_status": ACK_PAID,
        "vn_payment_proof": proof,
        "vn_paid_at": now_datetime(),
        "vn_paid_by": frappe.session.user,
    }
    frappe.db.set_value(SLIP_DOCTYPE, slip.name, stamp)
    audit_api.log(
        "Payslip Paid",
        doc={"doctype": SLIP_DOCTYPE, "name": slip.name, **stamp},
        work_date=slip.start_date,
        description=f"{slip.employee} → {slip.vn_payment_ref}",
        old_value=ACK_AWAITING,
        new_value=ACK_PAID,
    )
    try:
        from gege_hr.gege_hr.utils import notify

        notify.push_notification(
            employee=slip.employee,
            notification_type="Payroll",
            title=_("Lương đã được chuyển"),
            message=_("Lương kỳ {0} đã được chuyển tới tài khoản của bạn.").format(str(slip.start_date)[:7]),
            reference_doctype=SLIP_DOCTYPE,
            reference_name=slip.name,
            action_url="/payslips",
        )
    except Exception:
        pass
    _publish_payslip_updated(slip)
    return {
        "name": slip.name,
        "status": ACK_PAID,
        "message": _("Đã ghi nhận chuyển lương cho {0}.").format(slip.employee_name or slip.employee),
    }
