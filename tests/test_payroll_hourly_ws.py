"""PR2 (plans/plan-cach-tinh-cong-theo-ca.md) — Hourly payroll đọc phiên chấm công.

``bracket_hours_from_sessions`` thay cho ghép cặp lượt chấm thô: chỉ trả giờ
tính lương (đã cộng ân hạn trễ) + OT đã duyệt; ngày cần xem lại không trả.
Pure — no frappe.
"""

from __future__ import annotations

from datetime import datetime

from gege_hr.gege_hr.utils.payroll import bracket_hours_from_sessions

BRACKETS = [
    {"from": 8, "to": 16, "coeff": 1.0},
    {"from": 16, "to": 24, "coeff": 1.2},
    {"from": 0, "to": 8, "coeff": 1.5},
]


def _ws(**kw):
    base = {
        "planned_end": datetime(2026, 10, 3, 17, 0),
        "actual_checkin": datetime(2026, 10, 3, 8, 33),
        "actual_checkout": datetime(2026, 10, 3, 17, 9),
        "payable_regular_hours": 8.0,
        "approved_overtime_hours": 0.0,
        "need_review": 0,
        "absent": 0,
    }
    base.update(kw)
    return base


def _total(d):
    return round(sum(d.values()), 4)


def test_regular_day_pays_payable_hours_only():
    # 08:33 → 17:09, ca 9h-17h: đến sớm + 9' sau ca không trả → 8h.
    assert bracket_hours_from_sessions([_ws()], []) == {1.0: 8.0}


def test_unapproved_ot_not_paid():
    # Lượt ra giả 20:00 (quên chấm ra) — OT chưa duyệt → vẫn 8h.
    ws = _ws(actual_checkout=datetime(2026, 10, 3, 20, 0))
    assert _total(bracket_hours_from_sessions([ws], [])) == 8.0


def test_approved_ot_paid():
    ws = _ws(actual_checkout=datetime(2026, 10, 3, 20, 0), approved_overtime_hours=2.0)
    assert _total(bracket_hours_from_sessions([ws], [])) == 10.0


def test_need_review_and_absent_pay_nothing():
    assert bracket_hours_from_sessions([_ws(need_review=1), _ws(absent=1)], []) == {}


def test_missing_checkout_pays_nothing():
    ws = _ws(actual_checkout=None, payable_regular_hours=0.0)
    assert bracket_hours_from_sessions([ws], []) == {}


def test_late_within_grace_paid_full():
    # Vào 09:03 (ân hạn 5') → engine đã trả 8h tính lương.
    ws = _ws(actual_checkin=datetime(2026, 10, 3, 9, 3), actual_checkout=datetime(2026, 10, 3, 17, 0))
    assert _total(bracket_hours_from_sessions([ws], [])) == 8.0


def test_night_shift_split_by_brackets():
    # Ca tối 20:00 → 08:00: 4h hệ số 1.2 (20–24) + 8h hệ số 1.5 (0–8).
    ws = _ws(
        planned_end=datetime(2026, 10, 5, 8, 0),
        actual_checkin=datetime(2026, 10, 4, 19, 55),
        actual_checkout=datetime(2026, 10, 5, 8, 0),
        payable_regular_hours=12.0,
    )
    out = bracket_hours_from_sessions([ws], BRACKETS)
    assert round(out[1.2], 4) == 4.0
    assert round(out[1.5], 4) == 8.0


def test_string_datetimes_from_db():
    ws = _ws(
        planned_end="2026-10-03 17:00:00",
        actual_checkin="2026-10-03 08:33:00",
        actual_checkout="2026-10-03 17:09:00",
    )
    assert bracket_hours_from_sessions([ws], []) == {1.0: 8.0}


def test_blank():
    assert bracket_hours_from_sessions([], BRACKETS) == {}
