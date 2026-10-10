"""
Shift API — plan v5 §10.3 + plans/plan-team-schedule-desk-free.md.

Milestone-2 materialises VN Employee Shift Instance rows from Shift Assignment
(``generate_daily_shift_instances``) and enqueues work-session recalculation
on submit (``on_shift_instance_submit``). The read-only ``my_schedule`` /
``shift_type_options`` endpoints drive the personal Schedule view, while the
batched ``team_schedule_grid`` / ``team_schedule_context`` power the desk-free
/hr/team/schedule command center (per-cell ``can`` matrix, leave/OT overlays,
coverage gaps, CSV export + token-gated ICS feed).
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from gege_hr.gege_hr.utils import employee as emp_utils, tz as tz_utils

# ---------------------------------------------------------------------------
# Shift Type validate hook — keep Frappe-native auto-attendance windows in sync
# with the gege_hr custom fields. The portal UI edits ``vn_*`` custom fields
# (e.g. vn_max_checkout_after_end_minutes = 360) but Frappe's NATIVE auto-
# attendance reads its own fields (allow_check_out_after_shift_end_time), which
# previously stayed at the default 60 → overnight checkouts ~70 min after the
# shift end were dropped / mis-paired. Mirroring the values on every save makes
# native Attendance pairing agree with the Work-Session engine.
# ---------------------------------------------------------------------------
_NATIVE_FROM_CUSTOM = {
    "allow_check_out_after_shift_end_time": "vn_max_checkout_after_end_minutes",
    "begin_check_in_before_shift_start_time": "vn_earliest_checkin_minutes",
}


def sync_native_shift_windows(doc, method: str | None = None) -> None:
    """``Shift Type.validate`` hook — mirror gege_hr custom windows → native."""
    for native, custom in _NATIVE_FROM_CUSTOM.items():
        custom_val = getattr(doc, custom, None)
        if custom_val is None:
            continue
        try:
            setattr(doc, native, custom_val)
        except Exception:
            # Field may be absent on a stripped meta — never block the save.
            pass


@frappe.whitelist()
def my_schedule(
    employee: str | None = None, from_date: str | None = None, to_date: str | None = None
) -> list[dict]:
    """Plan §10.3 — the employee's shift instances across a window."""
    # The SPA may pass the whole Employee object as ``employee``; reduce it to
    # its name string before using it as a filter value (see utils.employee.emp_name).
    employee_name = emp_utils.emp_name(employee) if employee else None
    own_emp = emp_utils.get_employee_for_user()
    # Hardening (plans/plan-schedule-desk-free.md §2.7): querying ANOTHER
    # employee's schedule requires a manager role — previously any employee
    # could read anyone's shifts by passing ``employee``.
    _assert_can_view_employee(employee_name, own_emp)
    emp = employee_name or own_emp
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today - timedelta(days=7)
    end = getdate(to_date) if to_date else today + timedelta(days=7)

    # Milestone-1 derives the schedule from Shift Assignment (no Shift Instance
    # DocType yet). Each active assignment expands into per-day planned windows.
    out: list[dict] = []
    has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    sa_fields = ["name", "shift_type", "start_date", "end_date"]
    if has_loc_field:
        sa_fields.append("vn_work_location")
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters={"employee": emp, "status": "Active", "docstatus": 1, "start_date": ["<=", end]},
        fields=sa_fields,
    )
    work_location_names = {a.get("vn_work_location") for a in assignments if a.get("vn_work_location")}
    loc_label_by_name = (
        {
            r["name"]: r["location_name"]
            for r in frappe.db.get_all(
                "VN Work Location",
                filters={"name": ["in", list(work_location_names)] or [""]},
                fields=["name", "location_name"],
            )
        }
        if work_location_names
        else {}
    )
    day = start
    while day <= end:
        for a in assignments:
            a_start = getdate(a.start_date)
            a_end = getdate(a.end_date) if a.end_date else end
            if a_start <= day <= a_end:
                st = frappe.get_cached_doc("Shift Type", a.shift_type)
                planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
                wl = a.get("vn_work_location") or None
                out.append(
                    {
                        "work_date": day.isoformat(),
                        "shift_type": a.shift_type,
                        "start_time": str(st.start_time),
                        "end_time": str(st.end_time),
                        "is_overnight": tz_utils.is_overnight(st.start_time, st.end_time),
                        # PHASE-1 FRAME: naive wall ISO (no Z) for the SPA.
                        "planned_start": tz_utils.wall(planned_start).isoformat(),
                        "planned_end": tz_utils.wall(planned_end).isoformat(),
                        "shift_assignment": a.name,
                        "work_location": wl,
                        "work_location_name": loc_label_by_name.get(wl) if wl else None,
                    }
                )
        day += timedelta(days=1)
    return out


@frappe.whitelist()
def shift_type_options() -> list[dict]:
    """Plan §10.3 — active shift types for selectors.

    ``disabled`` is handled defensively (bench-verified on erp.local): this
    hrms build may not even HAVE the column (pymysql 1054), and where it
    exists it is often NULL — a SQL ``disabled = 0`` filter then silently
    drops every row and the employee selector comes back empty (desk-free
    plan §3.4 ShiftRequestModal dependency). Meta-check first, filter in
    Python only when the column exists.
    """
    has_disabled = False
    try:
        has_disabled = bool(frappe.get_meta("Shift Type").get_field("disabled"))
    except Exception:
        has_disabled = False
    fields = ["name", "start_time", "end_time"] + (["disabled"] if has_disabled else [])
    rows = frappe.db.get_all("Shift Type", fields=fields, order_by="name")
    return [
        {
            "value": r.name,
            "label": r.name,
            "description": f"{r.start_time}–{r.end_time}",
        }
        for r in rows
        if not (getattr(r, "disabled", None) or 0)
    ]


