"""
Device sync API — plan v5 §10.10 (Manual Upload / Triggered Sync).

Fronts the VN Attendance Device DocType group (doctype-design §4/§5/§7). The
``hr-ui`` ``useDevice`` composable calls four endpoints defined here:

* ``list_devices``    → device directory + last-sync status
* ``sync_status``     → last-sync status of one (or all) device(s)
* ``sync_device``     → trigger an on-demand sync for one device
* ``upload_logs``     → manual upload of raw punch logs

Ingestion pipeline (plan §10.10):

    raw punch  →  VN Attendance Raw Log  →  Employee Checkin  →  Work-Session
                       (audit/dedup)            (Frappe HR)        (recalc hook)

The last hop reuses the existing ``Employee Checkin`` ``after_insert`` hook
(``api.attendance.on_employee_checkin_create`` → ``utils.calc.persist_work_session``)
so a device punch flows into attendance calculation exactly like a mobile tap.

Time-zone rule (plan §2.7): a device punch ``log_time`` is interpreted in the
**device timezone** (``VN Attendance Device.timezone``, default portal TZ), then
stored as UTC in ``Employee Checkin.time`` — mirroring ``mobile_checkin``.

The pure helpers at the top of this module (parse / normalize / derive-status)
deliberately avoid importing ``frappe`` so they can be unit-tested outside a
bench (same pattern as ``utils/calc.py``). Each ``@frappe.whitelist()`` endpoint
imports ``frappe`` lazily.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import pagination
from gege_hr.gege_hr.utils import tz as tz_utils


# --------------------------------------------------------------------------- #
# Lazy ``frappe`` shims — defined first so endpoint decorators/translators below
# resolve at module-import time WITHOUT importing frappe (keeps the pure helpers
# unit-testable outside a bench, matching ``utils/calc.py``'s pattern).
# --------------------------------------------------------------------------- #
def frappe_whitelist():
    """``@frappe_whitelist()`` → real ``frappe.whitelist()`` in a bench, else a
    no-op marker decorator so the module still imports outside a bench."""
    try:
        import frappe

        return frappe.whitelist()
    except Exception:  # pragma: no cover - outside bench

        def _decorator(func):
            func.whitelisted = True
            return func

        return _decorator


def _(msg: str, *args, **kwargs) -> str:
    """Lazy translation marker — resolves to ``frappe._`` inside a bench."""
    try:
        import frappe

        return frappe._(msg, *args, **kwargs)
    except Exception:  # pragma: no cover
        if args or kwargs:
            try:
                return msg.format(*args, **kwargs)
            except Exception:
                return msg
        return msg


# --------------------------------------------------------------------------- #
# Pure helpers (bench-free — unit tested in tests/test_device.py)
# --------------------------------------------------------------------------- #
_LOG_TYPE_IN = {"IN", "I", "1", "CHECKIN", "CHECK IN", "CLOCK IN", "CLOCK", "ENTER"}
_LOG_TYPE_OUT = {"OUT", "O", "0", "CHECKOUT", "CHECK OUT", "CLOCK OUT", "EXIT", "LEAVE"}


def normalize_log_type(value: Any) -> str:
    """Coerce a free-form punch_type into Frappe HR's ``IN``/``OUT`` vocabulary.

    Unknown / blank → ``IN`` (the safer default — the day's first punch).
    """
    raw = str(value or "").strip().upper()
    if raw in _LOG_TYPE_OUT:
        return "OUT"
    return "IN"


def parse_log_time(value: Any, tz: str | None = None) -> datetime | None:
    """Parse a punch timestamp into a **portal-local aware** datetime.

    Accepts:
      * ``datetime`` (returned unchanged, naive → assumed UTC then converted),
      * ISO-8601 (``2026-06-21T08:30:00`` / with offset / with ``Z``),
      * ``YYYY-MM-DD HH:MM[:SS]`` (Frappe display format),
      * epoch seconds (int/float/numeric string).

    ``tz`` is the timezone the *naive* timestamp is expressed in (the device
    timezone). Returns ``None`` if the value cannot be parsed.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        # Epoch seconds (purely numeric)?
        if isinstance(value, (int, float)):
            try:
                dt = datetime.fromtimestamp(float(value), tz=ZoneInfo("UTC"))
                return tz_utils.to_portal(dt, tz)
            except (OverflowError, OSError, ValueError):
                return None
        s = str(value).strip()
        if s.replace(".", "", 1).isdigit():
            try:
                dt = datetime.fromtimestamp(float(s), tz=ZoneInfo("UTC"))
                return tz_utils.to_portal(dt, tz)
            except (OverflowError, OSError, ValueError):
                return None
        # Normalize a trailing "Z" so fromisoformat accepts it.
        iso = s.replace("Z", "+00:00")
        for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.fromisoformat(iso) if fmt is None else datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None  # try next format
        else:
            return None
        if dt is None:
            return None

    # ``tz_utils.to_portal`` treats a naive datetime as UTC. We instead want the
    # naive value interpreted in the *device* timezone, so attach it first.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_utils.get_tzinfo(tz))
    return dt.astimezone(tz_utils.get_tzinfo())


def to_utc_storage_str(portal_dt: datetime) -> str:
    """Format a portal-local aware datetime as the UTC string stored in
    ``Employee Checkin.time`` (``YYYY-MM-DD HH:MM:SS``)."""
    return portal_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def derive_device_status(
    is_active: int | bool | None = 1,
    last_sync_at: str | datetime | None = None,
    last_log_time: str | datetime | None = None,
    stale_after_hours: float = 24.0,
) -> str:
    """Derive a coarse device status for the FE ``StatusBadge``.

    Vocabulary understood by ``hr-ui/utils/hrStatus.deviceStatus``:
    ``Online | Synced | Idle | Offline | Inactive``.
    """
    if is_active is not None and int(is_active or 0) == 0:
        return "Inactive"
    now = tz_utils.now_in_portal()
    last = _coerce_dt(last_sync_at) or _coerce_dt(last_log_time)
    if last is None:
        return "Idle"
    if (now - last) <= timedelta(hours=stale_after_hours):
        return "Synced"
    return "Offline"


def _coerce_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return tz_utils.to_portal(value)
    return parse_log_time(value)


def normalize_upload_log(
    raw: dict | None, default_device_code: str | None = None
) -> tuple[dict | None, str | None]:
    """Validate + normalize one uploaded punch row.

    Returns ``(normalized, None)`` on success or ``(None, reason)`` on rejection.
    The normalized dict carries only the keys the ingest path needs:

        employee, raw_employee_code, user, log_time(portal dt),
        log_type(IN/OUT), device_code, latitude, longitude, payload
    """
    if not isinstance(raw, dict) or not raw:
        return None, "Dòng log trống."

    log_time = parse_log_time(raw.get("log_time") or raw.get("time") or raw.get("timestamp"))
    if log_time is None:
        return None, "Thiếu/th sai định dạng thời gian chấm công (log_time)."

    employee = str(raw.get("employee") or "").strip() or None
    user = str(raw.get("user") or "").strip() or None
    raw_employee_code = str(raw.get("raw_employee_code") or raw.get("code") or "").strip() or None
    if not (employee or user or raw_employee_code):
        return None, "Thiếu mã nhân viên / người dùng."

    device_code = (
        str(raw.get("device_id") or raw.get("device_code") or raw.get("device") or "").strip()
        or default_device_code
    )

    try:
        lat = float(raw["latitude"]) if raw.get("latitude") not in (None, "") else None
    except (TypeError, ValueError):
        lat = None
    try:
        lon = float(raw["longitude"]) if raw.get("longitude") not in (None, "") else None
    except (TypeError, ValueError):
        lon = None

    return {
        "employee": employee,
        "raw_employee_code": raw_employee_code,
        "user": user,
        "log_time": log_time,
        "log_type": normalize_log_type(raw.get("log_type") or raw.get("punch_type") or raw.get("type")),
        "device_code": device_code or None,
        "latitude": lat,
        "longitude": lon,
        "payload": raw,
    }, None


def device_row(
    doc: dict,
    status: str | None = None,
    last_log_time: str | None = None,
    employee_count: int | None = None,
) -> dict:
    """Shape a VN Attendance Device row for the FE ``useDevice``/``HrDevicesView``.

    Includes both ``device_code`` and ``device_id`` (FE keys on either), plus the
    status/last-log/mapping-count enrichment from the sync endpoints.
    """
    code = doc.get("device_code") or doc.get("name")
    last_sync = doc.get("last_sync_at")
    is_active = doc.get("is_active", 1)
    derived = status or derive_device_status(is_active, last_sync, last_log_time)
    return {
        "name": doc.get("name"),
        "device_id": code,
        "device_code": code,
        "device_name": doc.get("device_name"),
        "device_type": doc.get("device_type"),
        "company": doc.get("company"),
        "location": doc.get("work_location") or doc.get("ip_address"),
        "work_location": doc.get("work_location"),
        "sync_method": doc.get("sync_method"),
        "timezone": doc.get("timezone"),
        "is_active": int(is_active or 0),
        "is_syncing": 0,
        "status": derived,
        "last_sync_at": last_sync,
        "last_log_time": last_log_time,
        "employee_count": employee_count,
    }


# --------------------------------------------------------------------------- #
# Bench-dependent endpoints (frappe imported lazily)
# --------------------------------------------------------------------------- #
def _assert_hr_manager() -> None:
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_MANAGER_ROLES):
        import frappe

        frappe.throw(
            _("Bạn không có quyền quản lý thiết bị chấm công."),
            frappe.PermissionError,
        )


