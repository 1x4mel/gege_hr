"""Chấm công trên máy công ty qua gege-seat — luật quyết định (pure).

plans/plan-cham-cong-pc-gege-seat.md: chỉ cho chấm khi vé hợp lệ, ca là của
chính nhân viên và máy không bị điều khiển từ xa.
"""

from __future__ import annotations

from gege_hr.gege_hr.utils.seat_checkin import evaluate

ME = "an.nguyen@gegeteam.net"


def _res(**kw):
    base = {"ok": True, "host": "b6", "username": ME, "remote": 0, "moonlight": 0}
    base.update(kw)
    return base


def test_valid_local_session_allowed():
    assert evaluate(_res(), ME) == (True, "", "b6")


def test_email_match_is_case_insensitive():
    ok, _msg, host = evaluate(_res(username="An.Nguyen@GegeTeam.net"), ME)
    assert ok and host == "b6"


def test_invalid_or_expired_ticket_rejected():
    ok, msg, _host = evaluate({"ok": False, "msg": "Vé không hợp lệ hoặc đã hết hạn"}, ME)
    assert not ok and "hết hạn" in msg
    assert evaluate(None, ME)[0] is False
    assert evaluate({}, ME)[0] is False


def test_other_users_session_rejected():
    ok, msg, host = evaluate(_res(username="khac@gegeteam.net"), ME)
    assert not ok and host == "b6"
    assert "khac@gegeteam.net" in msg


def test_remote_session_rejected():
    # Ca mở bằng vé Moonlight (remote=1) — owner 10/10: không cho chấm từ xa.
    ok, msg, _host = evaluate(_res(remote=1), ME)
    assert not ok and "từ xa" in msg


def test_active_moonlight_on_local_session_rejected():
    # Ca mở tại chỗ hôm trước rồi Moonlight vào từ nhà: remote=0 nhưng đang có phiên điều khiển.
    ok, msg, _host = evaluate(_res(moonlight=1), ME)
    assert not ok and "từ xa" in msg


def test_missing_identity_rejected():
    assert evaluate(_res(username=""), ME)[0] is False
    assert evaluate(_res(), "")[0] is False
    assert evaluate(_res(host=""), ME)[0] is False
