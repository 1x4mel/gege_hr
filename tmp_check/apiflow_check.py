"""Runtime API-flow smoke test (run: bench --site X execute gege_hr.tmp_check.apiflow_check.run)"""
import frappe


def run():
    import traceback

    lines = []
    emp_row = frappe.db.sql(
        "SELECT e.name, e.user_id FROM `tabEmployee` e"
        " WHERE e.user_id IS NOT NULL AND e.user_id != '' AND e.status='Active' LIMIT 1",
        as_dict=True,
    )
    emp, user = emp_row[0].name, emp_row[0].user_id
    lines.append("AS " + user)
    frappe.set_user(user)

    def t(label, mod, fn):
        try:
            m = __import__(f"gege_hr.gege_hr.api.{mod}", fromlist=[fn])
            r = getattr(m, fn)()
            lines.append(f"[OK] {mod}.{fn} -> {str(r)[:80]}")
        except Exception:
            frappe.db.rollback()
            lines.append(f"[FAIL] {mod}.{fn} :: {traceback.format_exc().splitlines()[-1][:160]}")

    t("auth", "auth", "me")
    t("today_status", "attendance", "today_status")
    t("my_logs", "attendance", "my_logs")
    t("my_monthly", "attendance", "my_monthly_summary")
    t("leave_balance", "leave", "my_leave_balance")
    t("my_applications", "leave", "my_applications")
    t("notifications", "notification", "get_notifications")
    t("pending_approvals", "approval", "get_pending_approvals")
    t("payslips", "payroll", "my_payslips")
    t("dashboard", "dashboard", "get_employee_dashboard")
    t("checkout_miss", "checkout_miss", "my_checkout_misses")
    print("\n".join(lines))