@frappe.whitelist()
def team_schedule(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Plan §10.3 — manager team schedule window (LEGACY shape, deprecated).

    Kept for backward compatibility with deployed SPA calls. New code (and the
    /hr/team/schedule grid) should use :func:`team_schedule_grid` — batched,
    instance-aware, with a per-cell action matrix
    (plans/plan-team-schedule-desk-free.md §4 WP1).
    """
    manager_emp, is_hr = _team_schedule_viewer()
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today
    end = getdate(to_date) if to_date else today + timedelta(days=6)
    members = _team_scope_members(manager_emp, is_hr)
    result = []
    for m in members:
        sched = my_schedule(employee=m.name, from_date=start.isoformat(), to_date=end.isoformat())
        result.append({"name": m.get("name"), "employee_name": m.get("employee_name"), "shifts": sched})
    return result


# --------------------------------------------------------------------------- #
# /hr/team/schedule desk-free grid (plans/plan-team-schedule-desk-free.md §4).
# Scope: Line Manager → own ``reports_to`` team; HR roles → company-wide
# (optionally filtered by department). Every read is BATCHED (fixed number of
# queries per request — never per-member loops) and every cell carries a
# server-computed ``can`` action matrix, mirroring the Team Today principle
# ("BE là nguồn sự thật duy nhất" — see attendance.team_day_can).
# --------------------------------------------------------------------------- #
TEAM_SCHEDULE_MAX_DAYS = 31
_TEAM_SCHEDULE_HR_ROLES = {"HR Manager", "HR User", "System Manager"}


def _team_schedule_viewer() -> tuple[str | None, bool]:
    """Resolve the team-schedule caller → ``(manager_emp, is_hr)``.

    Gate: any SCHEDULE_MANAGER_ROLES member may open the team grid; a Line
    Manager is scoped to their reports_to team, HR roles to the company.
    Anyone else → PermissionError (plain employees belong on /hr/schedule).
    """
    roles: set = set()
    try:
        roles = set(frappe.get_roles() or [])
    except Exception:
        roles = set()
    if not roles & set(SCHEDULE_MANAGER_ROLES):
        frappe.throw(_("Bạn không có quyền xem lịch làm việc của team."), frappe.PermissionError)
    is_hr = bool(roles & _TEAM_SCHEDULE_HR_ROLES)
    return emp_utils.get_employee_for_user(), is_hr


def _team_scope_members(manager_emp: str | None, is_hr: bool, department: str | None = None) -> list[dict]:
    """Active employees visible to the caller (one query — plan §4 WP1).

    Line Manager → ``reports_to = manager_emp`` only. HR roles → every Active
    employee (department filter applied server-side when given). An LM without
    a linked Employee sees nobody.
    """
    if not is_hr and not manager_emp:
        return []
    filters: dict = {"status": "Active"}
    if not is_hr:
        filters["reports_to"] = manager_emp
    if department:
        filters["department"] = department
    try:
        return frappe.db.get_all(
            "Employee", filters=filters, fields=["name", "employee_name", "department", "reports_to"]
        )
    except Exception:
        # Stripped benches may lack a column — degrade to the core fields.
        return frappe.db.get_all("Employee", filters=filters, fields=["name", "employee_name"])


def _grid_pending_requests_by_emp(emps: list[str], start, end) -> dict[str, list[dict]]:
    """Draft Shift Requests per employee overlapping the window (one query).

    Fetched broad (docstatus/status filters only) then narrowed in Python —
    keeps the stub harness (``_FakeDB2`` has no ``in`` support) and the bench
    on identical semantics.
    """
    out: dict[str, list[dict]] = {}
    if not emps:
        return out
    try:
        rows = frappe.db.get_all(
            "Shift Request",
            filters=[["docstatus", "=", 0], ["status", "=", "Draft"]],
            fields=["name", "employee", "shift_type", "from_date", "to_date"],
        )
    except Exception:
        return out
    emp_set = set(emps)
    for r in rows or []:
        emp = getattr(r, "employee", None)
        if emp not in emp_set:
            continue
        if not _date_overlaps(getattr(r, "from_date", None), getattr(r, "to_date", None), start, end):
            continue
        out.setdefault(emp, []).append(
            {
                "name": getattr(r, "name", None),
                "shift_type": getattr(r, "shift_type", None),
                "from_date": str(getattr(r, "from_date", "") or ""),
                "to_date": str(getattr(r, "to_date", "") or ""),
            }
        )
    return out


def _grid_cell_can(
    *,
    is_hr: bool,
    is_lm_of: bool,
    locked: bool = False,
    day=None,
    today=None,
    has_assignment: bool = False,
    has_instance: bool = False,
    instance_status: str | None = None,
    has_pending_request: bool = False,
) -> dict:
    """Pure — per-cell action matrix for the team grid (plan §4 WP3).

    Mirrors ``attendance.team_day_can``: the FE renders buttons from this
    matrix only. Rules: past days and locked periods are read-only; an
    ``Active``/``Completed`` instance is engine-owned (parity with
    :func:`set_shift_instance_status` refusals); a Line Manager only acts on
    own-report members.
    """
    today = today or getdate()
    manage = bool(is_hr or is_lm_of) and not bool(locked)
    if day is not None and getdate(day) < getdate(today):
        manage = False
    status = str(instance_status or "")
    if status in ("Active", "Completed"):
        manage = False
    return {
        "view_detail": True,
        "assign": bool(manage and not has_assignment),
        "override": bool(manage and has_assignment),
        "skip": bool(manage and has_instance and status in ("Scheduled", "Skipped", "Cancelled")),
        "amend": bool(manage and has_assignment),
        "end": bool(manage and has_assignment),
        "approve": bool(manage and has_pending_request),
    }


def _grid_locked_dates(start, end) -> list[str]:
    """Dates in the window locked by a VN Monthly Attendance Period (fail-soft)."""
    try:
        from gege_hr.gege_hr.api.attendance import _is_date_locked

        out = []
        day = start
        while day <= end:
            if _is_date_locked(str(day)):
                out.append(day.isoformat())
            day = add_days(day, 1)
        return out
    except Exception:
        return []


def _grid_filter_options() -> dict:
    """Dropdown options for the team-grid toolbar (fail-soft per group)."""
    out: dict = {"shift_types": [], "work_locations": [], "departments": []}
    try:
        out["shift_types"] = shift_type_options()
    except Exception:
        pass
    try:
        rows = frappe.db.get_all("VN Work Location", fields=["name", "location_name"])
        out["work_locations"] = [
            {"value": r.get("name"), "label": r.get("location_name") or r.get("name")} for r in rows
        ]
    except Exception:
        pass
    try:
        rows = frappe.db.get_all("Employee", filters={"status": "Active"}, fields=["department"])
        seen: list[str] = []
        for r in rows or []:
            d = getattr(r, "department", None) if not isinstance(r, dict) else r.get("department")
            if d and d not in seen:
                seen.append(d)
        out["departments"] = [{"value": d, "label": d} for d in sorted(seen)]
    except Exception:
        pass
    return out


@frappe.whitelist()
def team_schedule_context(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Plan §4 WP2 — role/context payload powering the /hr/team/schedule toolbar.

    Returns the viewer, the resolved scope, the toolbar ``can`` matrix, the
    in-window pending Shift Request count (scope-limited) and the dropdown
    filter options. Plain employees get PermissionError (their surface is
    /hr/schedule).
    """
    manager_emp, is_hr = _team_schedule_viewer()
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today
    end = getdate(to_date) if to_date else add_days(today, 6)
    members = _team_scope_members(manager_emp, is_hr)
    emp_names = [m.get("name") for m in members if m.get("name")]

    is_lm = bool(manager_emp) and not is_hr
    try:
        _roles = set(frappe.get_roles() or [])
    except Exception:
        _roles = set()
    # Bulk endpoints keep the strict HR-admin gate (HR Manager / System Manager).
    bulk_ok = bool(_roles & {"HR Manager", "System Manager"})
    pending = _grid_pending_requests_by_emp(emp_names, start, end)
    pending_count = sum(len(v) for v in pending.values())
    return {
        "viewer_employee": manager_emp,
        "scope": {"mode": "company" if is_hr else "team", "member_count": len(members)},
        "can": {
            "view_grid": True,
            "assign": bool(is_hr or is_lm),
            "override_day": bool(is_hr or is_lm),
            "end": bool(is_hr or is_lm),
            "amend": bool(is_hr or is_lm),
            "approve_requests": bool(is_hr or is_lm),
            "bulk": bulk_ok,
            "export": bool(is_hr or is_lm),
        },
        "pending_approvals": {"count": pending_count, "link": "team_shift_requests?status=Draft"},
        "filters": _grid_filter_options(),
        "window": {
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "max_days": TEAM_SCHEDULE_MAX_DAYS,
        },
    }


@frappe.whitelist()
def team_schedule_grid(
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    department: str | None = None,
    coverage: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Plan §4 WP3 — the batched team grid: members × days with cell truth.

    One request = fixed query budget (members, Shift Assignments, Shift
    Instances, approved Leave, approved OT, pending Shift Requests, locks).
    Each cell reports the materialised ``VN Employee Shift Instance`` status
    (Skipped/Cancelled beat the covering Shift Assignment), leave/OT chips and
    its ``can`` action matrix. ``coverage="unassigned"`` keeps only members
    with ≥1 future working gap (empty, not-on-leave day).
    """
    manager_emp, is_hr = _team_schedule_viewer()
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today
    end = getdate(to_date) if to_date else add_days(today, 6)
    if end < start:
        start, end = end, start
    if (end - start).days + 1 > TEAM_SCHEDULE_MAX_DAYS:
        frappe.throw(_("Cửa sổ lịch tối đa {0} ngày.").format(TEAM_SCHEDULE_MAX_DAYS), frappe.ValidationError)

    is_lm = bool(manager_emp) and not is_hr
    members = _team_scope_members(manager_emp, is_hr, department=department)
    q = str(search or "").strip().lower()
    if q:
        members = [m for m in members if q in f"{m.get('employee_name') or ''} {m.get('name') or ''}".lower()]
    emps = [m.get("name") for m in members if m.get("name")]
    emp_set = set(emps)

    # ── Batched reads (filters help the bench; Python guards keep the stub
    #    harness on identical semantics — plan §4 WP3) ───────────────────────
    sa_fields = ["name", "employee", "shift_type", "start_date", "end_date"]
    has_loc_field = False
    try:
        has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    except Exception:
        has_loc_field = False
    if has_loc_field:
        sa_fields.append("vn_work_location")
    try:
        sa_rows = frappe.db.get_all(
            "Shift Assignment",
            filters={"status": "Active", "docstatus": 1, "start_date": ["<=", end]},
            fields=sa_fields,
        )
    except Exception:
        sa_rows = []
    sa_by_emp: dict[str, list[dict]] = {}
    for r in sa_rows or []:
        emp = getattr(r, "employee", None)
        if emp in emp_set:
            sa_by_emp.setdefault(emp, []).append(r)

    try:
        inst_rows = frappe.db.get_all(
            "VN Employee Shift Instance",
            filters={"docstatus": ["!=", 2]},
            fields=[
                "name",
                "employee",
                "work_date",
                "shift_type",
                "status",
                "work_location",
                "work_location_name",
            ],
        )
    except Exception:
        inst_rows = []
    inst_by: dict[tuple[str, str], dict] = {}
    for r in inst_rows or []:
        emp = getattr(r, "employee", None)
        wd = str(getattr(r, "work_date", "") or "")[:10]
        if emp in emp_set and str(start) <= wd <= str(end):
            inst_by[(emp, wd)] = r

    try:
        leave_rows = frappe.db.get_all(
            "Leave Application",
            filters={"status": "Approved", "docstatus": 1},
            fields=["name", "employee", "leave_type", "from_date", "to_date", "half_day"],
        )
    except Exception:
        try:
            leave_rows = frappe.db.get_all(
                "Leave Application",
                filters={"status": "Approved", "docstatus": 1},
                fields=["name", "employee", "leave_type", "from_date", "to_date"],
            )
        except Exception:
            leave_rows = []
    leave_by: dict[tuple[str, str], dict] = {}
    for r in leave_rows or []:
        emp = getattr(r, "employee", None)
        if emp not in emp_set:
            continue
        if not _date_overlaps(getattr(r, "from_date", None), getattr(r, "to_date", None), start, end):
            continue
        day = getdate(getattr(r, "from_date", None) or start)
        last = getdate(getattr(r, "to_date", None) or end)
        while day <= last:
            if start <= day <= end:
                leave_by.setdefault(
                    (emp, day.isoformat()),
                    {
                        "name": getattr(r, "name", None),
                        "type": getattr(r, "leave_type", None),
                        "half_day": bool(getattr(r, "half_day", False)),
                    },
                )
            day = add_days(day, 1)

    try:
        ot_rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters={"docstatus": ["!=", 2]},
            fields=["employee", "work_date", "approved_overtime_hours"],
        )
    except Exception:
        ot_rows = []
    ot_by: dict[tuple[str, str], float] = {}
    for r in ot_rows or []:
        emp = getattr(r, "employee", None)
        wd = str(getattr(r, "work_date", "") or "")[:10]
        if emp in emp_set and str(start) <= wd <= str(end):
            try:
                ot_by[(emp, wd)] = round(float(getattr(r, "approved_overtime_hours", 0) or 0), 2)
            except (TypeError, ValueError):
                ot_by[(emp, wd)] = 0.0

    sr_by_emp = _grid_pending_requests_by_emp(emps, start, end)
    locked = set(_grid_locked_dates(start, end))

    # Shift Type cache for window labels (start_time/end_time strings).
    st_cache: dict[str, dict] = {}

    def _st(name: str | None):
        if not name:
            return None
        if name not in st_cache:
            try:
                doc = frappe.get_cached_doc("Shift Type", name)
                st_cache[name] = {
                    "start_time": str(doc.start_time),
                    "end_time": str(doc.end_time),
                    "is_overnight": tz_utils.is_overnight(doc.start_time, doc.end_time),
                }
            except Exception:
                st_cache[name] = {"start_time": "", "end_time": "", "is_overnight": False}
        return st_cache[name]

    def _sa_covers(r, day_iso: str) -> bool:
        s = str(getattr(r, "start_date", "") or "")[:10]
        e = str(getattr(r, "end_date", "") or "2999-12-31")[:10]
        return s <= day_iso <= e

    # ── Build member rows (all matching search/department), then coverage ──
    rows: list[dict] = []
    unassigned_total = 0
    day = start
    days_list = []
    while day <= end:
        days_list.append(day.isoformat())
        day = add_days(day, 1)

    for m in members:
        emp = m.get("name")
        pend = sr_by_emp.get(emp, [])
        pend_spans = [(str(p["from_date"])[:10], str(p["to_date"])[:10] or "2999-12-31") for p in pend]
        cells = []
        member_gaps = 0
        for d_iso in days_list:
            inst = inst_by.get((emp, d_iso))
            covering = [r for r in sa_by_emp.get(emp, []) if _sa_covers(r, d_iso)]
            sa = covering[0] if covering else None
            st_name = (
                getattr(inst, "shift_type", None)
                if inst
                else (getattr(sa, "shift_type", None) if sa else None)
            )
            meta = _st(st_name) or {"start_time": "", "end_time": "", "is_overnight": False}
            leave = leave_by.get((emp, d_iso))
            ot_hours = ot_by.get((emp, d_iso), 0.0)
            instance_status = str(getattr(inst, "status", "") or "") if inst else ""
            has_pending = any(ps <= d_iso <= pe for ps, pe in pend_spans)
            cell = {
                "date": d_iso,
                "shift_type": st_name,
                "start_time": meta["start_time"],
                "end_time": meta["end_time"],
                "is_overnight": bool(meta["is_overnight"]),
                "instance": getattr(inst, "name", None) if inst else None,
                "instance_status": instance_status or None,
                "shift_assignment": getattr(sa, "name", None) if sa else None,
                "work_location": (getattr(inst, "work_location", None) if inst else None)
                or (getattr(sa, "vn_work_location", None) if sa else None),
                "work_location_name": (getattr(inst, "work_location_name", None) if inst else None),
                "leave": leave,
                "ot_hours": ot_hours,
            }
            cell["can"] = _grid_cell_can(
                is_hr=is_hr,
                is_lm_of=is_lm,  # every scope member of an LM is a direct report
                locked=d_iso in locked,
                day=d_iso,
                today=today,
                has_assignment=bool(sa or inst),
                has_instance=bool(inst),
                instance_status=instance_status or None,
                has_pending_request=has_pending,
            )
            if not sa and not inst and not leave and d_iso >= today.isoformat():
                cell["unassigned"] = True
                member_gaps += 1
                unassigned_total += 1
            cells.append(cell)
        rows.append(
            {
                "employee": emp,
                "employee_name": m.get("employee_name"),
                "department": m.get("department"),
                "pending_requests": len(pend),
                "days": cells,
                "_gaps": member_gaps,
            }
        )

    if str(coverage or "").strip() == "unassigned":
        rows = [r for r in rows if r.pop("_gaps", 0) > 0]
    else:
        for r in rows:
            r.pop("_gaps", None)

    # ── Paging over members ──────────────────────────────────────────────────
    try:
        page_i = max(1, int(page or 1))
        size_i = max(1, min(200, int(page_size or 50)))
    except (TypeError, ValueError):
        page_i, size_i = 1, 50
    total_pages = max(1, -(-len(rows) // size_i))
    rows_page = rows[(page_i - 1) * size_i : page_i * size_i]

    return {
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "locked_dates": sorted(locked),
        "summary": {
            "members": len(rows),
            "unassigned_days": unassigned_total,
            "pending_requests": sum(r["pending_requests"] for r in rows),
            "page": page_i,
            "page_size": size_i,
            "pages": total_pages,
        },
        "members": rows_page,
    }


@frappe.whitelist()
def team_schedule_export(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Plan §4 WP6 — CSV export of the team grid window (HR / Line Manager).

    Streams the same rows the grid renders (page_size capped at 200 — teams
    are small). Sets the Frappe download response when available; always
    returns the payload too so stub harnesses can assert on it.
    """
    import csv
    import io

    grid = team_schedule_grid(from_date=from_date, to_date=to_date, page_size=200)
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(
        [
            "employee",
            "employee_name",
            "department",
            "date",
            "shift_type",
            "start_time",
            "end_time",
            "instance_status",
            "leave_type",
            "ot_hours",
            "work_location",
        ]
    )
    for m in grid.get("members") or []:
        for c in m.get("days") or []:
            writer.writerow(
                [
                    m.get("employee"),
                    m.get("employee_name"),
                    m.get("department"),
                    c.get("date"),
                    c.get("shift_type") or "",
                    c.get("start_time") or "",
                    c.get("end_time") or "",
                    c.get("instance_status") or "",
                    (c.get("leave") or {}).get("type") or "",
                    c.get("ot_hours") or 0,
                    c.get("work_location_name") or c.get("work_location") or "",
                ]
            )
    content = buf.getvalue()
    filename = f"team_schedule_{grid['from_date']}_{grid['to_date']}.csv"
    try:
        frappe.response["filename"] = filename
        frappe.response["filecontent"] = content
        frappe.response["type"] = "csv"
    except Exception:
        pass
    return {
        "filename": filename,
        "rows": len((grid.get("members") or [])[0].get("days") or []) if grid.get("members") else 0,
        "content": content,
    }


def _schedule_ics_token(employee: str) -> str:
    """Deterministic per-employee feed token (site-secret HMAC — no schema)."""
    import hashlib

    try:
        secret = str(frappe.get_site_config().get("encryption_key") or "gege-hr")
    except Exception:
        secret = "gege-hr"
    return hashlib.sha256(f"{secret}:schedule-ics:{employee}".encode()).hexdigest()[:32]


@frappe.whitelist(allow_guest=True)
def team_schedule_ics(employee: str | None = None, token: str | None = None) -> str:
    """Plan §4 WP6 — per-employee ICS feed of the next 60 days.

    Read-only, token-gated (``_schedule_ics_token``), no session needed — so
    the employee can subscribe from Google/Outlook. Floating local datetimes
    (portal wall clock) are used deliberately: the feed follows whatever TZ
    the calendar client is in.
    """
    employee_name = emp_utils.emp_name(employee) if employee else None
    if not employee_name or not token or token != _schedule_ics_token(employee_name):
        frappe.throw(_("Liên kết lịch không hợp lệ."), frappe.PermissionError)
    today = tz_utils.now_in_portal().date()
    start = today
    end = add_days(today, 60)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gege_hr//Team Schedule//VI",
        "CALSCALE:GREGORIAN",
    ]
    try:
        has_loc = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
        sa_fields = ["name", "shift_type", "start_date", "end_date"]
        if has_loc:
            sa_fields.append("vn_work_location")
        assignments = frappe.db.get_all(
            "Shift Assignment",
            filters={"employee": employee_name, "status": "Active", "docstatus": 1},
            fields=sa_fields,
        )
    except Exception:
        assignments = []
    day = start
    while day <= end:
        for a in assignments or []:
            a_start = str(getattr(a, "start_date", "") or "")[:10]
            a_end = str(getattr(a, "end_date", "") or "2999-12-31")[:10]
            if not (a_start <= day.isoformat() <= a_end):
                continue
            try:
                st = frappe.get_cached_doc("Shift Type", a.shift_type)
            except Exception:
                continue
            if not st.start_time or not st.end_time:
                continue
            planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
            ps = tz_utils.wall(planned_start).strftime("%Y%m%dT%H%M%S")
            pe = tz_utils.wall(planned_end).strftime("%Y%m%dT%H%M%S")
            stamp = tz_utils.wall(tz_utils.now_in_portal()).strftime("%Y%m%dT%H%M%S")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{a.name}-{day.isoformat()}@gege-hr",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{ps}",
                f"DTEND:{pe}",
                f"SUMMARY:{a.shift_type}",
                "END:VEVENT",
            ]
        day = add_days(day, 1)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# --------------------------------------------------------------------------- #
