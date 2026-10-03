"""Bench-free tests for ``utils/month_sheet.py`` (plans/plan-employee-month-sheet.md)."""

from __future__ import annotations

import datetime as dt

from gege_hr.gege_hr.utils import month_sheet as ms

TODAY = dt.date(2026, 10, 10)
D = dt.date


def _ws(**kw):
    base = {
        "name": "WS-1",
        "shift_type": "Ca sáng",
        "planned_start": "2026-10-01 08:00:00",
        "planned_end": "2026-10-01 17:00:00",
        "actual_checkin": "2026-10-01 08:00:00",
        "actual_checkout": "2026-10-01 17:00:00",
        "late_minutes": 0,
        "early_leave_minutes": 0,
        "regular_hours": 8,
        "total_actual_hours": 9,
        "raw_overtime_hours": 0,
        "approved_overtime_hours": 0,
    }
    base.update(kw)
    return base


# ---- buckets / leave fraction ------------------------------------------------ #
def test_minute_bucket_edges():
    assert ms.minute_bucket(0) is None
    assert ms.minute_bucket(15) == "le15"
    assert ms.minute_bucket(16) == "16_30"
    assert ms.minute_bucket(30) == "16_30"
    assert ms.minute_bucket(31) == "gt30"


def test_leave_fraction_half_day():
    la = {"half_day": 1, "half_day_date": "2026-10-03", "from_date": "2026-10-02", "to_date": "2026-10-04"}
    assert ms.leave_fraction(la, D(2026, 10, 3)) == 0.5
    assert ms.leave_fraction(la, D(2026, 10, 2)) == 1.0
    single = {"half_day": 1, "from_date": "2026-10-05", "to_date": "2026-10-05"}
    assert ms.leave_fraction(single, D(2026, 10, 5)) == 0.5


def test_leaves_by_date_clips_and_marks_lwp():
    rows = [
        {"name": "LA1", "leave_type": "Phép năm", "from_date": "2026-09-29", "to_date": "2026-10-02"},
        {"name": "LA2", "leave_type": "Nghỉ việc riêng", "from_date": "2026-10-05", "to_date": "2026-10-05"},
    ]
    out = ms.leaves_by_date(rows, {"Nghỉ việc riêng"}, D(2026, 10, 1), D(2026, 10, 31))
    assert sorted(out) == ["2026-10-01", "2026-10-02", "2026-10-05"]
    assert out["2026-10-01"]["paid"] is True
    assert out["2026-10-05"]["paid"] is False


# ---- day classification -------------------------------------------------------- #
def test_every_day_is_a_workday_absent_without_leave():
    """24/7: a past Sunday with no punch and no leave is absent."""
    sunday = D(2026, 10, 4)
    day = ms.build_day(sunday, today=TODAY)
    assert day["status"] == ms.ABSENT
    assert day["absent"] == 1.0


def test_worked_day_with_late_early_ot():
    day = ms.build_day(
        D(2026, 10, 1),
        today=TODAY,
        sessions=[
            _ws(late_minutes=20, early_leave_minutes=5, raw_overtime_hours=2, approved_overtime_hours=1.5)
        ],
        explanations={"late": "Approved"},
    )
    assert day["status"] == ms.WORKED
    assert day["worked"] == 1.0
    assert (day["late_minutes"], day["late_bucket"], day["late_explained"]) == (20, "16_30", True)
    assert (day["early_minutes"], day["early_bucket"], day["early_explained"]) == (5, "le15", False)
    assert day["ot_approved_hours"] == 1.5
    assert day["ot_pending_hours"] == 0.5


def test_missing_checkout_flag_is_not_checkout_miss():
    """missing_checkout alone (mid-shift) is NOT a forgotten checkout; the
    auto-checkout marker or a ticket is."""
    open_ws = _ws(actual_checkout=None, missing_checkout=1)
    assert ms.build_day(D(2026, 10, 1), today=TODAY, sessions=[open_ws])["checkout_miss"] is False
    auto = _ws(vn_auto_checkout=1)
    assert ms.build_day(D(2026, 10, 1), today=TODAY, sessions=[auto])["checkout_miss"] is True
    ticket = ms.build_day(D(2026, 10, 1), today=TODAY, sessions=[_ws()], checkout_miss={"status": "Pending"})
    assert ticket["checkout_miss"] is True
    assert ticket["checkout_miss_status"] == "Pending"


def test_checkin_miss_only_when_out_without_in():
    day = ms.build_day(D(2026, 10, 1), today=TODAY, sessions=[_ws(actual_checkin=None, late_minutes=40)])
    assert day["checkin_miss"] is True
    assert day["late_minutes"] == 0  # no IN → no late


