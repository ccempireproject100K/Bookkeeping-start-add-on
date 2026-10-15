"""Clients: profiles, purchase history, rule-based sales suggestions; discount codes (game rewards) applied at checkout."""

import uuid
from collections import Counter, defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lib.auth import Principal, require
from lib.dates import now_iso, today_iso
from lib.db import db
from lib.money import discounted_unit_price
from lib.repo import Scoped

router = APIRouter()


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)


class Client(ClientIn):
    id: str
    total_spent: float = 0
    visits: int = 0
    last_visit: str | None = None
    created_at: str


class ClientPurchase(BaseModel):
    movement_id: str
    product_id: str
    product_name: str
    quantity: int
    unit_price: float
    date: str
    barber_name: str | None


class Suggestion(BaseModel):
    product_id: str
    name: str
    sell_price: float
    in_stock: bool
    kind: str  # rebuy | pairs | popular
    reason: str


class ClientProfile(BaseModel):
    client: Client
    purchases: list[ClientPurchase]
    favorites: list[str]
    suggestions: list[Suggestion]


async def _stats(p: Principal) -> dict[str, dict]:
    rows = await Scoped("movements", p).aggregate([
        {"$match": {"type": "sale", "client_id": {"$ne": None}}},
        {"$group": {"_id": "$client_id", "spent": {"$sum": {"$multiply": [{"$multiply": ["$quantity", -1]}, {"$ifNull": ["$unit_price", 0]}]}},
                    "days": {"$addToSet": {"$substr": ["$created_at", 0, 10]}}, "last": {"$max": "$created_at"}}},
    ])
    return {r["_id"]: {"total_spent": round(r["spent"], 2), "visits": len(r["days"]), "last_visit": r["last"][:10]} for r in rows}


@router.get("/clients", response_model=list[Client])
async def list_clients(q: str = "", p: Principal = Depends(require("client:read"))):
    stats = await _stats(p)
    docs = await Scoped("clients", p).find({"name": {"$regex": q, "$options": "i"}} if q else {}, sort=[("name", 1)])
    return [Client(**{k: v for k, v in d.items() if k != "tenant_id"}, **stats.get(d["id"], {})) for d in docs]


@router.post("/clients", response_model=Client)
async def create_client(body: ClientIn, p: Principal = Depends(require("stock:write"))):
    doc = {"id": str(uuid.uuid4()), **body.model_dump(), "created_at": now_iso()}
    await Scoped("clients", p).insert(dict(doc))
    return Client(**doc)


@router.put("/clients/{id}", response_model=Client)
async def update_client(id: str, body: ClientIn, p: Principal = Depends(require("stock:write"))):
    d = await Scoped("clients", p).update({"id": id}, {"$set": body.model_dump()})
    if not d:
        raise HTTPException(404, "Client not found")
    return Client(**{k: v for k, v in d.items() if k != "tenant_id"}, **(await _stats(p)).get(id, {}))