# Desk-free /hr/schedule — self-service Shift Request + viewer context
# (plans/plan-schedule-desk-free.md §2). The SPA calendar page uses these to
# let an employee propose/withdraw a shift (native hrms "Shift Request",
# kept at Draft until HR approves via admin G9) and to know which actions
# the logged-in user may take.
# --------------------------------------------------------------------------- #

SCHEDULE_MANAGER_ROLES = ["HR Manager", "HR User", "System Manager", "Line Manager"]


def _is_schedule_manager() -> bool:
    """True when the session user may manage schedules (assign/override/skip)."""
    try:
        roles = set(frappe.get_roles() or [])
    except Exception:
        return False
    return bool(roles & set(SCHEDULE_MANAGER_ROLES))


def _assert_can_view_employee(employee_name: str | None, own_employee: str | None) -> None:
    """Harden ``my_schedule``: viewing ANOTHER employee requires a manager role."""
    if employee_name and own_employee and employee_name != own_employee:
        if not _is_schedule_manager():
            frappe.throw(
                _("Bạn không có quyền xem lịch làm việc của nhân viên khác."),
                frappe.PermissionError,
            )


def _audit_schedule(
    description: str,
    *,
    reference_doctype: str,
    reference_name: str,
    employee: str | None = None,
    new_value=None,
) -> None:
    """Best-effort audit row — mirrors admin._audit_admin (failures swallowed)."""
    try:
        from gege_hr.gege_hr.api import audit as audit_api

        company = None
        if employee:
            try:
                company = frappe.db.get_value("Employee", employee, "company")
            except Exception:
                company = None
        audit_api.log(
            "Manual Override",
            company=company,
            employee=employee,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
            new_value=new_value,
        )
    except Exception:
        pass


