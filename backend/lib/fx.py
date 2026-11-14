"""Multi-currency FX lane (Top-20 #16, bounded version).

Governing shape (from the 2026-11-14 screen, SCREEN.md in
buildout/multicurrency-screen-2026-11-14):

- Rate source is **Frankfurter** (MIT code, no key, no quota, commercial use
  allowed per the project's own FAQ), pulled into our own `fx_rates` table.
  The **raw ECB euro reference feed** is the fallback (free reuse with the
  ECB cited as the source). No keyed "free" tier is wired anywhere — they
  either cap out or bar commercial use.
- A document (invoice / bill) is written in a TRANSACTION currency and gets a
  **stamp** at issue time: `fx_rate` (home-currency units per 1 txn unit),
  `fx_rate_date`, `fx_source`. The stamp is the single source of truth for
  that document forever — history is never rewritten by a newer rate.
- The general ledger always posts HOME currency, converted off the stamp.
- When a payment lands at a different rate than the stamp, the difference is
  a REALIZED gain/loss posted to 4950 (gain) / 6990 (loss).

Out of scope for this wave (named, not half-built): unrealized period-end
revaluation, foreign-currency bank accounts, multi-currency payroll/tax.
The workspace home currency itself stays fixed at creation (money.py's 409
stands) — multi-currency rides on top, per document.
"""
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException

from lib.base import new_id, now_iso, today_str

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base={base}&symbols={quote}"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

SOURCE_HOME = "home"
SOURCE_MANUAL = "manual"
SOURCE_FRANKFURTER = "Frankfurter (ECB reference rates)"
SOURCE_ECB = "ECB euro reference rates"

FX_GAIN_ACCOUNT = "4950"
FX_LOSS_ACCOUNT = "6990"

# Currencies offered in the UI pickers (Frankfurter covers ~200; this is the
# working set a trades book is ever likely to touch, home currency included).
COMMON_CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD", "NZD", "SGD",
                     "MXN", "CHF", "JPY", "CNY", "INR", "BRL", "ZAR", "AED"]


