"""Bill-pay PREP logic (Top-20 gap #14, owner-approved 2026-11-14).

Pure functions only — no db, no HTTP. Books prepares, approves, schedules
and RECORDS bill payments; it does not move money. The paying half waits
for a payments partner (see lib/payments_rail.py).

State machine (VA pattern, mirrors the change-order workflow):
    (none) -> prepared -> reviewed -> approved
Approval means READY to pay outside Books — never "paid".
"""
import io
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

PREP_STATES = ("prepared", "reviewed", "approved")

# allowed next state from each current state (None = not yet in workflow)
NEXT_STATE = {None: "prepared", "prepared": "reviewed", "reviewed": "approved"}


def prep_state(bill: dict):
    return bill.get("prep_state") or None


def can_transition(bill: dict, target: str) -> bool:
    return NEXT_STATE.get(prep_state(bill)) == target


def bucket_for(due_date: str, today: str) -> str:
    """due_now = due today or overdue; due_this_week = within 7 days."""
    if not due_date:
        return "upcoming"
    if due_date <= today:
        return "due_now"
    if due_date <= (date.fromisoformat(today) + timedelta(days=7)).isoformat():
        return "due_this_week"
    return "upcoming"


def discount_info(bill: dict, today: str):
    """Early-pay discount terms carried on the bill, computed — never applied.

    Returns None when the bill carries no terms. The discount is figured on
    the outstanding balance: what the shop would save by paying the balance
    on or before discount_date, and what it would actually hand over.
    """
    pct = bill.get("discount_percent")
    ddate = bill.get("discount_date")
    if not pct or not ddate:
        return None
    balance = bill["amount_cents"] - bill.get("paid_cents", 0)
    if balance <= 0:
        return None
    save = int((Decimal(balance) * Decimal(str(pct)) / Decimal(100)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "percent": pct,
        "discount_date": ddate,
        "save_cents": save,
        "pay_cents": balance - save,
        "window_open": ddate >= today,
        "days_left": (date.fromisoformat(ddate) - date.fromisoformat(today)).days,
    }


def packet_rows(bills):
    """The ready-to-pay packet: exactly the approved batch, in pay order."""
    rows = []
    for b in sorted(bills, key=lambda x: (x.get("pay_date_planned") or x.get("due_date") or "",
                                          x.get("vendor", ""))):
        balance = b["amount_cents"] - b.get("paid_cents", 0)
        rows.append({
            "bill_id": b["id"],
            "vendor": b.get("vendor", ""),
            "bill_ref": b.get("bill_ref") or "",
            "amount_cents": balance,
            "due_date": b.get("due_date") or "",
            "pay_date": b.get("pay_date_planned") or b.get("due_date") or "",
        })
    return rows


def packet_csv(rows) -> str:
    def dollars(c):
        return f"{c / 100:.2f}"
    out = ["vendor,bill_ref,amount,due_date,pay_date,bill_id"]
    for r in rows:
        vendor = '"' + str(r["vendor"]).replace('"', '""') + '"'
        out.append(f'{vendor},{r["bill_ref"]},{dollars(r["amount_cents"])},'
                   f'{r["due_date"]},{r["pay_date"]},{r["bill_id"]}')
    total = sum(r["amount_cents"] for r in rows)
    out.append(f"TOTAL,,{dollars(total)},,,")
    return "\n".join(out) + "\n"


def pdf_bytes(title: str, lines: list[str]) -> bytes:
    """Minimal dependency-free PDF (pattern copied from the fold-in lane's
    invoice_delivery._pdf_bytes)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:110]
    pages, chunk = [], []
    for ln in lines:
        chunk.append(ln)
        if len(chunk) >= 46:
            pages.append(chunk); chunk = []
    if chunk or not pages:
        pages.append(chunk)
    content = []
    for pg in pages:
        text = "BT /F1 10 Tf 50 770 Td 14 TL " + " ".join(
            f"({esc(ln)}) Tj T*" for ln in pg) + " ET"
        content.append(text)
    n = 3 + len(pages) * 2
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    def w(data: bytes):
        offsets.append(buf.tell()); buf.write(data)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    w(b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n")
    w(f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>endobj\n".encode())
    w(b"3 0 obj<</Type/Font/Subtype/Type1/BaseFont/Courier>>endobj\n")
    for i, text in enumerate(content):
        w(f"{4 + i * 2} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
          f"/Contents {5 + i * 2} 0 R/Resources<</Font<</F1 3 0 R>>>>>>endobj\n".encode())
        stream = text.encode("latin-1", "replace")
        w(f"{5 + i * 2} 0 obj<</Length {len(stream)}>>stream\n".encode() + stream + b"\nendstream endobj\n")
    xref = buf.tell()
    buf.write(f"xref\n0 {n + 1}\n0000000000 65535 f \n".encode())
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer<</Size {n + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF".encode())
    return buf.getvalue()


def packet_pdf(rows, generated_on: str) -> bytes:
    def dollars(c):
        return f"${c / 100:,.2f}"
    lines = ["READY-TO-PAY PACKET — approved bills",
             f"Generated {generated_on}",
             "Books prepares and records payments; it does not send money.",
             "Pay these outside Books, then mark each bill paid in Books.",
             "",
             f"{'Vendor':<26}{'Ref':<12}{'Amount':>12}  {'Due':<11}{'Pay date':<11}"]
    for r in rows:
        lines.append(f"{str(r['vendor'])[:25]:<26}{str(r['bill_ref'])[:11]:<12}"
                     f"{dollars(r['amount_cents']):>12}  {r['due_date']:<11}{r['pay_date']:<11}")
    total = sum(r["amount_cents"] for r in rows)
    lines += ["", f"TOTAL TO PAY: {dollars(total)}  ({len(rows)} bills)"]
    return pdf_bytes("Ready-to-pay packet", lines)
