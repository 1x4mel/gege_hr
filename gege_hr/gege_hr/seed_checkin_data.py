"""Seed Users + Employees + Employee Checkins from an exported checkin CSV.

Builds a ready-to-test dataset from ``plans/Employee Checkin.csv`` (a Frappe Data
Import template exported from a gege_hr site). For every distinct employee found
in the file it creates:

* a **User** whose email is the employee's email — password ``123456`` for all
  (set directly into ``__Auth`` via ``frappe.utils.password.update_password`` so
  Frappe's password-strength policy cannot reject the weak seed password);
* an **Employee** named exactly like the file (``HR-EMP-00001`` …) linked to that
  user via ``user_id`` (the portal's identity link);
* the **Employee Checkin** rows themselves, so the ``/hr`` attendance views and
  the gege_hr check-in engine have real IN/OUT data to test against.

It also forces gege_hr to **always use Vietnam time** by setting both
``System Settings.time_zone`` and ``VN HR Portal Setting.timezone`` to
``Asia/Ho_Chi_Minh``.

Run (as the bench user)::

    bench --site erp.local execute gege_hr.gege_hr.seed_checkin_data.run

Optional kwargs::

    bench --site erp.local execute gege_hr.gege_hr.seed_checkin_data.run \
        --kwargs "{'csv_path': '/home/frappe/plans/Employee Checkin.csv', 'times_are_local': true}"

Design mirrors the proven idempotent pattern of ``setup_demo.py`` /
``setup_test_data.py``:

* every creator is **idempotent** (skips / re-applies when the record exists),
* every write is ``ignore_permissions`` + ``ignore_mandatory``,
* the ``Employee Checkin.after_insert`` recalc hook is **temporarily neutralised**
  during the bulk import so thousands of inserts do not flood the ``short`` queue
  with work-session recalculation jobs (run ``recalculate_period`` afterwards to
  rebuild Work Sessions),
* CSV checkin times are treated as **local Vietnam time** and converted to the
  UTC convention gege_hr stores ``Employee Checkin.time`` in — so after the
  portal TZ conversion they display as Vietnam time (set ``times_are_local=False``
  only if the source is already UTC).

Returns a summary dict (also printed to the bench log) describing what was
created, including a sample time conversion and the login credentials.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

try:  # bench-free import safety (unit tests run outside a Frappe site)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

# --------------------------------------------------------------------------- #
# Constants — centralised so re-runs match exactly.
# --------------------------------------------------------------------------- #
DEFAULT_CSV_PATH = "/home/frappe/plans/Employee Checkin.csv"
COMPANY = "GeGe Esport"
COMPANY_ABBR = "GE"
PWD = "123456"  # requested seed password for every account
PORTAL_TZ = "Asia/Ho_Chi_Minh"  # Vietnam = UTC+7, no DST (plan v5 §2.7)
DEFAULT_JOINING = "2024-01-01"  # before all checkin dates so Attendance validates

# Vietnamese shift windows. Keys are diacritic-stripped + lower-cased so accented
# names ("Ca Sáng" / "Ca Tối") match via _strip_accents(). Windows for the shifts
# actually present in the CSV were derived from the real IN/OUT times:
#   Ca Sáng → IN ~08:00, OUT ~20:00   (12h day shift)
#   Ca Tối  → IN ~20:00, OUT ~08:00   (12h night shift)
#   Ca Hành Chính / Ca Chiều / Ca Đêm follow the codebase (setup_test_data).
_KNOWN_SHIFT_WINDOWS = {
    "ca hanh chinh": ("08:00:00", "17:00:00"),
    "ca sang": ("08:00:00", "20:00:00"),
    "ca chieu": ("14:00:00", "22:00:00"),
    "ca dem": ("22:00:00", "06:00:00"),
    "ca toi": ("20:00:00", "08:00:00"),
}


def _strip_accents(value: str) -> str:
    """Remove Vietnamese diacritics ("Ca Sáng" → "ca sang", "Ca Đêm" → "ca dem").

    Uses NFKD (not NFD) so the compatibility-only decomposition of "đ"/"Đ" → "d"
    is also handled — plain NFD leaves "đ" intact, which would break "Ca Đêm".
    """
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFKD", value) if unicodedata.category(c) != "Mn"
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ok(name: str) -> bool:
    return bool(frappe and frappe.db.exists("DocType", name))


def _exists(dt: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(dt, name))
    except Exception:
        return False


def _log(msg: str) -> None:
    """Best-effort error log. Never raises (logging must not abort the seed).

    The Error Log ``title`` column caps at 140 chars, so we pass a short fixed
    title and put the (truncated) detail in ``message``.
    """
    if not frappe:
        return
    try:
        frappe.log_error(title="seed_checkin_data", message=str(msg)[:65000])
    except Exception:
        pass


def _has_field(dt: str, field: str) -> bool:
    try:
        return bool(frappe.meta.has_field(dt, field))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Pure, bench-free helpers (CSV parsing + time conversion) — unit-testable.
# --------------------------------------------------------------------------- #
def _parse_dt(value: str):
    """Parse a checkin time string flexibly (handles dd-mm & iso ordering)."""
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def local_to_utc_str(value: str, tz: str = PORTAL_TZ) -> str:
    """Convert a local-time string to the ``YYYY-MM-DD HH:MM:SS`` UTC string
    gege_hr stores ``Employee Checkin.time`` in. Unparseable values pass through.
    """
    dt = _parse_dt(value)
    if dt is None:
        return (value or "").strip()
    aware = dt.replace(tzinfo=ZoneInfo(tz))
    return aware.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def _to_iso(value: str) -> str:
    """Normalise a time string to ``YYYY-MM-DD HH:MM:SS`` without TZ conversion."""
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else (value or "").strip()


def _norm_time(value) -> str:
    """Normalise a DB datetime/string to ``YYYY-MM-DD HH:MM:SS`` for dedup keys."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "").split(".")[0]


