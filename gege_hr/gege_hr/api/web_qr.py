"""Đăng nhập trang web khác (Gege Forum) bằng tài khoản HR: mã QR quét từ app HR, hoặc email + mật khẩu HR nhập
ngay trên trang đó (plans/plan-forum-qr-login.md).

Forum đăng nhập qua OIDC của HR, nên "đăng nhập forum" = trình duyệt có phiên HR. Luồng:

1. Trang đăng nhập forum (JS của theme — khác origin, KHÔNG gửi cookie HR) gọi :func:`start` → ma trận QR +
   khoá hỏi kết quả (chỉ trình duyệt đó giữ).
2. Nhân viên quét bằng app HR → trang ``/hr/seat-login`` → ``seat_bridge.seat_qr_info`` / ``seat_qr_approve``
   → :func:`peek` / :func:`approve` ở đây (mã do HR tự cấp, không hỏi console).
3. Trang forum hỏi :func:`status`; đã xác nhận thì nhận *vé hoàn tất* (một lần, 60 giây).
4. Trình duyệt mở :func:`finish` → HR tạo phiên cho ĐÚNG người đã xác nhận rồi chuyển về forum; forum đi tiếp
   đường OIDC sẵn có.

Kết quả giống hệt nhập mật khẩu HR ở bước OIDC (trình duyệt có phiên HR) — chỉ khác cách xác thực. Rủi ro riêng
của QR: ai đó đưa mã của HỌ cho nhân viên quét; vì vậy màn xác nhận hiện IP + trình duyệt đang xin đăng nhập, mã
sống 150 giây, dùng một lần, và tính năng mặc định TẮT (``enable_forum_qr_login``).

Email + mật khẩu HR nhập ở form của forum đi cùng đường: :func:`password_start` (khác origin, không cookie) kiểm mật
khẩu rồi trả luôn vé hoàn tất → :func:`finish`. KHÔNG dùng form POST thẳng tới HR: trình duyệt đang có phiên HR sẽ gửi
kèm cookie (forum và HR cùng site) và Frappe từ chối mọi POST có phiên mà thiếu CSRF token.

Helper thuần (test không cần bench) nằm ở ``utils/web_qr.py``.
"""

from __future__ import annotations

import json
import secrets
import time

import frappe
from frappe import _
from werkzeug.wrappers import Response

from gege_hr.gege_hr.utils import web_qr
from gege_hr.gege_hr.utils.seat_checkin import parse_qr_code

_CODE = "gege_webqr:c:"
_POLL = "gege_webqr:p:"
_FIN = "gege_webqr:f:"
_ONCE = "gege_hr:webqr:once:"
_FAIL_KEY = (
    "gege_seat_pwfail:"  # đếm lần sai mật khẩu theo email — chung với giấy xác nhận console / gege-seat
)
FLAG_QR = "enable_forum_qr_login"
FLAG_PASSWORD = "enable_forum_password_login"


def _setting():
    return frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")


def enabled(flag: str = FLAG_QR) -> bool:
    try:
        return bool(_setting().get(flag))
    except Exception:
        return False


def _app(app: str | None, flag: str | None = FLAG_QR) -> dict | None:
    """Cấu hình trang web được phép. ``flag`` = công tắc phải đang bật; ``None`` = chỉ kiểm tên trang."""
    if flag and not enabled(flag):
        return None
    return web_qr.app_config(app, _setting().get("forum_url"))


def _client_ip() -> str:
    return web_qr.client_ip(
        getattr(frappe.local, "request_ip", None), frappe.get_request_header("CF-Connecting-IP")
    )


def _json(payload: dict, origin: str | None = None) -> Response:
    """Trả JSON thô. ``origin`` = cho đúng trang đó đọc (CORS, không kèm cookie); bỏ trống = trang khác không đọc được."""
    res = Response(json.dumps(payload), mimetype="application/json")
    res.headers["Cache-Control"] = "no-store"
    if origin:
        res.headers["Access-Control-Allow-Origin"] = origin
        res.headers["Vary"] = "Origin"
    return res


def _once(key: str, ttl: int) -> bool:
    """Chỉ lượt gọi ĐẦU TIÊN với ``key`` nhận True (Redis INCR — không chạy đua)."""
    cache = frappe.cache()
    full = _ONCE + key
    first = int(cache.incr(full)) == 1
    if first:
        cache.expire(full, ttl)
    return first


def _employee_user(email: str | None) -> str | None:
    """Tài khoản còn bật + gắn nhân viên đang làm → tên User chuẩn; không thì None."""
    user = frappe.db.get_value(
        "User", {"name": str(email or ""), "enabled": 1, "user_type": "System User"}, "name"
    )
    if user and frappe.db.exists("Employee", {"user_id": user, "status": "Active"}):
        return user
    return None