# ---------------------------------------------------------------- HTTP seam
def _http_get(url: str) -> str:
    """Single network seam — tests monkeypatch this. Returns body text."""
    req = urllib.request.Request(url, headers={"User-Agent": "bookkeeping-fx/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed provider URLs
        return resp.read().decode("utf-8")


# ------------------------------------------------------------------- parsing
def parse_frankfurter(text: str):
    """Frankfurter v1 payload -> (base, date, {quote: rate-as-str})."""
    import json
    payload = json.loads(text)
    rates = {q.upper(): str(r) for q, r in (payload.get("rates") or {}).items()}
    return payload["base"].upper(), payload["date"], rates


def parse_ecb_xml(text: str):
    """ECB daily XML -> (date, {code: rate-vs-EUR-as-str}). EUR itself is 1."""
    root = ET.fromstring(text)
    ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01",
          "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    day = root.find(".//ecb:Cube/ecb:Cube", ns)
    if day is None:
        raise ValueError("ECB feed: no daily Cube element")
    rates = {"EUR": "1"}
    for cube in day.findall("ecb:Cube", ns):
        rates[cube.attrib["currency"].upper()] = cube.attrib["rate"]
    return day.attrib["time"], rates


def cross_rate(eur_rates: dict, base: str, quote: str) -> str:
    """home-per-txn rate from a vs-EUR table: rate(base->quote) is expressed
    here as units of `base` per 1 `quote` = eur[base] / eur[quote]."""
    b = Decimal(eur_rates[base.upper()])
    q = Decimal(eur_rates[quote.upper()])
    return str((b / q).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))


def convert_cents(cents: int, rate) -> int:
    """Integer txn cents -> integer home cents at `rate` (home per 1 txn)."""
    r = Decimal(str(rate))
    return int((Decimal(int(cents)) * r).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def convert_home_to_txn_cents(cents: int, rate) -> int:
    r = Decimal(str(rate))
    if r == 0:
        raise HTTPException(status_code=422, detail="FX rate cannot be zero")
    return int((Decimal(int(cents)) / r).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rate_str(rate) -> str:
    """Canonical rate string: Decimal-exact, no trailing zeros, no exponent
    (1.10 -> '1.1', 10.00 -> '10'). Stamps and rate rows compare equal
    regardless of which feed or human typed them."""
    return format(Decimal(str(rate)).normalize(), "f")


# ------------------------------------------------------------------- storage
async def store_rate(db, tenant_id, base, quote, rate, rate_date, source):
    doc = {
        "id": new_id(), "tenant_id": tenant_id,
        "base": base.upper(), "quote": quote.upper(),
        "rate": rate_str(rate), "date": rate_date, "source": source,
        "fetched_at": now_iso(),
    }
    await db.fx_rates.update_one(
        {"tenant_id": tenant_id, "base": doc["base"], "quote": doc["quote"],
         "date": rate_date},
        {"$set": doc}, upsert=True)
    return doc


async def get_stored_rate(db, tenant_id, base, quote, on_date=None):
    """Newest stored row for base->quote with date <= on_date (or overall
    newest when on_date is None). Returns the raw doc or None."""
    q = {"tenant_id": tenant_id, "base": base.upper(), "quote": quote.upper()}
    if on_date:
        q["date"] = {"$lte": on_date}
    cur = db.fx_rates.find(q).sort([("date", -1)]).limit(1)
    rows = await cur.to_list(length=1)
    return rows[0] if rows else None


# ------------------------------------------------------------------- refresh
async def refresh_pair(db, tenant_id, base, quote, http_get=None):
    """Pull one pair: Frankfurter first, ECB cross-rate as fallback.
    Returns the stored doc, or None when both sources fail.

    Direction matters: the stored rate is HOME (base) units per 1 unit of
    the transaction currency (quote), so Frankfurter is asked the other way
    round — base=<txn>, symbols=<home> — and its answer is already in
    home-per-txn terms, no inversion (and no rounding) needed."""
    http_get = http_get or _http_get
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return None
    try:
        text = http_get(FRANKFURTER_URL.format(base=quote, quote=base))
        _fbase, fdate, rates = parse_frankfurter(text)
        if base in rates:
            return await store_rate(db, tenant_id, base, quote,
                                    rates[base], fdate, SOURCE_FRANKFURTER)
    except Exception:
        pass
    try:
        text = http_get(ECB_URL)
        edate, eur_rates = parse_ecb_xml(text)
        if base in eur_rates and quote in eur_rates:
            rate = cross_rate(eur_rates, base, quote)
            return await store_rate(db, tenant_id, base, quote,
                                    rate, edate, SOURCE_ECB)
    except Exception:
        pass
    return None


async def refresh_rates(db, tenant_id, home, currencies=None, http_get=None):
    """Refresh home->quote for each needed currency. One failure never
    blocks the others; the summary says plainly what landed and what didn't."""
    home = home.upper()
    wanted = [c.upper() for c in (currencies or COMMON_CURRENCIES)
              if c.upper() != home]
    stored, failed = [], []
    for quote in wanted:
        doc = await refresh_pair(db, tenant_id, home, quote, http_get=http_get)
        if doc:
            stored.append({"quote": quote, "rate": doc["rate"],
                           "date": doc["date"], "source": doc["source"]})
        else:
            failed.append(quote)
    return {"base": home, "stored": stored, "failed": failed}


# -------------------------------------------------------------------- stamps
async def home_currency(db, tenant_id) -> str:
    tenant = await db.tenants.find_one({"id": tenant_id})
    cur = ((tenant or {}).get("settings") or {}).get("currency") or "USD"
    return str(cur).upper()


async def resolve_stamp(db, tenant_id, home, txn_currency, on_date=None,
                        manual_rate=None, http_get=None):
    """The document stamp. Resolution order:
    home currency (rate 1) -> user-typed manual rate -> stored table
    (refreshing once when the newest stored row predates `on_date`) ->
    last stored rate, honestly flagged stale. 422 when no rate exists
    anywhere — a foreign document is never stamped with an invented rate."""
    on_date = on_date or today_str()
    home = home.upper()
    txn = (txn_currency or home).upper()
    if txn == home:
        return {"txn_currency": txn, "fx_rate": "1", "fx_rate_date": on_date,
                "fx_source": SOURCE_HOME, "fx_stale": False}
    if manual_rate is not None:
        if Decimal(str(manual_rate)) <= 0:
            raise HTTPException(status_code=422, detail="FX rate must be positive")
        return {"txn_currency": txn, "fx_rate": rate_str(manual_rate),
                "fx_rate_date": on_date, "fx_source": SOURCE_MANUAL,
                "fx_stale": False}
    row = await get_stored_rate(db, tenant_id, home, txn, on_date)
    if row is None or row["date"] < on_date:
        await refresh_pair(db, tenant_id, home, txn, http_get=http_get)
        row = await get_stored_rate(db, tenant_id, home, txn, on_date)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"No FX rate available for {txn} in {home} — refresh rates or enter a manual rate")
    return {"txn_currency": txn, "fx_rate": row["rate"],
            "fx_rate_date": row["date"], "fx_source": row["source"],
            "fx_stale": row["date"] < on_date}


async def resolve_payment_rate(db, tenant_id, home, doc, pay_date=None,
                               manual_rate=None, http_get=None):
    """Rate for the payment day, same rules as the document stamp. For a
    home-currency document this is always the rate-1 home stamp."""
    return await resolve_stamp(db, tenant_id, home, doc.get("txn_currency"),
                               on_date=pay_date, manual_rate=manual_rate,
                               http_get=http_get)
