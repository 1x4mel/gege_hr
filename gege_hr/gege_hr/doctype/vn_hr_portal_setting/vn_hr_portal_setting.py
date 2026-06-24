# For license information, please see license.txt

from __future__ import unicode_literals

from frappe.model.document import Document


class VNHRPortalSetting(Document):
    """Server controller for the VN HR Portal Setting single DocType.

    Feature flags (portal timezone, GPS radius, mobile check-in toggle, …) are
    read directly via ``frappe.db.get_single_value("VN HR Portal Setting", ...)``,
    so this class intentionally has no custom logic. It exists because Frappe's
    controller loader requires a ``<DocTypeName as PascalCase>(Document)`` class
    in the controller module, otherwise the doctype is flagged orphaned during
    ``bench migrate``.
    """

    pass


def get_context(self, context):
    """Optionally restrict web view access (not used by the SPA)."""
    return context
