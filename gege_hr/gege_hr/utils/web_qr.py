"""Đăng nhập trang web khác (Gege Forum) bằng mã QR quét từ app HR — helper thuần.

Thiết kế: ``plans/plan-forum-qr-login.md``. Không import frappe ở đây (bộ test chạy không cần bench);
phần gọi Redis / tạo phiên nằm ở ``api/web_qr.py``.
"""

from __future__ import annotations

from urllib.parse import urlparse

CODE_TTL_S = 150  # mã QR sống (trang đăng nhập tự xin mã mới trước khi hết)
APPROVED_GRACE_S = 30  # đã xác nhận ở giây cuối → trình duyệt vẫn kịp hỏi kết quả
FINISH_TTL_S = 60  # vé hoàn tất: trình duyệt phải đổi ngay sau khi được xác nhận
APP_FORUM = "forum"
DEFAULT_FORUM_URL = "https://forum.gegeteam.xyz"
STATE_WAIT, STATE_OK, STATE_GONE = "wait", "ok", "gone"


def normalize_origin(url: str | None) -> str:
    """``https://forum.gegeteam.xyz/abc?x`` → ``https://forum.gegeteam.xyz``. Chỉ http(s); sai → ``""``."""
    raw = str(url or "").strip()
    try:
        p = urlparse(raw)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return ""
    return f"{p.scheme}://{p.netloc}".lower()


def app_config(app: str | None, forum_url: str | None = None) -> dict | None:
    """Trang web được phép xin mã. Đích chuyển hướng CHỈ lấy từ đây — không nhận URL từ người gọi."""
    if str(app or "") != APP_FORUM:
        return None
    base = normalize_origin(forum_url) or DEFAULT_FORUM_URL
    return {
        "app": APP_FORUM,
        "title": "Gege Forum",
        "origin": base,
        "done": f"{base}/login?gege_qr=1",
        "fail": f"{base}/login?gege_qr=err",
    }


def public_base(host: str | None) -> str:
    """Địa chỉ công khai của HR để in vào mã QR.

    gunicorn đứng sau nginx + Cloudflare Tunnel nên luôn thấy ``http`` (nginx ghi đè ``X-Forwarded-Proto``);
    tên miền thì ép ``https`` (phiên HR trên điện thoại gắn với https), còn IP / localhost giữ ``http``.
    """
    h = str(host or "").strip().lower()
    if not h or any(ch in h for ch in "/\\@ ?#"):
        return ""
    name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    is_domain = "." in name and not name.replace(".", "").isdigit() and ":" not in h and name != "localhost"
    return f"https://{h}" if is_domain else f"http://{h}"


def scan_url(base: str, code: str) -> str:
    """Trang xác nhận trong app HR — cùng trang với mã vào ca / mã đăng nhập console."""
    return f"{base.rstrip('/')}/hr/seat-login?c={code}"


def matrix_rows(modules) -> list[str]:
    """Ma trận QR (danh sách hàng 0/1) → mỗi hàng một chuỗi ``'0'``/``'1'`` cho trình duyệt tự vẽ."""
    return ["".join("1" if cell else "0" for cell in row) for row in (modules or [])]


def client_ip(request_ip: str | None, cf_ip: str | None) -> str:
    """IP của trình duyệt đang xin đăng nhập (để người quét đối chiếu).

    Qua Cloudflare Tunnel nginx chỉ thấy loopback → lấy ``CF-Connecting-IP``. Vào thẳng trong LAN thì dùng
    IP nginx thấy (header ``CF-Connecting-IP`` khi đó là do người gọi tự gửi, không tin).
    """
    ip = str(request_ip or "").strip()
    cf = str(cf_ip or "").strip()
    if (
        ip in ("::1", "127.0.0.1", "")
        and cf
        and len(cf) <= 45
        and all(ch.isalnum() or ch in ".:" for ch in cf)
    ):
        return cf
    return ip