def shift_window(name: str) -> tuple[str, str]:
    """Best-effort (start_time, end_time) for a Vietnamese shift name.

    Parses explicit ``21h-9h`` / ``9h-21h`` notation, then falls back to known
    keyword windows, then a sane office default.
    """
    n = _strip_accents((name or "").strip().lower())
    m = re.search(r"(\d{1,2})\s*h\s*[-–]\s*(\d{1,2})\s*h", n)
    if m:
        return f"{int(m.group(1)):02d}:00:00", f"{int(m.group(2)):02d}:00:00"
    for key, win in _KNOWN_SHIFT_WINDOWS.items():
        if key in n:
            return win
    return "08:00:00", "17:00:00"


def load_records(path: str = DEFAULT_CSV_PATH) -> list[dict]:
    """Parse a Frappe Data Import template CSV into a list of checkin dicts.

    Robust to the 20-line template header: it locates the ``Column Name:`` row to
    map field→column index and the ``Start entering data below this line`` marker
    to know where data begins. Returns dicts keyed by the standard Employee
    Checkin column names.
    """
    import csv
    import os

    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))

    col: dict[str, int] = {}
    start = None
    for i, row in enumerate(rows):
        if row and (row[0] or "").strip() == "Column Name:":
            for pos, val in enumerate(row):
                v = (val or "").strip()
                if v and v != "Column Name:":
                    col[v] = pos
        if row and len(row) == 1 and "Start entering data below this line" in (row[0] or ""):
            start = i + 1
            break
    if not col:
        raise RuntimeError("seed_checkin_data: 'Column Name:' header row not found in CSV")
    if start is None:
        start = 0

    out: list[dict] = []
    for row in rows[start:]:
        if not row or not any((c or "").strip() for c in row):
            continue

        def cell(field: str) -> str:
            pos = col.get(field)
            if pos is None or pos >= len(row):
                return ""
            return (row[pos] or "").strip()

        emp = cell("employee")
        if not emp:
            continue
        out.append(
            {
                "employee": emp,
                "email": cell("owner"),
                "name": cell("employee_name"),
                "time": cell("time"),
                "log_type": cell("log_type"),
                "shift": cell("shift"),
                "lat": cell("latitude"),
                "lon": cell("longitude"),
                "geolocation": cell("geolocation"),
            }
        )
    return out


def dedupe_employees(records: list[dict]) -> dict[str, dict]:
    """Collapse checkin rows into ``{employee_id: {email, name}}``.

    Picks the most-frequent email/name per employee so a one-off stray owner
    (e.g. ``HR-EMP-00001`` once owned by ``chillsweed@gmail.com`` before
    ``fujiwameow@gmail.com`` became dominant) does not win. Result is sorted by
    employee id for deterministic re-runs.
    """
    emails: dict[str, Counter] = defaultdict(Counter)
    names: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        emp = r["employee"]
        if r["email"]:
            emails[emp][r["email"].strip().lower()] += 1
        if r["name"]:
            names[emp][r["name"].strip()] += 1
    out: dict[str, dict] = {}
    for emp in sorted(set(emails) | set(names)):
        email = emails[emp].most_common(1)[0][0] if emails[emp] else ""
        name = names[emp].most_common(1)[0][0] if names[emp] else ""
        out[emp] = {"email": email, "name": name}
    return out