def test_leave_wins_over_absent_and_half_day_split():
    paid = {"name": "LA", "leave_type": "Phép năm", "paid": True, "fraction": 1.0}
    day = ms.build_day(D(2026, 10, 2), today=TODAY, leave=paid)
    assert (day["status"], day["absent"], day["worked"]) == (ms.LEAVE_PAID, 0.0, 0.0)

    half = {"name": "LA", "leave_type": "Nghỉ việc riêng", "paid": False, "fraction": 0.5}
    with_punch = ms.build_day(D(2026, 10, 3), today=TODAY, leave=half, sessions=[_ws()])
    assert (with_punch["status"], with_punch["worked"], with_punch["absent"]) == (ms.LEAVE_UNPAID, 0.5, 0.0)
    no_punch = ms.build_day(D(2026, 10, 3), today=TODAY, leave=half)
    assert no_punch["absent"] == 0.5


def test_future_today_and_employment_window():
    assert ms.build_day(TODAY, today=TODAY)["status"] == ms.FUTURE  # today, not punched yet
    assert ms.build_day(TODAY, today=TODAY, sessions=[_ws()])["status"] == ms.WORKED
    assert (
        ms.build_day(D(2026, 10, 1), today=TODAY, date_of_joining=D(2026, 10, 5))["status"] == ms.BEFORE_JOIN
    )
    assert (
        ms.build_day(D(2026, 10, 9), today=TODAY, relieving_date=D(2026, 10, 8))["status"] == ms.AFTER_RELIEVE
    )


def test_multi_session_day_counts_once_and_sums_minutes():
    day = ms.build_day(
        D(2026, 10, 1),
        today=TODAY,
        sessions=[_ws(name="A", late_minutes=10), _ws(name="B", late_minutes=10, actual_checkout=None)],
    )
    assert day["multi_session"] is True
    assert day["worked"] == 1.0
    assert day["late_minutes"] == 20


# ---- monthly aggregate --------------------------------------------------------- #
def test_aggregate_month_totals():
    leaves = {
        "2026-10-02": {"name": "LA1", "leave_type": "Phép năm", "paid": True, "fraction": 1.0},
        "2026-10-03": {"name": "LA2", "leave_type": "Nghỉ việc riêng", "paid": False, "fraction": 0.5},
        "2026-10-20": {"name": "LA3", "leave_type": "Phép năm", "paid": True, "fraction": 1.0},
    }
    sessions = {
        "2026-10-01": [_ws(late_minutes=40, raw_overtime_hours=2, approved_overtime_hours=2)],
        "2026-10-03": [_ws()],
        "2026-10-05": [_ws(late_minutes=10, vn_auto_checkout=1)],
    }
    days = [
        ms.build_day(
            d,
            today=TODAY,
            sessions=sessions.get(d.isoformat()),
            leave=leaves.get(d.isoformat()),
            pending=[{"doctype": "Leave Application"}] if d.day == 6 else None,
        )
        for d in ms.iter_days(D(2026, 10, 1), D(2026, 10, 31))
    ]
    t = ms.aggregate_month(days)
    assert t["standard_days"] == 31
    assert t["elapsed_days"] == 9  # 1..9 (10 = today, no punch → not counted)
    assert t["worked_days"] == 2.5  # 01, 05 + half of 03
    assert t["leave_paid_days"] == 2.0  # 02 + future 20
    assert t["leave_unpaid_days"] == 0.5
    assert t["leave_by_type"]["Phép năm"] == {"days": 2.0, "paid": True}
    assert t["absent_days"] == 5  # 04, 06, 07, 08, 09
    assert t["late"]["days"] == 2 and t["late"]["minutes"] == 50
    assert t["late"]["buckets"] == {"le15": 1, "16_30": 0, "gt30": 1}
    assert t["ot"] == {"days": 1, "approved_hours": 2.0, "pending_hours": 0.0, "night_hours": 0.0}
    assert t["checkout_miss_count"] == 1
    assert t["pending_count"] == 1


# ---- monthly review (team view + legacy Excel) ------------------------------- #
def test_compress_days_ranges_and_half_days():
    assert ms.compress_days([(2, 1), (3, 1), (4, 1), (9, 0.5), (23, 1)]) == "2 - 4, 9 (½), 23"
    assert ms.compress_days([(5, 1)]) == "5"
    assert ms.compress_days([]) == ""