def device_label(user_agent: str | None) -> str:
    """``Chrome trên Windows`` — đủ để người quét nhận ra máy mình, không cần chính xác tuyệt đối."""
    ua = str(user_agent or "")
    if not ua:
        return ""
    browser = next(
        (
            name
            for key, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("CocCoc", "Cốc Cốc"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in ua
        ),
        "Trình duyệt",
    )
    system = next(
        (
            name
            for key, name in (
                ("Windows", "Windows"),
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Mac OS X", "macOS"),
                ("Linux", "Linux"),
            )
            if key in ua
        ),
        "",
    )
    return f"{browser} trên {system}" if system else browser


def new_record(app: str, ip: str, device: str, now_ts: float) -> dict:
    return {"app": app, "ip": ip, "device": device, "ts": float(now_ts), "state": STATE_WAIT, "user": None}


def record_age_ok(record: dict | None, now_ts: float, ttl: int = CODE_TTL_S) -> bool:
    if not record:
        return False
    try:
        age = float(now_ts) - float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= ttl


def approve(record: dict | None, user: str | None, now_ts: float) -> tuple[dict | None, str]:
    """Người đã đăng nhập app HR xác nhận mã → ``(bản ghi mới, "")`` hoặc ``(None, lý do)``. Mỗi mã một lần."""
    if not record_age_ok(record, now_ts):
        return None, "Mã QR không hợp lệ hoặc đã hết hạn."
    if record.get("state") != STATE_WAIT or record.get("user"):
        return None, "Mã này đã được dùng — tải lại trang đăng nhập để lấy mã mới."
    email = str(user or "").strip().lower()
    if not email or email in ("guest", "administrator"):
        return None, "Vui lòng đăng nhập bằng tài khoản nhân viên."
    return dict(record, state=STATE_OK, user=email, approved_ts=float(now_ts)), ""


def remaining_ttl(record: dict, now_ts: float) -> int:
    """Số giây còn giữ bản ghi sau khi xác nhận (ít nhất ``APPROVED_GRACE_S``)."""
    left = CODE_TTL_S - (float(now_ts) - float(record.get("ts") or 0))
    return int(max(left, 0)) + APPROVED_GRACE_S


def poll_state(record: dict | None, now_ts: float) -> str:
    """Trình duyệt hỏi kết quả: ``wait`` (chưa ai quét) / ``ok`` (đã xác nhận) / ``gone`` (hết hạn, đã dùng)."""
    if not record or not record_age_ok(record, now_ts, CODE_TTL_S + APPROVED_GRACE_S):
        return STATE_GONE
    if record.get("state") == STATE_OK and record.get("user"):
        return STATE_OK
    return STATE_WAIT if record_age_ok(record, now_ts) else STATE_GONE


def info_view(record: dict, title: str) -> dict:
    """Thông tin cho màn xác nhận trên điện thoại — cùng dạng với mã của console (``kind = "web"``)."""
    return {
        "kind": "web",
        "app": record.get("app"),
        "title": title,
        "host": title,
        "state": None,
        "group": None,
        "ip": record.get("ip") or "",
        "device": record.get("device") or "",
        "user": record.get("user") or None,
    }


def finish_valid(record: dict | None, app: str | None, now_ts: float) -> bool:
    """Vé hoàn tất còn hạn (≤ 60 giây), đúng trang web, có người xác nhận."""
    if not record or not record.get("user") or str(record.get("app") or "") != str(app or ""):
        return False
    return record_age_ok(record, now_ts, FINISH_TTL_S)


# --------------------------------------------------------------------------- #
# Đăng nhập bằng email + mật khẩu HR nhập ngay trên trang web đó (không qua QR)
# --------------------------------------------------------------------------- #
PW_OK, PW_BAD, PW_LOCK, PW_EMP = "", "bad", "lock", "emp"
PW_MESSAGES = {
    PW_BAD: "Sai email hoặc mật khẩu.",
    PW_LOCK: "Sai quá nhiều lần — đợi 10 phút rồi thử lại.",
    PW_EMP: "Tài khoản chưa được liên kết nhân viên đang làm việc.",
}
VIA_QR, VIA_PASSWORD = "qr", "password"


def pw_message(code: str | None) -> str:
    """Lý do từ chối cho người dùng đọc (mã lạ → câu chung, không lộ gì thêm)."""
    return PW_MESSAGES.get(str(code or ""), PW_MESSAGES[PW_BAD])


def login_email(usr: str | None) -> str:
    """Email đăng nhập đã chuẩn hoá; không phải email → ``""`` (không tra cứu, không tính lần sai)."""
    email = str(usr or "").strip().lower()
    if not email or "@" not in email or len(email) > 140 or any(ch.isspace() for ch in email):
        return ""
    return email


def finish_record(user: str, app: str, ip: str | None, via: str, now_ts: float) -> dict:
    """Vé hoàn tất: ai, vào trang nào, xác thực bằng gì (``qr`` / ``password``) — để ghi nhật ký đúng."""
    return {"user": user, "app": app, "ip": ip or "", "via": via, "ts": float(now_ts)}


def finish_log_subject(record: dict, title: str) -> str:
    how = "mật khẩu HR nhập trên trang đó" if record.get("via") == VIA_PASSWORD else "QR"
    return f"Đăng nhập {title} bằng {how} (trình duyệt {record.get('ip') or '?'})"
