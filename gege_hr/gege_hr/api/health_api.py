"""WP4 — health endpoints for the HR settings tab "Sức khoẻ hệ thống".

* ``get_health()``   — snapshot: per-job 🟢/🟡/🔴 + Error Log 24h count (HC3)
* ``ack_alert()``    — mark an overdue job as handled
* ``run_job_now()``  — execute a job synchronously, time it, record the
  heartbeat (HC6) so HR can revive/verify the engine from the UI.
"""

from __future__ import annotations

import time

import frappe
from frappe import _

from gege_hr.gege_hr.utils import health


def _assert_hr() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if not (roles & {"HR Manager", "System Manager", "Payroll Manager"}):
        frappe.throw(_("Chỉ HR Manager mới được xem sức khoẻ hệ thống."), frappe.PermissionError)


@frappe.whitelist()
def get_health() -> dict:
    """Snapshot every monitored scheduler job + 24h error count."""
    _assert_hr()
    return health.check_health()


@frappe.whitelist()
def ack_alert(job: str | None = None) -> dict:
    """Mark a job's overdue alert as handled (records who + when)."""
    _assert_hr()
    job = (job or "").strip()
    if not job or job not in health.HEARTBEATS:
        frappe.throw(_("Job không hợp lệ."), frappe.ValidationError)
    ok = health.ack_alert(job, user=frappe.session.user)
    return {"ok": bool(ok)}


@frappe.whitelist()
def run_job_now(job: str | None = None) -> dict:
    """Run a monitored job synchronously; returns duration + result (HC6).

    The heartbeat is only recorded when the run SUCCEEDS — identical to the
    scheduler semantics (HC4).
    """
    _assert_hr()
    job = (job or "").strip()
    cfg = health.HEARTBEATS.get(job)
    if not cfg:
        frappe.throw(_("Job không hợp lệ."), frappe.ValidationError)
    path = cfg.get("path")
    started = time.monotonic()
    try:
        fn = frappe.get_attr(path)
        result = fn()
    except Exception as exc:
        frappe.log_error(
            title=f"health.run_job_now failed {job}",
            message=frappe.get_traceback(),
        )
        frappe.throw(
            _("Chạy job thất bại: {0}").format(str(exc)),
            frappe.ValidationError,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        health.record_heartbeat(job, duration_ms=duration_ms, summary={"manual": True})
    except Exception:
        pass
    return {
        "ok": True,
        "job": job,
        "duration_ms": duration_ms,
        "result": result if isinstance(result, (dict, int, float, str, bool, type(None))) else str(result),
    }
