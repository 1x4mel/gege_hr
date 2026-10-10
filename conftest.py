"""Pytest bootstrap for gege_hr (no bench required).

The calculation engine (``gege_hr.gege_hr.utils.calc``) and TZ helpers are
**pure functions** guarded against a missing ``frappe`` import, so they can be
unit-tested outside a bench. This conftest inserts the app root onto
``sys.path`` so the dotted import ``gege_hr.gege_hr.utils...`` resolves
regardless of how pytest is invoked.
"""

import importlib.abc
import os
import sys

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)


# --------------------------------------------------------------------------- #
# Deterministic "no frappe" harness for the bench-free unit suite.
# --------------------------------------------------------------------------- #
# gege_hr's API modules expose two flavours of frappe access:
#   (a) *lazy shims* — ``frappe_whitelist()`` / ``_()`` in api/dashboard.py,
#       api/device.py, api/notification.py, … activate their no-op fallback
#       ONLY when ``import frappe`` raises. The "module imports outside a
#       bench" tests assert that fallback (``func.whitelisted is True``).
#   (b) *stub injection* — several tests install their own frappe double via
#       ``monkeypatch.setitem(sys.modules, "frappe", stub)`` and assert against
#       its recorded calls.
#
# Problem: when real frappe IS importable (e.g. running pytest from the bench
# virtualenv, or anywhere ``apps/frappe`` is on the path), case (a) silently
# degrades — ``frappe_whitelist()`` returns the *real* ``frappe.whitelist()``,
# which does not set ``.whitelisted``, so those tests fail; and lazy readers
# hit an unbound werkzeug ``LocalProxy`` instead of the guarded fallback.
#
# This finder makes case (a) deterministic by forcing ``import frappe`` to
# fail during the suite, exactly as if frappe were not installed — reproducing
# the canonical environment these bench-free tests were authored for. Case (b)
# is untouched: an injected stub already present in ``sys.modules`` short-
# circuits the import machinery before any finder is consulted, so doubles
# always win.
class _FrappeImportBlocker(importlib.abc.MetaPathFinder):
    """Block ``import frappe`` so the lazy shims activate (bench-free mode)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "frappe" and fullname not in sys.modules:
            raise ImportError("frappe intentionally blocked for bench-free unit tests")
        return None


_BLOCKER = _FrappeImportBlocker()
if not any(isinstance(f, _FrappeImportBlocker) for f in sys.meta_path):
    sys.meta_path.insert(0, _BLOCKER)
