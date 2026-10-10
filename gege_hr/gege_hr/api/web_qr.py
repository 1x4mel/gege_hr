"""Đăng nhập trang web khác (Gege Forum) bằng mã QR quét từ app HR (plans/plan-forum-qr-login.md).

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


def _setting():
    return frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")


def enabled() -> bool:
    try:
        return bool(_setting().get("enable_forum_qr_login"))
    except Exception:
        return False


def _app(app: str | None) -> dict | None:
    """Cấu hình trang web được phép (tính năng phải đang bật)."""
    if not enabled():
        return None
    return web_qr.app_config(app, _setting().get("forum_url"))


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
    ip = web_qr.client_ip(
        getattr(frappe.local, "request_ip", None), frappe.get_request_header("CF-Connecting-IP")
    )
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
        {"user": record["user"], "app": record["app"], "ip": record.get("ip"), "ts": now},
        expires_in_sec=web_qr.FINISH_TTL_S,
    )
    cache.delete_value(_CODE + str(code))
    cache.delete_value(_POLL + key)
    return _json({"state": web_qr.STATE_OK, "t": token}, origin)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def finish(t: str | None = None, app: str | None = None) -> None:
    """Trình duyệt đổi vé hoàn tất lấy phiên HR rồi quay về trang web (đích lấy từ cấu hình, không nhận từ URL)."""
    from frappe.core.doctype.activity_log.activity_log import add_authentication_log

    cfg = _app(app)
    if not cfg:
        frappe.throw(_("Đăng nhập bằng QR chưa được bật."), frappe.ValidationError)
    token = parse_qr_code(t)
    record = None
    if token and _once("f:" + token, web_qr.FINISH_TTL_S):
        cache = frappe.cache()
        record = cache.get_value(_FIN + token)
        cache.delete_value(_FIN + token)

    target = cfg["fail"]
    if web_qr.finish_valid(record, cfg["app"], time.time()):
        user = _employee_user(record["user"])
        if user:
            try:
                frappe.local.login_manager.login_as(user)
                add_authentication_log(
                    f"Đăng nhập {cfg['title']} bằng QR (trình duyệt {record.get('ip') or '?'})", user
                )
                frappe.db.commit()  # yêu cầu GET: Frappe không tự commit — phiên phải được lưu trước khi chuyển hướng
                target = cfg["done"]
            except Exception:
                frappe.db.rollback()
                frappe.log_error(title="web_qr.finish")
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = target


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
