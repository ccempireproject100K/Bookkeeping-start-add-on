"""Inventory reports, alerts (badge + daily digest email)."""

import uuid
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from lib.auth import ROLE_PERMS, Principal, require
from lib.dates import business_date_filter, now_iso, today_iso
from lib.db import db
from lib.mailer import send_email
from lib.repo import Scoped
from lib.stock import lot_status, stock_status
from models.inventory import (Alerts, BarberPerf, ExpiringLot, InventoryReport, OutboxEmail, ProductPerf, StockRow,
                              UsageRow)
from routers.movements import _range_filter

router = APIRouter()


def _stock_row(d: dict) -> StockRow:
    return StockRow(product_id=d["id"], name=d["name"], type=d["type"], quantity_on_hand=d["quantity_on_hand"],
                    unit_cost=d["unit_cost"], value=round(max(d["quantity_on_hand"], 0) * d["unit_cost"], 2),
                    reorder_point=d["reorder_point"], status=stock_status(d["quantity_on_hand"], d["reorder_point"]))


async def expiring_lots(p: Principal) -> list[ExpiringLot]:
    warn, today = p.settings["expiry_warning_days"], today_iso(p.settings.get("timezone"))
    names = {d["id"]: d["name"] for d in await Scoped("products", p).find({"active": True})}
    out = []
    for lot in await Scoped("lots", p).find({"quantity_on_hand": {"$gt": 0}, "expiry_date": {"$ne": None}}):
        if lot["product_id"] in names and lot_status(lot, warn, today) in ("expiring", "expired"):
            out.append(ExpiringLot(lot_id=lot["id"], product_id=lot["product_id"], product_name=names[lot["product_id"]],
                                   lot_number=lot["lot_number"], expiry_date=lot["expiry_date"],
                                   quantity_on_hand=lot["quantity_on_hand"],
                                   days_left=(date.fromisoformat(lot["expiry_date"]) - date.fromisoformat(today)).days))
    return sorted(out, key=lambda l: l.days_left)


async def compute_alerts(p: Principal) -> Alerts:
    products = await Scoped("products", p).find({"active": True})
    st = [stock_status(d["quantity_on_hand"], d["reorder_point"]) for d in products]
    lots = await expiring_lots(p)
    low, out = st.count("low"), st.count("out")
    expired = sum(1 for l in lots if l.days_left < 0)
    pending = 0
    if "po:approve" in ROLE_PERMS.get(p.role, set()):
        from routers.purchase_orders import needs_approval
        pending = sum(1 for d in await Scoped("purchase_orders", p).find({"status": "draft"}) if needs_approval(p, d))
    return Alerts(low_stock_count=low, out_of_stock_count=out, expiring_count=len(lots) - expired, expired_count=expired,
                  pending_approvals=pending, badge=low + out + len(lots))


@router.get("/alerts", response_model=Alerts)
async def alerts(p: Principal = Depends(require("product:read"))):
    return await compute_alerts(p)