# --------------------------------------------------------------------------- #
# Trang web xin mã / hỏi kết quả (khách, khác origin)
# --------------------------------------------------------------------------- #
@frappe.whitelist(allow_guest=True, methods=["POST"])
def start(app: str | None = None):
    """Trang đăng nhập của ``app`` xin mã QR mới."""
    from gege_hr.gege_hr.utils.ratelimit import rate_limit

    cfg = _app(app)
    origin = frappe.get_request_header("Origin")
    if not cfg or origin != cfg["origin"]:
        return _json({"ok": False})
    ip = _client_ip()
    try:
        # cả văn phòng ra internet bằng một IP → giới hạn rộng; chỉ để chặn kiểu gọi dồn dập
        rate_limit(f"webqr:new:{ip}", max_requests=60, window_seconds=60)
    except Exception:
        return _json({"ok": False, "msg": "Quá nhiều yêu cầu — thử lại sau một phút."}, origin)
    base = web_qr.public_base(frappe.local.request.host)
    if not base:
        return _json({"ok": False}, origin)

    import pyqrcode

    code = secrets.token_urlsafe(12)
    poll = secrets.token_urlsafe(24)
    record = web_qr.new_record(
        cfg["app"], ip, web_qr.device_label(frappe.get_request_header("User-Agent")), time.time()
    )
    cache = frappe.cache()
    cache.set_value(_CODE + code, record, expires_in_sec=web_qr.CODE_TTL_S + web_qr.APPROVED_GRACE_S)
    cache.set_value(_POLL + poll, code, expires_in_sec=web_qr.CODE_TTL_S + web_qr.APPROVED_GRACE_S)
    matrix = web_qr.matrix_rows(pyqrcode.create(web_qr.scan_url(base, code), error="M").code)
    return _json(
        {"ok": True, "matrix": matrix, "poll": poll, "ttl": web_qr.CODE_TTL_S, "title": cfg["title"]}, origin
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])
def status(poll: str | None = None, app: str | None = None):
    """Trình duyệt giữ khoá ``poll`` hỏi kết quả: ``wait`` / ``gone`` / ``ok`` kèm vé hoàn tất (chỉ trả một lần)."""
    cfg = _app(app)
    origin = frappe.get_request_header("Origin")
    if not cfg or origin != cfg["origin"]:
        return _json({"state": web_qr.STATE_GONE})
    key = parse_qr_code(poll)
    cache = frappe.cache()
    code = cache.get_value(_POLL + key) if key else None
    record = cache.get_value(_CODE + str(code)) if code else None
    now = time.time()
    state = web_qr.poll_state(record, now)
    if state != web_qr.STATE_OK or record.get("app") != cfg["app"]:
        return _json({"state": web_qr.STATE_GONE if state == web_qr.STATE_OK else state}, origin)
    if not _once("p:" + key, web_qr.CODE_TTL_S):
        return _json({"state": web_qr.STATE_GONE}, origin)
    token = secrets.token_urlsafe(24)
    cache.set_value(
        _FIN + token,
        web_qr.finish_record(record["user"], record["app"], record.get("ip"), web_qr.VIA_QR, now),
        expires_in_sec=web_qr.FINISH_TTL_S,
    )
    cache.delete_value(_CODE + str(code))
    cache.delete_value(_POLL + key)
    return _json({"state": web_qr.STATE_OK, "t": token}, origin)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def finish(t: str | None = None, app: str | None = None) -> None:
    """Trình duyệt đổi vé hoàn tất lấy phiên HR rồi quay về trang web (đích lấy từ cấu hình, không nhận từ URL)."""
    from frappe.core.doctype.activity_log.activity_log import add_authentication_log

    cfg = _app(app, None)
    if not cfg or not (enabled(FLAG_QR) or enabled(FLAG_PASSWORD)):
        frappe.throw(_("Đăng nhập từ trang này chưa được bật."), frappe.ValidationError)
    token = parse_qr_code(t)
    record = None
    if token and _once("f:" + token, web_qr.FINISH_TTL_S):
        cache = frappe.cache()
        record = cache.get_value(_FIN + token)
        cache.delete_value(_FIN + token)

    target = cfg["fail"]
    via_flag = FLAG_PASSWORD if (record or {}).get("via") == web_qr.VIA_PASSWORD else FLAG_QR
    if web_qr.finish_valid(record, cfg["app"], time.time()) and enabled(via_flag):
        user = _employee_user(record["user"])
        if user:
            try:
                frappe.local.login_manager.login_as(user)
                add_authentication_log(web_qr.finish_log_subject(record, cfg["title"]), user)
                frappe.db.commit()  # yêu cầu GET: Frappe không tự commit — phiên phải được lưu trước khi chuyển hướng
                target = cfg["done"]
            except Exception:
                frappe.db.rollback()
                frappe.log_error(title="web_qr.finish")
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = target