def _split_name(full: str) -> tuple[str, str]:
    """Split a Vietnamese full name into (first_name, last_name).

    Frappe's Employee uses first_name (reqd) + last_name; ``employee_name`` is the
    cached combo. For names like "Vũ Winner" → ("Vũ", "Winner"); a single token →
    (token, "").
    """
    parts = (full or "").strip().split(None, 1)
    if not parts:
        return ("Employee", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def common_shifts(records: list[dict]) -> dict[str, str]:
    """Return ``{employee_id: most_frequent_shift_name}`` from the checkin rows.

    Drives Shift Assignment creation so each employee gets the shift they actually
    worked most (e.g. ``HR-EMP-00016`` → ``Ca Tối``), which is what the gege_hr
    Work-Session engine needs to match a Shift Instance to a checkin.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        emp = r["employee"]
        shift = (r["shift"] or "").strip()
        if emp and shift:
            counts[emp][shift] += 1
    return {emp: c.most_common(1)[0][0] for emp, c in counts.items() if c}


# --------------------------------------------------------------------------- #
# Frappe-aware creators — bench-guarded + idempotent.
# --------------------------------------------------------------------------- #
def _ensure_timezone() -> str:
    """Force gege_hr + Frappe to always use Vietnam time (Asia/Ho_Chi_Minh)."""
    if not frappe:
        return PORTAL_TZ
    if _ok("System Settings"):
        try:
            frappe.db.set_single_value("System Settings", "time_zone", PORTAL_TZ)
        except Exception:
            _log("seed_checkin_data: failed to set System Settings.time_zone")
    if _ok("VN HR Portal Setting") and _has_field("VN HR Portal Setting", "timezone"):
        try:
            frappe.db.set_single_value("VN HR Portal Setting", "timezone", PORTAL_TZ)
        except Exception:
            _log("seed_checkin_data: failed to set VN HR Portal Setting.timezone")
    return PORTAL_TZ


def _ensure_company() -> str:
    """Return a usable Company, creating ``GeGe Esport`` when none exists."""
    if not _ok("Company"):
        return ""
    if _exists("Company", COMPANY):
        return COMPANY
    existing = []
    try:
        existing = frappe.get_all("Company", filters={"disabled": 0}, pluck="name", order_by="name", limit=1)
    except Exception:
        existing = []
    if existing:
        return existing[0]
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Company",
                "company_name": COMPANY,
                "abbr": COMPANY_ABBR,
                "default_currency": "VND",
                "country": "Vietnam",
                "is_group": 0,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return COMPANY
    except Exception:
        _log("seed_checkin_data: failed to create Company")
        return COMPANY if _exists("Company", COMPANY) else ""


def _ensure_genders() -> None:
    """Pre-create Gender masters (link validation is not skipped by ignore_mandatory)."""
    if not _ok("Gender"):
        return
    for name in ("Male", "Female", "Other"):
        if _exists("Gender", name):
            continue
        try:
            frappe.get_doc({"doctype": "Gender", "gender": name}).insert(ignore_permissions=True)
        except Exception:
            _log(f"seed_checkin_data: failed to create Gender {name}")


def _ensure_shift_types(names: list[str]) -> list[str]:
    """Create missing Shift Types referenced by the CSV. Returns existing names."""
    if not _ok("Shift Type"):
        return []
    ok: list[str] = []
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if _exists("Shift Type", name):
            ok.append(name)
            continue
        start, end = shift_window(name)
        try:
            doc = frappe.get_doc(
                {"doctype": "Shift Type", "name": name, "start_time": start, "end_time": end}
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            ok.append(name)
        except Exception:
            _log(f"seed_checkin_data: failed to create Shift Type {name}")
    return ok


def _ensure_shift_assignments(
    emp_map, shifts_by_emp: dict[str, str], company: str, start: str, end: str
) -> int:
    """Create an Active, submitted Shift Assignment per employee if missing.

    The gege_hr Work-Session engine derives ``VN Employee Shift Instance`` rows
    from ``Shift Assignment`` (Active, docstatus=1): without an assignment covering
    a checkin's day, no Shift Instance exists → no Work Session → the
    ``/hr/admin/attendance`` page is empty even though ``Employee Checkin`` rows
    exist. Each employee gets their most-frequent shift from the CSV, over the full
    checkin date window ``[start, end]``. Idempotent (skips existing assignments).

    ``emp_map`` keys are the CSV employee ids (``HR-EMP-00001``…). The value may be
    either the DB Employee name (str) or the ``dedupe_employees`` info dict — the DB
    name is resolved directly from the id so the function is robust to both.
    """
    if not _ok("Shift Assignment"):
        return 0
    created = 0
    for emp_id in emp_map:
        emp_name = emp_id if _exists("Employee", emp_id) else None
        if not emp_name:
            # dedupe_employees value is {email,name}; fall back to user_id lookup.
            info = emp_map[emp_id]
            email = info.get("email") if isinstance(info, dict) else info
            if email:
                emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if not emp_name:
            continue
        shift = (shifts_by_emp.get(emp_id) or "").strip()
        if not shift or not _exists("Shift Type", shift):
            continue
        # default_shift on the Employee feeds leave-hours + dashboard fallbacks.
        try:
            frappe.db.set_value("Employee", emp_name, "default_shift", shift, update_modified=False)
        except Exception:
            _log(f"seed_checkin_data: set default_shift {emp_name}")
        try:
            existing = frappe.db.exists(
                "Shift Assignment",
                {"employee": emp_name, "shift_type": shift, "docstatus": 1},
            )
        except Exception:
            existing = None
        if existing:
            continue
        try:
            doc = frappe.get_doc(
                {
                    "doctype": "Shift Assignment",
                    "employee": emp_name,
                    "shift_type": shift,
                    "company": company or COMPANY,
                    "start_date": start,
                    "end_date": end,
                    "status": "Active",
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            doc.submit()
            created += 1
        except Exception:
            _log(f"seed_checkin_data: failed Shift Assignment {emp_name}/{shift}")
    try:
        frappe.db.commit()
    except Exception:
        pass
    return created


def build_work_sessions(
    csv_path: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
):
    """Create Shift Assignments + materialise Work Sessions so the HR attendance
    page shows data.

    Pipeline: ``Employee Checkin`` → needs a ``VN Employee Shift Instance``
    (materialised from ``Shift Assignment``) → ``persist_work_session`` builds
    ``VN Attendance Work Session`` (what ``/hr/admin/attendance`` renders).

    This closes the gap left by the bulk import (which neutralised the
    ``on_employee_checkin_create`` recalc hook): it assigns each employee their
    most-common shift over the CSV date window, then runs
    ``attendance.recalculate_period(backfill=1)`` which creates Shift Instances +
    recomputes Work Sessions. Idempotent + safe to re-run.
    """
    if frappe is None:
        return {"error": "frappe not available — run inside a bench"}
    try:
        frappe.set_user("Administrator")
    except Exception:
        pass

    records = load_records(csv_path or DEFAULT_CSV_PATH)
    employees = dedupe_employees(records)
    shifts = common_shifts(records)

    # Date window: explicit args → CSV min/max → fallback current month.
    if from_date and to_date:
        start, end = str(from_date), str(to_date)
    else:
        times = [t for r in records if (t := _parse_dt(r["time"]))]
        start = min(times).strftime("%Y-%m-%d") if times else "2026-04-01"
        end = max(times).strftime("%Y-%m-%d") if times else "2026-08-31"

    report: dict = {
        "from_date": start,
        "to_date": end,
        "shift_assignments": 0,
        "recalculate": None,
        "work_sessions": 0,
        "attendance_backfill": None,
        "attendance": 0,
    }

    company = _ensure_company() or COMPANY
    report["shift_assignments"] = _ensure_shift_assignments(
        employees, shifts, company, start, end
    )

    try:
        from gege_hr.gege_hr.api import attendance as att

        report["recalculate"] = str(
            att.recalculate_period(start, end, backfill=1)
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"seed_checkin_data: recalculate_period failed: {exc}")
        report["recalculate"] = f"error: {exc}"

    try:
        report["work_sessions"] = frappe.db.count("VN Attendance Work Session")
    except Exception:
        report["work_sessions"] = 0

    # /hr/team/attendance + employee monthly views read Attendance (docstatus=1),
    # NOT Work Sessions — regenerate it from every Work Session above so those
    # pages show data too (attendance_sync.backfill_attendance is idempotent).
    try:
        from gege_hr.gege_hr.api import attendance_sync

        report["attendance_backfill"] = str(
            attendance_sync.backfill_attendance(start, end)
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"seed_checkin_data: backfill_attendance failed: {exc}")
        report["attendance_backfill"] = f"error: {exc}"
    try:
        report["attendance"] = frappe.db.count("Attendance", {"docstatus": 1})
    except Exception:
        report["attendance"] = 0

    # ``team_attendance`` + employee monthly views read Attendance with
    # docstatus=1. sync_attendance only submits when the day is in a *Locked*
    # Monthly Period (none exist in a fresh seed), so the backfilled rows are
    # drafts (docstatus=0) and invisible to those views. Submit them directly so
    # the seed data is immediately visible (tolerant: any HRMS validation failure
    # is logged + skipped, never aborting the run).
    report["submitted"] = _submit_draft_attendance(start, end)

    # Populate in_time/out_time + late_entry/early_exit from the real Employee
    # Checkins, and mark scheduled-but-no-checkin days as Absent — otherwise
    # /hr/team/attendance shows 0 late days & 0 absent days even though the
    # raw checkin data contains them (Attendance.in_time was empty).
    try:
        report["enriched"] = _enrich_attendance(start, end)
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb

        msg = "seed_checkin_data: _enrich_attendance raised: " + repr(exc) + "\n" + _tb.format_exc()
        _log(msg)
        report["enriched"] = {"error": repr(exc)}

    try:
        frappe.db.commit()
    except Exception:
        pass
    return report


def _enrich_attendance(start: str, end: str) -> dict:
    """Populate in_time/out_time/late_entry/early_exit on Attendance + mark absent.

    The core ``Attendance`` rows synced from Work Sessions carry only ``status`` —
    ``in_time``/``out_time`` are blank and ``late_entry`` is almost never set, so
    ``team_attendance`` cannot compute late minutes or show absent days. This reads
    the real ``Employee Checkin`` rows per (employee, portal-day), stamps the
    earliest IN / latest OUT, derives late/early against the shift window, and
    flips scheduled days with no checkin to ``status=Absent``.

    Updates are written via ``db.set_value`` (Attendance is already submitted) so
    HRMS validation is not re-triggered per row.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    # tz_utils is the module ``gege_hr.gege_hr.utils.tz`` (not a package attr),
    # so import the helper directly.
    from gege_hr.gege_hr.utils.tz import as_time as _as_time

    out = {"attendance": 0, "with_times": 0, "absent": 0, "late": 0, "early": 0}
    if not _ok("Attendance") or not _ok("Employee Checkin"):
        return out
    VN = ZoneInfo(PORTAL_TZ)

    # 1) Build (employee, portal-day) → {in: min_utc, out: max_utc} from checkins.
    ins: dict[tuple, _dt] = {}
    outs: dict[tuple, _dt] = {}
    try:
        rows = frappe.db.get_all(
            "Employee Checkin",
            filters={"time": ["between", [start, end + " 23:59:59"]]},
            fields=["employee", "time", "log_type"],
            limit_page_length=0,
        )
    except Exception:
        _log("seed_checkin_data: enrich list checkins failed")
        return out
    for r in rows:
        try:
            t = _dt.strptime(str(r["time"])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        except Exception:
            continue
        pday = t.astimezone(VN).date()
        key = (r["employee"], pday)
        lt = (r.get("log_type") or "").strip().upper()
        if lt == "IN":
            if key not in ins or t < ins[key]:
                ins[key] = t
        elif lt == "OUT":
            if key not in outs or t > outs[key]:
                outs[key] = t

    # 2) Shift windows (start_time/end_time) keyed by Shift Type name.
    shift_win: dict[str, tuple] = {}
    try:
        for st in frappe.db.get_all("Shift Type", fields=["name", "start_time", "end_time"]):
            shift_win[st.name] = (st.start_time, st.end_time)
    except Exception:
        pass

    try:
        atts = frappe.db.get_all(
            "Attendance",
            filters={"attendance_date": ["between", [start, end]]},
            fields=["name", "employee", "attendance_date", "shift"],
            limit_page_length=0,
        )
    except Exception:
        _log("seed_checkin_data: enrich list attendance failed")
        return out
    out["attendance"] = len(atts)
    for a in atts:
        key = (a["employee"], _dt.strptime(str(a["attendance_date"]), "%Y-%m-%d").date())
        in_t = ins.get(key)
        out_t = outs.get(key)
        if in_t is None and out_t is None:
            # Scheduled but no punch → Absent.
            try:
                frappe.db.set_value("Attendance", a["name"], "status", "Absent", update_modified=False)
                out["absent"] += 1
            except Exception:
                _log(f"seed_checkin_data: set Absent {a['name']}")
            continue
        start_t, end_t = shift_win.get(a.get("shift"), (None, None))
        late = 0
        early = 0
        if in_t is not None and start_t is not None:
            planned = _dt.combine(key[1], _as_time(start_t)).replace(tzinfo=VN)
            late = 1 if in_t.astimezone(VN) > planned else 0
        if out_t is not None and end_t is not None:
            planned = _dt.combine(key[1], _as_time(end_t)).replace(tzinfo=VN)
            early = 1 if out_t.astimezone(VN) < planned else 0
        vals = {
            "in_time": in_t.strftime("%Y-%m-%d %H:%M:%S") if in_t else None,
            "out_time": out_t.strftime("%Y-%m-%d %H:%M:%S") if out_t else None,
            "status": "Present",
        }
        if late:
            vals["late_entry"] = 1
            out["late"] += 1
        if early:
            vals["early_exit"] = 1
            out["early"] += 1
        try:
            frappe.db.set_value("Attendance", a["name"], vals, update_modified=False)
            out["with_times"] += 1
        except Exception:
            _log(f"seed_checkin_data: enrich Attendance {a['name']}")
    return out


def _submit_draft_attendance(start: str, end: str) -> dict:
    """Submit every draft ``Attendance`` row in [start, end]. Returns counts.

    Idempotent: only touches docstatus=0 rows; submitted/cancelled rows are left
    alone. Failures (e.g. HRMS validation on a specific row) are logged + skipped.
    """
    out = {"total": 0, "submitted": 0, "failed": 0}
    if not _ok("Attendance"):
        return out
    try:
        names = frappe.db.get_all(
            "Attendance",
            filters={
                "attendance_date": ["between", [start, end]],
                "docstatus": 0,
            },
            pluck="name",
        )
    except Exception:
        _log("seed_checkin_data: failed to list draft Attendance")
        return out
    out["total"] = len(names)
    for n in names:
        try:
            doc = frappe.get_doc("Attendance", n)
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.submit()
            out["submitted"] += 1
        except Exception:
            out["failed"] += 1
            _log(f"seed_checkin_data: submit Attendance {n} failed")
    return out


def _set_password(email: str, pwd: str) -> None:
    """Set a User's password directly into ``__Auth``, bypassing strength policy."""
    try:
        from frappe.utils.password import update_password

        update_password(email, pwd)
    except Exception:
        _log(f"seed_checkin_data: failed to set password for {email}")


def _ensure_employee_role(email: str) -> None:
    try:
        user = frappe.get_doc("User", email)
        roles = {r.role for r in (user.roles or [])}
        if "Employee" not in roles:
            user.append("roles", {"role": "Employee", "doctype": "Has Role"})
            user.flags.ignore_permissions = True
            user.save(ignore_permissions=True)
    except Exception:
        _log(f"seed_checkin_data: failed to add Employee role to {email}")


def _ensure_user(email: str, name: str) -> str | None:
    """Return a User email, creating it when missing. Always (re)sets PWD + role."""
    if not _ok("User") or not email:
        return None
    first = (name or email.split("@")[0]).strip() or email
    if not _exists("User", email):
        try:
            doc = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": first,
                    "send_welcome_email": 0,
                    "enabled": 1,
                    "roles": [{"role": "Employee", "doctype": "Has Role"}],
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
        except Exception:
            _log(f"seed_checkin_data: failed to create User {email}")
    if _exists("User", email):
        _set_password(email, PWD)  # idempotent — also fixes half-provisioned accounts
        _ensure_employee_role(email)
        return email
    return None


def _ensure_employee(emp_id: str, email: str, name: str, company: str) -> str | None:
    """Return an Employee name, creating it (with the file's exact id) when missing.

    Lookup order: the file's id → ``user_id`` → create+rename. Returns the *actual*
    DB name (which may differ if the rename was blocked) so the checkin importer
    can map rows reliably.
    """
    if not _ok("Employee") or not emp_id:
        return None
    if _exists("Employee", emp_id):
        # The row may pre-exist from another seeder (e.g. setup_test_data) with a
        # STALE identity (Nhân Viên Test / hr.employee@gege.test). Re-point user_id
        # AND sync name + email fields so the profile shows the CSV employee, not
        # leftover test data. ``employee_name`` is Frappe's cached full name.
        if email or name:
            first, last = _split_name(name)
            vals = {"user_id": email}
            if email:
                vals["prefered_email"] = email
                vals["personal_email"] = email
            if name:
                vals["first_name"] = first
                vals["last_name"] = last
                vals["employee_name"] = (name or "").strip()
            try:
                frappe.db.set_value("Employee", emp_id, vals, update_modified=False)
            except Exception:
                _log(f"seed_checkin_data: sync Employee identity {emp_id}")
        return emp_id
    try:
        rows = frappe.get_all("Employee", {"user_id": email}, pluck="name", limit=1)
    except Exception:
        rows = []
    if rows:
        return rows[0]
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Employee",
                "naming_series": "HR-EMP-",
                "first_name": (name or email or emp_id).strip(),
                "company": company,
                "user_id": email or "",
                "prefered_email": email or "",
                "personal_email": email or "",
                "status": "Active",
                "gender": "Other",
                "date_of_joining": DEFAULT_JOINING,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        auto = doc.name
        if auto != emp_id and not _exists("Employee", emp_id):
            try:
                frappe.rename_doc("Employee", auto, emp_id, force=True, show_alert=False)
            except Exception:
                _log(f"seed_checkin_data: rename Employee {auto} -> {emp_id}")
                return auto
        return emp_id if _exists("Employee", emp_id) else auto
    except Exception:
        _log(f"seed_checkin_data: failed to create Employee {emp_id}")
        return None


def _import_checkins(records, emp_map, shift_set, times_are_local=True):
    """Insert Employee Checkin rows. Returns {inserted, skipped, failed}."""
    out = {"inserted": 0, "skipped": 0, "failed": 0}
    if not _ok("Employee Checkin"):
        return out

    has_lat = _has_field("Employee Checkin", "latitude")
    has_lon = _has_field("Employee Checkin", "longitude")
    has_geo = _has_field("Employee Checkin", "geolocation")
    has_src = _has_field("Employee Checkin", "vn_source_type")

    # Pre-load existing (employee, time, log_type) keys for fast idempotent dedup.
    existing: set[tuple] = set()
    emps = [v for v in emp_map.values() if v]
    if emps:
        try:
            for r in frappe.db.get_all(
                "Employee Checkin",
                filters={"employee": ["in", emps]},
                fields=["employee", "time", "log_type"],
            ):
                existing.add((r["employee"], _norm_time(r["time"]), (r["log_type"] or "").strip()))
        except Exception:
            _log("seed_checkin_data: failed to preload existing checkins")

    # Neutralise the after_insert recalc hook for the bulk import (see module doc).
    hook_path = "gege_hr.gege_hr.api.attendance"
    _orig_hook = None
    try:
        att = frappe.get_module(hook_path) if hasattr(frappe, "get_module") else None
    except Exception:
        att = None
    if att is None:
        try:
            import importlib

            att = importlib.import_module(hook_path)
        except Exception:
            att = None
    if att is not None and hasattr(att, "on_employee_checkin_create"):
        _orig_hook = att.on_employee_checkin_create
        att.on_employee_checkin_create = lambda doc, method=None: None

    try:
        for rec in records:
            emp = emp_map.get(rec["employee"])
            if not emp:
                out["skipped"] += 1
                continue
            if times_are_local:
                t = local_to_utc_str(rec["time"])
            else:
                t = _to_iso(rec["time"])
            lt = (rec["log_type"] or "").strip().upper()
            if lt not in ("IN", "OUT"):
                out["skipped"] += 1
                continue
            if not t or (emp, t, lt) in existing:
                out["skipped"] += 1
                continue

            payload = {
                "doctype": "Employee Checkin",
                "employee": emp,
                "time": t,
                "log_type": lt,
            }
            shift = rec["shift"]
            if shift and shift in shift_set:
                payload["shift"] = shift
            if has_src:
                payload["vn_source_type"] = "Import"
            if has_lat and rec["lat"]:
                payload["latitude"] = rec["lat"]
            if has_lon and rec["lon"]:
                payload["longitude"] = rec["lon"]
            if has_geo and rec["geolocation"]:
                payload["geolocation"] = rec["geolocation"]

            try:
                d = frappe.get_doc(payload)
                d.flags.ignore_permissions = True
                d.flags.ignore_mandatory = True
                d.insert(ignore_permissions=True)
                existing.add((emp, t, lt))
                out["inserted"] += 1
            except Exception:
                out["failed"] += 1
                _log(f"seed_checkin_data: checkin insert failed {emp} {t} {lt}")

            # Commit in batches so a huge import does not hold one giant transaction.
            if (out["inserted"] + out["failed"]) % 500 == 0:
                try:
                    frappe.db.commit()
                except Exception:
                    pass
    finally:
        # Always restore the real recalc hook.
        if att is not None and _orig_hook is not None:
            att.on_employee_checkin_create = _orig_hook

    return out


# --------------------------------------------------------------------------- #
# Cleanup — remove throwaway test/demo seed users + their cascaded data.
# --------------------------------------------------------------------------- #
# Email domains that mark a user as random test/demo seed data (to be purged).
TEST_DOMAINS = (
    "@example.com",
    "@example.org",
    "@example.net",
    "@test.com",
    "@test.local",
    "@test",
    "@gege.test",
    "@gege.demo",
)

# Accounts that must NEVER be removed (Frappe/system users).
_NEVER_PURGE = {"administrator", "guest", "guest@example.com"}

# gege_hr / Frappe doctypes that hang off an Employee and should be cascaded.
# (doctype, link_fieldname) — each is only touched if the doctype exists.
_EMPLOYEE_LINKED = (
    ("Employee Checkin", "employee"),
    ("Attendance", "employee"),
    ("Leave Allocation", "employee"),
    ("Leave Application", "employee"),
    ("VN Attendance Exception", "employee"),
    ("VN Attendance Work Session", "employee"),
    ("VN Employee Shift Instance", "employee"),
    ("VN Mobile Checkin Attempt", "employee"),
    ("VN Attendance Raw Log", "employee"),
    ("VN Attendance Correction Request", "employee"),
    ("VN Overtime Request", "employee"),
    ("VN Salary Advance Request", "employee"),
    ("VN Leave Handover Task", "employee"),
    ("VN Employee Portal Profile", "employee"),
    ("VN Monthly Attendance Line", "employee"),
)


def _is_test_email(email: str, keep_emails: set[str]) -> bool:
    e = (email or "").strip().lower()
    if not e or e in _NEVER_PURGE or e in keep_emails:
        return False
    return any(e.endswith(d) for d in TEST_DOMAINS)


def find_test_users(keep_emails: set[str] | None = None) -> list[str]:
    """Return the ``User.name`` of throwaway test/demo users (domain-matched).

    A user is targeted when its email matches a :data:`TEST_DOMAINS` suffix and it
    is not in ``keep_emails`` (the real CSV employees) nor a system account.
    """
    if not _ok("User"):
        return []
    keep = {str(k).strip().lower() for k in (keep_emails or set())}
    out: list[str] = []
    try:
        rows = frappe.db.get_all("User", fields=["name", "email"])
    except Exception:
        _log("seed_checkin_data: failed to list users for purge")
        return []
    for r in rows:
        name = (r.get("name") or "").strip()
        # Never purge system accounts by name — Administrator's email is often
        # "admin@example.com" which would otherwise match the test-domain rule.
        if name.lower() in _NEVER_PURGE:
            continue
        ident = (r.get("email") or name or "").strip().lower()
        if _is_test_email(ident, keep):
            out.append(name)
    return sorted(set(out))


def purge_test_data(keep_emails: set[str] | list[str] | None = None, dry_run: bool = False):
    """Delete throwaway test/demo users and all data cascaded off their Employees.

    Safe by design: it only touches users whose email ends with a test/demo domain
    AND is not in ``keep_emails``; Administrator/Guest are never removed. Each
    linked doctype is deleted by employee first (so FK rows go before the Employee
    /User), then the Employee, then the User. Every failure is logged + swallowed.

    Set ``dry_run=True`` to only report what *would* be deleted (no writes).
    """
    if frappe is None:
        return {"error": "frappe not available — run inside a bench"}
    try:
        frappe.set_user("Administrator")
    except Exception:
        pass

    keep = {str(k).strip().lower() for k in (keep_emails or set())}
    report: dict = {
        "dry_run": dry_run,
        "users": [],
        "employees": [],
        "deleted": {},
    }

    targets = find_test_users(keep)
    report["users"] = targets

    # Resolve every Employee owned by a test user (by user_id + email fields).
    emps: set[str] = set()
    for u in targets:
        ident = u.strip().lower()
        for fld in ("user_id", "prefered_email", "personal_email", "company_email"):
            try:
                emps.update(frappe.db.get_all("Employee", {fld: ident}, pluck="name"))
            except Exception:
                pass
    report["employees"] = sorted(emps)

    if dry_run:
        # Count what would be deleted, without writing.
        for dt, fld in _EMPLOYEE_LINKED:
            if not _ok(dt) or not emps:
                continue
            try:
                report["deleted"][dt] = frappe.db.count(dt, {fld: ["in", list(emps)]})
            except Exception:
                report["deleted"][dt] = 0
        return report

    # Cascade-delete linked rows by employee.
    for dt, fld in _EMPLOYEE_LINKED:
        if not _ok(dt) or not emps:
            continue
        try:
            names = frappe.db.get_all(dt, {fld: ["in", list(emps)]}, pluck="name")
        except Exception:
            names = []
        removed = 0
        for n in names:
            try:
                frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
                removed += 1
            except Exception:
                _log(f"seed_checkin_data: purge {dt} {n}")
        report["deleted"][dt] = removed

    # Then the Employees themselves.
    for e in list(emps):
        try:
            frappe.delete_doc("Employee", e, force=True, ignore_permissions=True)
        except Exception:
            _log(f"seed_checkin_data: purge Employee {e}")

    # Finally the Users.
    for u in targets:
        try:
            frappe.delete_doc("User", u, force=True, ignore_permissions=True)
        except Exception:
            _log(f"seed_checkin_data: purge User {u}")

    try:
        frappe.db.commit()
    except Exception:
        pass
    return report


# --------------------------------------------------------------------------- #
# Orchestration — entry point for ``bench execute``.
# --------------------------------------------------------------------------- #
def run(
    csv_path: str | None = None,
    times_are_local: bool = True,
    password: str | None = None,
    purge: bool = False,
    purge_extra: list[str] | None = None,
    build_sessions: bool = False,
):
    """Create Company + Users + Employees + Employee Checkins from the CSV.

    Idempotent: re-running only fills gaps and re-applies the password/TZ. Returns
    a summary dict describing the result (also written to the Frappe error log).
    """
    if frappe is None:
        return {"error": "frappe not available — run inside a bench"}

    csv_path = csv_path or DEFAULT_CSV_PATH
    pwd = password or PWD

    try:
        frappe.set_user("Administrator")
    except Exception:
        pass

    summary: dict = {
        "csv": csv_path,
        "rows": 0,
        "unique_employees": 0,
        "timezone": None,
        "company": None,
        "shift_types": [],
        "users": 0,
        "employees": 0,
        "checkins": {"inserted": 0, "skipped": 0, "failed": 0},
        "sample_time": {},
        "work_sessions": None,
        "login": f"<email> / {pwd}",
    }

    records = load_records(csv_path)
    summary["rows"] = len(records)
    if not records:
        summary["error"] = "no data rows parsed from CSV"
        return summary

    employees = dedupe_employees(records)
    summary["unique_employees"] = len(employees)

    if purge:
        # Remove old random/test/demo seed users + their data, keeping the real
        # CSV employees (and Administrator) intact, so the dataset stays clean.
        keep = {info["email"] for info in employees.values() if info["email"]}
        keep.update(purge_extra or [])
        try:
            summary["purge"] = purge_test_data(keep_emails=keep, dry_run=False)
        except Exception:
            _log("seed_checkin_data: purge step failed")
            summary["purge"] = {"error": "purge step failed"}

    try:
        summary["timezone"] = _ensure_timezone()
    except Exception:
        _log("seed_checkin_data: timezone step failed")

    try:
        summary["company"] = _ensure_company()
    except Exception:
        _log("seed_checkin_data: company step failed")

    try:
        _ensure_genders()
    except Exception:
        _log("seed_checkin_data: gender step failed")

    shift_names = sorted({r["shift"] for r in records if (r["shift"] or "").strip()})
    try:
        summary["shift_types"] = _ensure_shift_types(shift_names)
    except Exception:
        _log("seed_checkin_data: shift type step failed")
    shift_set = set(summary["shift_types"]) | {
        n for n in shift_names if _exists("Shift Type", n)
    }

    # 1) Users (email → password 123456)
    users = 0
    for emp_id, info in employees.items():
        if _ensure_user(info["email"], info["name"]):
            users += 1
    summary["users"] = users

    # 2) Employees (exact HR-EMP-00001 … ids, linked to users)
    emp_map: dict[str, str] = {}
    for emp_id, info in employees.items():
        name = _ensure_employee(emp_id, info["email"], info["name"], summary["company"])
        if name:
            emp_map[emp_id] = name
    summary["employees"] = len(emp_map)

    # 3) Employee Checkins (the actual check-in data)
    summary["checkins"] = _import_checkins(records, emp_map, shift_set, times_are_local)

    if build_sessions:
        # Build the Work Sessions the /hr/admin/attendance page renders: assign
        # each employee their most-common shift, then recalculate_period()
        # materialises Shift Instances + Work Sessions from the checkins.
        try:
            summary["work_sessions"] = build_work_sessions(csv_path)
        except Exception:
            _log("seed_checkin_data: build_work_sessions step failed")
            summary["work_sessions"] = {"error": "build_work_sessions step failed"}

    # Sample time conversion for sanity-checking the TZ handling.
    sample = next((r["time"] for r in records if r["time"]), "")
    summary["sample_time"] = {
        "raw": sample,
        "stored_utc": local_to_utc_str(sample) if times_are_local else _to_iso(sample),
        "displays_as_vn": (local_to_utc_str(sample) if times_are_local else _to_iso(sample)),
    }

    try:
        frappe.db.commit()
    except Exception:
        pass

    try:
        frappe.logger().info("seed_checkin_data summary: %s", summary)
    except Exception:
        pass
    return summary
