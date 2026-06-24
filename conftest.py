"""Pytest bootstrap for gege_hr (no bench required).

The calculation engine (``gege_hr.gege_hr.utils.calc``) and TZ helpers are
**pure functions** guarded against a missing ``frappe`` import, so they can be
unit-tested outside a bench. This conftest inserts the app root onto
``sys.path`` so the dotted import ``gege_hr.gege_hr.utils...`` resolves
regardless of how pytest is invoked.
"""

import os
import sys

_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
