"""PDF invoice/receipt intake pilot (standout extra A) — invoice2data.

Fills the gap where PDF receipts previously returned 422 from the scan
endpoint ("AI scan needs an image"). invoice2data (MIT) matches the PDF's
text layer against templates: our own generic template first, then the
library's bundled vendor templates. The library is used as-is; templates
and normalization are ours.

Returns a normalized dict in the receipts `extracted` shape, or None when
no template matches — the caller then keeps the manual-entry path. No AI
key is needed for this path (pure template matching on the text layer);
scanned-image PDFs without a text layer still need the vision path.
"""
import tempfile
from datetime import datetime
from pathlib import Path

_TEMPLATES = None


def _templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        from invoice2data.extract.loader import read_templates
        ours = read_templates(str(Path(__file__).parent / "invoice_templates"))
        bundled = read_templates()
        _TEMPLATES = list(ours) + list(bundled)
    return _TEMPLATES


def _norm_date(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10]


def extract_pdf_invoice(data: bytes):
    """bytes -> normalized extraction dict or None."""
    try:
        from invoice2data import extract_data
    except ImportError:
        return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(data)
            tmp = f.name
        result = extract_data(tmp, templates=_templates())
    except Exception:
        return None
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    if not result:
        return None
    amount = result.get("amount")
    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    generic = (result.get("template_name") or "").startswith("generic-invoice")
    return {
        # A bundled vendor template's issuer IS the vendor. Our generic
        # template proves nothing about who issued the document — leave
        # the vendor blank for the human review instead of inventing one.
        "vendor": "" if generic else (result.get("issuer") or "").strip(),
        "date": _norm_date(result.get("date")),
        "amount": amount,
        "tax": float(result.get("amount_tax") or 0),
        "invoice_number": str(result.get("invoice_number") or ""),
        "currency": "USD" if generic else (result.get("currency") or "USD"),
        "source": "invoice2data",
        "template": result.get("template_name") or result.get("issuer") or "",
        "needs_review": True,     # template extraction always gets a human look
    }