def _notify_schedule_updated(employee: str | None) -> None:
    """Best-effort realtime ping so open /hr/schedule tabs can refresh themselves."""
    try:
        frappe.publish_realtime("gege_hr:schedule_updated", {"employee": employee})
    except Exception:
        pass


def _resolve_shift_approver(employee: str) -> dict | None:
    """Standard HRMS resolution: Employee.shift_request_approver → Department
    Approver (parentfield=shift_request_approver, idx 1)."""
    user_id = None
    try:
        user_id = frappe.db.get_value("Employee", employee, "shift_request_approver")
    except Exception:
        user_id = None
    if not user_id:
        try:
            department = frappe.db.get_value("Employee", employee, "department")
        except Exception:
            department = None
        if department:
            try:
                user_id = frappe.db.get_value(
                    "Department Approver",
                    {"parent": department, "parentfield": "shift_request_approver", "idx": 1},
                    "approver",
                )
            except Exception:
                user_id = None
    if not user_id:
        return None
    full_name = None
    try:
        full_name = frappe.db.get_value("User", user_id, "full_name")
    except Exception:
        full_name = None
    return {"user": user_id, "full_name": full_name or user_id}


def _date_overlaps(a_start, a_end, b_start, b_end) -> bool:
    """Inclusive date-range overlap; ``None``/empty end = open-ended (till 2999)."""
    a_end = str(a_end or "2999-12-31")[:10]
    b_end = str(b_end or "2999-12-31")[:10]
    return str(a_start)[:10] <= b_end and str(b_start)[:10] <= a_end


