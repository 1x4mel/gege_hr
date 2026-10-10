# Copyright (c) 2026
# For license information, please see license.txt

from frappe.model.document import Document


class VNSchedulerHeartbeat(Document):
    """WP4 — one row per scheduler job; ``last_run`` advances on every
    successful execution so :mod:`gege_hr.gege_hr.utils.health` can detect a
    dead engine within hours instead of weeks."""

    pass