# --------------------------------------------------------------------------- #
# Email + mật khẩu HR nhập ngay trên trang web đó
# --------------------------------------------------------------------------- #
def check_hr_password(usr: str | None, pwd: str | None) -> tuple[str | None, str]:
    """Kiểm email + mật khẩu HR → ``(User, "")`` hoặc ``(None, mã)`` với mã ``bad`` / ``lock`` / ``emp``.

    Không tạo phiên. Sai 5 lần / 10 phút theo email thì khoá tạm (bộ đếm dùng chung với giấy xác nhận của console
    và màn hình vào ca). Chỉ nhân viên đang làm; mật khẩu đúng mà không phải nhân viên → ``emp``.
    """
    from frappe.utils.password import check_password

    from gege_hr.gege_hr.utils import seat_checkin

    email = web_qr.login_email(usr)
    if not email or not pwd:
        return None, web_qr.PW_BAD
    cache = frappe.cache()
    fail_key = _FAIL_KEY + email
    fails = int(cache.get_value(fail_key) or 0)
    if fails >= seat_checkin.PW_FAIL_LIMIT:
        return None, web_qr.PW_LOCK

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
        return None, web_qr.PW_BAD
    if not frappe.db.exists("Employee", {"user_id": user, "status": "Active"}):
        return None, web_qr.PW_EMP
    cache.delete_value(fail_key)
    return user, web_qr.PW_OK


@frappe.whitelist(allow_guest=True, methods=["POST"])
def features(app: str | None = None):
    """Trang đăng nhập của ``app`` hỏi HR đang cho những cách nào: ``{"qr": bool, "password": bool}``."""
    cfg = _app(app, None)
    origin = frappe.get_request_header("Origin")
    if not cfg or origin != cfg["origin"]:
        return _json({})
    return _json({"qr": enabled(FLAG_QR), "password": enabled(FLAG_PASSWORD), "title": cfg["title"]}, origin)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def password_start(usr: str | None = None, pwd: str | None = None, app: str | None = None):
    """Form đăng nhập của ``app`` gửi email + mật khẩu HR (khác origin, không cookie) → vé hoàn tất cho :func:`finish`."""
    from gege_hr.gege_hr.utils.ratelimit import rate_limit

    cfg = _app(app, FLAG_PASSWORD)
    origin = frappe.get_request_header("Origin")
    if not cfg or origin != cfg["origin"]:
        return _json({"ok": False})
    ip = _client_ip()
    try:
        rate_limit(f"webpw:{ip}", max_requests=30, window_seconds=60)
    except Exception:
        return _json(
            {"ok": False, "code": web_qr.PW_LOCK, "msg": "Quá nhiều yêu cầu — thử lại sau một phút."}, origin
        )
    user, code = check_hr_password(usr, pwd)
    if not user:
        return _json({"ok": False, "code": code, "msg": web_qr.pw_message(code)}, origin)
    token = secrets.token_urlsafe(24)
    frappe.cache().set_value(
        _FIN + token,
        web_qr.finish_record(user, cfg["app"], ip, web_qr.VIA_PASSWORD, time.time()),
        expires_in_sec=web_qr.FINISH_TTL_S,
    )
    return _json({"ok": True, "t": token}, origin)


# --------------------------------------------------------------------------- #
# App HR (người quét — đã đăng nhập): seat_bridge gọi sang
# --------------------------------------------------------------------------- #
def peek(code: str) -> dict | None:
    """Mã này có phải mã đăng nhập web do HR cấp không → thông tin cho màn xác nhận; không phải → None."""
    if not enabled():
        return None
    record = frappe.cache().get_value(_CODE + code)
    cfg = web_qr.app_config(record.get("app"), _setting().get("forum_url")) if record else None
    if not cfg or not web_qr.record_age_ok(record, time.time()):
        return None
    return web_qr.info_view(record, cfg["title"])


def approve(code: str, user: str) -> dict | None:
    """Người quét xác nhận. ``None`` = không phải mã của HR (để seat_bridge hỏi console)."""
    if not enabled():
        return None
    cache = frappe.cache()
    record = cache.get_value(_CODE + code)
    cfg = web_qr.app_config(record.get("app"), _setting().get("forum_url")) if record else None
    if not cfg:
        return None
    now = time.time()
    updated, msg = web_qr.approve(record, user, now)
    if updated and not _once("a:" + code, web_qr.CODE_TTL_S):
        updated, msg = None, "Mã này đã được dùng — tải lại trang đăng nhập để lấy mã mới."
    if not updated:
        frappe.throw(_(msg), frappe.ValidationError)
    cache.set_value(_CODE + code, updated, expires_in_sec=web_qr.remaining_ttl(record, now))
    return {
        "host": cfg["title"],
        "action": "web",
        "msg": f"Đã xác nhận — trình duyệt sẽ tự vào {cfg['title']}.",
    }