def _assert_no_request_overlap(
    employee: str,
    shift_type: str,
    start,
    end,
    exclude_name: str | None = None,
) -> None:
    """Native ``validate_overlapping_shift_requests`` parity (date-overlap only):
    block when an Active Shift Assignment or another Draft Shift Request of the
    same employee already covers any day of the proposed span."""
    start_s = str(start)[:10]
    end_s = str(end)[:10] if end else "2999-12-31"
    rows = frappe.db.get_all(
        "Shift Assignment",
        filters=[["employee", "=", employee], ["status", "=", "Active"], ["docstatus", "=", 1]],
        fields=["name", "shift_type", "start_date", "end_date"],
    )
    conflicts = [
        r
        for r in rows
        if _date_overlaps(r.start_date, r.end_date, start_s, end_s)
        and getattr(r, "name", None) != exclude_name
    ]
    if conflicts:
        frappe.throw(
            _("Trùng ca đã gán: {0} ({1} → {2}).").format(
                ", ".join(str(c.shift_type) for c in conflicts),
                str(conflicts[0].start_date),
                str(conflicts[0].end_date or _("đến nay")),
            )
        )
    reqs = frappe.db.get_all(
        "Shift Request",
        filters=[["employee", "=", employee], ["docstatus", "=", 0], ["status", "=", "Draft"]],
        fields=["name", "shift_type", "from_date", "to_date"],
    )
    dup = [
        r
        for r in reqs
        if getattr(r, "name", None) != exclude_name and _date_overlaps(r.from_date, r.to_date, start_s, end_s)
    ]
    if dup:
        frappe.throw(
            _("Bạn đã có yêu cầu ca trùng khoảng ngày này: {0}.").format(", ".join(str(d.name) for d in dup))
        )


