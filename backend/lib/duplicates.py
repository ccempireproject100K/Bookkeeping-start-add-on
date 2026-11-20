"""Duplicate-entry guards for native invoice / bill creation and
unkeyed payments (safeguard audit 2026-11-19, gap 2).

The customer create path has always hard-blocked a duplicate with a
409 naming the existing record (routers/customers.py). Invoices,
bills and payments had no equivalent: double-entering a bill silently
posted it twice. These pure matchers give those paths the same shape:

- a duplicate document is the same counterparty + the same amount +
  the same document date. A bill additionally matches on the vendor's
  own bill_ref (the same vendor document entered twice) — the
  strongest signal of all;
- a duplicate payment is the same amount + the same date + the same
  method already recorded on the same document;
- callers raise a 409 whose detail names the existing record, exactly
  like the customer pattern;
- flows that legitimately create look-alike documents and carry their
  own dedupe (recurring materialization, invoice splits, PO→bill
  conversion) pass allow_duplicate=True explicitly at their call
  sites, so the exception is visible in code, never silent.

Voided documents never count as duplicates: re-issuing after a void
is the normal correction flow.
"""

_DEAD_STATUSES = {"void", "voided", "cancelled", "canceled", "deleted"}


def norm_name(s):
    return " ".join(str(s or "").split()).strip().lower()


def _same_party(a_id, a_name, b_id, b_name):
    """Counterparty identity: ids decide when both sides carry one;
    otherwise the normalized display name decides (the customer
    duplicate rule's shape)."""
    if a_id and b_id:
        return a_id == b_id
    return bool(norm_name(a_name)) and norm_name(a_name) == norm_name(b_name)


def find_duplicate_invoice(existing, *, customer_id, client_name,
                           total_cents, issue_date):
    """First live invoice in `existing` that duplicates the one being
    issued, else None."""
    for inv in existing or []:
        if str(inv.get("status") or "").lower() in _DEAD_STATUSES:
            continue
        if inv.get("total_cents") != total_cents:
            continue
        if inv.get("issue_date") != issue_date:
            continue
        if _same_party(customer_id, client_name,
                       inv.get("customer_id"), inv.get("client_name")):
            return inv
    return None


def find_duplicate_bill(existing, *, vendor_id, vendor, gross_cents,
                        bill_date, bill_ref):
    """First live bill in `existing` that duplicates the one being
    created, else None. Matches on the vendor's own reference when
    both sides carry one, else on gross amount + bill date."""
    ref = norm_name(bill_ref)
    for bill in existing or []:
        if str(bill.get("status") or "").lower() in _DEAD_STATUSES:
            continue
        if not _same_party(vendor_id, vendor,
                           bill.get("vendor_id"), bill.get("vendor")):
            continue
        if ref and norm_name(bill.get("bill_ref")) == ref:
            return bill
        if (bill.get("gross_cents", bill.get("amount_cents")) == gross_cents
                and bill.get("bill_date") == bill_date):
            return bill
    return None


def find_duplicate_payment(payments, *, amount_cents, pay_date, method):
    """First recorded payment identical to the one being posted (same
    amount applied, same date, same method), else None."""
    for pmt in payments or []:
        if (pmt.get("amount_cents") == amount_cents
                and pmt.get("date") == pay_date
                and (pmt.get("method") or "bank") == (method or "bank")):
            return pmt
    return None
