"""WP4 (prod-readiness-plan) — scheduler health-check / heartbeat monitoring.

Problem: a scheduler job that dies silently (worker crash, exception before
commit, stuck lock) only wrote an Error Log nobody read — payroll complaints
a WEEK later were the first signal. This module makes engine death VISIBLE
within hours:

* every monitored job records a heartbeat (cache + ``VN Scheduler Heartbeat``
  row) at the END of a successful run — a crashed run leaves the heartbeat
  stale, which is exactly what we want to detect;
* ``check_health()`` builds a per-job snapshot (🟢 fresh / 🟡 near-deadline /
  🔴 overdue) plus the 24h Error Log count;
* ``alert_if_unhealthy()`` (cron ``*/10``) raises Notifications for HR
  Managers + a WARN Error Log entry for every 🔴 job;
* ``daily_error_digest()`` (WP8) groups the last 24h of Error Logs by title
  and notifies HR-admins with the top 10.

Bench-free guard: every DB touch is wrapped — outside a bench the module
degrades to pure cache-less logic so unit tests run without a site.
"""

from __future__ import annotations

import json
import time

try:  # bench-free safe import
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None

HB_DOCTYPE = "VN Scheduler Heartbeat"
CACHE_PREFIX = "gege_hr:hb:"


def _now():
    """Current wall clock (indirection seam so tests can freeze time)."""
    from datetime import datetime as _dt

    return _dt.now()

# job_key → monitoring contract. max_minutes is the alert threshold (job older
# than this = 🔴); warn_minutes (default 75% of max) = 🟡.
HEARTBEATS: dict[str, dict] = {
    "checkout_miss.run_hourly": {
        "label": "Tự đóng phiên quên checkout (mỗi giờ)",
        "path": "gege_hr.gege_hr.utils.checkout_miss.run_hourly",
        "max_minutes": 120,
    },
    "shift.generate_daily_shift_instances": {
        "label": "Sinh ca hằng ngày",
        "path": "gege_hr.gege_hr.api.shift.generate_daily_shift_instances",
        "max_minutes": 26 * 60,
    },
    "attendance.auto_mark_absent_job": {
        "label": "Tự đánh dấu vắng (02:00)",
        "path": "gege_hr.gege_hr.api.attendance.auto_mark_absent_job",
        "max_minutes": 26 * 60,
    },
    "payroll.auto_close_payroll": {
        "label": "Tự đóng kỳ lương (ngày 1, 07:30)",
        "path": "gege_hr.gege_hr.api.payroll.auto_close_payroll",
        "max_minutes": 32 * 60,  # monthly cadence — red only if a MONTH passes
        "monthly": True,
    },
}


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def record_heartbeat(job_key: str, duration_ms: int = 0, summary: dict | None = None) -> None:
    """Stamp a successful run of ``job_key`` (cache + persistent row).

    Called at the END of each job — on success ONLY, so a crashing job never
    refreshes its heartbeat (HC4). Best-effort: never raises into the job.
    """
    if frappe is None:
        return
    try:
        frappe.cache().set(CACHE_PREFIX + job_key, frappe.utils.now())
    except Exception:
        pass
    try:
        now = frappe.utils.now()
        name = frappe.db.get_value(HB_DOCTYPE, {"job_key": job_key}, "name")
        payload = {
            "job_label": HEARTBEATS.get(job_key, {}).get("label") or job_key,
            "last_run": now,
            "duration_ms": int(duration_ms or 0),
            "status": "Ok",
            "last_summary": json.dumps(summary or {}, default=str, ensure_ascii=False),
        }
        if name:
            frappe.db.set_value(HB_DOCTYPE, name, payload)
        else:
            doc = frappe.get_doc({"doctype": HB_DOCTYPE, "job_key": job_key, "run_count": 1, **payload})
            doc.insert(ignore_permissions=True)
            return
        # bump run_count separately (set_value can't do expressions portably)
        try:
            count = frappe.db.get_value(HB_DOCTYPE, name, "run_count") or 0
            frappe.db.set_value(HB_DOCTYPE, name, "run_count", int(count) + 1)
        except Exception:
            pass
        frappe.db.commit()
    except Exception:
        try:
            frappe.log_error(title=f"health.record_heartbeat failed {job_key}")
        except Exception:
            pass


def run_job_with_heartbeat(job_key: str, fn, *args, **kwargs):
    """Execute ``fn`` and record a heartbeat when (and only when) it succeeds.

    Returns ``fn``'s result; exceptions propagate after a failed-run Error Log
    (the heartbeat intentionally stays untouched — HC4).
    """
    started = time.monotonic()
    result = fn(*args, **kwargs)
    try:
        record_heartbeat(
            job_key,
            duration_ms=int((time.monotonic() - started) * 1000),
            summary={"result": result} if isinstance(result, dict) else None,
        )
    except Exception:
        pass
    return result


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def _last_run(job_key: str) -> str | None:
    """Most recent heartbeat timestamp (cache first, DB fallback)."""
    if frappe is None:
        return None
    try:
        cached = frappe.cache().get(CACHE_PREFIX + job_key)
        if cached:
            if isinstance(cached, bytes):
                cached = cached.decode()
            return str(cached)
    except Exception:
        pass
    try:
        return frappe.db.get_value(HB_DOCTYPE, {"job_key": job_key}, "last_run")
    except Exception:
        return None


def classify(job_key: str, minutes_ago: float | None) -> str:
    """🟢 fresh / 🟡 near deadline / 🔴 overdue (never-run = 🔴)."""
    cfg = HEARTBEATS.get(job_key) or {}
    if minutes_ago is None:
        return "red"
    limit = float(cfg.get("max_minutes") or 120)
    if minutes_ago > limit:
        return "red"
    if minutes_ago > limit * 0.75:
        return "amber"
    return "green"


