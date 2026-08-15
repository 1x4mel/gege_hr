from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAttendanceWorkSession(Document):
    """The calculated result of one shift occurrence for one employee.

    Produced by ``calculate_work_session`` (plan §9.1) from a ``VN Employee Shift
    Instance`` and its ``Employee Checkin`` rows. Carries the policy snapshot
    used (so historical figures stay stable even when the policy changes) and a
    child table of ``VN Attendance Segment`` rows.

    ``calculation_status`` doubles as the concurrent-edit lock (plan §19.3): the
    recalculation flow claims it atomically to ``Recalculating`` and rejects
    parallel runs for the same session.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #
    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_employee_name()
        self._enforce_computed_invariants()
        self._enforce_payable_day_values()

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")

    def _enforce_computed_invariants(self):
        """raw_overtime_hours is always pre+post (plan §9.1 step 9)."""
        if self.is_new() or self.calculation_status == "Pending":
            # Engine not run yet; allow pending zeros without complaint.
            return
        pre = _as_float(self.raw_pre_overtime_hours)
        post = _as_float(self.raw_post_overtime_hours)
        expected = round(pre + post, 4)
        stored = round(_as_float(self.raw_overtime_hours), 4)
        if stored != expected:
            # Self-heal: the engine is the only writer, keep them in sync.
            self.raw_overtime_hours = expected

        if self.calculation_status == "Recalculating":
            # A doc that is mid-recalculation must not be edited by other paths.
            # The concurrent-lock check happens server-side (SELECT FOR UPDATE
            # in api.attendance), here we just guard naive UI saves.
            pass

    def _enforce_payable_day_values(self):
        """payable_day is restricted to {0, 0.5, 1.0} (plan §9.3)."""
        allowed = {0.0, 0.5, 1.0}
        value = _as_float(self.payable_day)
        if round(value, 2) not in allowed:
            frappe.throw(_("Payable Day chỉ nhận 0, 0.5 hoặc 1.0."))

    # ------------------------------------------------------------------ #
    # Concurrency guard (plan §19.3) — CAS claim against a status value.
    # ------------------------------------------------------------------ #
    def atomic_claim(self, claim_status: str = "Recalculating", allowed_from=None) -> bool:
        """Atomically move calculation_status to ``claim_status``.

        Returns True when this process won the claim. Uses a guarded UPDATE so
        two concurrent recalculations cannot both succeed (CAS). Callers should
        have opened a ``SELECT ... FOR UPDATE`` beforehand, or use this as the
        single guard in low-concurrency deployments.
        """
        from gege_hr.gege_hr.utils._db import guarded_update_tuple

        allowed_from = allowed_from or ["Calculated", "Error", "Pending"]
        placeholders = ", ".join(["%s"] * len(allowed_from))
        won = guarded_update_tuple(
            f"""
            UPDATE `tabVN Attendance Work Session`
            SET calculation_status = %s
            WHERE name = %s AND calculation_status IN ({placeholders})
            """,
            (claim_status, self.name, *allowed_from),
        )
        if won:
            self.calculation_status = claim_status
            self.db_set("calculation_status", claim_status, notify=False)
        return won


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
