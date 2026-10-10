"""Chấm công trên máy công ty qua gege-seat (plans/plan-cham-cong-pc-gege-seat.md).

Bằng chứng "đang ngồi đúng máy công ty" là một VÉ dùng một lần: trang HR (trình
duyệt trên máy) xin dịch vụ gege-seat của máy (``127.0.0.1:47150`` — máy khác
không gọi được) → dịch vụ xin console (API khoá theo IP máy) → trang gửi vé
kèm lệnh chấm công → máy chủ HR hỏi console ``/api/seat/hr/verify`` (console
chỉ nhận IP máy chủ HR + khoá bí mật). Console trả ai đang trong ca ở máy nào
và ca có phải từ xa (Moonlight) không; :func:`evaluate` quyết định cho chấm.
"""

from __future__ import annotations

try:  # bench-free import guard (unit tests exercise ``evaluate`` only)
    import frappe
except Exception:  # pragma: no cover
    frappe = None

VERIFY_PATH = "/api/seat/hr/verify"
TIMEOUT_S = 5


def evaluate(result: dict | None, user_email: str | None) -> tuple[bool, str, str | None]:
    """Console verify result → ``(ok, message, host)`` — pure.

    Chỉ cho chấm khi vé hợp lệ, ca trên máy là của CHÍNH nhân viên đang đăng
    nhập HR (khớp email, không phân biệt hoa thường) và máy KHÔNG bị điều khiển
    từ xa (ca mở bằng Moonlight hoặc đang có phiên Moonlight điều khiển).
    """
    res = result or {}
    host = (res.get("host") or "").strip() or None
    if not res.get("ok"):
        return False, (res.get("msg") or "Vé chấm công không hợp lệ hoặc đã hết hạn."), host
    seat_user = (res.get("username") or "").strip().lower()
    me = (user_email or "").strip().lower()
    if not seat_user or not me or seat_user != me:
        return (
            False,
            f"Ca trên máy {host or '?'} đang thuộc tài khoản {res.get('username') or '?'} — "
            "bạn phải vào ca bằng tài khoản của chính mình mới chấm công được.",
            host,
        )
    if res.get("remote") or res.get("moonlight"):
        return (
            False,
            f"Máy {host or '?'} đang được điều khiển từ xa (Moonlight) — "
            "không chấm công trên máy công ty được.",
            host,
        )
    if not host:
        return False, "Console không cho biết máy nào cấp vé.", None
    return True, "", host


def verify_ticket(ticket: str, log_type: str | None = None) -> dict:
    """Hỏi console kiểm vé (dùng một lần). Không ném lỗi — trả ``{"ok": False, "msg": …}``."""
    if frappe is None:
        return {"ok": False, "msg": "Không có môi trường bench."}
    try:
        from frappe.utils.password import get_decrypted_password

        setting = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
        base = (setting.get("seat_console_url") or "").strip().rstrip("/")
        key = get_decrypted_password(
            "VN HR Portal Setting", "VN HR Portal Setting", "seat_console_key", raise_exception=False
        )
    except Exception:
        base, key = "", None
    if not base or not key:
        return {"ok": False, "msg": "Chưa cấu hình kết nối gege-seat (địa chỉ console / khoá)."}
    try:
        import requests

        resp = requests.post(
            base + VERIFY_PATH,
            json={"ticket": str(ticket or "")[:64], "log_type": log_type or ""},
            headers={"X-Seat-Key": key},
            timeout=TIMEOUT_S,
        )
        if resp.status_code != 200:
            return {"ok": False, "msg": f"Console từ chối kiểm vé (HTTP {resp.status_code})."}
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": False, "msg": "Console trả dữ liệu lạ."}
    except Exception:
        return {
            "ok": False,
            "msg": "Không liên lạc được console gege-seat — thử lại hoặc chấm bằng điện thoại.",
        }