def error_log_count(hours: int = 24) -> int:
    """Error Logs created in the last ``hours`` (0 outside a bench)."""
    if frappe is None:
        return 0
    try:
        from datetime import timedelta as _td

        since = (_now() - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        return int(frappe.db.count("Error Log", {"creation": [">=", since]}) or 0)
    except Exception:
        return 0


def check_health() -> dict:
    """Snapshot every monitored job + the 24h error count (HC1/HC3)."""
    jobs: list[dict] = []
    for job_key, cfg in HEARTBEATS.items():
        last = _last_run(job_key)
        minutes_ago = None
        if last:
            try:
                from datetime import datetime as _dt

                dt = _dt.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")
                minutes_ago = (_now() - dt).total_seconds() / 60.0
            except Exception:
                minutes_ago = None
        jobs.append(
            {
                "job_key": job_key,
                "label": cfg.get("label") or job_key,
                "path": cfg.get("path"),
                "last_run": last,
                "minutes_ago": round(minutes_ago, 1) if minutes_ago is not None else None,
                "status": classify(job_key, minutes_ago),
                "max_minutes": cfg.get("max_minutes"),
            }
        )
    return {"jobs": jobs, "error_log_24h": error_log_count(24)}


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #
def _hr_manager_users() -> list[str]:
    """Users holding HR Manager / System Manager (deduped)."""
    if frappe is None:
        return []
    users: set[str] = set()
    for role in ("HR Manager", "System Manager"):
        try:
            for parent in frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent"):
                if parent and parent not in ("Administrator", "Guest"):
                    users.add(parent)
        except Exception:
            continue
    return sorted(users)


def _notify_users(users: list[str], subject: str, message: str) -> None:
    if frappe is None or not users:
        return
    for user in users:
        try:
            frappe.get_doc(
                {
                    "doctype": "Notification Log",
                    "subject": subject,
                    "for_user": user,
                    "type": "Alert",
                    "email_content": message,
                    "document_type": "VN Scheduler Heartbeat",
                }
            ).insert(ignore_permissions=True)
        except Exception:
            # Older Frappe: fall back to core Notification doctype name.
            try:
                frappe.get_doc(
                    {
                        "doctype": "Notification",
                        "subject": subject,
                        "for_user": user,
                        "type": "Alert",
                        "email_content": message,
                    }
                ).insert(ignore_permissions=True)
            except Exception:
                pass
    try:
        frappe.db.commit()
    except Exception:
        pass


def alert_if_unhealthy() -> dict:
    """Cron ``*/10`` — alert HR Managers about every 🔴 job (HC2).

    A WARN Error Log entry is also written so the failure shows up in the
    digest/backup tooling even when notifications are ignored.
    """
    if frappe is None:
        return {"alerted": 0}
    snapshot = check_health()
    overdue = [j for j in snapshot["jobs"] if j["status"] == "red"]
    if not overdue:
        return {"alerted": 0, "jobs": len(snapshot["jobs"])}
    lines = [
        f"• {j['label']} ({j['job_key']}): lần chạy cuối {j['last_run'] or 'KHÔNG BAO GIỜ'}"
        for j in overdue
    ]
    message = (
        "Các job định kỳ sau ĐÃ QUÁ HẠN — engine có thể đã chết:\n" + "\n".join(lines) +
        "\nKiểm tra /hr/admin/health và Scheduled Job Type."
    )
    _notify_users(_hr_manager_users(), "[GeGe HR] Scheduler quá hạn", message)
    try:
        frappe.log_error(
            title="scheduler health: overdue jobs",
            message="\n".join(f"{j['job_key']} last_run={j['last_run']}" for j in overdue),
        )
    except Exception:
        pass
    return {"alerted": len(overdue)}


def ack_alert(job_key: str, user: str | None = None) -> bool:
    """Mark a job's alert as handled (HR saw it and owns the fix)."""
    if frappe is None:
        return False
    try:
        name = frappe.db.get_value(HB_DOCTYPE, {"job_key": job_key}, "name")
        if not name:
            return False
        frappe.db.set_value(
            HB_DOCTYPE,
            name,
            {
                "status": "Acked",
                "acknowledged_by": user or (frappe.session.user if hasattr(frappe, "session") else "system"),
                "acknowledged_at": frappe.utils.now(),
            },
        )
        frappe.db.commit()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# WP8 — daily Error Log digest
# --------------------------------------------------------------------------- #
def daily_error_digest() -> dict:
    """Daily Notification: last-24h Error Logs grouped by title (top 10)."""
    if frappe is None:
        return {"titles": 0}
    try:
        from datetime import timedelta as _td

        since = (_now() - _td(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        rows = frappe.db.get_all(
            "Error Log",
            filters={"creation": [">=", since]},
            fields=["name", "method"],
            limit=10000,
        )
    except Exception:
        rows = []
    counts: dict[str, int] = {}
    for r in rows or []:
        title = (r.get("method") or "(không rõ)")[:120]
        counts[title] = counts.get(title, 0) + 1
    if not counts:
        return {"titles": 0, "total": 0}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
    body = "\n".join(f"• [{n:4d}×] {t}" for t, n in top)
    _notify_users(
        _hr_manager_users(),
        f"[GeGe HR] Error Log 24h: {sum(counts.values())} lỗi / {len(counts)} loại",
        body,
    )
    return {"titles": len(counts), "total": sum(counts.values())}
