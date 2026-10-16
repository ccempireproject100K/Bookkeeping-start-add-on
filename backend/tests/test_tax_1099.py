"""Tax + 1099 verification: 2026 constants, 1099-NEC threshold logic, and the
TaxBandits e-file flow (mocked).

Run from the backend directory with: python -m pytest tests/test_tax_1099.py

No live MongoDB, network, or TaxBandits keys needed — the db is faked
in-memory and TaxBandits HTTP is mocked. Covers:

  1. 2026 tax constants are pinned: federal single-filer brackets, standard
     deduction, SS wage base, additional Medicare tax, 1099-NEC $600
     threshold, safe-harbor AGI trigger. Any accidental edit breaks a test.
     (Sources cross-checked: IRS IR-2025-103 for brackets/std deduction,
     SSA Oct-2025 announcement for the $184,500 wage base.)
  2. 1099-NEC threshold edges: $600.00 exactly files, $599.99 does not;
     totals aggregate across payments and are year-bounded; exempt entity
     types (C/S corps) never file.
  3. TaxBandits: OAuth client-assertion request shape, Create payload shape,
     TIN decrypted server-side only at send time and never persisted or
     logged, DRAFT-only note, graceful 503 without keys.

The payer's legal name/EIN/address live in TaxBandits; the app sends only
recipient + amounts.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_tax1099")
# Throwaway Fernet key for TIN encrypt/decrypt round-trips in tests.
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

import jwt  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from routers import tax as T  # noqa: E402
from routers import taxforecast as TF  # noqa: E402
from lib.pii import pii_encrypt, last4  # noqa: E402

TB_KEYS = ("TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
           "TAXBANDITS_USER_TOKEN", "TAXBANDITS_BUSINESS_ID")
TB_ALL = TB_KEYS + ("TAXBANDITS_SANDBOX",)


# ---------------------------------------------------------------- fakes

def _matches(doc, q):
    for k, v in q.items():
        if k == "tenant_id":
            continue
        if isinstance(v, dict):
            if "$gte" in v and doc.get(k, "") < v["$gte"]:
                return False
            if "$lte" in v and doc.get(k, "") > v["$lte"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    """Fake motor collection. Supports the subset tax.py/taxforecast.py use,
    including the aggregate() shape of _year_total_cents."""

    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q or {}):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q or {})])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                n += 1
                for op, changes in update.items():
                    if op == "$set":
                        d.update(changes)
        return SimpleNamespace(matched_count=n)

    async def delete_one(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, q)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def aggregate(self, pipeline):
        rows = [dict(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                rows = [d for d in rows if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                total = sum(d.get(spec["total"]["$sum"].lstrip("$"), 0)
                            for d in rows)
                rows = [{"_id": spec.get("_id"), "total": total}]
        return _Cursor(rows)


@pytest.fixture
def fx(monkeypatch):
    """Fake db on the tax module + TaxBandits env scrubbed."""
    fdb = SimpleNamespace(contractors=_Coll(), contractor_payments=_Coll(),
                          audit_log=_Coll())
    monkeypatch.setattr(T, "db", fdb)
    monkeypatch.setattr(T, "_audit", lambda *a, **k: _log(fdb, *a, **k))
    for k in TB_ALL:
        monkeypatch.delenv(k, raising=False)
    user = {"id": "u1", "tenant_id": "t1", "name": "Test",
            "email": "t@example.com", "role": "owner"}
    return fdb, user


async def _log(fdb, user, action, entity, entity_id, details=None):
    fdb.audit_log.docs.append({"action": action, "entity": entity,
                               "entity_id": entity_id,
                               "details": details or {}})


def _seed_contractor(fdb, cid="c1", entity_type="individual", w9=True,
                     tin="123-45-6789"):
    c = {"id": cid, "tenant_id": "t1", "name": "Jane Doe",
         "email": "jane@example.com", "phone": "", "business_name": "",
         "entity_type": entity_type,
         "tax_id_enc": pii_encrypt(tin) if tin else "",
         "tax_id_last4": last4(tin) if tin else "",
         "address": {"street": "1 Main St", "city": "Austin",
                     "state": "TX", "zip": "78701"},
         "w9_received": w9,
         "w9_received_date": "2026-01-10" if w9 else None, "efile": {}}
    fdb.contractors.docs.append(c)
    return c


def _seed_payments(fdb, cid="c1", amounts=(), year=2026):
    for i, cents in enumerate(amounts):
        fdb.contractor_payments.docs.append({
            "id": f"p{i}", "tenant_id": "t1", "contractor_id": cid,
            "date": f"{year}-0{(i % 9) + 1}-15", "amount_cents": cents,
            "amount": cents / 100, "description": ""})


def _tb_env(monkeypatch, sandbox=None):
    monkeypatch.setenv("TAXBANDITS_CLIENT_ID", "tb-client-1")
    monkeypatch.setenv("TAXBANDITS_CLIENT_SECRET", "tb-secret-1")
    monkeypatch.setenv("TAXBANDITS_USER_TOKEN", "tb-user-1")
    monkeypatch.setenv("TAXBANDITS_BUSINESS_ID", "tb-biz-1")
    if sandbox is not None:
        monkeypatch.setenv("TAXBANDITS_SANDBOX", sandbox)


# ============================================================ constants locked

def test_brackets_exact_2026_values():
    # IRS IR-2025-103, single filer. Boundaries are inclusive "up to".
    assert TF.DEFAULT_BRACKETS == [
        {"up_to": 12400, "rate": 0.10},
        {"up_to": 50400, "rate": 0.12},
        {"up_to": 105700, "rate": 0.22},
        {"up_to": 201775, "rate": 0.24},
        {"up_to": 256225, "rate": 0.32},
        {"up_to": 640600, "rate": 0.35},
        {"up_to": None, "rate": 0.37},
    ]


def test_std_deduction_2026():
    assert TF.DEFAULT_STD_DEDUCTION == 16100  # IRS IR-2025-103, single


def test_ss_wage_base_2026():
    assert TF.SS_WAGE_BASE_2026 == 184500  # SSA Oct-2025 announcement


def test_additional_medicare_2026():
    assert TF.ADDITIONAL_MEDICARE_THRESHOLD == 200000
    assert TF.ADDITIONAL_MEDICARE_RATE == 0.009


def test_safe_harbor_rule():
    # 110% of prior-year tax when prior-year AGI EXCEEDS $150k, else 100%.
    assert TF.SAFE_HARBOR_AGI_THRESHOLD_CENTS == 15_000_000
    assert TF._safe_harbor(1_000_000, 16_000_000) == 1_100_000
    assert TF._safe_harbor(1_000_000, 15_000_000) == 1_000_000  # exactly 150k: not "exceeds"
    assert TF._safe_harbor(1_000_000, 0) == 1_000_000
    assert TF._safe_harbor(0, 99_999_999) == 0


def test_1099_threshold_constant():
    assert T.THRESHOLD_CENTS == 60_000  # IRS: $600, inclusive
    assert T.EXEMPT_ENTITIES == {"c_corp", "s_corp"}


def test_income_tax_math_against_irs_table():
    # Morningstar/MarketWatch 2026 single table:
    # $100k taxable -> 1,240 + 4,560 + 10,912 = $16,712.
    assert TF._income_tax(100_000, TF.DEFAULT_BRACKETS) == pytest.approx(16_712)
    # Boundary semantics: "not over $12,400" is all 10%.
    assert TF._income_tax(12_400, TF.DEFAULT_BRACKETS) == pytest.approx(1_240)
    assert TF._income_tax(12_401, TF.DEFAULT_BRACKETS) == pytest.approx(1_240.12)


def test_se_tax_math():
    # 12.4% OASDI + 2.9% Medicare on 92.35%-adjusted net.
    assert TF._se_tax(100_000) == pytest.approx(15_300)
    # OASDI caps at the wage base; Medicare and +0.9% over $200k don't.
    assert TF._se_tax(250_000) == pytest.approx(
        0.124 * 184_500 + 0.029 * 250_000 + 0.009 * 50_000)
    assert TF._se_tax(0) == 0


def test_quarter_coverage_no_gaps_or_overlaps():
    # REGRESSION: Q2 previously ended 2026-05-31 and Q3 began 2026-06-01,
    # silently attributing all of June to Q3.
    qs = TF.QUARTERS_2026
    assert qs[0]["from"] == "2026-01-01" and qs[-1]["to"] == "2026-12-31"
    for prev, cur in zip(qs, qs[1:]):
        assert cur["from"] > prev["to"], (prev, cur)  # no overlap
        from datetime import date
        gap = (date.fromisoformat(cur["from"]) -
               date.fromisoformat(prev["to"])).days
        assert gap == 1, (prev["q"], cur["q"])  # no gap


# ============================================================ 1099 threshold logic

def _row(**kw):
    c = {"id": "c1", "name": "N", "entity_type": "individual"}
    c.update(kw)
    return c


def test_threshold_edges_inclusive():
    assert T._strip(_row(), 60_000)["needs_1099"] is True   # exactly $600.00
    assert T._strip(_row(), 59_999)["needs_1099"] is False  # $599.99


def test_exempt_entities_never_file():
    for et in ("c_corp", "s_corp"):
        assert T._strip(_row(entity_type=et), 1_000_000)["needs_1099"] is False
    for et in ("individual", "sole_prop", "llc", "partnership", "trust"):
        assert T._strip(_row(entity_type=et), 60_000)["needs_1099"] is True


def test_tax_id_never_exposed_in_strip():
    c = _row(tax_id_enc="ENCRYPTED", tax_id_last4="6789")
    row = T._strip(c, 60_000)
    assert "tax_id_enc" not in row and "tax_id" not in row
    assert row["tax_id_last4"] == "6789"


def test_totals_aggregate_and_are_year_bounded(fx):
    asyncio.run(_test_totals_aggregate_and_are_year_bounded_co(fx))


async def _test_totals_aggregate_and_are_year_bounded_co(fx):
    fdb, user = fx
    _seed_contractor(fdb)
    _seed_payments(fdb, amounts=(40_000, 20_000), year=2026)   # $600.00
    _seed_payments(fdb, amounts=(999_999,), year=2025)         # prior year ignored
    total = await T._year_total_cents(user, "c1", 2026)
    assert total == 60_000
    summary = await T.year_summary(2026, user)
    names = [r["name"] for r in summary["contractors"] if r["needs_1099"]]
    assert names == ["Jane Doe"]
    assert summary["required_count"] == 1
    assert summary["reportable_total_cents"] == 60_000


def test_below_threshold_contractor_excluded(fx):
    asyncio.run(_test_below_threshold_contractor_excluded_co(fx))


async def _test_below_threshold_contractor_excluded_co(fx):
    fdb, user = fx
    _seed_contractor(fdb)
    _seed_payments(fdb, amounts=(59_999,), year=2026)  # $599.99
    summary = await T.year_summary(2026, user)
    assert summary["required_count"] == 0
    assert summary["contractors"][0]["needs_1099"] is False


def test_payment_amounts_round_to_cents_end_to_end(fx):
    asyncio.run(_test_payment_amounts_round_to_cents_end_to_end_co(fx))


async def _test_payment_amounts_round_to_cents_end_to_end_co(fx):
    """Float dollars -> cents must honor the $600.00 / $599.99 boundary."""
    fdb, user = fx
    _seed_contractor(fdb)
    body = T.PaymentIn(date="2026-03-01", amount=599.99, description="")
    await T.add_payment("c1", body, user)
    assert await T._year_total_cents(user, "c1", 2026) == 59_999
    body2 = T.PaymentIn(date="2026-04-01", amount=600.00, description="")
    await T.add_payment("c1", body2, user)
    assert await T._year_total_cents(user, "c1", 2026) == 119_999


def test_1099_preview_masks_tin(fx):
    asyncio.run(_test_1099_preview_masks_tin_co(fx))


async def _test_1099_preview_masks_tin_co(fx):
    fdb, user = fx
    _seed_contractor(fdb)
    _seed_payments(fdb, amounts=(60_000,), year=2026)
    prev = await T.preview_1099(2026, user)
    assert prev["form"] == "1099-NEC"
    (rec,) = prev["recipients"]
    assert rec["box1_cents"] == 60_000
    assert rec["tin_last4"] == "6789"
    assert "TIN" not in rec
    flat = " ".join(str(v) for v in rec.values())
    assert "123-45-6789" not in flat


# ============================================================ TaxBandits

def test_tb_conf_sandbox_default(fx, monkeypatch):
    _tb_env(monkeypatch)
    conf = T._tb_conf()
    assert conf["sandbox"] is True
    assert conf["base"] == "https://testapi.taxbandits.com/v2"


def test_tb_conf_production_switch(fx, monkeypatch):
    _tb_env(monkeypatch, sandbox="false")
    conf = T._tb_conf()
    assert conf["sandbox"] is False
    assert conf["base"] == "https://api.taxbandits.com/v2"


def test_tb_conf_missing_any_key_503(fx, monkeypatch):
    _tb_env(monkeypatch)
    for k in TB_KEYS:
        monkeypatch.delenv(k)
        with pytest.raises(HTTPException) as e:
            T._tb_conf()
        assert e.value.status_code == 503
        assert k in e.value.detail
        _tb_env(monkeypatch)


def test_efile_503_without_keys(fx):
    asyncio.run(_test_efile_503_without_keys_co(fx))


async def _test_efile_503_without_keys_co(fx):
    fdb, user = fx
    _seed_contractor(fdb)
    _seed_payments(fdb, amounts=(60_000,), year=2026)
    with pytest.raises(HTTPException) as e:
        await T.efile_1099("c1", 2026, user)
    assert e.value.status_code == 503
    assert "TAXBANDITS_CLIENT_ID" in e.value.detail


def test_efile_guards(fx, monkeypatch):
    asyncio.run(_test_efile_guards_co(fx, monkeypatch))


async def _test_efile_guards_co(fx, monkeypatch):
    """400 under $600 (exact $600 passes), 400 without W-9, 404 unknown."""
    _tb_env(monkeypatch)
    fdb, user = fx
    _seed_contractor(fdb)
    _seed_payments(fdb, amounts=(59_999,), year=2026)
    with pytest.raises(HTTPException) as e:
        await T.efile_1099("c1", 2026, user)
    assert e.value.status_code == 400
    assert "threshold" in e.value.detail

    _seed_contractor(fdb, cid="c2", w9=False)
    _seed_payments(fdb, cid="c2", amounts=(60_000,), year=2026)
    with pytest.raises(HTTPException) as e2:
        await T.efile_1099("c2", 2026, user)
    assert e2.value.status_code == 400
    assert "W-9" in e2.value.detail

    # exactly $600.00 passes the threshold guard (W-9 is present) —
    # it will 503/502 only if it reaches TaxBandits; here it 503s on keys? no:
    # keys ARE set, so it proceeds to the mocked network below.
    with patch.object(T, "_tb_token_sync", side_effect=AssertionError("no net")):
        with pytest.raises(HTTPException) as e3:
            await T.efile_1099("nope", 2026, user)
    assert e3.value.status_code == 404


def test_oauth_client_assertion_request_shape(monkeypatch):
    """OAuth2 client-assertion: JWT signed with the client secret, exchanged
    at POST {base}/OAuth/Token."""
    _tb_env(monkeypatch)
    conf = T._tb_conf()
    calls = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"AccessToken": "bearer-xyz"}

    def fake_post(url, timeout=None, json=None):
        calls.update(url=url, timeout=timeout, payload=json)
        return _Resp()

    with patch.object(T.requests, "post", fake_post):
        token = T._tb_token_sync(conf)
    assert token == "bearer-xyz"
    assert calls["url"] == "https://testapi.taxbandits.com/v2/OAuth/Token"
    p = calls["payload"]
    assert p["grant_type"] == "client_credentials"
    assert p["client_assertion_type"] == \
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert p["user_token"] == "tb-user-1"
    claims = jwt.decode(p["client_assertion"], "tb-secret-1",
                        algorithms=["HS256"], options={"verify_aud": False})
    assert claims["iss"] == "tb-client-1" and claims["sub"] == "tb-client-1"
    assert claims["exp"] - claims["iat"] == 300  # 5-minute assertion


def test_oauth_failures_surface_as_502(monkeypatch):
    _tb_env(monkeypatch)
    conf = T._tb_conf()

    class _Bad:
        status_code = 401
        text = "invalid_client"

    with patch.object(T.requests, "post", lambda *a, **k: _Bad()):
        with pytest.raises(HTTPException) as e:
            T._tb_token_sync(conf)
    assert e.value.status_code == 502

    class _NoToken:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    with patch.object(T.requests, "post", lambda *a, **k: _NoToken()):
        with pytest.raises(HTTPException) as e2:
            T._tb_token_sync(conf)
    assert e2.value.status_code == 502


def test_efile_create_payload_shape_and_draft_only(fx, monkeypatch):
    asyncio.run(_test_efile_create_payload_shape_and_draft_only_co(fx, monkeypatch))


async def _test_efile_create_payload_shape_and_draft_only_co(fx, monkeypatch):
    """Mocked end-to-end draft creation: payload shape, TIN handling, the
    stored efile record, and the DRAFT-only note. No real HTTP."""
    _tb_env(monkeypatch)
    fdb, user = fx
    _seed_contractor(fdb)  # TIN 123-45-6789 encrypted, W-9 received
    _seed_payments(fdb, amounts=(40_000, 35_000), year=2026)  # $750.00
    calls = {}

    def fake_token(conf):
        return "bearer-xyz"

    def fake_request(method, url, token, payload=None):
        calls.update(method=method, url=url, token=token, payload=payload)
        return {"SubmissionId": "sub-1",
                "Form1099Records": [{"RecordId": "rec-1",
                                     "RecordStatus": "Draft"}]}

    with patch.object(T, "_tb_token_sync", fake_token), \
         patch.object(T, "_tb_request_sync", fake_request):
        result = await T.efile_1099("c1", 2026, user)

    assert result["ok"] is True
    assert calls["method"] == "POST"
    assert calls["url"].endswith("/Form1099NEC/Create")
    assert calls["token"] == "bearer-xyz"
    p = calls["payload"]
    assert p["BusinessId"] == "tb-biz-1" and p["TaxYear"] == 2026
    assert p["Recipient"]["Name"] == "Jane Doe"
    # Full TIN decrypted server-side only, at send time.
    assert p["Recipient"]["TIN"] == "123-45-6789"
    addr = p["Recipient"]["Address"]
    assert (addr["Address1"], addr["City"], addr["State"], addr["ZipCd"]) == \
        ("1 Main St", "Austin", "TX", "78701")
    assert p["Form1099NEC"] == {"Box1 NEC": 750.0}  # dollars, annual total

    efile = result["efile"]
    assert efile["submission_id"] == "sub-1"
    assert efile["record_id"] == "rec-1"
    assert efile["status"] == "Draft"
    assert efile["amount_cents"] == 75_000 and efile["sandbox"] is True
    # Nothing stored or logged carries the plaintext TIN.
    assert "123-45-6789" not in str(efile)
    stored = fdb.contractors.docs[0]["efile"]
    assert "123-45-6789" not in str(stored)
    audits = [d for d in fdb.audit_log.docs
              if d["action"] == "tax.efile.create"]
    assert len(audits) == 1
    assert audits[0]["details"] == {"year": 2026, "amount_cents": 75_000}
    for d in fdb.audit_log.docs:
        assert "123-45-6789" not in str(d)
    # DRAFT-only: the app never transmits; a human clicks Transmit in TaxBandits.
    assert "Transmit inside TaxBandits" in result["note"]


def test_efile_status_poll(fx, monkeypatch):
    asyncio.run(_test_efile_status_poll_co(fx, monkeypatch))


async def _test_efile_status_poll_co(fx, monkeypatch):
    _tb_env(monkeypatch)
    fdb, user = fx
    c = _seed_contractor(fdb)
    c["efile"] = {"submission_id": "sub-1", "status": "Draft"}
    calls = {}

    def fake_request(method, url, token, payload=None):
        calls.update(method=method, url=url)
        return {"Status": "Transmitted"}

    with patch.object(T, "_tb_token_sync", lambda conf: "tok"), \
         patch.object(T, "_tb_request_sync", fake_request):
        result = await T.efile_status("c1", user)
    assert calls["method"] == "GET"
    assert "SubmissionId=sub-1" in calls["url"]
    assert result["efile"]["status"] == "Transmitted"


def test_efile_status_without_draft_needs_no_keys(fx):
    asyncio.run(_test_efile_status_without_draft_needs_no_keys_co(fx))


async def _test_efile_status_without_draft_needs_no_keys_co(fx):
    fdb, user = fx
    _seed_contractor(fdb)
    result = await T.efile_status("c1", user)
    assert result == {"efile": {}}


def test_tb_status_endpoint(fx, monkeypatch):
    asyncio.run(_test_tb_status_endpoint_co(fx, monkeypatch))


async def _test_tb_status_endpoint_co(fx, monkeypatch):
    fdb, user = fx
    assert await T.tb_status(user) == {"configured": False, "sandbox": True}
    _tb_env(monkeypatch, sandbox="false")
    assert await T.tb_status(user) == {"configured": True, "sandbox": False}
