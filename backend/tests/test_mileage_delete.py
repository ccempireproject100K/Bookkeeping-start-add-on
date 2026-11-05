"""Mileage delete regression tests.

DELETE /mileage/vehicles/{vid} and DELETE /mileage/trips/{tid} called
``Scoped.delete(...)``, a method that does not exist on lib.base.Scoped,
so both endpoints raised AttributeError and 500'd on main. Same bug
class as receipts (fixed in PR #205) and combo leads (PR #206); these
tests mirror the combo regression tests in
tests/test_books_glue_endpoints.py: deletion works and audits,
cross-tenant deletes answer 404 and leave the document intact, and a
missing record answers 404. Add-then-delete round trips cover both
resources end to end.

Run from the backend directory with:
    python -m pytest tests/test_mileage_delete.py

No live Mongo: the db is faked in-memory following
tests/test_books_glue_endpoints.py conventions (tenant_id matched
literally, so tenant-scoping regressions cannot hide).
"""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_mileage_delete")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import lib.audit as AUD  # noqa: E402
from routers import mileage as MILEAGE  # noqa: E402


# ------------------------------------------------------------------ fakes

def _matches(doc, filt):
    for key, cond in (filt or {}).items():
        if isinstance(cond, dict):
            for op, val in cond.items():
                if op == "$exists":
                    if (key in doc) != bool(val):
                        return False
                elif op == "$gte":
                    if not (doc.get(key) is not None and doc.get(key) >= val):
                        return False
                elif op == "$lte":
                    if not (doc.get(key) is not None and doc.get(key) <= val):
                        return False
                elif op == "$in":
                    if doc.get(key) not in val:
                        return False
                else:
                    return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, spec):
        if isinstance(spec, (list, tuple)):
            for key, direction in reversed(list(spec)):
                self.docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                               reverse=(direction == -1))
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=0):
        return self.docs