def _device_fields() -> list[str]:
    return [
        "name",
        "device_name",
        "device_code",
        "company",
        "device_type",
        "work_location",
        "is_active",
        "ip_address",
        "port",
        "serial_number",
        "timezone",
        "sync_method",
        "last_sync_at",
    ]


def _enrich(devices: list[dict]) -> list[dict]:
    """Attach status / last-log-time / mapping-count to a list of device docs."""
    import frappe

    if not devices:
        return []
    names = [d["name"] for d in devices]

    # Latest punch per device (one query, group in Python).
    last_log_map: dict[str, str] = {}
    for row in frappe.db.get_all(
        "VN Attendance Raw Log",
        filters={"device": ["in", names], "log_time": ["is", "set"]},
        fields=["device", "log_time"],
        order_by="log_time desc",
        limit_page_length=500,
    ):
        dev = row.get("device")
        if dev and dev not in last_log_map:
            last_log_map[dev] = row.get("log_time")

    # Active employee-mapping count per device.
    count_map: dict[str, int] = {}
    for row in frappe.db.get_all(
        "VN Device Employee Mapping",
        filters={"device": ["in", names], "is_active": 1},
        fields=["device", "count(*) as cnt"],
        group_by="device",
    ):
        count_map[row["device"]] = int(row.get("cnt") or 0)

    out = []
    for d in devices:
        last_log = last_log_map.get(d["name"])
        out.append(
            device_row(
                d,
                last_log_time=last_log,
                employee_count=count_map.get(d["name"], 0),
            )
        )
    return out


