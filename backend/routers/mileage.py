"""Automatic mileage log: IRS-ready trip tracking with the correct standard
mileage rate applied per trip date (2026: $0.725 Jan-Jun, $0.76 Jul-Dec).

Trips can be entered directly (miles) or via odometer readings. The summary
endpoint computes the deductible amount; the CSV export is the contemporaneous
log the IRS requires.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from db import db
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, today_str

router = APIRouter(prefix="/mileage", tags=["mileage"])

# IRS standard mileage rates (business), per period. Overridable per workspace.
DEFAULT_RATES = [
    {"from": "2026-01-01", "to": "2026-06-30", "rate": 0.725},
    {"from": "2026-07-01", "to": "2026-12-31", "rate": 0.76},
    {"from": "2025-01-01", "to": "2025-12-31", "rate": 0.70},
]


def vehicles(user):
    return Scoped(db.mileage_vehicles, user["tenant_id"])


def trips(user):
    return Scoped(db.mileage_trips, user["tenant_id"])


async def _rates(user) -> list:
    t = await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}
    return t.get("mileage_rates") or DEFAULT_RATES


def _rate_for(rates: list, iso_date: str) -> float:
    d = (iso_date or "")[:10]
    for r in rates:
        if r["from"] <= d <= r["to"]:
            return float(r["rate"])
    return float(rates[-1]["rate"]) if rates else 0.76


# ---------------- Vehicles ----------------
class VehicleBody(BaseModel):
    name: str
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    odometer: Optional[float] = None


@router.get("/vehicles")
async def list_vehicles(user: dict = Depends(require("ledger:read"))):
    return await vehicles(user).find_list(sort=[("name", 1)], limit=100)


@router.post("/vehicles")
async def add_vehicle(body: VehicleBody, user: dict = Depends(require("ops:manage"))):
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await vehicles(user).insert_one(doc)
    await audit(user, "mileage.vehicle.add", "vehicle", doc["id"], {"name": body.name})
    doc.pop("_id", None)
    return doc


@router.delete("/vehicles/{vid}")
async def delete_vehicle(vid: str, user: dict = Depends(require("ops:manage"))):
    if not await vehicles(user).delete({"id": vid}):
        raise HTTPException(404, "Vehicle not found")
    return {"ok": True}


# ---------------- Trips ----------------
class TripBody(BaseModel):
    vehicle_id: Optional[str] = None
    trip_date: Optional[str] = None
    miles: Optional[float] = None
    odometer_start: Optional[float] = None
    odometer_end: Optional[float] = None
    origin: str = ""
    destination: str = ""
    purpose: str = ""          # client visit | supply run | job site | ...
    job_id: Optional[str] = None
    notes: str = ""


@router.get("/trips")
async def list_trips(start: str = "", end: str = "",
                     user: dict = Depends(require("ledger:read"))):
    q = {}
    if start or end:
        q["trip_date"] = {**({"$gte": start} if start else {}),
                          **({"$lte": end} if end else {})}
    docs = await trips(user).find_list(q, sort=[("trip_date", -1)], limit=2000)
    rates = await _rates(user)
    for d in docs:
        d["rate"] = _rate_for(rates, d["trip_date"])
        d["deductible"] = round(d["miles"] * d["rate"], 2)
    return docs


@router.post("/trips")
async def log_trip(body: TripBody, user: dict = Depends(require("ops:manage"))):
    miles = body.miles
    if miles is None and body.odometer_start is not None and body.odometer_end is not None:
        miles = round(body.odometer_end - body.odometer_start, 1)
    if not miles or miles <= 0:
        raise HTTPException(422, "Enter miles or valid odometer readings")
    if body.vehicle_id and not await vehicles(user).find_one({"id": body.vehicle_id}):
        raise HTTPException(404, "Vehicle not found")
    tdate = (body.trip_date or today_str())[:10]
    rates = await _rates(user)
    rate = _rate_for(rates, tdate)
    doc = {"id": new_id(), "vehicle_id": body.vehicle_id, "trip_date": tdate,
           "miles": miles, "odometer_start": body.odometer_start,
           "odometer_end": body.odometer_end, "origin": body.origin.strip(),
           "destination": body.destination.strip(), "purpose": body.purpose.strip(),
           "job_id": body.job_id, "notes": body.notes.strip(),
           "rate": rate, "deductible": round(miles * rate, 2),
           "created_at": now_iso()}
    await trips(user).insert_one(doc)
    await audit(user, "mileage.trip.log", "trip", doc["id"],
                {"miles": miles, "date": tdate})
    doc.pop("_id", None)
    return doc


@router.delete("/trips/{tid}")
async def delete_trip(tid: str, user: dict = Depends(require("ops:manage"))):
    if not await trips(user).delete({"id": tid}):
        raise HTTPException(404, "Trip not found")
    return {"ok": True}


# ---------------- Summary & export ----------------
@router.get("/summary")
async def mileage_summary(year: str = Query("2026"),
                          user: dict = Depends(require("ledger:read"))):
    docs = await trips(user).find_list({"trip_date": {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}},
                                       limit=5000)
    rates = await _rates(user)
    total_miles = sum(d["miles"] for d in docs)
    total_ded = sum(round(d["miles"] * _rate_for(rates, d["trip_date"]), 2) for d in docs)
    by_month = {}
    for d in docs:
        m = d["trip_date"][:7]
        b = by_month.setdefault(m, {"miles": 0, "deductible": 0})
        b["miles"] += d["miles"]
        b["deductible"] = round(b["deductible"] + d["miles"] * _rate_for(rates, d["trip_date"]), 2)
    return {"year": year, "trip_count": len(docs),
            "total_miles": round(total_miles, 1),
            "total_deductible": round(total_ded, 2),
            "by_month": [{"month": m, **v} for m, v in sorted(by_month.items())],
            "rates": rates}


@router.get("/export.csv", response_class=PlainTextResponse)
async def export_csv(year: str = Query("2026"),
                     user: dict = Depends(require("ledger:read"))):
    docs = await trips(user).find_list({"trip_date": {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}},
                                       sort=[("trip_date", 1)], limit=5000)
    rates = await _rates(user)
    veh = {v["id"]: v["name"] for v in await vehicles(user).find_list(limit=100)}
    lines = ["Date,Vehicle,Origin,Destination,Purpose,Miles,Rate,Deductible,Notes"]
    for d in docs:
        rate = _rate_for(rates, d["trip_date"])
        q = lambda s: '"' + str(s or "").replace('"', '""') + '"'
        lines.append(",".join([d["trip_date"], q(veh.get(d.get("vehicle_id"), "")),
                               q(d.get("origin")), q(d.get("destination")),
                               q(d.get("purpose")), str(d["miles"]), str(rate),
                               str(round(d["miles"] * rate, 2)), q(d.get("notes"))]))
    return "\n".join(lines)


@router.get("/rates")
async def get_rates(user: dict = Depends(require("ledger:read"))):
    return await _rates(user)


class RatesBody(BaseModel):
    rates: list  # [{from, to, rate}]


@router.put("/rates")
async def set_rates(body: RatesBody, user: dict = Depends(require("ops:manage"))):
    for r in body.rates:
        if not (r.get("from") and r.get("to") and float(r.get("rate", 0)) > 0):
            raise HTTPException(422, "Each rate needs from/to dates and a positive rate")
    await db.tenants.update_one({"id": user["tenant_id"]},
                                {"$set": {"mileage_rates": body.rates}})
    return {"ok": True, "rates": body.rates}
