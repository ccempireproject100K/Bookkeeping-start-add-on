"""Import cleanup layer tests (2026-10-30).

Covers: every dataclean normalizer on messy-real-world fixtures, column
detection + mess report, schema gating (blocking vs row-level, override
audit), importer cleanup integration, duplicate suggestions with a
LABELED fixture (precision/recall), the human-approved merge + reverse
flow, tenant isolation, unapproved pairs never merging, rules-v2 matching
on normalized bank descriptions, and a 5,000-row performance sanity check.
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from lib.dataclean import linkage, mess, schema  # noqa: E402
from lib.dataclean.addresses import parse_address  # noqa: E402
from lib.dataclean.cleaning import clean_row  # noqa: E402
from lib.dataclean.names import company_key, parse_name  # noqa: E402
from lib.dataclean.normalize import (normalize_date, normalize_phone,  # noqa: E402
                                     normalize_description)


def run(coro):
    # Fresh loop per call: sibling test modules in the same xdist worker
    # close/unset the shared loop, so never rely on get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------------ fakes
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

    async def to_list(self, n=None, length=None):
        return self._docs[:(n if n is not None else length)]


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0

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
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    def find(self, q=None, proj=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$addToSet") or {}).items():
                    cur = d.setdefault(k, [])
                    if v not in cur:
                        cur.append(v)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def delete_one(self, q):
        for k, d in list(self.docs.items()):
            if self._match(d, q):
                del self.docs[k]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


class FakeDB:
    def __init__(self):
        for name in ("customers", "vendors", "products", "import_profiles",
                     "dedupe_suggestions", "entity_merges", "audit_log",
                     "bank_transactions", "bank_rules", "bank_accounts",
                     "import_runs", "dedupe_scan_runs",
                     "dedupe_schedule_configs"):
            setattr(self, name, FakeColl())


USER1 = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
         "role": "owner", "permissions": ["*"]}
USER2 = {"id": "u2", "tenant_id": "t2", "email": "other@shop.example",
         "role": "owner", "permissions": ["*"]}


# ------------------------------------------------------------- normalizers
def test_dates_mixed_formats():
    assert normalize_date("2026-03-04")["normalized"] == "2026-03-04"
    assert normalize_date("03/04/2026")["normalized"] == "2026-03-04"
    assert normalize_date("3-4-26")["normalized"] == "2026-03-04"
    assert normalize_date("4 Mar 2026")["normalized"] == "2026-03-04"
    assert normalize_date("March 4, 2026")["normalized"] == "2026-03-04"
    assert normalize_date("20260304")["normalized"] == "2026-03-04"
    assert normalize_date("2026-03-04T10:22:00")["normalized"] == "2026-03-04"
    assert normalize_date("13/04/2026")["normalized"] == "2026-04-13"  # day-first forced
    assert normalize_date("not a date")["ok"] is False
    assert normalize_date("")["normalized"] is None
    amb = normalize_date("03/04/2026")
    assert amb["confidence"] < 0.9 and "ambiguous" in " ".join(amb["notes"])


def test_phones_us_and_international_variants():
    assert normalize_phone("(512) 555-0134")["normalized"] == "+15125550134"
    assert normalize_phone("512.555.0134")["normalized"] == "+15125550134"
    assert normalize_phone("1-512-555-0134")["normalized"] == "+15125550134"
    assert normalize_phone("+44 20 7946 0958")["normalized"] == "+442079460958"
    assert normalize_phone("+52 55 1234 5678")["ok"] is True
    bad = normalize_phone("555-1")
    assert bad["ok"] is False and bad["normalized"] is None  # never invented
    assert normalize_phone("")["ok"] is False


def test_company_key_suffix_chaos():
    assert company_key("The Home Depot, Inc.")["normalized"] == "home depot"
    assert company_key("HOME DEPOT INC")["normalized"] == "home depot"
    assert company_key("Home Depot, LLC")["normalized"] == "home depot"
    hd = company_key("The Home Depot, Inc. #4412")
    assert hd["normalized"] == "home depot" and hd["store_number"] == "4412"
    assert company_key("Acme Supply Corp.")["normalized"] == "acme supply"


def test_parse_name_people_and_companies():
    p = parse_name("John Q. Smith")
    assert p["type"] == "Person" and p["normalized"] == "smith john"
    p2 = parse_name("Smith, John")
    assert p2["type"] == "Person" and p2["normalized"].startswith("smith")
    c = parse_name("Acme Supply Inc")
    assert c["type"] == "Corporation" and c["normalized"] == "acme supply"


def test_address_suite_and_unit_parsing():
    a = parse_address("123 Main St Apt 4, Austin, TX 78701")
    assert a["ok"] and a["parts"]["unit"] == "Apt 4"
    assert a["parts"]["zip"] == "78701" and a["parts"]["city"] == "Austin"
    b = parse_address("4500 Commerce Street Suite 2100, Dallas, TX 75201")
    assert b["ok"] and "Suite 2100" in (b["parts"]["unit"] or "")
    assert parse_address("somewhere over the rainbow")["ok"] is False


def test_normalize_description_for_rules():
    assert normalize_description("ACME  SUPPLY,  INC.") == "acme supply"
    assert normalize_description("The Home Depot, Inc.") == "home depot"
    assert normalize_description("LONE STAR PLUMBING LLC") == "lone star plumbing"


def test_clean_row_explainable_changes():
    doc = {"name": "  ACME  SUPPLY, INC. ", "phone": "(512) 555-0134",
           "email": " Ops@Acme.Example ", "address": "9 Oak Ave, Dallas, TX 75001"}
    cleaned, changes = clean_row(doc, "vendors")
    assert cleaned["phone"] == "+15125550134"
    assert cleaned["email"] == "ops@acme.example"
    assert cleaned["name"] == "ACME SUPPLY, INC."        # display preserved
    assert cleaned["name_key"] == "acme supply"          # suffix-free key aside
    fields = {c["field"] for c in changes}
    assert {"phone", "email", "name"} <= fields
    for c in changes:                                    # every change explains itself
        assert c["before"] != c["after"] and c["rule"] and c["confidence"]
    bad, _ = clean_row({"name": "X", "phone": "555-1"}, "customers")
    assert bad["phone"] == "555-1"                       # unparseable kept verbatim


# ------------------------------------------------------- mess report/schema
UGLY_ROWS = [
    {"name": "Acme Supply Inc", "email": "ops@acme.example",
     "phone": "(512) 555-0134", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"name": "ACME SUPPLY, INC.", "email": "ops@acme.example",
     "phone": "512-555-0134", "address": "9 Oak Ave, Dallas, TX 75001"},
    {"name": "", "email": "not-an-email", "phone": "call me maybe",
     "address": ""},
    {"name": "Lone Star Plumbing", "email": "", "phone": "+1 512 555 0142",
     "address": "77 Ranch Rd, Austin, TX 78701"},
]


def test_mess_report_and_column_detection():
    rep = mess.mess_report(UGLY_ROWS)
    cols = {c["column"]: c for c in rep["columns"]}
    assert rep["rows"] == 4
    assert cols["email"]["detected_type"] == "email"
    assert cols["email"]["issues"].get("invalid_emails") == 1
    assert cols["phone"]["issues"].get("invalid_phones") == 1
    assert cols["name"]["empty"] == 1
    dates = mess.detect_column_type(
        ["03/04/2026", "2026-03-05", "Mar 6, 2026", "junk"])
    assert dates["type"] in ("date", "mixed")
    rep2 = mess.mess_report([{"d": "03/04/2026"}, {"d": "2026-03-05"},
                             {"d": "Mar 6, 2026"}])
    dcol = rep2["columns"][0]
    assert dcol["detected_type"] == "date"
    assert "mixed_date_formats" in dcol["issues"]


def test_schema_blocking_vs_row_level():
    bad_file = [{"name": "A", "email": "x"}, {"name": "B", "email": "y"},
                {"name": "C", "email": "z"}, {"name": "D", "email": "w"}]
    sv = schema.validate_rows(bad_file, "customers")
    assert sv["blocking"] and "Email column" in sv["blocking"][0]
    ok = schema.validate_rows(UGLY_ROWS, "customers")
    assert not ok["blocking"]
    assert any(e["field"] == "name" for e in ok["row_errors"])
    assert any(e["field"] == "phone" and e["severity"] == "warning"
               for e in ok["row_errors"])
    nonames = schema.validate_rows([{"email": "a@b.co"}], "vendors")
    assert nonames["blocking"]
    items = schema.validate_rows(
        [{"name": "W", "price": "abc"}, {"name": "X", "price": "def"},
         {"name": "Y", "price": "ghi"}], "items")
    assert items["blocking"]


# ------------------------------------------------------- importer endpoints
def _patch_importer(fake):
    import routers.importer as IMP
    IMP.db = fake
    return IMP


CSV_UGLY = ("Client,E-mail,Phone,Address\n"
            "Acme Supply Inc,ops@acme.example,(512) 555-0134,\"9 Oak Ave, Dallas, TX 75001\"\n"
            "Lone Star Plumbing,,512.555.0142,\"77 Ranch Rd, Austin, TX 78701\"\n")


def test_importer_cleanup_preview_shows_before_after_and_mess():
    IMP = _patch_importer(FakeDB())
    out = run(IMP.cleanup_preview(
        IMP.CleanupPreviewIn(kind="customers", csv_text=CSV_UGLY), USER1))
    assert out["can_import"] is True
    assert out["mess_report"]["rows"] == 2
    phone_changes = out["normalization_preview"].get("phone", [])
    assert phone_changes and phone_changes[0]["after"] == "+15125550134"
    assert out["column_types"]["phone"]["type"] == "phone"


def test_importer_run_normalizes_and_stores_keys():
    fake = FakeDB()
    IMP = _patch_importer(fake)
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_UGLY, email_summary=False), USER1))
    assert out["created"] == 2 and out["cleanup"]["applied"]
    assert out["cleanup"]["changes"] >= 2
    docs = list(fake.customers.docs.values())
    acme = next(d for d in docs if "Acme" in d["name"])
    assert acme["phone"] == "+15125550134"
    assert acme["name_key"] == "acme supply"
    assert acme["address_parts"]["zip"] == "75001"


BAD_EMAIL_CSV = ("Client,E-mail\nA Co,x\nB Co,y\nC Co,z\nD Co,w\n")


def test_importer_run_schema_gate_blocks_and_override_is_audited():
    fake = FakeDB()
    IMP = _patch_importer(fake)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        run(IMP.run_import(IMP.RunIn(
            kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False),
            USER1))
    assert ei.value.status_code == 422
    assert len(fake.customers.docs) == 0                    # nothing imported
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False,
        override_schema=True), USER1))
    assert out["created"] == 4 and out["cleanup"]["schema_override"] is True
    audits = [d for d in fake.audit_log.docs.values()
              if d.get("action") == "import.schema_override"]
    assert len(audits) == 1 and audits[0]["tenant_id"] == "t1"


# ------------------------------------------------------------- dedupe core
def _vendor(i, name, phone="", email="", address=""):
    return {"id": f"v{i}", "name": name, "phone": phone,
            "email": email, "address": address}


LABELED = [
    # true cluster 1
    _vendor(1, "Home Depot Inc", "(512) 555-0134", "ops@hd.example", "9 Oak Ave, Dallas, TX 75001"),
    _vendor(2, "The Home Depot, LLC", "512-555-0134", "ops@hd.example", "9 Oak Ave, Dallas, TX 75001"),
    # true cluster 2 (incl. a typo variant)
    _vendor(3, "Acme Supply Inc", "(512) 555-0111", "a@acme.example", "1 Industrial Pkwy, Austin, TX 78701"),
    _vendor(4, "ACME SUPPLY, INC.", "512.555.0111", "a@acme.example", "1 Industrial Pkwy, Austin, TX 78701"),
    _vendor(5, "Acme Suply Inc", "(512) 555-0111", "orders@acme.example", "1 Industrial Pkwy, Austin, TX 78701"),
    # true cluster 3 (accent / Spanglish variant, same phone + email)
    _vendor(6, "José's Landscaping LLC", "(512) 555-0160", "jose@jl.example", "5 Verde St, Austin, TX 78702"),
    _vendor(7, "Joses Landscaping", "512 555 0160", "jose@jl.example", "5 Verde St, Austin, TX 78702"),
    # lookalikes that must NOT pair
    _vendor(8, "Smith Plumbing LLC", "(512) 555-0201", "a@smithplumb.example", "2 A St, Waco, TX 76701"),
    _vendor(9, "Smith Painting LLC", "(512) 555-0202", "b@smithpaint.example", "3 B St, Waco, TX 76701"),
    _vendor(10, "Home Depot #4412", "(512) 555-0301", "s4412@hd.example", "10 Pine St, Dallas, TX 75001"),
    _vendor(11, "Home Depot #4413", "(512) 555-0302", "s4413@hd.example", "88 Elm St, Plano, TX 75023"),
    _vendor(12, "Dallas Electric Inc", "(512) 555-0401", "a@de.example", "4 Volt Way, Dallas, TX 75001"),
    _vendor(13, "Dallas Electric Supply Inc", "(512) 555-0402", "b@des.example", "9 Amp Rd, Dallas, TX 75002"),
    # unrelated singles
    _vendor(14, "Lone Star Roofing", "(512) 555-0501", "a@lsr.example", ""),
    _vendor(15, "Pecos Pest Control", "(512) 555-0502", "a@pecos.example", ""),
    _vendor(16, "Bluebonnet Bakery", "(512) 555-0503", "a@bbb.example", ""),
]
TRUE_PAIRS = {frozenset(p) for p in [("v1", "v2"), ("v3", "v4"), ("v3", "v5"),
                                     ("v4", "v5"), ("v6", "v7")]}


def test_linkage_precision_recall_on_labeled_fixture():
    pairs = linkage.find_duplicates(LABELED)
    found = {frozenset((p["a_id"], p["b_id"])) for p in pairs}
    tp = found & TRUE_PAIRS
    precision = len(tp) / len(found) if found else 1.0
    recall = len(tp) / len(TRUE_PAIRS)
    # Precision is the expensive direction: ZERO false suggestions allowed
    # on this fixture; recall bar is 4 of 5 known duplicate pairs.
    assert precision == 1.0, f"false suggestions: {found - TRUE_PAIRS}"
    assert recall >= 0.8, f"missed: {TRUE_PAIRS - found}"
    for p in pairs:  # suggestions carry their evidence
        assert p["evidence"]["why"] and p["score"] >= linkage.SUGGEST_THRESHOLD


def test_splink_engine_path_on_larger_list():
    ents = []
    for i in range(7):
        ents.append({"id": f"a{i}", "name": f"Vendor {i} Inc",
                     "phone": f"(512) 555-10{i:02d}", "email": f"v{i}@x.example"})
        ents.append({"id": f"b{i}", "name": f"The Vendor {i}, LLC",
                     "phone": f"512-555-10{i:02d}", "email": f"v{i}@x.example"})
    res = linkage.suggest_duplicates(ents)
    assert res["engine"] in ("splink", "deterministic-fallback")
    found = {frozenset((p["a_id"], p["b_id"])) for p in res["pairs"]}
    expected = {frozenset((f"a{i}", f"b{i}")) for i in range(7)}
    assert expected <= found
    assert found <= expected  # no cross-vendor false pairs on this clean set


# ------------------------------------------------------------ dedupe router
def _patch_dedupe(fake):
    import routers.dedupe as DD
    DD.db = fake
    return DD


def _seed_customers(fake, user, rows):
    from lib.base import Scoped
    coll = Scoped(fake.customers, user["tenant_id"])
    for r in rows:
        run(coll.insert_one(dict(r)))


def test_scan_suggests_only_never_merges_and_is_tenant_isolated():
    fake = FakeDB()
    DD = _patch_dedupe(fake)
    rows = [{"id": "c1", "name": "Acme Supply Inc", "phone": "(512) 555-0111",
             "email": "a@acme.example", "address": ""},
            {"id": "c2", "name": "ACME SUPPLY, INC.", "phone": "512.555.0111",
             "email": "a@acme.example", "address": ""},
            {"id": "c3", "name": "Unrelated Co", "phone": "(512) 555-0999",
             "email": "u@u.example", "address": ""}]
    _seed_customers(fake, USER1, rows)
    _seed_customers(fake, USER2, [dict(r, id=f"t2-{r['id']}") for r in rows])
    out = run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    assert out["scanned"] == 3
    sugg = out["suggestions"]
    assert len(sugg) == 1
    assert {sugg[0]["a_id"], sugg[0]["b_id"]} == {"c1", "c2"}   # t1 only
    # Suggestions only: NO record was merged, NO merge record exists.
    assert all("merged_into" not in d for d in fake.customers.docs.values())
    assert len(fake.entity_merges.docs) == 0


def test_merge_requires_approval_then_reversible_with_audit():
    fake = FakeDB()
    DD = _patch_dedupe(fake)
    rows = [{"id": "c1", "name": "Acme Supply Inc", "phone": "(512) 555-0111",
             "email": "", "address": "1 Industrial Pkwy, Austin, TX 78701",
             "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "c2", "name": "ACME SUPPLY, INC.", "phone": "512.555.0111",
             "email": "a@acme.example", "address": "",
             "created_at": "2026-02-01T00:00:00+00:00"}]
    _seed_customers(fake, USER1, rows)
    out = run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    sid = out["suggestions"][0]["id"]
    merged = run(DD.merge(sid, DD.MergeIn(), USER1))
    survivor = merged["survivor"]
    assert survivor["id"] == "c1"                       # earlier record wins
    assert survivor["email"] == "a@acme.example"        # filled from absorbed
    docs = {d["id"]: d for d in fake.customers.docs.values()
            if d.get("tenant_id") == "t1"}
    assert docs["c2"]["status"] == "merged" and docs["c2"]["merged_into"] == "c1"
    records = list(fake.entity_merges.docs.values())
    assert len(records) == 1
    assert records[0]["absorbed_before"]["name"] == "ACME SUPPLY, INC."
    assert records[0]["survivor_before"]["email"] == ""
    assert any(d.get("action") == "dedupe.merge"
               for d in fake.audit_log.docs.values())
    # Second merge attempt on the same suggestion is refused.
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        run(DD.merge(sid, DD.MergeIn(), USER1))
    # Reverse restores BOTH records exactly.
    rev = run(DD.reverse_merge(records[0]["id"], USER1))
    assert rev["status"] == "reversed"
    docs = {d["id"]: d for d in fake.customers.docs.values()
            if d.get("tenant_id") == "t1"}
    assert docs["c1"]["email"] == "" and "merged_into" not in docs["c2"]
    assert docs["c2"].get("status") not in ("merged",)
    assert any(d.get("action") == "dedupe.merge_reverse"
               for d in fake.audit_log.docs.values())


# ------------------------------------------------------------- banking feed
def test_banking_dates_normalized_and_rules_match_normalized_text():
    import routers.banking as BK
    fake = FakeDB()
    BK.db = fake
    rows = BK._parse_csv("date,description,amount\n03/04/2026,Test,10.00\n")
    assert rows[0]["txn_date"] == "2026-03-04"

    from lib.base import Scoped
    run(Scoped(fake.bank_accounts, "t1").insert_one(
        {"id": "acc1", "name": "Checking"}))
    run(Scoped(fake.bank_rules, "t1").insert_one({
        "id": "r1", "version": 2, "name": "Acme rule", "priority": 1,
        "match_mode": "all",
        "conditions": [{"field": "description", "op": "contains",
                        "value": "acme supply"}],
        "actions": {"category_code": "5010"}, "stop_processing": False}))
    doc = run(BK._insert_txn(USER1, "acc1", "2026-03-04",
                              "ACME  SUPPLY,  INC.", -42.0, "test-batch"))
    assert doc is not None
    assert doc["category_code"] == "5010"               # matched via normalized text
    assert doc["description_normalized"] == "acme supply"
    plain = run(BK._insert_txn(USER1, "acc1", "2026-03-05",
                                "acme supply store", -9.0, "test-batch"))
    assert plain["category_code"] == "5010"             # raw path still works


# ------------------------------------------------------------- performance
def test_mess_and_clean_preview_scale_to_5000_rows():
    rows = []
    for i in range(5000):
        rows.append({"name": f"Vendor {i} Inc" if i % 7 else f"VENDOR {i}, LLC",
                     "email": f"v{i}@x.example" if i % 11 else "bad-email",
                     "phone": f"(512) 555-{i % 10000:04d}",
                     "address": f"{i} Main St, Austin, TX 78701"})
    t0 = time.time()
    rep = mess.mess_report(rows)
    for r in rows[:500]:
        clean_row(r, "vendors")
    elapsed = time.time() - t0
    assert rep["rows"] == 5000
    assert elapsed < 30, f"mess pipeline too slow: {elapsed:.1f}s"
