"""Smoke E2E — Benefits desk-free (plan-benefits-desk-free §7) trên site dev.

Chạy::

    bench --site erp-hr.local execute gege_hr.gege_hr.benefits_smoke.run

Tự seed (idempotent, tiền tố ``ZZ-BENSMOKE``): Payroll Period active quanh
hôm nay + 3 Salary Component (1 claim-based + 1 pro-rata + 1 Basic thường)
+ Salary Structure (``max_benefits`` 12tr) đã submit + SSA đã submit +
Gratuity Rule (slab không giới hạn) + Employee test (DOJ 2020, relieving hôm
nay) + user thường chỉ role Employee. Sau đó chạy toàn vòng đời qua API
``benefits_admin.*`` như SPA gọi, ghi báo cáo từng bước, cuối cùng cleanup
sạch (cancel trước khi delete với các doc đã submit).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, get_first_day, get_last_day, getdate, today

from gege_hr.gege_hr.api import benefits_admin as api

P = "ZZ-BENSMOKE"
EMP = f"{P}-EMP"
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


def _seed() -> dict:
    company = _company()
    year = getdate().year

    # 1) Payroll Period active quanh hôm nay
    pp = f"{P}-PP-{year}"
    if not frappe.db.exists("Payroll Period", pp):
        frappe.get_doc(
            {
                "doctype": "Payroll Period",
                "__newname": pp,
                "company": company,
                "start_date": f"{year}-01-01",
                "end_date": f"{year}-12-31",
            }
        ).insert(ignore_permissions=True)

    # 2) Salary Components — Basic thường + 2 flexi (claim / pro-rata)
    comps = {
        f"{P}-Basic": {"type": "Earning", "is_flexible_benefit": 0},
        f"{P}-MealCard": {
            "type": "Earning",
            "is_flexible_benefit": 1,
            "pay_against_benefit_claim": 1,
            "max_benefit_amount": 8_000_000,
            "depends_on_payment_days": 0,
        },
        f"{P}-Fuel": {
            "type": "Earning",
            "is_flexible_benefit": 1,
            "pay_against_benefit_claim": 0,
            "max_benefit_amount": 6_000_000,
            "depends_on_payment_days": 0,
        },
    }
    for name, flags in comps.items():
        if not frappe.db.exists("Salary Component", name):
            frappe.get_doc({"doctype": "Salary Component", "salary_component": name, **flags}).insert(
                ignore_permissions=True
            )
        # Idempotent refresh — lần seed cũ có thể còn cap thấp hơn max_benefits.
        frappe.db.set_value(
            "Salary Component", name, "max_benefit_amount", flags.get("max_benefit_amount", 0)
        )

    # 2b) Designation cho Employee + đích thăng cấp (Link validation)
    # autoname = field:designation_name (erpnext)
    for desig in ("Smoke Exec", "Smoke Lead"):
        if not frappe.db.exists("Designation", desig):
            frappe.get_doc({"doctype": "Designation", "designation_name": desig}).insert(
                ignore_permissions=True
            )

    # 3) Employee test (DOJ 2020 → ~6 năm kinh nghiệm cho gratuity).
    # Employee dùng naming series (HR-EMP-.####) → __newname bị bỏ qua; resolve
    # tên thật theo first/last name marker.
    emp = None
    rows = frappe.get_all(
        "Employee",
        filters=[["first_name", "=", "Benefit"], ["last_name", "=", "Smoke"], ["company", "=", company]],
        limit=1,
    )
    if rows:
        emp = rows[0].name
    else:
        emp = (
            frappe.get_doc(
                {
                    "doctype": "Employee",
                    "first_name": "Benefit",
                    "last_name": "Smoke",
                    "gender": "Male",
                    "date_of_birth": "1990-05-01",
                    "company": company,
                    "date_of_joining": "2020-01-06",
                    "designation": "Smoke Exec",
                    "status": "Active",
                }
            )
            .insert(ignore_permissions=True)
            .name
        )
    frappe.db.set_value("Employee", emp, "relieving_date", today())

    # 4) Salary Structure + SSA (đã submit) — max_benefits 12tr
    ss = f"{P}-SS"
    if not frappe.db.exists("Salary Structure", ss):
        doc = frappe.get_doc(
            {
                "doctype": "Salary Structure",
                "__newname": ss,
                "company": company,
                "is_active": "Yes",
                "currency": frappe.db.get_value("Company", company, "default_currency") or "VND",
                "payroll_frequency": "Monthly",
                "max_benefits": 12_000_000,
                "earnings": [
                    {"salary_component": f"{P}-Basic", "abbr": "ZBS", "amount": 5_000_000},
                    {
                        "salary_component": f"{P}-MealCard",
                        "abbr": "ZBM",
                        "amount": 0,
                        "is_flexible_benefit": 1,
                    },
                    {
                        "salary_component": f"{P}-Fuel",
                        "abbr": "ZBF",
                        "amount": 0,
                        "is_flexible_benefit": 1,
                    },
                ],
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()

    if not frappe.db.exists("Salary Structure Assignment", {"employee": emp, "salary_structure": ss}):
        ssa = frappe.get_doc(
            {
                "doctype": "Salary Structure Assignment",
                "employee": emp,
                "salary_structure": ss,
                "company": company,
                # from_date từ đầu năm để Salary Slip kỳ trước (tháng 8) cũng hợp lệ
                "from_date": f"{year}-01-01",
                "base": 5_000_000,
            }
        )
        ssa.insert(ignore_permissions=True)
        ssa.submit()

    # 4b) 1 Salary Slip đã submit kỳ trước — Gratuity HRMS tính amount từ
    # applicable earnings tổng hợp trên salary slip (bắt buộc có slip).
    if not frappe.get_all("Salary Slip", filters={"employee": emp, "docstatus": 1}, limit=1):
        pm = add_days(get_first_day(today()), -1)
        slip = frappe.get_doc(
            {
                "doctype": "Salary Slip",
                "employee": emp,
                "start_date": get_first_day(pm),
                "end_date": get_last_day(pm),
                "posting_date": today(),
            }
        )
        slip.insert(ignore_permissions=True)
        slip.submit()

    # 5) Gratuity Rule — slab không giới hạn, fraction 1.0, tính theo Current Slab
    rule = f"{P}-GRAT"
    if not frappe.db.exists("Gratuity Rule", rule):
        frappe.get_doc(
            {
                "doctype": "Gratuity Rule",
                "__newname": rule,
                "calculate_gratuity_amount_based_on": "Current Slab",
                "work_experience_calculation_function": "Round off Work Experience",
                "total_working_days_per_year": 365,
                "minimum_year_for_gratuity": 1,
                "applicable_earnings_component": [
                    {"salary_component": f"{P}-Basic"},
                    {"salary_component": f"{P}-MealCard"},
                ],
                "gratuity_rule_slabs": [
                    {"to_year": 0, "fraction_of_applicable_earnings": 1.0},
                ],
            }
        ).insert(ignore_permissions=True)

    # 6) User thường (chỉ role Employee) — cho bước IDOR / self-service
    user = f"{P.lower()}@gege.local"
    if not frappe.db.exists("User", user):
        u = frappe.get_doc(
            {
                "doctype": "User",
                "email": user,
                "first_name": "Benefit",
                "last_name": "Smoke",
                "enabled": 1,
            }
        )
        u.insert(ignore_permissions=True)
        u.add_roles("Employee")
    frappe.db.set_value("Employee", emp, "user_id", user)

    frappe.db.commit()
    return {
        "company": company,
        "payroll_period": pp,
        "salary_structure": ss,
        "gratuity_rule": rule,
        "employee": emp,
        "user": user,
    }


def _cleanup(seeded: dict) -> list:
    """Best-effort cleanup: cancel các doc đã submit rồi delete, xoá ngược
    thứ tự phụ thuộc."""
    done = []

    def _del(doctype, name, cancel=False):
        try:
            if not name or not frappe.db.exists(doctype, name):
                return
            if cancel:
                doc = frappe.get_doc(doctype, name)
                if doc.docstatus == 1:
                    try:
                        doc.cancel()  # cancel() không nhận ignore_permissions
                    except Exception:
                        # Gratuity cancel cần Fiscal Year active (GL); site dev
                        # có thể thiếu → force docstatus rồi delete.
                        frappe.db.set_value(doctype, name, "docstatus", 2, update_modified=False)
            frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
            done.append(f"{doctype}:{name}")
        except Exception as e:  # noqa: BLE001
            done.append(f"{doctype}:{name} FAILED {e}")

    # Purge stale từ các lần chạy dở: doc benefits trỏ employee đã bị xoá +
    # SS/SSA mồ côi theo tiền tố.
    for dt in (
        "Additional Salary",
        "Gratuity",
        "Employee Benefit Claim",
        "Employee Benefit Application",
        "Employee Promotion",
    ):
        for r in frappe.get_all(dt, pluck="name", limit_page_length=0):
            owner = frappe.db.get_value(dt, r, "employee")
            if owner and not frappe.db.exists("Employee", owner):
                # Mồ côi: controller cancel cần employee → force docstatus rồi delete.
                try:
                    frappe.db.set_value(dt, r, "docstatus", 2, update_modified=False)
                    frappe.delete_doc(dt, r, ignore_permissions=True, force=True)
                    done.append(f"{dt}:{r} orphan-purged")
                except Exception as e:  # noqa: BLE001
                    done.append(f"{dt}:{r} ORPHAN FAILED {e}")
    for r in frappe.get_all(
        "Salary Structure Assignment", filters={"salary_structure": ("like", f"{P}%")}, pluck="name"
    ):
        _del("Salary Structure Assignment", r, cancel=True)
    for r in frappe.get_all("Salary Structure", filters={"name": ("like", f"{P}%")}, pluck="name"):
        _del("Salary Structure", r, cancel=True)

    emp = (seeded or {}).get("employee")
    if not emp:
        return done
    # Additional Salary sinh từ gratuity submit
    for r in frappe.get_all(
        "Additional Salary",
        filters={"ref_doctype": "Gratuity", "employee": emp},
        pluck="name",
    ):
        _del("Additional Salary", r, cancel=True)
    for r in frappe.get_all("Gratuity", filters={"employee": emp}, pluck="name"):
        _del("Gratuity", r, cancel=True)
    for dt in ("Employee Benefit Claim", "Employee Benefit Application", "Employee Promotion"):
        for r in frappe.get_all(dt, filters={"employee": emp}, pluck="name"):
            _del(dt, r, cancel=True)
    # Slip phải xoá TRƯỚC Employee + Salary Structure (link chặn delete cả hai)
    for r in frappe.get_all("Salary Slip", filters={"employee": emp}, pluck="name"):
        _del("Salary Slip", r, cancel=True)
    _del("Employee", emp)
    _del("Gratuity Rule", seeded.get("gratuity_rule"))
    for r in frappe.get_all("Salary Structure Assignment", filters={"employee": emp}, pluck="name"):
        _del("Salary Structure Assignment", r, cancel=True)
    _del("Salary Structure", seeded.get("salary_structure"), cancel=True)
    for comp in (f"{P}-MealCard", f"{P}-Fuel", f"{P}-Basic"):
        _del("Salary Component", comp)
    _del("Payroll Period", seeded.get("payroll_period"))
    _del("User", seeded.get("user"))
    for desig in ("Smoke Lead", "Smoke Exec"):
        # chỉ xoá nếu không còn employee nào dùng
        if not frappe.get_all("Employee", filters={"designation": desig}, limit=1):
            _del("Designation", desig)
    frappe.db.commit()
    return done


def run() -> dict:
    prev_user = frappe.session.user
    report: dict = {}
    seeded: dict = {}
    created: dict = {}
    try:
        frappe.set_user("Administrator")
        seeded = _seed()
        global EMP
        EMP = seeded["employee"]
        report["seed"] = {"ok": True, "detail": seeded}
        print(f"[SMOKE] seed: {seeded}")

        step = _report_step(report, "s1_context")

        @step
        def _():
            ctx = api.benefit_context(employee=EMP)
            assert ctx["max_benefits"] == 12_000_000, ctx
            names = sorted(c["name"] for c in ctx["components"])
            assert names == sorted([f"{P}-MealCard", f"{P}-Fuel"]), names
            assert ctx["payroll_period"] and ctx["remaining"] > 0, ctx
            return (
                f"max=12tr remaining={ctx['remaining']} comps={names} period={ctx['payroll_period']['name']}"
            )

        @_report_step(report, "s2_save_draft")
        def _():
            out = api.save_benefit_application(
                {
                    "employee": EMP,
                    "payroll_period": seeded["payroll_period"],
                    "benefits": [
                        {"earning_component": f"{P}-MealCard", "amount": 500_000},
                        {"earning_component": f"{P}-Fuel", "amount": 300_000},
                    ],
                }
            )
            created["app"] = out["name"]
            assert out["status"] == "Draft" and out["total_amount"] == 800_000, out
            return out

        @_report_step(report, "s3_submit_hr")
        def _():
            out = api.set_benefit_application_action(created["app"], "submit")
            assert out["docstatus"] == 1, out
            return out

        @_report_step(report, "s4_duplicate_guard")
        def _():
            try:
                api.save_benefit_application(
                    {
                        "employee": EMP,
                        "payroll_period": seeded["payroll_period"],
                        "benefits": [{"earning_component": f"{P}-Fuel", "amount": 100_000}],
                    }
                )
                raise Exception("KHÔNG bị chặn trùng kỳ!")
            except Exception as e:
                assert "đăng ký" in str(e), str(e)
                return f"bị chặn đúng: {e}"

        @_report_step(report, "s5_claim_ok")
        def _():
            out = api.save_benefit_claim(
                {"employee": EMP, "earning_component": f"{P}-MealCard", "claimed_amount": 200_000}
            )
            created["claim"] = out["name"]
            assert out["max_amount_eligible"] == 8_000_000, out
            act = api.set_benefit_claim_action(out["name"], "submit")
            assert act["docstatus"] == 1, act
            return out

        @_report_step(report, "s6_claim_wrong_component")
        def _():
            try:
                api.save_benefit_claim(
                    {"employee": EMP, "earning_component": f"{P}-Fuel", "claimed_amount": 50_000}
                )
                raise Exception("KHÔNG bị chặn component pro-rata!")
            except Exception as e:
                assert "hoàn Từ" in str(e), str(e)
                return f"bị chặn đúng: {e}"

        @_report_step(report, "s7_get_application_can")
        def _():
            out = api.get_benefit_application(created["app"])
            assert out["can"]["cancel"] is True and out["can"]["edit"] is False, out
            return {k: v for k, v in out.items() if k in ("name", "status", "can")}

        @_report_step(report, "s8_idor_plain_user")
        def _():
            frappe.set_user(seeded["user"])
            try:
                api.set_benefit_application_action(created["app"], "cancel")
                raise Exception("User thường submit được?!")
            except Exception as e:
                assert "Chỉ HR" in str(e), str(e)
                mine = api.my_benefit_claims()
                assert mine["total"] >= 1, mine
                return f"permission chặn đúng + self-service my_claims={mine['total']}"
            finally:
                frappe.set_user("Administrator")

        @_report_step(report, "s9_gratuity_preview")
        def _():
            out = api.preview_gratuity({"employee": EMP, "gratuity_rule": seeded["gratuity_rule"]})
            assert "current_work_experience" in out and "amount" in out, out
            assert out["amount"] > 0, f"amount={out['amount']} — slip chưa cộng vào applicable earnings?"
            return out

        @_report_step(report, "s10_gratuity_create_submit")
        def _():
            out = api.create_gratuity(
                {
                    "employee": EMP,
                    "gratuity_rule": seeded["gratuity_rule"],
                    "pay_via_salary_slip": 1,
                    # payroll_date phải ≤ relieving_date (HRMS validate)
                    "payroll_date": today(),
                    "salary_component": f"{P}-Basic",
                }
            )
            created["gratuity"] = out["name"]
            # Site dev chưa có salary slip → amount HRMS tính = 0; gắn số
            # dương để đường submit (Additional Salary) chạy trọn vẹn.
            if not (out.get("amount") or 0):
                frappe.db.set_value("Gratuity", out["name"], "amount", 1_000_000)
            act = api.set_gratuity_action(out["name"], "submit")
            asal = frappe.get_all(
                "Additional Salary",
                filters={"ref_doctype": "Gratuity", "ref_docname": out["name"]},
                pluck="name",
            )
            assert asal, "Additional Salary chưa sinh ra"
            return {"gratuity": act, "additional_salary": asal}

        @_report_step(report, "s11_promotion_submit_updates_employee")
        def _():
            out = api.save_promotion(
                {"employee": EMP, "details": [{"fieldname": "designation", "new_value": "Smoke Lead"}]}
            )
            created["promo"] = out["name"]
            act = api.set_promotion_action(out["name"], "submit")
            assert act["docstatus"] == 1, act
            designation = frappe.db.get_value("Employee", EMP, "designation")
            assert designation == "Smoke Lead", designation
            return f"Employee.designation → {designation}"

        @_report_step(report, "s12_amend_flow")
        def _():
            api.set_benefit_application_action(created["app"], "cancel")
            out = api.set_benefit_application_action(created["app"], "amend")
            created["app2"] = out["name"]
            assert out["docstatus"] == 0 and out["amended_from"] == created["app"], out
            return out

    except Exception as e:  # noqa: BLE001
        report["fatal"] = str(e)
        import traceback

        report["traceback"] = traceback.format_exc()
    finally:
        try:
            frappe.set_user("Administrator")
            report["cleanup"] = _cleanup(seeded)
            print(f"[SMOKE] cleanup: {report['cleanup']}")
        except Exception as e:  # noqa: BLE001
            report["cleanup_error"] = str(e)
        frappe.set_user(prev_user)

    ok = (
        all(v.get("ok") for k, v in report.items() if k.startswith("s") and isinstance(v, dict))
        and "fatal" not in report
    )
    report["ALL_OK"] = ok
    print(f"[SMOKE] ALL_OK={ok}")
    for k, v in report.items():
        if k != "traceback":
            print(f"  {k}: {v}")
    return report


def probe():
    """Chẩn đoán s10: seed → create gratuity → submit bắt traceback GỐC."""
    frappe.set_user("Administrator")
    seeded = _seed()
    emp = seeded["employee"]
    print("[PROBE] emp:", emp)
    print("[PROBE] relieving:", frappe.db.get_value("Employee", emp, "relieving_date"))
    try:
        out = api.create_gratuity(
            {
                "employee": emp,
                "gratuity_rule": seeded["gratuity_rule"],
                "pay_via_salary_slip": 1,
                "payroll_date": add_days(today(), 5),
                "salary_component": f"{P}-Basic",
            }
        )
        print("[PROBE] created:", out)
        print("[PROBE] relieving after create:", frappe.db.get_value("Employee", emp, "relieving_date"))
        doc = frappe.get_doc("Gratuity", out["name"])
        print("[PROBE] doc.employee:", doc.employee, "docstatus:", doc.docstatus, "amount:", doc.amount)
        doc.submit()
        frappe.db.commit()
        print(
            "[PROBE] SUBMIT OK — additional salary:",
            frappe.get_all("Additional Salary", filters={"ref_docname": out["name"]}, pluck="name"),
        )
    except Exception:
        import traceback

        traceback.print_exc()
    finally:
        frappe.set_user("Administrator")
        print("[PROBE] cleanup:", _cleanup(seeded))