@frappe.whitelist()
def schedule_context(from_date: str | None = None, to_date: str | None = None) -> dict:
    """Plan §2.1 — role/context payload powering the /hr/schedule toolbar.

    Never throws for a plain employee: a user without a linked Employee simply
    gets ``viewer_employee: null`` + ``can.create_shift_request: false``.
    """
    today = getdate()
    try:
        start = getdate(from_date) if from_date else add_days(today, -90)
        end = getdate(to_date) if to_date else add_days(today, 90)
    except Exception:
        start, end = add_days(today, -90), add_days(today, 90)
    own_emp = emp_utils.get_employee_for_user()
    manager = _is_schedule_manager()

    def _in_window(row, start_field: str, end_field: str) -> bool:
        return _date_overlaps(getattr(row, start_field, None), getattr(row, end_field, None), start, end)

    out = {
        "viewer_employee": own_emp,
        "can": {"create_shift_request": bool(own_emp), "manage_schedule": manager},
        "approver": _resolve_shift_approver(own_emp) if own_emp else None,
        "my_requests": {"pending": 0},
        "pending_team_requests": 0,
    }
    if own_emp:
        reqs = frappe.db.get_all(
            "Shift Request",
            filters=[["employee", "=", own_emp], ["docstatus", "=", 0], ["status", "=", "Draft"]],
            fields=["name", "from_date", "to_date"],
        )
        out["my_requests"]["pending"] = len([r for r in reqs if _in_window(r, "from_date", "to_date")])
    if manager:
        try:
            drafts = frappe.db.get_all(
                "Shift Request",
                filters=[["docstatus", "=", 0], ["status", "=", "Draft"]],
                fields=["name", "from_date", "to_date"],
            )
            out["pending_team_requests"] = len([r for r in drafts if _in_window(r, "from_date", "to_date")])
        except Exception:
            out["pending_team_requests"] = 0
    return out


def _safe_sr_fields(fields: list[str]) -> list[str]:
    """Project only the Shift Request columns that exist on THIS hrms build.

    The live site's ``Shift Request`` has no ``reason`` column (caught by the
    bench smoke) — a bare get_all with a missing field raises pymysql 1054.
    Mirrors admin._safe_fields' contract; always keeps ``name``.
    """
    try:
        meta = frappe.get_meta("Shift Request")
        valid = {df.fieldname for df in meta.fields}
        valid |= {"name", "creation", "modified", "owner", "docstatus"}
    except Exception:
        return ["name"]
    out = [f for f in fields if f in valid]
    if "name" not in out:
        out.append("name")
    return out


