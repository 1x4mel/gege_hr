"""hooks.py must not re-assign a hook dict: a second top-level ``doc_events`` /
``scheduler_events`` silently REPLACES the first (FIX 2026-10-04: the
VN Leave Blackout Period realtime hooks never ran)."""

from __future__ import annotations

import ast
import os

_HOOKS = os.path.join(os.path.dirname(__file__), "..", "gege_hr", "hooks.py")


def _assigned_names() -> list[str]:
    tree = ast.parse(open(_HOOKS, encoding="utf-8").read())
    return [
        t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
    ]


def test_no_top_level_hook_assigned_twice():
    names = _assigned_names()
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_blackout_doc_events_and_hourly_checkout_miss_registered():
    tree = ast.parse(open(_HOOKS, encoding="utf-8").read())
    values = {
        t.id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    doc_events = ast.literal_eval(values["doc_events"])
    blackout = doc_events["VN Leave Blackout Period"]
    for event in ("after_insert", "on_update", "on_trash"):
        assert blackout[event] == "gege_hr.gege_hr.api.leave_blackout.on_doc_event"
    scheduler = ast.literal_eval(values["scheduler_events"])
    assert "gege_hr.gege_hr.utils.checkout_miss.run_hourly" in scheduler["cron"]["0 * * * *"]
