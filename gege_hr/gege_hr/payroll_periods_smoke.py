"""Smoke E2E — Payroll Periods desk-free (plan payroll-periods-desk-free §5.4).

Chạy::

    bench --site erp-hr.local execute gege_hr.gege_hr.payroll_periods_smoke.run

Tự seed idempotent (tiền tố ``ZZ-PPSMOKE``), 7 bước runtime + cleanup:
 1. payroll_period_context — shape + suggested theo kỳ cuối.
 2. save_payroll_period tạo kỳ chuẩn tương lai (không giao kỳ hiện có).
 3. save kỳ GIAO NHAU → phải fail, message tiếng Việt, KHÔNG lộ HTML Desk.
 4. get_benefit_filter_options thấy kỳ mới → chứng minh redis cache đã clear.
 5. update end_date kỳ smoke (update path + audit previous).
 6. delete kỳ smoke (usage=0) + verify đã biến mất.
 7. VN review: create Draft → update_payroll_review đổi to_date →
    cancel_payroll_review (reason) → status Cancelled.
Cleanup xoá sạch mọi doc tiền tố ZZ-PPSMOKE (delete_doc trực tiếp — cancel
block delete_payroll_review trên kỳ đã Cancelled).
"""

from __future__ import annotations

import frappe
from frappe.utils import get_first_day, get_last_day, add_months, getdate

from gege_hr.gege_hr.api import payroll as payroll_api
from gege_hr.gege_hr.api import payroll_master as master_api
from gege_hr.gege_hr.api import benefits_admin as benefits_api

P = "ZZ-PPSMOKE"
COMPANY_CACHE: str | None = None


def _company() -> str:
    global COMPANY_CACHE
    if not COMPANY_CACHE:
        row = frappe.get_all("Company", limit=1, order_by="creation asc")
        if not row:
            raise Exception("Site chưa có Company nào.")
        COMPANY_CACHE = row[0].name
    return COMPANY_CACHE


def _report_step(report, key):
    def deco(fn):
        try:
            out = fn()
            report[key] = {"ok": True, "detail": out}
            print(f"[SMOKE] {key}: OK — {out}")
        except Exception as e:  # noqa: BLE001 — smoke ghi rõ từng bước
            import traceback

            tb = traceback.format_exc()
            report[key] = {"ok": False, "detail": str(e), "tb": tb[-1200:]}
            print(f"[SMOKE] {key}: FAIL — {e}")
            print(tb[-1200:])

    return deco


def _cleanup() -> int:
    """Xoá sạch mọi dấu vết ZZ-PPSMOKE (kỳ chuẩn + VN review period)."""
    n = 0
    for doctype in ("Payroll Period", "VN Payroll Review Period"):
        rows = frappe.get_all(doctype, filters={"name": ["like", f"{P}%"]}, fields=["name"])
        for r in rows:
            try:
                frappe.delete_doc(doctype, r.name, ignore_permissions=True, force=True)
                n += 1
            except Exception:
                frappe.db.delete(doctype, {"name": r.name})
                n += 1
    frappe.db.delete("VN Payroll Review Line", {"payroll_review_period": ["like", f"{P}%"]})
    frappe.db.commit()
    return n


def run() -> dict:
    report: dict = {}
    _cleanup()

    company = _company()
    year = getdate().year + 1  # năm tương lai — không giao kỳ active hiện tại
    pp_name = f"{P}-{year}"

    # 1) Context — shape + suggested
    @_report_step(report, "1_context")
    def _ctx():
        ctx = master_api.payroll_period_context()
        assert "companies" in ctx and "suggested" in ctx, f"thiếu key: {list(ctx)}"
        assert ctx["suggested"]["start_date"] and ctx["suggested"]["end_date"]
        assert ctx["health"] in ("ok", "missing")
        return f"suggested={ctx['suggested']} health={ctx['health']}"

    # 2) Tạo kỳ chuẩn tương lai
    @_report_step(report, "2_create")
    def _create():
        res = master_api.save_payroll_period(
            company=company,
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
            label=pp_name,
        )
        assert res["name"] == pp_name, res
        return res

    # 3) Kỳ giao nhau → PHẢI fail tiếng Việt, không HTML
    @_report_step(report, "3_overlap_blocked")
    def _overlap():
        try:
            master_api.save_payroll_period(
                company=company,
                start_date=f"{year}-06-01",
                end_date=f"{year + 1}-05-31",
                label=f"{P}B-{year}",
            )
        except Exception as e:
            msg = str(e)
            assert "bao trùm" in msg or "exists between" in msg, msg
            assert "<a" not in msg and "/app/" not in msg, f"lộ HTML Desk: {msg}"
            assert pp_name in msg, f"không nêu tên kỳ giao nhau: {msg}"
            return f"overlap chặn đúng: {msg[:90]}…"
        raise AssertionError("overlap KHÔNG bị chặn!")

    # 4) Benefits thấy kỳ mới (redis cache đã clear)
    @_report_step(report, "4_benefits_see_new_period")
    def _benefits():
        opts = benefits_api.get_benefit_filter_options()
        names = [p.get("name") for p in (opts.get("payroll_periods") or [])]
        assert pp_name in names, f"{pp_name} không có trong {names[:10]}"
        return f"payroll_periods có {pp_name}"

    # 5) Update end_date (update path)
    @_report_step(report, "5_update")
    def _update():
        res = master_api.save_payroll_period(
            name=pp_name,
            company=company,
            start_date=f"{year}-01-01",
            end_date=f"{year}-11-30",
        )
        assert res["end_date"] == f"{year}-11-30", res
        return res

    # 6) Delete usage=0 + verify
    @_report_step(report, "6_delete")
    def _delete():
        res = master_api.delete_payroll_period(pp_name)
        assert res["name"] == pp_name
        assert not frappe.db.exists("Payroll Period", pp_name), "kỳ vẫn còn sau delete"
        return res

    # 7) VN review lifecycle: create Draft → update → cancel
    @_report_step(report, "7_vn_review_lifecycle")
    def _vn():
        nxt = add_months(getdate(), 1)
        month = f"{nxt.month:02d}"
        vyear = str(nxt.year)
        res = payroll_api.create_payroll_review(
            company=company,
            payroll_month=month,
            payroll_year=vyear,
            from_date=str(get_first_day(nxt)),
            to_date=str(get_last_day(nxt)),
        )
        name = res["name"]
        assert res["status"] == "Draft", res
        upd = payroll_api.update_payroll_review(
            name=name,
            to_date=str(get_last_day(nxt)),
            attendance_period="",
        )
        assert upd["to_date"], upd
        can = payroll_api.cancel_payroll_review(name=name, reason="smoke test huỷ kỳ")
        assert can["status"] == "Cancelled", can
        # dọn bằng tay: delete_payroll_review chặn kỳ Cancelled
        frappe.delete_doc("VN Payroll Review Period", name, ignore_permissions=True, force=True)
        frappe.db.commit()
        return f"{name}: Draft → update → Cancelled → cleaned"

    cleaned = _cleanup()
    report["cleanup"] = {"ok": cleaned >= 0, "detail": f"{cleaned} doc dọn sau smoke"}
    report["ALL_OK"] = all(
        v.get("ok") for k, v in report.items() if isinstance(v, dict) and k != "cleanup"
    )
    print(f"[SMOKE] ALL_OK={report['ALL_OK']} — cleanup {cleaned} doc")
    return report