class FakeCollection:
    """In-memory collection. tenant_id is matched LITERALLY (unlike
    some house fakes) so tenant-scoping regressions cannot hide."""

    def __init__(self, docs=()):
        self.docs = [dict(d) for d in docs]

    async def find_one(self, filt=None, projection=None):
        for d in self.docs:
            if _matches(d, filt):
                return dict(d)
        return None

    def find(self, filt=None, projection=None):
        return FakeCursor([dict(d) for d in self.docs
                           if _matches(d, filt)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def update_one(self, filt, update, upsert=False):
        for d in self.docs:
            if _matches(d, filt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                for k, v in (update.get("$inc") or {}).items():
                    d[k] = d.get(k, 0) + v
                return SimpleNamespace(matched_count=1, modified_count=1,
                                       upserted_id=None)
        if upsert:
            doc = dict(filt or {})
            for k, v in (update.get("$set") or {}).items():
                doc[k] = v
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=0,
                                   upserted_id=doc.get("id"))
        return SimpleNamespace(matched_count=0, modified_count=0,
                               upserted_id=None)

    async def delete_one(self, filt):
        for i, d in enumerate(self.docs):
            if _matches(d, filt):
                del self.docs[i]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def count_documents(self, filt=None):
        return sum(1 for d in self.docs if _matches(d, filt))


def _fake_db(monkeypatch, **collections):
    fake = SimpleNamespace(
        mileage_vehicles=FakeCollection(),
        mileage_trips=FakeCollection(),
        tenants=FakeCollection(),
        audit_log=FakeCollection(),
    )
    for name, docs in collections.items():
        setattr(fake, name, FakeCollection(docs))
    for module in (MILEAGE, AUD):
        monkeypatch.setattr(module, "db", fake)
    return fake


USER1 = {"id": "u1", "tenant_id": "t1", "role": "owner",
         "email": "owner@test", "name": "Owner"}


# ------------------------------------------------------------- vehicles

def test_mileage_delete_vehicle_works_and_audits(monkeypatch):
    asyncio.run(_vehicle_delete_ok(monkeypatch))


async def _vehicle_delete_ok(monkeypatch):
    fake = _fake_db(monkeypatch, mileage_vehicles=[
        {"id": "V1", "tenant_id": "t1", "name": "Work truck"}])
    out = await MILEAGE.delete_vehicle("V1", dict(USER1))
    assert out == {"ok": True}
    assert fake.mileage_vehicles.docs == []
    actions = [r["action"] for r in fake.audit_log.docs]
    assert "mileage.vehicle.delete" in actions


def test_mileage_delete_vehicle_stays_tenant_scoped(monkeypatch):
    asyncio.run(_vehicle_delete_scope(monkeypatch))


async def _vehicle_delete_scope(monkeypatch):
    fake = _fake_db(monkeypatch, mileage_vehicles=[
        {"id": "V2", "tenant_id": "t2", "name": "Other tenant's van"}])
    with pytest.raises(HTTPException) as ei:
        await MILEAGE.delete_vehicle("V2", dict(USER1))  # t1 user, t2 vehicle
    assert ei.value.status_code == 404
    assert len(fake.mileage_vehicles.docs) == 1  # untouched


def test_mileage_delete_missing_vehicle_is_404(monkeypatch):
    asyncio.run(_vehicle_delete_missing(monkeypatch))


async def _vehicle_delete_missing(monkeypatch):
    _fake_db(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await MILEAGE.delete_vehicle("nope", dict(USER1))
    assert ei.value.status_code == 404


def test_mileage_add_then_delete_vehicle_round_trip(monkeypatch):
    asyncio.run(_vehicle_round_trip(monkeypatch))


async def _vehicle_round_trip(monkeypatch):
    fake = _fake_db(monkeypatch)
    doc = await MILEAGE.add_vehicle(
        MILEAGE.VehicleBody(name="F-150"), dict(USER1))
    assert len(fake.mileage_vehicles.docs) == 1
    out = await MILEAGE.delete_vehicle(doc["id"], dict(USER1))
    assert out == {"ok": True}
    assert fake.mileage_vehicles.docs == []
    assert await MILEAGE.list_vehicles(user=dict(USER1)) == []


# ---------------------------------------------------------------- trips

def test_mileage_delete_trip_works_and_audits(monkeypatch):
    asyncio.run(_trip_delete_ok(monkeypatch))


async def _trip_delete_ok(monkeypatch):
    fake = _fake_db(monkeypatch, mileage_trips=[
        {"id": "T1", "tenant_id": "t1", "trip_date": "2026-03-01",
         "miles": 12.5}])
    out = await MILEAGE.delete_trip("T1", dict(USER1))
    assert out == {"ok": True}
    assert fake.mileage_trips.docs == []
    actions = [r["action"] for r in fake.audit_log.docs]
    assert "mileage.trip.delete" in actions


def test_mileage_delete_trip_stays_tenant_scoped(monkeypatch):
    asyncio.run(_trip_delete_scope(monkeypatch))


async def _trip_delete_scope(monkeypatch):
    fake = _fake_db(monkeypatch, mileage_trips=[
        {"id": "T2", "tenant_id": "t2", "trip_date": "2026-03-02",
         "miles": 40.0}])
    with pytest.raises(HTTPException) as ei:
        await MILEAGE.delete_trip("T2", dict(USER1))  # t1 user, t2 trip
    assert ei.value.status_code == 404
    assert len(fake.mileage_trips.docs) == 1  # untouched


def test_mileage_delete_missing_trip_is_404(monkeypatch):
    asyncio.run(_trip_delete_missing(monkeypatch))


async def _trip_delete_missing(monkeypatch):
    _fake_db(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await MILEAGE.delete_trip("nope", dict(USER1))
    assert ei.value.status_code == 404


def test_mileage_add_then_delete_trip_round_trip(monkeypatch):
    asyncio.run(_trip_round_trip(monkeypatch))


async def _trip_round_trip(monkeypatch):
    fake = _fake_db(monkeypatch)
    doc = await MILEAGE.log_trip(
        MILEAGE.TripBody(miles=25.0, origin="Shop", destination="Site"),
        dict(USER1))
    assert len(fake.mileage_trips.docs) == 1
    out = await MILEAGE.delete_trip(doc["id"], dict(USER1))
    assert out == {"ok": True}
    assert fake.mileage_trips.docs == []
    assert await MILEAGE.list_trips(user=dict(USER1)) == []
