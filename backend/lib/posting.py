"""Posting-rule registry: translate business events into balanced journal-entry drafts (cents)."""


def dr(code, cents, memo=None):
    return {"account_code": code, "debit": int(cents), "credit": 0, "memo": memo}


def cr(code, cents, memo=None):
    return {"account_code": code, "credit": int(cents), "debit": 0, "memo": memo}


def _cash_account(payment_method: str) -> str:
    return {"cash": "1010", "card": "1050", "bank": "1020"}.get(payment_method, "1010")


REVENUE_FOR = {
    "haircut": "4000", "beard": "4010", "service": "4020",
    "retail": "4100", "product": "4100",
}


def build_sale(*, lines_in, payment_method, subtotal_cents, discount_cents,
               tax_cents, cogs_cents, commission_cents, entry_date, source_ref, memo, currency: str = "USD"):
    """lines_in: list of {revenue_code, amount_cents}. Produces one balanced JE."""
    cash_code = _cash_account(payment_method)
    gross = subtotal_cents - discount_cents + tax_cents
    jl = [dr(cash_code, gross, "Sale proceeds")]
    if discount_cents:
        jl.append(dr("4900", discount_cents, "Discount"))
    for li in lines_in:
        jl.append(cr(li["revenue_code"], li["amount_cents"], "Revenue"))
    if tax_cents:
        jl.append(cr("2100", tax_cents, "Sales tax collected"))
    if cogs_cents:
        jl.append(dr("5010", cogs_cents, "Cost of goods sold"))
        jl.append(cr("1300", cogs_cents, "Inventory relieved"))
    if commission_cents:
        # accrue staff commission as expense + payable (does not affect cash leg)
        jl.append(dr("5100", commission_cents, "Staff commission"))
        jl.append(cr("2300", commission_cents, "Commission payable"))
    return {"entry_date": entry_date, "currency": currency, "source": "sale", "source_ref": source_ref, "memo": memo, "lines": jl}


def build_invoice_issue(*, revenue_lines, tax_cents, cogs_cents, gross_cents, entry_date, source_ref, memo, currency: str = "USD"):
    jl = [dr("1200", gross_cents, "Invoice issued")]
    for rl in revenue_lines:
        jl.append(cr(rl["revenue_code"], rl["amount_cents"], "Revenue"))
    if tax_cents:
        jl.append(cr("2100", tax_cents, "Sales tax"))
    if cogs_cents:
        jl.append(dr("5010", cogs_cents, "COGS"))
        jl.append(cr("1300", cogs_cents, "Inventory relieved"))
    return {"entry_date": entry_date, "currency": currency, "source": "invoice_issue", "source_ref": source_ref, "memo": memo, "lines": jl}


def build_invoice_paid(*, amount_cents, payment_method, entry_date, source_ref, memo, currency: str = "USD"):
    cash_code = _cash_account(payment_method)
    return {"entry_date": entry_date, "currency": currency, "source": "invoice_paid", "source_ref": source_ref, "memo": memo,
            "lines": [dr(cash_code, amount_cents, "Invoice payment"), cr("1200", amount_cents, "AR cleared")]}


def build_adjustment(*, amount_cents, found, entry_date, source_ref, memo, currency: str = "USD"):
    """Shrinkage (loss) or found inventory, valued at cost."""
    if found:
        lines = [dr("1300", amount_cents, "Inventory found"), cr("5600", amount_cents, "Count correction")]
    else:
        lines = [dr("5600", amount_cents, "Shrinkage"), cr("1300", amount_cents, "Inventory lost")]
    return {"entry_date": entry_date, "currency": currency, "source": "adjustment", "source_ref": source_ref, "memo": memo, "lines": lines}


def build_restock(*, amount_cents, on_terms, entry_date, source_ref, memo, currency: str = "USD"):
    credit_code = "2010" if on_terms else "1020"
    return {"entry_date": entry_date, "currency": currency, "source": "restock", "source_ref": source_ref, "memo": memo,
            "lines": [dr("1300", amount_cents, "Inventory received"), cr(credit_code, amount_cents, "Payable/cash")]}


def build_cash_close(*, variance_cents, entry_date, source_ref, memo, currency: str = "USD"):
    """variance = counted - expected. Positive => over, negative => short."""
    if variance_cents == 0:
        return None
    if variance_cents > 0:
        lines = [dr("1010", variance_cents, "Cash over"), cr("5590", variance_cents, "Cash over/short")]
    else:
        v = -variance_cents
        lines = [dr("5590", v, "Cash short"), cr("1010", v, "Cash over/short")]
    return {"entry_date": entry_date, "currency": currency, "source": "cash_close", "source_ref": source_ref, "memo": memo, "lines": lines}


def build_opening_balances(*, snapshot, entry_date, memo, currency: str = "USD"):
    """snapshot: dict of account_code -> cents for assets/liabilities; equity 3010 is the plug."""
    jl = []
    assets = 0
    liabilities = 0
    for code, cents in snapshot.items():
        if cents <= 0:
            continue
        from lib.coa import ACCOUNT_TYPE_OF
        t = ACCOUNT_TYPE_OF.get(code)
        if t == "asset":
            jl.append(dr(code, cents, "Opening balance"))
            assets += cents
        elif t == "liability":
            jl.append(cr(code, cents, "Opening balance"))
            liabilities += cents
    plug = assets - liabilities
    if plug >= 0:
        jl.append(cr("3010", plug, "Opening equity (plug)"))
    else:
        jl.append(dr("3010", -plug, "Opening equity (plug)"))
    return {"entry_date": entry_date, "currency": currency, "source": "opening_balance", "source_ref": "migration", "memo": memo, "lines": jl}


POSTING_RULES = {
    "sale": build_sale,
    "invoice_issue": build_invoice_issue,
    "invoice_paid": build_invoice_paid,
    "adjustment": build_adjustment,
    "restock": build_restock,
    "cash_close": build_cash_close,
    "opening": build_opening_balances,
}
