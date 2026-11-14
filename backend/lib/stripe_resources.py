"""Stripe object -> plain dict normalization.

RESTORED 2026-11-14 (InvoiceEase fold-in, FIX-IT-FIRST): routers/billing.py
and routers/payments.py import `stripe_dict` from here, but the module was
missing from the tree. Stripe SDK objects are dict-like but not plain
mappings (Stripe 11+/16 objects), so webhook handlers normalize them with
this helper immediately after signature verification.
"""


def stripe_dict(obj):
    """Recursively convert a Stripe object (or any nested structure of
    Stripe objects / dicts / lists) into plain Python dicts/lists."""
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if isinstance(obj, dict):
        return {k: stripe_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [stripe_dict(v) for v in obj]
    return obj