def test_review_items_legacy_sections_and_reasons():
    leaves = {
        "2026-10-02": {
            "name": "L1",
            "leave_type": "Nghỉ việc riêng",
            "paid": False,
            "fraction": 1.0,
            "reason": "Việc nhà",
        },
        "2026-10-03": {
            "name": "L1",
            "leave_type": "Nghỉ việc riêng",
            "paid": False,
            "fraction": 1.0,
            "reason": "Việc nhà",
        },
        "2026-10-07": {"name": "L2", "leave_type": "Phép năm", "paid": True, "fraction": 0.5, "reason": ""},
    }
    sessions = {
        "2026-10-01": [_ws(late_minutes=12, early_leave_minutes=228)],
        "2026-10-04": [_ws(vn_auto_checkout=1)],
        "2026-10-05": [_ws(actual_checkin=None)],
    }
    days = [
        ms.build_day(
            d,
            today=TODAY,
            sessions=sessions.get(d.isoformat()),
            leave=leaves.get(d.isoformat()),
            reasons={"late": "Kẹt xe", "checkout_miss": "Quên"} if d.day in (1, 4) else None,
        )
        for d in ms.iter_days(D(2026, 10, 1), D(2026, 10, 9))
    ]
    items = ms.review_items(days)
    assert items["late_early"] == [
        {"date": "2026-10-01", "text": "Ngày 01/10: Trễ 12 phút, Về sớm 228 phút", "reason": "Kẹt xe"}
    ]
    assert [(f["text"], f["reason"]) for f in items["forgot"]] == [
        ("Ngày 04/10: Chấm ra", "Quên"),
        ("Ngày 05/10: Chấm vào", ""),
    ]
    assert items["leave_unpaid"] == {"count": 2.0, "text": "Nghỉ việc riêng: 2 - 3", "reason": "Việc nhà"}
    assert items["leave_paid"]["text"] == "Phép năm: 7 (½)"
    # 06, 08, 09 absent + the other half of 07
    assert items["absent"]["text"] == "Thiếu công: 6, 7 (½), 8 - 9"
    assert items["absent"]["count"] == 3.5


def test_legacy_layout_shape():
    from gege_hr.gege_hr.utils import month_review_xlsx as mrx

    emps = [
        {
            "employee_name": "PLA Tuấn",
            "items": {
                "forgot": [{"text": "Ngày 04/08: Chấm ra", "reason": "Quên"}],
                "late_early": [{"text": f"Ngày {d:02d}/08: Trễ 10 phút", "reason": ""} for d in range(1, 6)],
                "leave_unpaid": {"count": 9, "text": "Nghỉ việc riêng: 2 - 9, 23", "reason": ""},
                "leave_paid": {"count": 0, "text": "", "reason": ""},
                "absent": {"count": 0, "text": "", "reason": ""},
            },
        },
        {
            "employee_name": "Châu",
            "items": {
                "forgot": [],
                "late_early": [],
                "leave_unpaid": {"count": 0, "text": "", "reason": ""},
                "leave_paid": {"count": 0, "text": "", "reason": ""},
                "absent": {"count": 30, "text": "Thiếu công: 1 - 30", "reason": ""},
            },
        },
    ]
    lay = mrx.legacy_layout(emps)
    cell = {(r, c): v for r, c, v, _ in lay["cells"]}
    assert cell[(1, 2)] == "PLA Tuấn" and cell[(1, 4)] == "Châu"
    assert (cell[(2, 2)], cell[(2, 3)]) == ("Ngày", "Nội dung")
    # forgot block = MIN_BLOCK_ROWS (3) rows from row 3; late block = 5 rows from row 6
    assert cell[(3, 1)] == "Quên điểm danh" and (3, 1, 5, 1) in lay["merges"]
    assert (cell[(3, 2)], cell[(3, 3)]) == ("Ngày 04/08: Chấm ra", "Quên")
    assert cell[(6, 1)] == "Đi trễ/Về sớm" and (6, 1, 10, 1) in lay["merges"]
    assert cell[(11, 2)] == "Nghỉ việc riêng: 2 - 9, 23 (9 ngày)"
    assert cell[(13, 4)] == "Thiếu công: 1 - 30 (30 ngày)"
    assert lay["rows"] == 13


def test_render_xlsx_roundtrip():
    openpyxl = __import__("pytest").importorskip("openpyxl")
    import io

    from gege_hr.gege_hr.utils import month_review_xlsx as mrx

    lay = mrx.legacy_layout(
        [
            {
                "employee_name": "A",
                "items": {"forgot": [], "late_early": [], "leave_unpaid": {}, "leave_paid": {}, "absent": {}},
            }
        ]
    )
    wb = openpyxl.load_workbook(io.BytesIO(mrx.render_xlsx(lay, "Thang 10-2026")))
    ws = wb.active
    assert ws.title == "Thang 10-2026"
    assert ws["B1"].value == "A"
    assert ws["A3"].fill.fgColor.rgb.endswith("000000")
