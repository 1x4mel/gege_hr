"""WP2 (prod-readiness-plan, F-LC13) — REAL-hours payroll summary unit tests.

Plan checklist: "Viết unit test cho summarize_period TRƯỚC rồi mới sửa API."
Pure function — no frappe, no bench. Matrix mirrors the PR* E2E scenarios:

  PR1  5 ngày × 8h        → regular 40, payable 5
  PR2  2 ngày công + 1 phép (no WS) → payable 3
  PR3  OT duyệt 3h        → overtime_hours 3
  PR4  ca đêm 2 phiên     → gộp giờ, 1 ngày không double
  PR5  need_review        → ngày đó KHÔNG payable, đếm cảnh báo
  PR7  (waived ticket)    → OUT thật đã duyệt → giờ tính đủ (WS thường)
  PR8  vắng không phép    → absent_days đúng, payable 0
  + hours_per_day lệch 6h, rounding 2dp, blank input, extra leave double-count
    protection (ngày phép CÓ công không double).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "gege_hr"),
)

from gege_hr.gege_hr.utils.payroll import summarize_work_sessions  # noqa: E402


def _ws(**kw):
    base = {
        "work_date": "2026-08-01",
        "regular_hours": 8.0,
        "approved_overtime_hours": 0.0,
        "need_review": 0,
        "absent": 0,
        "has_leave": 0,
    }
    base.update(kw)
    return base


# PR1 — five full 8h days → 40h / 5 payable days
def test_pr1_five_full_days():
    rows = [_ws(work_date=f"2026-08-0{d}") for d in range(1, 6)]
    s = summarize_work_sessions(rows)
    assert s["regular_hours"] == 40.0
    assert s["payable_days"] == 5.0
    assert s["need_review_days"] == 0


# PR2 — 2 worked days + 1 approved leave day WITHOUT a WS → payable 3
def test_pr2_worked_plus_leave_without_session():
    rows = [_ws(work_date="2026-08-03"), _ws(work_date="2026-08-04")]
    s = summarize_work_sessions(rows, extra_leave_days=1.0)
    assert s["payable_days"] == 3.0
    assert s["leave_days"] == 1.0


# Pure paid-leave WS row (has_leave, 0 hours) → 1 payable day, no double count
def test_leave_session_row_counts_one_payable_day():
    s = summarize_work_sessions([_ws(regular_hours=0.0, has_leave=1)])
    assert s["payable_days"] == 1.0
    assert s["leave_days"] == 1.0


# A leave day that ALSO has worked hours must not double the payable day
def test_leave_with_worked_hours_no_double_count():
    s = summarize_work_sessions([_ws(regular_hours=8.0, has_leave=1)])
    assert s["payable_days"] == 1.0
    assert s["leave_days"] == 0.0


# PR3 — approved OT 3h summed into overtime_hours
def test_pr3_approved_overtime_summed():
    rows = [_ws(regular_hours=8.0, approved_overtime_hours=3.0)]
    s = summarize_work_sessions(rows)
    assert s["overtime_hours"] == 3.0


# PR4 — two overnight sessions attributed to the shift's start date merge hours
def test_pr4_two_sessions_same_day_merge():
    rows = [
        _ws(work_date="2026-08-05", regular_hours=4.0),
        _ws(work_date="2026-08-05", regular_hours=4.5),
    ]
    s = summarize_work_sessions(rows)
    # 8.5h → 1.06 payable days (rounded 2dp), not 2 days
    assert s["regular_hours"] == 8.5
    assert s["payable_days"] == 1.06


# PR5 — need_review rows pay NOTHING and raise the warning counter
def test_pr5_need_review_pays_nothing():
    rows = [
        _ws(work_date="2026-08-10"),
        _ws(work_date="2026-08-11", regular_hours=8.0, need_review=1),
    ]
    s = summarize_work_sessions(rows)
    assert s["regular_hours"] == 8.0
    assert s["payable_days"] == 1.0
    assert s["need_review_days"] == 1


# PR7 — waived ticket → WS is a normal row, full hours counted
def test_pr7_waived_ticket_full_hours():
    s = summarize_work_sessions([_ws(regular_hours=9.5, approved_overtime_hours=1.5)])
    assert s["regular_hours"] == 9.5
    assert s["payable_days"] == round(9.5 / 8, 2)


# PR8 — absent without leave → absent_days 1, payable 0
def test_pr8_absent_unpaid():
    rows = [_ws(), _ws(work_date="2026-08-02", regular_hours=0.0, absent=1)]
    s = summarize_work_sessions(rows)
    assert s["absent_days"] == 1
    assert s["payable_days"] == 1.0  # only the worked day


# hours_per_day ≠ 8 — 6h shift → 1 day at hpd=6
def test_custom_hours_per_day():
    s = summarize_work_sessions([_ws(regular_hours=6.0)], hours_per_day=6.0)
    assert s["payable_days"] == 1.0


# rounding: 4h at hpd 8 → 0.5 day exactly (2dp)
def test_rounding_two_decimals():
    s = summarize_work_sessions([_ws(regular_hours=4.0)])
    assert s["payable_days"] == 0.5
    # Python banker's rounding: round(0.125, 2) == 0.12
    s2 = summarize_work_sessions([_ws(regular_hours=1.0)])
    assert s2["payable_days"] == 0.12


# blank input → neutral zeroed summary
def test_blank_input():
    s = summarize_work_sessions([])
    assert s == {
        "regular_hours": 0.0,
        "overtime_hours": 0.0,
        "payable_days": 0.0,
        "leave_days": 0.0,
        "absent_days": 0,
        "need_review_days": 0,
        "worked_days": 0,
        "session_count": 0,
    }
