"""Exchange-rate service (multi-currency Phase 2).

Stores daily FX rates in the ``rates`` collection and converts amounts
between the Phase 1 currencies. Rates come from the ECB euro-reference
feed (no API key required).

Clean stopping point: rates are stored and queryable, but nothing
consumes them yet — forex postings (Phase 3) and report translation
(Phase 4) come later. No posting or reporting behavior changes here.

Rate convention: a stored rate answers "1 unit of base = rate units of
quote", e.g. {base: "EUR", quote: "USD", rate: 1.08}. The ECB publishes
EUR-based rates; cross rates (USD->GBP, ...) are derived through the
EUR pivot at query time.
"""
import asyncio
import logging

from lib import currencies

logger = logging.getLogger(__name__)

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_BASE = "EUR"
SOURCE_ECB = "ecb"
FETCH_TIMEOUT = 20
SEED_HISTORY_DAYS = 30


class FXRateError(ValueError):
    """Raised when no usable rate exists for a conversion."""


def _db(db=None):
    """Live db, lazily imported so unit tests stay env-free."""
    if db is not None:
        return db
    from db import db as live_db
    return live_db


async def get_rate(base, quote, date, db=None, _pivot=True):
    """Rate to convert 1 unit of ``base`` into ``quote`` on ``date``.

    ``date`` is "YYYY-MM-DD". Returns the latest rate on or before that
    date. Returns 1.0 when base == quote (no DB hit). Cross rates are
    derived via the EUR pivot. Raises FXRateError when nothing is found.
    """
    base = currencies.normalize(base)
    quote = currencies.normalize(quote)
    if base == quote:
        return 1.0
    coll = _db(db).rates
    doc = await coll.find_one(
        {"base": base, "quote": quote, "date": {"$lte": date}},
        sort=[("date", -1)],
    )
    if doc:
        return float(doc["rate"])
    if _pivot:
        # Derive through the EUR pivot: base->quote
        #   = (EUR->quote) / (EUR->base). The recursive calls never pivot
        #   again, so this always terminates.
        try:
            eur_to_base = await get_rate(ECB_BASE, base, date, db=db,
                                         _pivot=False)
            eur_to_quote = await get_rate(ECB_BASE, quote, date, db=db,
                                          _pivot=False)
        except FXRateError:
            raise FXRateError(f"No {base}->{quote} rate on or before {date}.")
        return eur_to_quote / eur_to_base
    raise FXRateError(f"No {base}->{quote} rate on or before {date}.")


async def convert(amount, base, quote, date, db=None):
    """Convert ``amount`` from ``base`` to ``quote`` at ``date``'s rate.

    Rounds to the quote currency's minor-unit decimals (e.g. whole yen).
    """
    rate = await get_rate(base, quote, date, db=db)
    places = currencies.decimals_for(quote)
    return round(float(amount) * rate, places)


def parse_ecb_xml(xml_text):
    """Parse ECB eurofxref XML -> [(date_str, {currency: rate})]. Pure; no I/O."""
    import xml.etree.ElementTree as ET

    out = []
    root = ET.fromstring(xml_text)
    for cube in root.iter():
        if not cube.tag.endswith("Cube") or "time" not in cube.attrib:
            continue
        rates = {}
        for child in cube:
            if child.tag.endswith("Cube") and "currency" in child.attrib:
                try:
                    rates[child.attrib["currency"].upper()] = float(
                        child.attrib["rate"])
                except (ValueError, KeyError):
                    continue
        if rates:
            out.append((cube.attrib["time"], rates))
    return out


def _fetch_url(url):
    """Blocking HTTP GET; callers run it in a thread."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "mindatease-books/fx"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


async def store_rates(date, eur_rates, db=None, source=SOURCE_ECB):
    """Upsert one day of EUR-based rates for Phase-1 currencies.

    Idempotent: re-storing the same day is a no-op thanks to the unique
    (base, quote, date) index. Returns the number of pairs stored.
    """
    coll = _db(db).rates
    stored = 0
    for code in currencies.CURRENCIES:
        if code == ECB_BASE or code not in eur_rates:
            continue
        await coll.update_one(
            {"base": ECB_BASE, "quote": code, "date": date},
            {"$set": {"rate": float(eur_rates[code]), "source": source}},
            upsert=True,
        )
        stored += 1
    return stored


async def fetch_daily_rates(db=None):
    """Fetch today's ECB rates and store them. Never raises on failure."""
    try:
        xml_text = await asyncio.to_thread(_fetch_url, ECB_DAILY_URL)
    except Exception as exc:
        logger.warning("FX: ECB daily feed unreachable: %s", exc)
        return {"fetched": 0, "dates": [], "error": str(exc)[:200]}
    stored, dates = 0, []
    for date, eur_rates in parse_ecb_xml(xml_text):
        stored += await store_rates(date, eur_rates, db=db)
        dates.append(date)
    logger.info("FX: stored %d rate pair(s) for %d day(s)", stored, len(dates))
    return {"fetched": stored, "dates": dates}


async def backfill_history(days=SEED_HISTORY_DAYS, db=None):
    """Seed path: store the last ``days`` of history from the ECB 90-day feed."""
    from datetime import date as _date, timedelta

    try:
        xml_text = await asyncio.to_thread(_fetch_url, ECB_HIST_URL)
    except Exception as exc:
        logger.warning("FX: ECB history feed unreachable: %s", exc)
        return {"fetched": 0, "dates": [], "error": str(exc)[:200]}
    cutoff = (_date.today() - timedelta(days=days)).isoformat()
    stored, dates = 0, []
    for date, eur_rates in parse_ecb_xml(xml_text):
        if date < cutoff:
            continue
        stored += await store_rates(date, eur_rates, db=db)
        dates.append(date)
    logger.info("FX: backfilled %d rate pair(s) for %d day(s)", stored, len(dates))
    return {"fetched": stored, "dates": dates}


async def ensure_seeded(db=None):
    """Backfill 30d of history on first run (empty rates collection)."""
    coll = _db(db).rates
    if await coll.count_documents({}) == 0:
        logger.info("FX: rates collection empty; backfilling 30d history.")
        report = await backfill_history(SEED_HISTORY_DAYS, db=db)
        report["seeded"] = True
        return report
    return {"fetched": 0, "dates": [], "seeded": False}


async def refresh_rates(db=None):
    """Scheduler entrypoint: seed on first run, then fetch today's rates."""
    seed = await ensure_seeded(db=db)
    daily = await fetch_daily_rates(db=db)
    return {"seed": seed, "daily": daily}