@router.get("/clients/{id}", response_model=ClientProfile)
async def profile(id: str, p: Principal = Depends(require("client:read"))):
    c = await Scoped("clients", p).find_one({"id": id})
    if not c:
        raise HTTPException(404, "Client not found")
    sales = await Scoped("movements", p).find({"type": "sale", "client_id": {"$ne": None}}, sort=[("created_at", -1)])
    mine = [m for m in sales if m["client_id"] == id]
    products = {d["id"]: d for d in await Scoped("products", p).find({"active": True, "type": "retail"})}
    today = date.fromisoformat(today_iso(p.settings.get("timezone")))
    sugg: list[Suggestion] = []
    seen: set[str] = set()

    def add(pid: str, kind: str, reason: str):
        d = products.get(pid)
        if d and pid not in seen and len(sugg) < 6:
            seen.add(pid)
            sugg.append(Suggestion(product_id=pid, name=d["name"], sell_price=d["sell_price"], in_stock=d["quantity_on_hand"] > 0, kind=kind, reason=reason))

    # 1) Due for a re-buy: their own rhythm (avg gap between buys), else ~30 days for consumables.
    by_prod: dict[str, list[str]] = defaultdict(list)
    for m in mine:
        by_prod[m["product_id"]].append(m["created_at"][:10])
    for pid, ds in sorted(by_prod.items(), key=lambda kv: kv[1][0]):
        ds = sorted(ds)
        gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:]) if b != a]
        cycle = max(14, round(sum(gaps) / len(gaps))) if gaps else 30
        since = (today - date.fromisoformat(ds[-1])).days
        if since >= cycle * 0.8:
            add(pid, "rebuy", f"Last bought {since} days ago — they usually rebuy every ~{cycle} days")
    # 2) Bought together: what other clients who share a product also bought.
    bought = set(by_prod)
    others: dict[str, set[str]] = defaultdict(set)
    for m in sales:
        if m["client_id"] != id:
            others[m["client_id"]].add(m["product_id"])
    pairs: Counter = Counter()
    for basket in others.values():
        if basket & bought:
            pairs.update(basket - bought)
    for pid, n in pairs.most_common(4):
        add(pid, "pairs", f"{n} client(s) who buy what they buy also picked this up")
    # 3) Shop best sellers they haven't tried.
    for pid, _ in Counter(m["product_id"] for m in sales).most_common(10):
        if pid not in bought:
            add(pid, "popular", "One of the shop's best sellers — they haven't tried it yet")
    fav = [products[pid]["name"] if pid in products else mine[0]["product_name"] for pid, _ in Counter(m["product_id"] for m in mine).most_common(3)]
    stats = (await _stats(p)).get(id, {})
    return ClientProfile(
        client=Client(**{k: v for k, v in c.items() if k != "tenant_id"}, **stats),
        purchases=[ClientPurchase(movement_id=m["id"], product_id=m["product_id"], product_name=m["product_name"], quantity=-m["quantity"],
                                  unit_price=m.get("unit_price") or 0, date=m["created_at"][:10], barber_name=m.get("barber_name")) for m in mine[:100]],
        favorites=fav, suggestions=sugg)


# ---------- discount codes (issued by the waiting-room game) ----------
class DiscountCheck(BaseModel):
    code: str
    pct: float
    valid: bool
    reason: str = ""


@router.get("/discounts/{code}", response_model=DiscountCheck)
async def check_discount(code: str, p: Principal = Depends(require("stock:write"))):
    d = await Scoped("discount_codes", p).find_one({"code": code.strip().upper()})
    if not d:
        return DiscountCheck(code=code, pct=0, valid=False, reason="Unknown code")
    if d.get("used_at"):
        return DiscountCheck(code=code, pct=d["pct"], valid=False, reason="Already used")
    if d["expires"] < today_iso(p.settings.get("timezone")):
        return DiscountCheck(code=code, pct=d["pct"], valid=False, reason="Expired")
    return DiscountCheck(code=d["code"], pct=d["pct"], valid=True)


async def apply_discount(p: Principal, body) -> float:
    """Validate + atomically consume the code, then scale line prices. Returns pct (0 if none)."""
    if not body.discount_code:
        return 0.0
    code = body.discount_code.strip().upper()
    d = await Scoped("discount_codes", p).update({"code": code, "used_at": None, "expires": {"$gte": today_iso(p.settings.get("timezone"))}}, {"$set": {"used_at": now_iso()}})
    if not d:
        raise HTTPException(400, "Discount code is invalid, expired or already used")
    for ln in body.lines:
        ln.unit_price = discounted_unit_price(ln.unit_price, d["pct"])
    return float(d["pct"])


async def release_discount(p: Principal, code: str) -> None:
    await db.discount_codes.update_one({"tenant_id": p.tenant_id, "code": code}, {"$set": {"used_at": None}})


async def tag_sale(p: Principal, body, movement_ids: list[str], txn_id: str | None, pct: float) -> None:
    patch: dict = {"payment_method": body.payment_method}
    if body.client_id:
        c = await Scoped("clients", p).find_one({"id": body.client_id})
        if c:
            patch.update(client_id=c["id"], client_name=c["name"])
    await db.movements.update_many({"tenant_id": p.tenant_id, "id": {"$in": movement_ids}}, {"$set": patch})
    if txn_id:
        extra = {**patch, **({"discount_code": body.discount_code.upper(), "discount_pct": pct} if pct else {})}
        await db.transactions.update_one({"tenant_id": p.tenant_id, "id": txn_id}, {"$set": extra})
    if pct and body.discount_code:
        await db.discount_codes.update_one({"tenant_id": p.tenant_id, "code": body.discount_code.strip().upper()},
                                           {"$set": {"movement_ids": movement_ids}})
