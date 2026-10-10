"""Cầu nối HR ↔ console gege-seat (plans/plan-tai-khoan-hr-cho-console.md).

Hai chiều, cùng một khoá bí mật ``VN HR Portal Setting.seat_console_key``:

* **console → HR** (không có phiên đăng nhập; chỉ nhận đúng IP console + header
  ``X-Seat-Key``): :func:`list_accounts` — danh sách tài khoản để console đồng
  bộ (HR là nguồn quyết định AI có tài khoản; vai trò / quyền máy chia ở console).
* **app HR → console** (nhân viên đã đăng nhập HR): :func:`seat_qr_info` /
  :func:`seat_qr_approve` — quét mã QR trên màn hình vào ca để bắt đầu / tiếp
  tục ca. Console tự kiểm quyền máy; HR chỉ khẳng định danh tính. Cùng hai hàm
  này nhận cả mã đăng nhập trang web do HR tự cấp (Gege Forum — ``api/web_qr.py``).
* **đăng nhập console bằng mật khẩu HR** — mật khẩu KHÔNG đi qua console:
  trình duyệt (:func:`console_login`, form POST → chuyển hướng) hoặc dịch vụ
  gege-seat của máy (:func:`login_assertion`) gửi thẳng tới HR qua HTTPS, HR
  cấp *giấy xác nhận* dùng một lần (60 giây); console đổi giấy lấy danh tính
  qua :func:`verify_assertion` (chỉ console gọi được).

Helper thuần (test không cần bench) nằm ở ``utils/seat_checkin.py``.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api import web_qr
from gege_hr.gege_hr.utils import seat_checkin
from gege_hr.gege_hr.utils.seat_checkin import build_accounts, caller_allowed, parse_qr_code

QR_INFO_PATH = "/api/seat/hr/qr-info"
QR_APPROVE_PATH = "/api/seat/hr/qr-approve"


# --------------------------------------------------------------------------- #
# console → HR
# --------------------------------------------------------------------------- #
def _require_console() -> None:
    base, key = seat_checkin.console_config()
    ok = caller_allowed(
        getattr(frappe.local, "request_ip", None),
        frappe.get_request_header("X-Seat-Key"),
        base,
        key,
    )
    if not ok:
        frappe.throw(_("Không phải console gege-seat."), frappe.PermissionError)


@frappe.whitelist(allow_guest=True)
def list_accounts() -> dict:
    """Danh sách tài khoản HR cho console đồng bộ (chỉ console gọi được)."""
    _require_console()
    users = frappe.get_all(
        "User",
        filters={"user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
        fields=["name", "full_name", "enabled"],
        limit_page_length=0,
    )
    employees = frappe.get_all(
        "Employee",
        filters={"user_id": ["is", "set"]},
        fields=["user_id", "status", "employee_name", "department"],
        limit_page_length=0,
    )
    return {"ok": True, "accounts": build_accounts(users, employees)}


# --------------------------------------------------------------------------- #
# app HR → console (quét QR vào ca)
# --------------------------------------------------------------------------- #
def _employee_user() -> str:
    """Người gọi là nhân viên đang làm → trả email đăng nhập."""
    user = frappe.session.user
    if not user or user in ("Guest", "Administrator"):
        frappe.throw(_("Vui lòng đăng nhập bằng tài khoản nhân viên."), frappe.PermissionError)
    if not frappe.db.exists("Employee", {"user_id": user, "status": "Active"}):
        frappe.throw(_("Tài khoản chưa được liên kết nhân viên đang làm việc."), frappe.ValidationError)
    return user


def _qr_guard() -> str:
    """Mã của console (vào ca / đăng nhập console): tính năng phải bật + người gọi là nhân viên đang làm."""
    setting = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
    if not setting.get("enable_seat_qr_login"):
        frappe.throw(_("Vào ca bằng QR chưa được bật."), frappe.ValidationError)
    return _employee_user()


def _code_or_throw(code: str | None) -> str:
    c = parse_qr_code(code)
    if not c:
        frappe.throw(_("Mã QR không hợp lệ."), frappe.ValidationError)
    return c


@frappe.whitelist()
def seat_qr_info(code: str | None = None) -> dict:
    """Mã QR này của máy nào, máy đang trống hay có ca của ai (chưa tiêu mã)."""
    c = _code_or_throw(code)
    _employee_user()
    # Mã đăng nhập trang web do chính HR cấp (Gege Forum) — không hỏi console.
    local = web_qr.peek(c)
    if local is not None:
        return local
    _qr_guard()
    res = seat_checkin.console_post(QR_INFO_PATH, {"code": c})
    if not res.get("ok"):
        frappe.throw(_(res.get("msg") or "Mã QR không hợp lệ hoặc đã hết hạn."), frappe.ValidationError)
    # kind="web": mã của trang đăng nhập console (ip = trình duyệt đang xin đăng nhập); còn lại là màn hình vào ca.
    return {k: res.get(k) for k in ("kind", "host", "state", "user", "group", "ip")}


@frappe.whitelist()
def seat_qr_approve(code: str | None = None) -> dict:
    """Xác nhận vào ca / tiếp tục ca trên máy đã quét. Console kiểm quyền máy và mở ca."""
    from gege_hr.gege_hr.utils.ratelimit import rate_limit

    c = _code_or_throw(code)
    user = _employee_user()
    rate_limit(f"seatqr:{user}", max_requests=6, window_seconds=60)
    local = web_qr.approve(c, user)
    if local is not None:
        return local
    _qr_guard()
    res = seat_checkin.console_post(QR_APPROVE_PATH, {"code": c, "username": user})
    if not res.get("ok"):
        frappe.throw(_(res.get("msg") or "Không vào ca được."), frappe.ValidationError)
    return {k: res.get(k) for k in ("host", "action", "msg")}


# --------------------------------------------------------------------------- #
# Đăng nhập console bằng mật khẩu HR (giấy xác nhận dùng một lần)
# --------------------------------------------------------------------------- #
_ASSERT_KEY = "gege_seat_assert:"
_FAIL_KEY = "gege_seat_pwfail:"
_BAD_LOGIN = "Sai email hoặc mật khẩu."


def _issue_assertion(usr: str | None, pwd: str | None, aud: str | None) -> tuple[str | None, str]:
    """Kiểm email + mật khẩu HR → ``(giấy xác nhận, "")`` hoặc ``(None, lý do)``. Không tạo phiên HR."""
    import secrets
    import time

    from frappe.utils.password import check_password

    audience = seat_checkin.valid_audience(aud)
    email = str(usr or "").strip().lower()
    if not audience or not email or "@" not in email or not pwd:
        return None, _BAD_LOGIN
    cache = frappe.cache()
    fail_key = _FAIL_KEY + email
    fails = int(cache.get_value(fail_key) or 0)
    if fails >= seat_checkin.PW_FAIL_LIMIT:
        return None, "Sai quá nhiều lần — đợi 10 phút rồi thử lại."

    user = frappe.db.get_value("User", {"name": email, "enabled": 1, "user_type": "System User"}, "name")
    ok = False
    if user:
        try:
            check_password(user, str(pwd))
            ok = True
        except Exception:
            ok = False
    if not ok:
        cache.set_value(fail_key, fails + 1, expires_in_sec=seat_checkin.PW_FAIL_WINDOW_S)
        return None, _BAD_LOGIN
    if not frappe.db.exists("Employee", {"user_id": user, "status": "Active"}):
        return None, "Tài khoản chưa được liên kết nhân viên đang làm việc."

    cache.delete_value(fail_key)
    token = secrets.token_urlsafe(24)
    cache.set_value(
        _ASSERT_KEY + token,
        {"email": user.lower(), "aud": audience, "ts": time.time()},
        expires_in_sec=seat_checkin.ASSERTION_TTL_S,
    )
    return token, ""


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login_assertion(usr: str | None = None, pwd: str | None = None, aud: str | None = None) -> dict:
    """Dịch vụ gege-seat của máy đổi email + mật khẩu HR lấy giấy xác nhận (JSON)."""
    token, msg = _issue_assertion(usr, pwd, aud)
    return {"ok": True, "assertion": token} if token else {"ok": False, "msg": msg}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def console_login(usr: str | None = None, pwd: str | None = None) -> None:
    """Form đăng nhập của trang web console gửi thẳng tới đây (HTTPS) → chuyển hướng về console.

    Đích chuyển hướng lấy từ cấu hình ``seat_console_web_url`` — không nhận URL từ người gọi.
    """
    setting = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
    web = setting.get("seat_console_web_url")
    token, msg = _issue_assertion(usr, pwd, seat_checkin.AUD_CONSOLE_WEB)
    target = seat_checkin.console_redirect(web, assertion=token, error=msg)
    if not target:
        frappe.throw(_("Chưa cấu hình địa chỉ trang web console."), frappe.ValidationError)
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = target


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_assertion(assertion: str | None = None, aud: str | None = None) -> dict:
    """Console đổi giấy xác nhận lấy danh tính (dùng một lần; chỉ console gọi được)."""
    import time

    _require_console()
    key = _ASSERT_KEY + str(assertion or "")[:64]
    cache = frappe.cache()
    record = cache.get_value(key)
    cache.delete_value(key)
    if not seat_checkin.assertion_valid(record, seat_checkin.valid_audience(aud), time.time()):
        return {"ok": False, "msg": "Giấy xác nhận đăng nhập không hợp lệ hoặc đã hết hạn."}
    return {"ok": True, "email": record["email"]}
