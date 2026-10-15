"""Shared primitives: ids, time, Decimal-safe money (stored as integer cents), tenant scoping."""
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---- Money: everything internal is integer cents; never float arithmetic ----
def to_cents(value) -> int:
    """Parse a dollar amount (str/int/float/Decimal) into integer cents, ROUND_HALF_UP."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value * 100
    d = Decimal(str(value))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_dollars(cents: int) -> float:
    return round((cents or 0) / 100.0, 2)


def period_key_of(entry_date: str) -> str:
    """'2026-02' from '2026-02-14'."""
    return entry_date[:7]


def fiscal_year_of(entry_date: str) -> str:
    return entry_date[:4]


class Scoped:
    """Wraps a motor collection so every read/write is filtered by tenant_id (workspace isolation)."""

    def __init__(self, collection, tenant_id: str):
        self.c = collection
        self.tenant_id = tenant_id

    def _q(self, q):
        q = dict(q or {})
        q["tenant_id"] = self.tenant_id
        return q

    async def find_one(self, q=None, projection=None):
        return await self.c.find_one(self._q(q), projection or {"_id": 0})

    def find(self, q=None, projection=None):
        return self.c.find(self._q(q), projection or {"_id": 0})

    async def find_list(self, q=None, sort=None, limit=0, projection=None):
        cur = self.c.find(self._q(q), projection or {"_id": 0})
        if sort:
            cur = cur.sort(sort)
        if limit:
            cur = cur.limit(limit)
        return await cur.to_list(length=limit or 5000)

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        doc["tenant_id"] = self.tenant_id
        await self.c.insert_one(doc)
        doc.pop("_id", None)
        return doc

    async def insert_many(self, docs):
        out = []
        for d in docs:
            d = dict(d)
            d["tenant_id"] = self.tenant_id
            out.append(d)
        if out:
            await self.c.insert_many(out)
        return out

    async def update_one(self, q, update):
        return await self.c.update_one(self._q(q), update)

    async def count(self, q=None):
        return await self.c.count_documents(self._q(q))

    async def aggregate(self, pipeline):
        pipeline = [{"$match": {"tenant_id": self.tenant_id}}] + pipeline
        return await self.c.aggregate(pipeline).to_list(length=100000)

    async def next_seq(self, counters_coll, name: str) -> int:
        doc = await counters_coll.find_one_and_update(
            {"tenant_id": self.tenant_id, "name": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
        return doc["seq"]