@frappe.whitelist()
def my_shift_requests(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Plan §2.2 — the viewer's OWN Shift Requests (employee resolved from the
    session — no ``employee`` param, so one employee can never read another's)."""
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    today = getdate()
    start = getdate(from_date) if from_date else add_days(today, -90)
    end = getdate(to_date) if to_date else add_days(today, 90)
    rows = frappe.db.get_all(
        "Shift Request",
        filters=[["employee", "=", emp]],
        fields=_safe_sr_fields(
            [
                "name",
                "shift_type",
                "from_date",
                "to_date",
                "status",
                "docstatus",
                "approver",
                "reason",
                "creation",
            ]
        ),
    )
    rows = [r for r in rows if _date_overlaps(r.from_date, r.to_date, start, end)]
    # Approved requests → the Shift Assignment they produced. The link field
    # lives on the SA side (``shift_request``), so reverse-map it here.
    sa_by_req: dict[str, str] = {}
    try:
        sas = frappe.db.get_all(
            "Shift Assignment",
            filters=[["employee", "=", emp], ["docstatus", "=", 1]],
            fields=["name", "shift_request"],
        )
        sa_by_req = {s.shift_request: s.name for s in sas if getattr(s, "shift_request", None)}
    except Exception:
        sa_by_req = {}
    out = []
    for r in rows:
        d = dict(r)
        d["docstatus"] = int(d.get("docstatus") or 0)
        d["shift_assignment"] = sa_by_req.get(r.name)
        out.append(d)
    return out


@frappe.whitelist()
def create_my_shift_request(
    shift_type: str,
    from_date: str,
    to_date: str | None = None,
    reason: str | None = None,
) -> dict:
    """Plan §2.3 — employee self-service Shift Request (Draft, docstatus 0).

    The employee is resolved from the session (spoof-proof), an approver must
    be configured, and the span must not overlap an Active Shift Assignment or
    another Draft request (native ``OverlappingShiftRequestError`` parity).
    """
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    shift_type = (shift_type or "").strip()
    from_date = (from_date or "").strip()
    if not shift_type or not frappe.db.exists("Shift Type", shift_type):
        frappe.throw(_("Ca làm việc không tồn tại."))
    try:
        disabled = frappe.db.get_value("Shift Type", shift_type, "disabled")
    except Exception:
        disabled = 0
    if disabled:
        frappe.throw(_("Ca làm việc này đã ngừng hoạt động."))
    if not from_date:
        frappe.throw(_("Ngày bắt đầu là bắt buộc."))
    start = getdate(from_date)
    end = getdate(to_date) if to_date else None
    if end and end < start:
        frappe.throw(_("Ngày kết thúc phải sau hoặc bằng ngày bắt đầu."))
    if start < getdate():
        frappe.throw(_("Không thể đề xuất ca cho ngày đã qua."))

    approver = _resolve_shift_approver(emp)
    if not approver:
        frappe.throw(_("Chưa cấu hình người duyệt ca cho bạn — vui lòng liên hệ HR."))
    _assert_no_request_overlap(emp, shift_type, start, end)

    company = None
    try:
        company = frappe.db.get_value("Employee", emp, "company")
    except Exception:
        company = None
    doc = frappe.get_doc(
        {
            "doctype": "Shift Request",
            "employee": emp,
            "shift_type": shift_type,
            "from_date": start,
            "to_date": end,
            "status": "Draft",
            "approver": approver["user"],
            "reason": (reason or "").strip() or None,
            "company": company,
        }
    )
    doc.insert()
    _audit_schedule(
        _("Gửi yêu cầu ca {0} ({1} → {2})").format(shift_type, start, end if end else _("tiếp diễn")),
        reference_doctype="Shift Request",
        reference_name=doc.name,
        employee=emp,
        new_value={
            "shift_type": shift_type,
            "from_date": str(start),
            "to_date": str(end) if end else None,
        },
    )
    _notify_schedule_updated(emp)
    return {"name": doc.name, "approver": approver}


@frappe.whitelist()
def cancel_my_shift_request(name: str) -> dict:
    """Plan §2.4 — withdraw one of the viewer's OWN Draft Shift Requests.

    Frappe convention for a docstatus-0 draft is deletion; the audit row keeps
    the trail. Requests already Approved/Rejected are refused (their Shift
    Assignment, if any, must be ended through the HR flow instead).
    """
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    name = (name or "").strip()
    row = None
    if name:
        try:
            row = frappe.db.get_value(
                "Shift Request", name, ["employee", "docstatus", "status"], as_dict=True
            )
        except Exception:
            row = None
    if row is None:
        frappe.throw(_("Yêu cầu ca không tồn tại."))
    if row.employee != emp:
        frappe.throw(_("Chỉ được thu hồi yêu cầu của chính mình."), frappe.PermissionError)
    if int(row.docstatus or 0) != 0 or row.status != "Draft":
        frappe.throw(_("Chỉ yêu cầu đang chờ duyệt mới thu hồi được."))
    # Ownership is verified above (row.employee == session employee), so the
    # delete may bypass the role's missing DELETE perm — native hrms uses the
    # same ignore_permissions pattern when writing the linked Shift Assignment.
    frappe.delete_doc("Shift Request", name, ignore_permissions=True)
    _audit_schedule(
        _("Thu hồi yêu cầu ca {0}").format(name),
        reference_doctype="Shift Request",
        reference_name=name,
        employee=emp,
    )
    _notify_schedule_updated(emp)
    return {"name": name}


# --------------------------------------------------------------------------- #
# Hooks — M2: materialise Shift Instances + trigger recalculation
# --------------------------------------------------------------------------- #
SHIFT_INSTANCE_HORIZON_DAYS = 14  # forward window for daily materialisation


@frappe.whitelist()
def generate_shift_instances(days: int | None = None) -> dict:
    """Manually-triggerable generator (HR UI button)."""
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    horizon = int(days or SHIFT_INSTANCE_HORIZON_DAYS)
    res = _materialise_shift_instances(horizon)
    # WP3: skip-count surfaced so one bad assignment is VISIBLE, not silent.
    return {"ok": True, "created": res["created"], "skipped": res["skipped"]}


@frappe.whitelist()
def backfill_shift_instances(
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
) -> dict:
    """Materialise VN Employee Shift Instance rows for an arbitrary date window.

    Unlike the daily generator (which only looks forward from ``today``), this
    backfills **past** dates so that historical Employee Checkins can be matched
    to a Shift Instance and aggregated into Work Sessions. Idempotent: existing
    instance rows are skipped. Used by the ``/hr/attendance`` hotfix + the
    "Tính lại kỳ" recalculation flow.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    today = tz_utils.now_in_portal().date()
    win_start = getdate(from_date) if from_date else today
    win_end = getdate(to_date) if to_date else today
    if win_end < win_start:
        win_start, win_end = win_end, win_start
    res = _materialise_shift_instances(from_date=win_start, to_date=win_end, employee=employee)
    return {
        "ok": True,
        "from_date": win_start.isoformat(),
        "to_date": win_end.isoformat(),
        "employee": employee,
        "created": res["created"],
        "skipped": res["skipped"],
        "errors": res["errors"],
    }


@frappe.whitelist()
def set_shift_instance_status(name: str, status: str) -> dict:
    """Manually flip ONE VN Employee Shift Instance's day status
    (plans/hr-shifts-frontend-parity.md §8.4 — Phase 3).

    ``status`` ∈ Cancelled / Skipped / Scheduled (Scheduled = undo). Instances
    already Active or Completed are refused — the engine owns those. A linked,
    already-calculated Work Session surfaces as ``warning`` so the UI can hint
    the change may need a recalc, but does not block (HR override).
    """
    frappe.only_for(["HR Manager", "System Manager", "Line Manager"])
    allowed = {"Cancelled", "Skipped", "Scheduled"}
    status = (status or "").strip()
    name = (name or "").strip()
    if status not in allowed:
        frappe.throw(_("Trạng thái phải là một trong: {0}").format(", ".join(sorted(allowed))))
    doc = frappe.get_doc("VN Employee Shift Instance", name)
    if not doc:
        frappe.throw(_("Không tìm thấy phiên ca."))
    if getattr(doc, "status", "") in ("Active", "Completed"):
        frappe.throw(_("Ca đang diễn ra hoặc đã kết thúc — không đổi trạng thái bằng tay được."))
    if getattr(doc, "status", "") == status:
        return {"name": name, "status": status, "warning": ""}

    doc.db_set("status", status, update_modified=True)
    _notify_schedule_updated(getattr(doc, "employee", None))

    warning = ""
    try:
        ws = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": name}, "calculation_status")
        if ws:
            warning = _("Ngày này đã có phiên chấm công ({0}) — cân nhắc tính lại công.").format(ws)
    except Exception:
        pass
    return {"name": name, "status": status, "warning": warning}


def generate_daily_shift_instances(*args, **kwargs) -> int:
    """Daily scheduler → materialise VN Employee Shift Instance rows.

    Expands every active Shift Assignment into one VN Employee Shift Instance
    per calendar (portal) day for the configured horizon, computing planned
    windows + half-split + check-in/out windows via ``utils/tz``.
    Idempotent: existing instance rows are skipped.
    """
    horizon = SHIFT_INSTANCE_HORIZON_DAYS
    res = _materialise_shift_instances(horizon)
    # WP4: heartbeat only when the full pass succeeded (failures inside are
    # logged per-assignment by the WP3 guard; the job still "ran").
    try:
        from gege_hr.gege_hr.utils import health as _health

        _health.record_heartbeat(
            "shift.generate_daily_shift_instances",
            summary={"created": res.get("created"), "skipped": res.get("skipped")},
        )
    except Exception:
        pass
    return res


def _materialise_shift_instances(
    horizon: int = SHIFT_INSTANCE_HORIZON_DAYS,
    from_date=None,
    to_date=None,
    employee: str | None = None,
) -> dict:
    """Expand active Shift Assignments into per-day VN Employee Shift Instances.

    WP3 (F-LC17): ONE broken assignment (duplicate window, validation error,
    corrupt Shift Type…) must never abort the WHOLE company's materialisation.
    Each assignment is wrapped: a failure is logged (title
    ``materialise SI failed <employee>``), rolled back, counted as ``skipped``,
    and the loop continues. Returns ``{"created": n, "skipped": m,
    "errors": [<employee>...]}``.
    """
    today = tz_utils.now_in_portal().date()
    if from_date or to_date:
        # Explicit backfill window (may cover past dates).
        win_start = getdate(from_date) if from_date else today
        win_end = getdate(to_date) if to_date else add_days(today, horizon)
    else:
        win_start = today
        win_end = add_days(today, horizon)
    has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    sa_fields = ["name", "employee", "shift_type", "start_date", "end_date", "company"]
    if has_loc_field:
        sa_fields.append("vn_work_location")
    sa_filters = {"status": "Active", "docstatus": 1, "start_date": ["<=", win_end]}
    if employee:
        sa_filters["employee"] = employee
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters=sa_filters,
        fields=sa_fields,
    )
    created = 0
    skipped = 0
    errors: list[str] = []
    for a in assignments:
        try:
            a_start = getdate(a.start_date)
            a_end = getdate(a.end_date) if a.end_date else win_end
            day = max(a_start, win_start)
            while day <= min(a_end, win_end):
                if _ensure_shift_instance(a, day):
                    created += 1
                day = add_days(day, 1)
        except Exception:
            # WP3 guard: roll back THIS assignment's partial writes, log, and
            # keep going — other employees still get their instances (MH1).
            skipped += 1
            errors.append(a.get("employee") or a.get("name") or "?")
            try:
                frappe.log_error(
                    title=f"materialise SI failed {a.get('employee')}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
            try:
                frappe.db.rollback()
            except Exception:
                pass
    return {"created": created, "skipped": skipped, "errors": errors}


def _ensure_shift_instance(assignment: dict, day: date) -> bool:
    """Create the VN Employee Shift Instance for one day if not already present."""
    exists = frappe.db.exists(
        "VN Employee Shift Instance",
        {
            "employee": assignment.employee,
            "shift_type": assignment.shift_type,
            "work_date": day,
            "docstatus": ["!=", 2],
        },
    )
    if exists:
        return False

    st = frappe.get_cached_doc("Shift Type", assignment.shift_type)
    if not st.start_time or not st.end_time:
        return False

    planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
    overnight = tz_utils.is_overnight(st.start_time, st.end_time)
    half_span = (planned_end - planned_start) / 2
    first_half_end = planned_start + half_span
    second_half_start = planned_start + half_span

    # Check-in / out windows from VN Shift Type custom fields (fall back to defaults).
    earliest_in = planned_start - timedelta(minutes=_st_int(st, "vn_earliest_checkin_minutes", 60))
    latest_in = planned_start + timedelta(minutes=_st_int(st, "vn_latest_checkin_minutes", 30))
    earliest_out = planned_end - timedelta(minutes=_st_int(st, "vn_earliest_checkout_minutes", 30))
    latest_out = planned_end + timedelta(minutes=_st_int(st, "vn_latest_checkout_minutes", 60))
    max_checkout = planned_end + timedelta(minutes=_st_int(st, "vn_max_checkout_after_end_minutes", 360))

    employee_name = frappe.db.get_value("Employee", assignment.employee, "employee_name")
    policy = frappe.db.get_value("Employee", assignment.employee, "default_attendance_policy")
    work_loc = assignment.get("vn_work_location") or None
    work_loc_name = None
    if work_loc:
        work_loc_name = frappe.db.get_value("VN Work Location", work_loc, "location_name")

    doc = frappe.get_doc(
        {
            "doctype": "VN Employee Shift Instance",
            "employee": assignment.employee,
            "employee_name": employee_name,
            "work_date": day,
            "shift_type": assignment.shift_type,
            "shift_name": assignment.shift_type,
            "source_shift_assignment": assignment.name,
            "attendance_policy": policy,
            "work_location": work_loc,
            "work_location_name": work_loc_name,
            "company": assignment.company,
            "status": "Scheduled",
            "planned_start": _frappe_dt(planned_start),
            "planned_end": _frappe_dt(planned_end),
            "is_overnight": int(overnight),
            "first_half_start": _frappe_dt(planned_start),
            "first_half_end": _frappe_dt(first_half_end),
            "second_half_start": _frappe_dt(second_half_start),
            "second_half_end": _frappe_dt(planned_end),
            "checkin_window_start": _frappe_dt(earliest_in),
            "checkin_window_end": _frappe_dt(latest_in),
            "checkout_window_start": _frappe_dt(earliest_out),
            "checkout_window_end": _frappe_dt(latest_out),
            "max_checkout_time": _frappe_dt(max_checkout),
        }
    )
    # Scheduler-driven generation (daily job) — no interactive user session, so
    # the system creates + submits the shift instance directly.
    doc.insert(ignore_permissions=True)
    try:
        doc.submit()
    except Exception:
        # Submit permissions may be missing in some test benches; leave as draft.
        pass
    return True


def _st_int(shift_type_doc, field: str, default: int) -> int:
    val = getattr(shift_type_doc, field, None)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _frappe_dt(dt) -> str:
    """PHASE-1 FRAME: format an aware datetime as naive PORTAL WALL storage string."""
    return tz_utils.wall(dt).strftime("%Y-%m-%d %H:%M:%S")


def on_shift_instance_submit(doc, method: str | None = None) -> None:
    """VN Employee Shift Instance on_submit → enqueue work-session recalculation."""
    try:
        frappe.enqueue(
            "gege_hr.gege_hr.utils.calc.persist_work_session",
            queue="short",
            timeout=60,
            shift_instance_name=doc.name,
            calculate_mode="batch",
        )
    except Exception:
        # Enqueue not available in some contexts (tests) → compute inline.
        from gege_hr.gege_hr.utils import calc

        calc.persist_work_session(doc.name, calculate_mode="batch")