# Broad-search fields for the device list (DNA §6.6 D — OR-combined free text).
_DEVICE_SEARCH_FIELDS = (
    "name",
    "device_name",
    "device_code",
    "device_type",
    "work_location",
    "company",
    "ip_address",
)


def _device_search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text device search, or None."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{q}%"
    return [[field, "like", like] for field in _DEVICE_SEARCH_FIELDS]


_DEVICE_SUMMARY_FIELDS = ["name", "is_active"]


def _device_summary(light_rows) -> dict:
    """Aggregate counts over the full filtered set (SPA summary tiles)."""
    active = sum(1 for r in light_rows or [] if int(r.get("is_active") or 0) == 1)
    return {"total": len(light_rows or []), "active": active}


@frappe_whitelist()
def list_devices(
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """GET device.list_devices — device directory + last-sync status (HR-only).

    ``search`` performs a server-side broad LIKE across the device's text fields
    (DNA §6.6 D, HR-BL device) so the SPA broad-search box no longer re-filters
    an already-loaded list client-side.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates the *full* filtered set so the SPA
    summary tiles stay correct under pagination. Without ``page_size`` the legacy
    bare-list return is preserved.
    """
    import frappe

    _assert_hr_manager()
    or_filters = _device_search_or_filters(search)

    if page_size:
        summary = _device_summary(
            pagination.all_rows(
                "VN Attendance Device",
                fields=_DEVICE_SUMMARY_FIELDS,
                or_filters=or_filters,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.db.get_all(
                    "VN Attendance Device",
                    or_filters=or_filters,
                    fields=_device_fields(),
                    order_by="is_active desc, modified desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="device.list_devices failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {"data": _enrich(rows), "total": summary["total"], "summary": summary}

    devices = frappe.db.get_all(
        "VN Attendance Device",
        or_filters=or_filters,
        fields=_device_fields(),
        order_by="is_active desc, modified desc",
    )
    return _enrich(devices)


@frappe_whitelist()
def sync_status(device_id: str | None = None) -> list[dict]:
    """GET device.sync_status — last-sync status of one (by device_code) or all devices."""
    import frappe

    _assert_hr_manager()
    filters = {}
    if device_id:
        # name == device_code (autoname:field:device_code); accept either key.
        filters = {"device_code": device_id}
    devices = frappe.db.get_all("VN Attendance Device", filters=filters, fields=_device_fields())
    return _enrich(devices)


@frappe_whitelist()
def sync_device(device_id: str | None = None) -> dict:
    """POST device.sync_device — trigger an on-demand sync for one device.

    Real network polling of a physical device is out of scope here; this endpoint
    reprocesses any *Pending* raw logs for the device (e.g. queued punches that
    failed validation earlier), stamps ``last_sync_at``, and returns the new
    status. It is the single entry point the FE "Đồng bộ" button calls.
    """
    import frappe

    _assert_hr_manager()
    if not device_id:
        frappe.throw(_("Thiếu mã thiết bị."), frappe.ValidationError)

    device = frappe.db.get_value(
        "VN Attendance Device",
        device_id,
        _device_fields(),
        as_dict=True,
    )
    if not device:
        frappe.throw(_("Không tìm thấy thiết bị."), frappe.NotFound)

    processed = _process_pending_logs(device.get("name"))

    now_str = tz_utils.now_in_portal().strftime("%Y-%m-%d %H:%M:%S")
    frappe.db.set_value("VN Attendance Device", device.get("name"), "last_sync_at", now_str)
    device["last_sync_at"] = now_str
    frappe.db.commit()

    rows = _enrich([device])
    status = rows[0]["status"] if rows else derive_device_status(device.get("is_active"), now_str, None)
    return {
        "status": status,
        "last_sync_at": now_str,
        "processed": processed,
        "message": _("Đã xử lý {0} log đang chờ.").format(processed)
        if processed
        else _("Không có log mới cần xử lý."),
    }


def _process_pending_logs(device_name: str) -> int:
    """Re-ingest raw logs stuck in ``Pending`` for ``device_name``.

    Returns the number of logs that transitioned to ``Processed``/``Skipped``.
    """
    import frappe

    pending = frappe.db.get_all(
        "VN Attendance Raw Log",
        filters={"device": device_name, "processing_status": "Pending"},
        fields=["name"],
        limit_page_length=500,
    )
    count = 0
    for row in pending:
        try:
            _process_raw_log(row["name"])
            count += 1
        except Exception as exc:  # noqa: BLE001 - log + continue, don't abort the batch
            frappe.db.set_value(
                "VN Attendance Raw Log",
                row["name"],
                {"processing_status": "Error", "validation_message": str(exc)[:300]},
            )
    return count


@frappe_whitelist()
def upload_logs(logs: list | None = None) -> dict:
    """POST device.upload_logs — manual upload of raw punch logs (HR-only).

    Each item: ``{ employee?, user?, raw_employee_code?, log_time, punch_type?,
    device_id?, latitude?, longitude? }``. Returns ``{ accepted, rejected,
    message }``. Accepted punches are turned into ``Employee Checkin`` rows
    (which fire the existing recalc hook) and back-linked from the raw log.
    """
    import frappe

    _assert_hr_manager()
    if not isinstance(logs, list) or not logs:
        frappe.throw(_("Chưa có dữ liệu để tải lên."), frappe.ValidationError)

    accepted = 0
    rejected = 0
    errors: list[str] = []

    for idx, raw in enumerate(logs):
        normalized, reason = normalize_upload_log(raw)
        if normalized is None:
            rejected += 1
            errors.append(f"#{idx + 1}: {reason}")
            continue
        try:
            _create_raw_log(normalized, source_type="Import")
            accepted += 1
        except frappe.DuplicateEntryError:
            # Idempotent: same punch already ingested — count as accepted.
            accepted += 1
        except Exception as exc:  # noqa: BLE001
            rejected += 1
            errors.append(f"#{idx + 1}: {str(exc)[:200]}")

    frappe.db.commit()
    message = _("Đã nhận {0} log hợp lệ{1}.").format(accepted, f", {rejected} bị từ chối" if rejected else "")
    if errors:
        message += " " + "; ".join(errors[:5])
    return {"accepted": accepted, "rejected": rejected, "message": message}


# --------------------------------------------------------------------------- #
# Ingest primitives
# --------------------------------------------------------------------------- #
def _resolve_employee(normalized: dict, device_name: str | None) -> tuple[str | None, str | None]:
    """Resolve the punch's Employee name.

    Priority: explicit ``employee`` (name/number) → ``user`` email (user_id) →
    ``raw_employee_code`` via VN Device Employee Mapping. Returns ``(name, msg)``.
    """
    import frappe

    emp_field = normalized.get("employee")
    if emp_field:
        for key in ("name", "employee_number"):
            hit = frappe.db.get_value("Employee", {key: emp_field, "status": "Active"})
            if hit:
                return hit, None
        # Fall through to code mapping using the supplied value as raw code.

    user = normalized.get("user")
    if user:
        hit = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"})
        if hit:
            return hit, None

    raw_code = normalized.get("raw_employee_code") or emp_field
    if raw_code and device_name:
        mapping = frappe.db.get_value(
            "VN Device Employee Mapping",
            {"device": device_name, "raw_employee_code": raw_code, "is_active": 1},
            "employee",
        )
        if mapping:
            return mapping, None

    if raw_code:
        # Last resort: a global mapping (any device) for the same raw code.
        mapping = frappe.db.get_value(
            "VN Device Employee Mapping",
            {"raw_employee_code": raw_code, "is_active": 1},
            "employee",
        )
        if mapping:
            return mapping, None

    return None, _("Không xác định được nhân viên cho log.")


def _resolve_device(device_code: str | None) -> str | None:
    """Return the VN Attendance Device name for a punch's device_code."""
    import frappe

    if not device_code:
        return None
    return frappe.db.get_value("VN Attendance Device", {"device_code": device_code}, "name")


def _create_raw_log(normalized: dict, source_type: str = "Device") -> str:
    """Persist a normalized punch as a VN Attendance Raw Log, then process it.

    Dedup: an existing raw log for the same (employee + log_time + log_type +
    device) is treated as a no-op. Raises ``DuplicateEntryError`` semantics are
    handled by the caller.
    """
    import frappe

    device_code = normalized.get("device_code")
    device_name = _resolve_device(device_code)
    employee, err_msg = _resolve_employee(normalized, device_name)
    portal_dt = normalized["log_time"]
    log_type = normalized["log_type"]
    log_time_utc = to_utc_storage_str(portal_dt)

    is_valid = 1
    validation_message = ""
    if not employee:
        is_valid = 0
        validation_message = err_msg or "Unresolved employee"

    # Dedup against already-stored raw logs (same employee + time + type + device).
    dup_filters = {
        "log_time": log_time_utc,
        "log_type": log_type,
    }
    if employee:
        dup_filters["employee"] = employee
    if device_name:
        dup_filters["device"] = device_name
    existing = frappe.db.get_value("VN Attendance Raw Log", dup_filters, "name")
    if existing:
        # Idempotent — surface as a DuplicateEntry so the caller counts it OK.
        raise frappe.DuplicateEntryError("VN Attendance Raw Log", existing)

    raw = frappe.get_doc(
        {
            "doctype": "VN Attendance Raw Log",
            "source_type": source_type,
            "device": device_name,
            "raw_employee_code": normalized.get("raw_employee_code") or normalized.get("employee"),
            "employee": employee,
            "log_time": log_time_utc,
            "log_type": log_type,
            "latitude": normalized.get("latitude"),
            "longitude": normalized.get("longitude"),
            "raw_payload": json.dumps(normalized.get("payload") or {}, ensure_ascii=False, default=str),
            "is_valid": is_valid,
            "validation_message": validation_message,
            "processing_status": "Pending",
        }
    )
    # Device ingestion — runs from a device push / sync job, not an interactive
    # user session, so the system writes the raw log directly.
    raw.insert(ignore_permissions=True)

    if is_valid:
        _process_raw_log(raw.name)
    else:
        frappe.db.set_value(
            "VN Attendance Raw Log",
            raw.name,
            {"processing_status": "Skipped", "validation_message": validation_message},
        )
    return raw.name


def _process_raw_log(raw_log_name: str) -> str | None:
    """Turn a valid raw log into an Employee Checkin (fires the recalc hook).

    Dedup against Employee Checkin (same employee + time + log_type) — if a
    matching checkin already exists, the raw log is flagged ``is_duplicate`` and
    ``Skipped`` rather than re-created. Returns the checkin name or ``None``.
    """
    import frappe

    raw = frappe.db.get_value(
        "VN Attendance Raw Log",
        raw_log_name,
        ["name", "employee", "log_time", "log_type", "device", "latitude", "longitude", "is_valid"],
        as_dict=True,
    )
    if not raw or not raw.get("employee") or not raw.get("is_valid"):
        return None

    # Lock guard: a punch inside a Locked monthly period must not create a new
    # checkin — the closed attendance would silently drift away from its locked
    # lines. Mark the raw log Skipped so HR can see (and re-process after an
    # unlock) instead of failing the whole sync batch.
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        from gege_hr.gege_hr.api.attendance import _is_date_locked

        _portal_date = (
            _dt.strptime(str(raw["log_time"])[:19], "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=ZoneInfo("UTC"))
            .astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
            .date()
        )
        if _is_date_locked(_portal_date.isoformat()):
            frappe.db.set_value(
                "VN Attendance Raw Log",
                raw_log_name,
                {
                    "processing_status": "Skipped",
                    "validation_message": "Ngày {} thuộc kỳ công đã khoá.".format(
                        _portal_date.isoformat()
                    ),
                },
            )
            return None
    except Exception:
        frappe.log_error(frappe.get_traceback(), "device lock guard")

    device_code = None
    if raw.get("device"):
        device_code = frappe.db.get_value("VN Attendance Device", raw["device"], "device_code")

    # Dedup against Employee Checkin directly.
    existing = frappe.db.get_value(
        "Employee Checkin",
        {
            "employee": raw["employee"],
            "time": raw["log_time"],
            "log_type": raw["log_type"],
        },
        "name",
    )
    if existing:
        frappe.db.set_value(
            "VN Attendance Raw Log",
            raw_log_name,
            {
                "is_duplicate": 1,
                "processing_status": "Skipped",
                "validation_message": "Duplicate Employee Checkin",
                "employee_checkin": existing,
            },
        )
        return existing

    checkin = frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": raw["employee"],
            "log_type": raw["log_type"],
            "time": raw["log_time"],
            "device_id": device_code or "gege_hr-device",
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
        }
    )
    # Device ingestion path (see above) — system writes the Employee Checkin.
    checkin.insert(ignore_permissions=True)
    # ``on_employee_checkin_create`` (after_insert hook) enqueues recalc here.

    frappe.db.set_value(
        "VN Attendance Raw Log",
        raw_log_name,
        {"processing_status": "Processed", "employee_checkin": checkin.name},
    )
    return checkin.name
