"""Cleanup Console aggregate tests (2026-10-30).

The console's data-quality panel reads ONE new endpoint,
GET /dashboard/data-quality. These tests pin its honesty rules: counts
come only from the caller's tenant, normalized means "carries a marker
the dataclean stage writes", and the files-cleaned metric reports
itself unavailable (the importer persists no per-run history) instead
of inventing a number.
"""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return self._docs[:length] if length else self._docs


class FakeColl:
    def __init__(self):
        self.docs = {}

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
                    return False
            elif dv != v:
                return False
        return True

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", f"oid{len(self.docs) + 1}")
        self.docs[doc["_id"]] = doc
        return doc

    def find(self, q=None, proj=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


class FakeDB:
    def __init__(self):
        for name in ("customers", "vendors", "products",
                     "dedupe_suggestions", "entity_merges",
                     "audit_log", "import_profiles"):
            setattr(self, name, FakeColl())


USER1 = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
         "role": "owner", "permissions": ["*"]}
USER2 = {"id": "u2", "tenant_id": "t2", "email": "other@shop.example",
         "role": "owner", "permissions": ["*"]}


def _seed(fake):
    from lib.base import Scoped, today_str
    month = today_str()[:7]

    async def go():
        sugg = Scoped(fake.dedupe_suggestions, "t1")
        await sugg.insert_one({"id": "s1", "entity": "customers",
                               "status": "pending",
                               "created_at": f"{month}-02T10:00:00+00:00"})
        await sugg.insert_one({"id": "s2", "entity": "vendors",
                               "status": "pending",
                               "created_at": f"{month}-05T10:00:00+00:00"})
        await sugg.insert_one({"id": "s3", "entity": "vendors",
                               "status": "dismissed",
                               "created_at": f"{month}-01T10:00:00+00:00"})
        await Scoped(fake.dedupe_suggestions, "t2").insert_one(
            {"id": "sX", "entity": "customers", "status": "pending",
             "created_at": f"{month}-06T10:00:00+00:00"})

        cust = Scoped(fake.customers, "t1")
        await cust.insert_one({"id": "c1", "name": "Acme",
                               "name_key": "acme",
                               "imported_via": "guided_importer",
                               "created_at": f"{month}-03T09:00:00+00:00"})
        await cust.insert_one({"id": "c2", "name": "Beta",
                               "phone": "+15125550134"})
        await cust.insert_one({"id": "c3", "name": "Raw Co",
                               "phone": "512 555 0100"})
        await cust.insert_one({"id": "c4", "name": "Gone",
                               "name_key": "gone", "status": "merged"})
        vend = Scoped(fake.vendors, "t1")
        await vend.insert_one({"id": "v1", "name": "Vendor",
                               "address_parts": {"zip": "78701"},
                               "imported_via": "guided_importer",
                               "created_at": f"{month}-04T09:00:00+00:00"})
        prod = Scoped(fake.products, "t1")
        await prod.insert_one({"id": "p1", "name": "Widget",
                               "imported_via": "guided_importer",
                               "created_at": f"{month}-04T09:30:00+00:00"})
        # Another tenant's records must never leak into t1's numbers.
        await Scoped(fake.customers, "t2").insert_one(
            {"id": "cX", "name": "Other", "name_key": "other"})
        merges = Scoped(fake.entity_merges, "t1")
        await merges.insert_one({"id": "m1", "status": "active",
                                 "created_at": f"{month}-05T11:00:00+00:00"})
        await merges.insert_one({"id": "m2", "status": "reversed",
                                 "created_at": f"{month}-05T12:00:00+00:00"})
    run(go())


def test_data_quality_counts_are_tenant_scoped_and_honest():
    import routers.dashboard as DASH
    fake = FakeDB()
    DASH.db = fake
    _seed(fake)
    out = run(DASH.data_quality(USER1))
    assert out["duplicates_pending"] == 2
    assert out["duplicates_pending_by_entity"] == {"customers": 1, "vendors": 1}
    # c1 (name_key), c2 (E.164 phone), v1 (address_parts) normalized;
    # c3 raw, c4 merged (not live) — 3 of 4 live contact records.
    assert out["normalized_records"] == 3 and out["records_total"] == 4
    assert out["normalized_percent"] == 75.0
    assert out["records_imported_this_month"] == 3
    # The importer persists no per-run history: the file count must say
    # so, never a fabricated number.
    assert out["files_cleaned_this_month"] is None
    assert out["files_cleaned_available"] is False
    assert out["last_scan_at"].endswith("-05T10:00:00+00:00")
    assert out["merges_active"] == 1 and out["merges_reversed"] == 1
    out2 = run(DASH.data_quality(USER2))
    assert out2["duplicates_pending"] == 1
    assert out2["records_total"] == 1 and out2["normalized_records"] == 1


CSV_UGLY = ("Client,E-mail,Phone,Address\n"
            "Acme Supply Inc,ops@acme.example,(512) 555-0134,\"9 Oak Ave, Dallas, TX 75001\"\n")
BAD_EMAIL_CSV = "Client,E-mail\nA Co,x\nB Co,y\nC Co,z\nD Co,w\n"


def test_import_run_reports_changes_by_rule_and_audits_override_reason():
    import routers.importer as IMP
    from fastapi import HTTPException
    import pytest
    fake = FakeDB()
    IMP.db = fake
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_UGLY, email_summary=False), USER1))
    assert out["cleanup"]["changes"] >= 1
    by_rule = out["cleanup"]["changes_by_rule"]
    assert by_rule.get("phone:libphonenumber-e164") == 1
    assert sum(by_rule.values()) == out["cleanup"]["changes"]
    with pytest.raises(HTTPException):
        run(IMP.run_import(IMP.RunIn(
            kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False),
            USER1))
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False,
        override_schema=True,
        override_reason="Owner verified the source system by phone"), USER1))
    assert out["cleanup"]["schema_override"] is True
    audits = [d for d in fake.audit_log.docs.values()
              if d.get("action") == "import.schema_override"]
    assert len(audits) == 1
    assert audits[0]["details"]["reason"] == "Owner verified the source system by phone"
    assert audits[0]["tenant_id"] == "t1"
