"""Chấm công trên máy công ty qua gege-seat (plans/plan-cham-cong-pc-gege-seat.md).

Bằng chứng "đang ngồi đúng máy công ty" là một VÉ dùng một lần: trang HR (trình
duyệt trên máy) xin dịch vụ gege-seat của máy (``127.0.0.1:47150`` — máy khác
không gọi được) → dịch vụ xin console (API khoá theo IP máy) → trang gửi vé
kèm lệnh chấm công → máy chủ HR hỏi console ``/api/seat/hr/verify`` (console
chỉ nhận IP máy chủ HR + khoá bí mật). Console trả ai đang trong ca ở máy nào
và ca có phải từ xa (Moonlight) không; :func:`evaluate` quyết định cho chấm.
"""

from __future__ import annotations

from urllib.parse import urlparse

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


def console_config() -> tuple[str, str | None]:
    """``(địa chỉ console, khoá bí mật)`` từ VN HR Portal Setting — ``("", None)`` khi chưa cấu hình."""
    if frappe is None:
        return "", None
    try:
        from frappe.utils.password import get_decrypted_password

        setting = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
        base = (setting.get("seat_console_url") or "").strip().rstrip("/")
        key = get_decrypted_password(
            "VN HR Portal Setting", "VN HR Portal Setting", "seat_console_key", raise_exception=False
        )
        return base, key
    except Exception:
        return "", None


def console_post(path: str, payload: dict) -> dict:
    """POST tới console gege-seat (kèm ``X-Seat-Key``). Không ném lỗi — trả ``{"ok": False, "msg": …}``."""
    base, key = console_config()
    if not base or not key:
        return {"ok": False, "msg": "Chưa cấu hình kết nối gege-seat (địa chỉ console / khoá)."}
    try:
        import requests

        resp = requests.post(base + path, json=payload, headers={"X-Seat-Key": key}, timeout=TIMEOUT_S)
        if resp.status_code != 200:
            return {"ok": False, "msg": f"Console từ chối yêu cầu (HTTP {resp.status_code})."}
        data = resp.json()
        return data if isinstance(data, dict) else {"ok": False, "msg": "Console trả dữ liệu lạ."}
    except Exception:
        return {"ok": False, "msg": "Không liên lạc được console gege-seat — thử lại sau."}


def verify_ticket(ticket: str, log_type: str | None = None) -> dict:
    """Hỏi console kiểm vé (dùng một lần). Không ném lỗi — trả ``{"ok": False, "msg": …}``."""
    return console_post(VERIFY_PATH, {"ticket": str(ticket or "")[:64], "log_type": log_type or ""})


# --------------------------------------------------------------------------- #
# Cầu nối HR ↔ console (api/seat_bridge.py) — helper thuần
# --------------------------------------------------------------------------- #
def console_host(url: str | None) -> str:
    """Tên máy / IP trong địa chỉ console (``http://192.168.2.90:8080`` → ``192.168.2.90``)."""
    try:
        return (urlparse((url or "").strip()).hostname or "").lower()
    except Exception:
        return ""


def caller_allowed(
    request_ip: str | None, header_key: str | None, console_url: str | None, key: str | None
) -> bool:
    """Chỉ console thật mới gọi được: đúng IP trong ``seat_console_url`` VÀ đúng khoá."""
    import hmac

    host = console_host(console_url)
    if not host or not key or not header_key:
        return False
    if (request_ip or "").strip().lower() != host:
        return False
    return hmac.compare_digest(str(header_key), str(key))


def build_accounts(users: list[dict], employees: list[dict]) -> list[dict]:
    """Ghép User + Employee → dòng đồng bộ cho console.

    ``eligible`` = User đang bật VÀ có hồ sơ nhân viên ``Active``. User không có
    hồ sơ nhân viên (tài khoản quản trị…) trả ``employee_status = None`` để
    console biết KHÔNG được tự khoá. Một email nhiều hồ sơ → ưu tiên ``Active``.
    """
    by_email: dict[str, dict] = {}
    for e in employees or []:
        email = str(e.get("user_id") or "").strip().lower()
        if not email:
            continue
        cur = by_email.get(email)
        if cur is None or (e.get("status") == "Active" and cur.get("status") != "Active"):
            by_email[email] = e
    out = []
    for u in users or []:
        email = str(u.get("name") or "").strip().lower()
        if not email or "@" not in email:
            continue
        emp = by_email.get(email)
        enabled = bool(int(u.get("enabled") or 0))
        status = emp.get("status") if emp else None
        out.append(
            {
                "email": email,
                "full_name": u.get("full_name") or (emp or {}).get("employee_name") or "",
                "user_enabled": enabled,
                "employee_status": status,
                "department": (emp or {}).get("department") or "",
                "eligible": bool(enabled and status == "Active"),
            }
        )
    return sorted(out, key=lambda r: r["email"])


def parse_qr_code(code: str | None) -> str:
    """Mã QR hợp lệ: 8–64 ký tự chữ/số/``-``/``_`` (token urlsafe của console)."""
    c = str(code or "").strip()
    if 8 <= len(c) <= 64 and all(ch.isalnum() or ch in "-_" for ch in c):
        return c
    return ""


# --------------------------------------------------------------------------- #
# Đăng nhập console bằng mật khẩu HR — giấy xác nhận dùng một lần (helper thuần)
# --------------------------------------------------------------------------- #
ASSERTION_TTL_S = 60
PW_FAIL_LIMIT = 5
PW_FAIL_WINDOW_S = 600
AUD_CONSOLE_WEB = "console-web"


def valid_audience(aud: str | None) -> str:
    """``console-web`` (trang web console) hoặc ``seat:<máy>`` (màn hình vào ca). Sai → ``""``."""
    a = str(aud or "").strip()
    if a == AUD_CONSOLE_WEB:
        return a
    if a.startswith("seat:"):
        host = a[5:]
        if 1 <= len(host) <= 32 and all(ch.isalnum() or ch in "-_." for ch in host):
            return a
    return ""


def assertion_valid(record: dict | None, aud: str | None, now_ts: float) -> bool:
    """Giấy xác nhận còn hạn (≤ 60 giây) và đúng nơi dùng (``aud``)."""
    if not record or not record.get("email"):
        return False
    if str(record.get("aud") or "") != str(aud or ""):
        return False
    try:
        age = float(now_ts) - float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= ASSERTION_TTL_S


def console_redirect(web_url: str | None, assertion: str | None = None, error: str | None = None) -> str:
    """Địa chỉ chuyển hướng về trang web console — CHỈ dựa trên cấu hình (không nhận URL từ người gọi).

    Thành công → ``<web>/api/sso?a=<giấy>``; lỗi → ``<web>/?login_err=<thông báo>``. Cấu hình trống / không
    phải http(s) → ``""`` (người gọi báo lỗi thay vì chuyển hướng lung tung).
    """
    from urllib.parse import quote

    base = (web_url or "").strip().rstrip("/")
    if not base.lower().startswith(("https://", "http://")) or not urlparse(base).hostname:
        return ""
    if assertion:
        return f"{base}/api/sso?a={quote(str(assertion), safe='')}"
    return f"{base}/?login_err={quote(str(error or 'Đăng nhập thất bại'), safe='')}"
