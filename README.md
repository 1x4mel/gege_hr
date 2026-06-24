# gege_hr — Vietnamese flexible-shift HR backend (Frappe)

`gege_hr` is the Frappe custom app that backs the **hr-ui** Vue SPA
(`../hr-ui`). It implements the Vietnamese flexible-shift attendance &
payroll domain described in [`../md/hr-ui-plan-v5.md`](../md/hr-ui-plan-v5.md)
and [`../md/doctype-design.md`](../md/doctype-design.md).

> **State:** Milestone-1 foundation. App scaffold + core DocTypes + auth &
> attendance APIs are in place; the Work-Session calculation engine
> (segments, night split, OT) ships in Milestone 2.

## Layout

```
gege_hr/
├── setup.py                  # pip packaging (install into a bench)
├── requirements.txt
└── gege_hr/
    ├── hooks.py              # Frappe integration (doc_events, scheduler, boot)
    ├── modules.txt           # module: gege_hr
    ├── patches.txt
    └── gege_hr/              # the module package
        ├── api/              # one file per domain (RPC endpoints)
        │   ├── auth.py       # get_csrf_token / login / logout / me
        │   ├── attendance.py # today_status / mobile_checkin / my_logs / my_monthly_summary
        │   ├── shift.py      # my_schedule / shift_type_options / team_schedule (+hook stubs)
        │   └── leave.py      # leave_type_options / my_leave_balance / my_applications (+hook stubs)
        ├── utils/            # tz, employee resolver, rate-limit, naming
        └── doctype/          # DocType definitions (JSON + Python)
            ├── vn_hr_portal_setting/          # Single — global config + flags
            ├── vn_attendance_policy/          # Master — grace/OT/night/penalty rules
            ├── vn_attendance_penalty_rule/    # Child table
            ├── vn_work_location/              # Master — geofence + Wi-Fi/IP
            └── vn_mobile_checkin_attempt/     # Transaction — check-in audit/idempotency
```

## Time-zone strategy (plan §2.7)

Vietnam = **UTC+7** (`Asia/Ho_Chi_Minh`), no DST. Frappe stores `Datetime` as
UTC; every calculation converts to the portal timezone first
([`utils/tz.py`](gege_hr/gege_hr/utils/tz.py)). `work_date` = portal calendar
date the shift **starts** on (a night shift crossing midnight keeps the start
date).

## Install (once a bench exists)

```bash
# from inside a frappe-bench
bench get-app /home/frappe/hr/gege_hr
bench --site <site> install-app gege_hr
bench --site <site> migrate
```

Frontend calls these via `gege_hr.gege_hr.api.<domain>.<fn>`
(see [`../hr-ui/src/api/index.js`](../hr-ui/src/api/index.js)).
