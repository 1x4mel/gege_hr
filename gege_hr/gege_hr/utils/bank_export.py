"""FIX-3 (I-1, hr-gap-audit) — Vietnamese bank payment file builder.

Builds NAPAS / ACCT / CSV payment files from normalized payroll rows. The
builders are **pure** (no frappe) so they unit-test without a bench; the endpoint
``api/payroll.export_bank_file`` resolves each employee's bank details, validates
them, then calls :func:`build_file`.

Each input row is a plain dict:
    ``{employee, employee_name, account_no, bank_code, bank_name, amount}``

The NAPAS layout here is a pragmatic, adjustable template (``H`` header / ``D``
detail / ``T`` trailer with a control total). Adjust field widths/order to the
exact spec of the destination bank — the invariants (1 detail per row, trailer
count + control total == sum) are what the tests lock down.
"""

from __future__ import annotations


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def amount_int(value) -> int:
    """Whole-đồng (no decimals) — banks receive integer đồng."""
    return int(round(_to_float(value)))


def control_total(rows) -> float:
    """Sum of every row's ``amount`` (float, 2dp)."""
    return round(sum(_to_float(r.get("amount")) for r in (rows or [])), 2)


def missing_fields(rows) -> list:
    """Rows that cannot be paid: no ``account_no`` or amount ≤ 0."""
    out = []
    for r in (rows or []):
        account = str(r.get("account_no") or "").strip()
        amt = _to_float(r.get("amount"))
        if not account:
            out.append(
                {
                    "employee": r.get("employee"),
                    "employee_name": r.get("employee_name"),
                    "reason": "missing account_no",
                }
            )
        elif amt <= 0:
            out.append(
                {
                    "employee": r.get("employee"),
                    "employee_name": r.get("employee_name"),
                    "reason": "amount <= 0",
                }
            )
    return out


def build_csv(rows, *, company: str = "", value_date: str = "", filename: str | None = None) -> dict:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["STT", "Ma nhan vien", "Ten", "So TK", "Ngan hang", "So tien (VND)"])
    for i, r in enumerate(rows, 1):
        writer.writerow(
            [
                i,
                r.get("employee"),
                r.get("employee_name"),
                r.get("account_no"),
                r.get("bank_name"),
                amount_int(r.get("amount")),
            ]
        )
    writer.writerow([])
    writer.writerow(["", "", "", "", "TONG CONG", amount_int(control_total(rows))])
    return {
        "filename": filename or f"payroll_{value_date or 'export'}.csv",
        "content": buf.getvalue(),
        "mime": "text/csv",
        "total": control_total(rows),
        "count": len(rows),
    }


def build_napas(
    rows,
    *,
    company: str = "",
    value_date: str = "",
    customer_code: str = "GEGE",
    filename: str | None = None,
) -> dict:
    total = control_total(rows)
    lines = [f"H|{customer_code}|{value_date}|{len(rows)}|{amount_int(total)}"]
    for r in rows:
        bank = r.get("bank_code") or r.get("bank_name") or ""
        lines.append(
            f"D|{r.get('account_no')}|{r.get('employee_name')}|{bank}|{amount_int(r.get('amount'))}"
        )
    lines.append(f"T|{len(rows)}|{amount_int(total)}")
    content = "\n".join(lines) + "\n"
    return {
        "filename": filename or f"payroll_{value_date or 'export'}.napas.txt",
        "content": content,
        "mime": "text/plain",
        "total": total,
        "count": len(rows),
    }


def build_acct(rows, *, company: str = "", value_date: str = "", filename: str | None = None) -> dict:
    lines = [
        f"{r.get('account_no')}|{amount_int(r.get('amount'))}|{r.get('employee_name')}" for r in rows
    ]
    content = "\n".join(lines) + ("\n" if lines else "")
    return {
        "filename": filename or f"payroll_{value_date or 'export'}.acct.txt",
        "content": content,
        "mime": "text/plain",
        "total": control_total(rows),
        "count": len(rows),
    }


def build_file(rows, fmt: str = "napas", **opts) -> dict:
    """Dispatch to the right builder by format (napas | acct | csv)."""
    fmt = (fmt or "napas").lower()
    if fmt == "csv":
        return build_csv(rows, **opts)
    if fmt == "acct":
        return build_acct(rows, **opts)
    return build_napas(rows, **opts)