@router.get("/reports", response_model=InventoryReport)
async def report(start: str = "", end: str = "", p: Principal = Depends(require("report:read"))):
    today = today_iso(p.settings.get("timezone"))
    start = start or today[:8] + "01"
    end = end or today
    products = await Scoped("products", p).find({"active": True}, sort=[("name", 1)])
    rows = [_stock_row(d) for d in products]
    mvs = await Scoped("movements", p).find({"type": {"$in": ["sale", "return", "use"]}, **business_date_filter(start, end)})
    users = {u["id"]: u for u in await db.users.find({"tenant_id": p.tenant_id}, {"_id": 0}).to_list(1000)}

    perf: dict[str, dict] = defaultdict(lambda: {"units": 0, "revenue": 0.0, "cogs": 0.0, "name": ""})
    barber: dict[str | None, dict] = defaultdict(lambda: {"units": 0, "revenue": 0.0, "cogs": 0.0})
    usage: dict[str, dict] = defaultdict(lambda: {"units": 0, "cost": 0.0, "name": ""})
    for m in mvs:
        if m["type"] == "use":
            u = usage[m["product_id"]]
            u["units"] += -m["quantity"]
            u["cost"] += m["cogs"]
            u["name"] = m["product_name"]
            continue
        units = -m["quantity"]  # sale: positive units, return: negative units
        revenue = units * (m.get("unit_price") or 0)
        for bucket in (perf[m["product_id"]], barber[m.get("barber_id")]):
            bucket["units"] += units
            bucket["revenue"] += revenue
            bucket["cogs"] += m["cogs"]
        perf[m["product_id"]]["name"] = m["product_name"]

    def pp(pid: str, v: dict) -> ProductPerf:
        profit = v["revenue"] - v["cogs"]
        return ProductPerf(product_id=pid, name=v["name"], units=v["units"], revenue=round(v["revenue"], 2),
                           cogs=round(v["cogs"], 2), profit=round(profit, 2),
                           margin=round(profit / v["revenue"] * 100, 1) if v["revenue"] else 0)

    perfs = [pp(k, v) for k, v in perf.items()]
    barbers = []
    for bid, v in barber.items():
        rate = float(users.get(bid or "", {}).get("commission_rate", 0) or 0)
        barbers.append(BarberPerf(barber_id=bid, name=users[bid]["name"] if bid in users else "Unassigned", units=v["units"],
                                  revenue=round(v["revenue"], 2), cogs=round(v["cogs"], 2),
                                  profit=round(v["revenue"] - v["cogs"], 2), commission_rate=rate,
                                  commission=round(v["revenue"] * rate / 100, 2)))
    revenue = sum(x.revenue for x in perfs)
    cogs = sum(x.cogs for x in perfs)
    return InventoryReport(
        start=start, end=end, stock=rows,
        total_value=round(sum(r.value for r in rows), 2),
        retail_value=round(sum(max(d["quantity_on_hand"], 0) * d["sell_price"] for d in products if d["type"] == "retail"), 2),
        low_stock=[r for r in rows if r.status != "in_stock"],
        expiring_lots=await expiring_lots(p),
        best_sellers=sorted(perfs, key=lambda x: x.units, reverse=True)[:10],
        profit_by_product=sorted(perfs, key=lambda x: x.profit, reverse=True),
        profit_by_barber=sorted(barbers, key=lambda x: x.revenue, reverse=True),
        supply_usage=sorted([UsageRow(product_id=k, name=v["name"], units=v["units"], cost=round(v["cost"], 2))
                             for k, v in usage.items()], key=lambda x: x.cost, reverse=True),
        totals={"retail_revenue": round(revenue, 2), "retail_cogs": round(cogs, 2), "retail_profit": round(revenue - cogs, 2),
                "supply_usage_cost": round(sum(v["cost"] for v in usage.values()), 2)},
    )


# ---------- Daily digest ----------
async def build_digest(p: Principal) -> tuple[str, str] | None:
    a = await compute_alerts(p)
    if a.badge == 0 and not a.pending_approvals:
        return None
    products = await Scoped("products", p).find({"active": True}, sort=[("name", 1)])
    low = [d for d in products if stock_status(d["quantity_on_hand"], d["reorder_point"]) != "in_stock"]
    lots = await expiring_lots(p)
    lines = [f"Inventory alerts for {p.tenant_name} — {today_iso(p.settings.get('timezone'))}", ""]
    if low:
        lines.append(f"LOW / OUT OF STOCK ({len(low)})")
        lines += [f"  • {d['name']}: {d['quantity_on_hand']} on hand (reorder at {d['reorder_point']})" for d in low]
        lines.append("")
    if lots:
        lines.append(f"EXPIRING / EXPIRED LOTS ({len(lots)})")
        lines += [f"  • {l.product_name} lot {l.lot_number}: {l.quantity_on_hand} units, "
                  + (f"expired {-l.days_left} day(s) ago" if l.days_left < 0 else f"expires in {l.days_left} day(s) ({l.expiry_date})")
                  for l in lots]
    if a.pending_approvals:
        lines += ["", f"PURCHASE ORDERS WAITING FOR YOUR APPROVAL ({a.pending_approvals}) — open Procurement to approve or reject"]
    subject = f"[{p.tenant_name}] {a.low_stock_count + a.out_of_stock_count} low stock, {len(lots)} expiring lots"
    return subject, "\n".join(lines)


async def send_digest(p: Principal, force: bool = False) -> OutboxEmail | None:
    built = await build_digest(p)
    if not built:
        return None
    owners = await db.users.find({"tenant_id": p.tenant_id, "role": "admin"}, {"_id": 0, "email": 1}).sort("created_at", 1).to_list(1)
    to = owners[0]["email"] if owners else p.email
    status, err = await send_email(to, built[0], built[1])
    doc = {"id": str(uuid.uuid4()), "to": to, "subject": built[0], "body": built[1], "status": status, "error": err, "created_at": now_iso()}
    await Scoped("email_outbox", p).insert(dict(doc))
    await db.tenants.update_one({"id": p.tenant_id}, {"$set": {"last_digest_date": today_iso(p.settings.get("timezone"))}})
    return OutboxEmail(**doc)


@router.post("/alerts/digest", response_model=OutboxEmail)
async def trigger_digest(p: Principal = Depends(require("settings:write"))):
    res = await send_digest(p, force=True)
    if not res:
        raise HTTPException(400, "Nothing to report — no low-stock or expiring items")
    return res


@router.get("/alerts/outbox", response_model=list[OutboxEmail])
async def outbox(p: Principal = Depends(require("settings:write"))):
    return [OutboxEmail(**d) for d in await Scoped("email_outbox", p).find(sort=[("created_at", -1)], limit=20)]
