"""Phase 1 multi-currency backfill: stamp currency="USD" on documents that
predate the currency field, and set settings.base_currency="USD" on tenants
that lack it.

Idempotent and safe to re-run: every update only touches documents where
the field does not exist yet. Run automatically on server startup; also
importable for manual runs.
"""
import logging

log = logging.getLogger(__name__)

# (collection, filter) pairs for money documents that now carry a currency.
_MONEY_COLLECTIONS = (
    "journal_entries",
    "invoices",
    "bills",
    "bank_accounts",
)


async def backfill_currency_fields(db) -> dict:
    """Returns {collection_or_key: matched_count} for observability."""
    report = {}
    for name in _MONEY_COLLECTIONS:
        coll = getattr(db, name)
        res = await coll.update_many(
            {"currency": {"$exists": False}},
            {"$set": {"currency": "USD"}},
        )
        matched = getattr(res, "matched_count", 0)
        report[name] = matched
        if matched:
            log.info("currency backfill: %s stamped %d doc(s)", name, matched)
    res = await db.tenants.update_many(
        {"settings.base_currency": {"$exists": False}},
        {"$set": {"settings.base_currency": "USD"}},
    )
    report["tenants.base_currency"] = getattr(res, "matched_count", 0)
    return report
