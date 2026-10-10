"""Đăng nhập Gege Forum bằng QR quét từ app HR — helper thuần (plans/plan-forum-qr-login.md)."""

from __future__ import annotations

from gege_hr.gege_hr.utils.web_qr import (
    APPROVED_GRACE_S,
    CODE_TTL_S,
    FINISH_TTL_S,
    app_config,
    approve,
    client_ip,
    device_label,
    finish_valid,
    info_view,
    matrix_rows,
    new_record,
    normalize_origin,
    poll_state,
    public_base,
    remaining_ttl,
    scan_url,
)

T0 = 1_000_000.0


def test_normalize_origin():
    assert normalize_origin("https://forum.gegeteam.xyz/login?x=1") == "https://forum.gegeteam.xyz"
    assert normalize_origin(" HTTPS://Forum.GegeTeam.xyz/ ") == "https://forum.gegeteam.xyz"
    assert normalize_origin("http://192.168.2.111:8080/a") == "http://192.168.2.111:8080"
    assert normalize_origin("javascript:alert(1)") == ""
    assert normalize_origin("https://user:pw@forum.gegeteam.xyz") == ""  # không nhận địa chỉ kèm tài khoản
    assert normalize_origin("forum.gegeteam.xyz") == ""
    assert normalize_origin("") == ""
    assert normalize_origin(None) == ""


def test_app_config_only_forum_and_targets_come_from_config():
    cfg = app_config("forum", None)
    assert cfg["origin"] == "https://forum.gegeteam.xyz"  # trường cấu hình trống → mặc định
    assert cfg["done"] == "https://forum.gegeteam.xyz/login?gege_qr=1"
    assert cfg["fail"] == "https://forum.gegeteam.xyz/login?gege_qr=err"
    assert cfg["title"] == "Gege Forum"
    assert app_config("forum", "https://dien-dan.example.vn/abc")["origin"] == "https://dien-dan.example.vn"
    assert app_config("forum", "khong-phai-url")["origin"] == "https://forum.gegeteam.xyz"
    assert app_config("console", None) is None
    assert app_config("https://evil.example", None) is None
    assert app_config(None, None) is None


def test_public_base_forces_https_for_domain_names():
    assert (
        public_base("hr.gegeteam.xyz") == "https://hr.gegeteam.xyz"
    )  # sau Cloudflare gunicorn chỉ thấy http
    assert public_base("192.168.2.116") == "http://192.168.2.116"
    assert public_base("192.168.2.116:8000") == "http://192.168.2.116:8000"
    assert public_base("localhost:8000") == "http://localhost:8000"
    assert public_base("") == ""
    assert public_base("hr.gegeteam.xyz/x") == ""  # Host lạ → không dựng địa chỉ
    assert public_base("a@b.vn") == ""


def test_scan_url_and_matrix_rows():
    assert (
        scan_url("https://hr.gegeteam.xyz/", "abcDEF12") == "https://hr.gegeteam.xyz/hr/seat-login?c=abcDEF12"
    )
    assert matrix_rows([[1, 0, 1], [0, 0, 1]]) == ["101", "001"]
    assert matrix_rows(None) == []


def test_client_ip_trusts_cloudflare_header_only_through_tunnel():
    assert client_ip("::1", "14.250.78.243") == "14.250.78.243"  # qua Cloudflare Tunnel
    assert client_ip("127.0.0.1", "2001:db8::1") == "2001:db8::1"
    assert (
        client_ip("192.168.2.49", "8.8.8.8") == "192.168.2.49"
    )  # vào thẳng trong LAN: header do người gọi tự gửi
    assert client_ip("::1", "") == "::1"
    assert client_ip("::1", "<script>") == "::1"
    assert client_ip(None, None) == ""


def test_device_label():
    chrome_win = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
    assert device_label(chrome_win) == "Chrome trên Windows"
    assert device_label(chrome_win + " Edg/141.0") == "Edge trên Windows"
    assert (
        device_label("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0")
        == "Firefox trên Linux"
    )
    safari = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"
    )
    assert device_label(safari) == "Safari trên macOS"
    assert device_label("curl/8.0") == "Trình duyệt"
    assert device_label("") == ""
    assert device_label(None) == ""


def test_approve_once_and_only_while_fresh():
    rec = new_record("forum", "1.2.3.4", "Chrome trên Windows", T0)
    assert poll_state(rec, T0 + 5) == "wait"

    ok, msg = approve(rec, "An@GegeTeam.net", T0 + 20)
    assert msg == "" and ok["user"] == "an@gegeteam.net" and ok["state"] == "ok"
    assert rec["state"] == "wait"  # không sửa bản ghi gốc
    assert poll_state(ok, T0 + 21) == "ok"

    again, msg = approve(ok, "khac@gegeteam.net", T0 + 22)  # người thứ hai quét cùng mã
    assert again is None and "đã được dùng" in msg

    late, msg = approve(rec, "an@gegeteam.net", T0 + CODE_TTL_S + 1)
    assert late is None and "hết hạn" in msg
    assert approve(None, "an@gegeteam.net", T0)[0] is None
    for bad in ("", None, "Guest", "Administrator"):
        assert approve(rec, bad, T0 + 1)[0] is None


def test_poll_state_expiry():
    rec = new_record("forum", "1.2.3.4", "", T0)
    assert poll_state(rec, T0 + CODE_TTL_S) == "wait"
    assert poll_state(rec, T0 + CODE_TTL_S + 1) == "gone"  # chưa ai quét, hết hạn
    ok, _ = approve(rec, "an@gegeteam.net", T0 + CODE_TTL_S - 1)  # xác nhận ở giây cuối
    assert poll_state(ok, T0 + CODE_TTL_S + APPROVED_GRACE_S) == "ok"  # trình duyệt vẫn kịp hỏi
    assert poll_state(ok, T0 + CODE_TTL_S + APPROVED_GRACE_S + 1) == "gone"
    assert poll_state(None, T0) == "gone"
    assert poll_state({"ts": "x"}, T0) == "gone"


def test_remaining_ttl_never_below_grace():
    rec = new_record("forum", "", "", T0)
    assert remaining_ttl(rec, T0 + 10) == CODE_TTL_S - 10 + APPROVED_GRACE_S
    assert remaining_ttl(rec, T0 + CODE_TTL_S + 99) == APPROVED_GRACE_S


def test_info_view_matches_console_web_code_shape():
    rec = new_record("forum", "14.250.78.243", "Chrome trên Windows", T0)
    v = info_view(rec, "Gege Forum")
    assert v["kind"] == "web" and v["title"] == "Gege Forum" and v["host"] == "Gege Forum"
    assert v["ip"] == "14.250.78.243" and v["device"] == "Chrome trên Windows" and v["user"] is None
    ok, _ = approve(rec, "an@gegeteam.net", T0 + 1)
    assert info_view(ok, "Gege Forum")["user"] == "an@gegeteam.net"  # app báo "mã đã được dùng"


def test_finish_valid():
    rec = {"user": "an@gegeteam.net", "app": "forum", "ts": T0}
    assert finish_valid(rec, "forum", T0 + FINISH_TTL_S) is True
    assert finish_valid(rec, "forum", T0 + FINISH_TTL_S + 1) is False  # quá 60 giây
    assert finish_valid(rec, "console", T0 + 1) is False  # vé của trang khác
    assert finish_valid({"user": "", "app": "forum", "ts": T0}, "forum", T0 + 1) is False
    assert finish_valid(None, "forum", T0) is False
