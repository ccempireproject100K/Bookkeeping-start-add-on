"""Books glue endpoints queued behind the 2026-11-05 merge session:

1. GET /api/v1/platform/entitlements — the DataClean/Security
   entitlement read a selling surface (AI Office) authenticates with
   its own machine credential (mdck_ key or mae1 tenant credential);
   the workspace always comes from the credential record, and the
   referrals platform credential class is refused 403.
2. POST /api/integrations/events — the books-side receiver for the
   empire-automation GLUE-1/2/3 flows: bearer (or HMAC) verified
   before any write, event-id dedupe, an audit row per outcome.
3. The books-side signed alert sender (GLUE-4's missing outbound
   half): HMAC-SHA256 over the exact raw body, `x-books-signature`
   header, {id, type, summary} body — verified here against the
   flow's own verification algorithm — plus its fan-out wiring in
   lib.alerts.raise_alert.
4. DELETE /combo/leads/{id} — the Scoped.delete() AttributeError that
   500'd lead deletion on main (the receipts half was fixed in
   PR #205); regression tests prove deletion works and stays
   tenant-scoped.

Run from the backend directory with:
    python -m pytest tests/test_books_glue_endpoints.py

No live Mongo/Stripe/HTTP: the db is faked in-memory following
tests/test_books_checkout.py conventions, and the sender's HTTP
client is a capturing fake.
"""
import asyncio
import hashlib
import hmac
import json
import os
from types import SimpleNamespace

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_books_glue")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import lib.audit as AUD  # noqa: E402
from lib import alerts as ALERTS  # noqa: E402
from lib import dataclean_api as DCA  # noqa: E402
from lib import integration_auth as IA  # noqa: E402
from lib import integration_events as IE  # noqa: E402
from routers import combo as COMBO  # noqa: E402
from routers import integration_events as RCV  # noqa: E402
from routers import platform_entitlements as ENT  # noqa: E402


# ------------------------------------------------------------------ fakes

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
        integration_events=FakeCollection(),
        audit_log=FakeCollection(),
        tenants=FakeCollection(),
        dataclean_api_keys=FakeCollection(),
        integration_credentials=FakeCollection(),
        combo_leads=FakeCollection(),
        combo_interactions=FakeCollection(),
        customers=FakeCollection(),
        estimates=FakeCollection(),
        security_alerts=FakeCollection(),
    )
    for name, docs in collections.items():
        setattr(fake, name, FakeCollection(docs))
    for module in (RCV, ENT, COMBO, IA, AUD, ALERTS):
        monkeypatch.setattr(module, "db", fake)
    return fake


class FakeRequest:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    async def body(self):
        return self._body


def _stripe_event(evt_id="evt_1", etype="charge.succeeded", obj_id="ch_1"):
    return {"id": evt_id, "object": "event", "type": etype,
            "data": {"object": {"id": obj_id}}}


# ------------------------------------------------- item 2: the receiver

def test_receiver_accepts_bearer_stripe_event(monkeypatch):
    asyncio.run(_receiver_bearer(monkeypatch))


async def _receiver_bearer(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    raw = json.dumps({"data": _stripe_event()}).encode()
    req = FakeRequest({"Authorization": "Bearer books-bearer-1"}, raw)
    out = await RCV.receive_event(req)
    assert out["status"] == "received"
    assert out["source"] == "stripe" and out["event_id"] == "evt_1"
    assert len(fake.integration_events.docs) == 1
    row = fake.integration_events.docs[0]
    assert row["type"] == "charge.succeeded" and row["auth"] == "bearer"
    actions = [r["action"] for r in fake.audit_log.docs]
    assert actions == ["integration.event.received"]


def test_receiver_duplicate_redelivery_changes_nothing(monkeypatch):
    asyncio.run(_receiver_dup(monkeypatch))


async def _receiver_dup(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    raw = json.dumps({"data": _stripe_event()}).encode()

    def req():
        return FakeRequest({"Authorization": "Bearer books-bearer-1"}, raw)

    first = await RCV.receive_event(req())
    second = await RCV.receive_event(req())
    assert first["status"] == "received"
    assert second["status"] == "duplicate"
    assert second["id"] == first["id"]
    assert len(fake.integration_events.docs) == 1  # no second write
    actions = sorted(r["action"] for r in fake.audit_log.docs)
    assert actions == ["integration.event.duplicate",
                       "integration.event.received"]


def test_receiver_accepts_data_as_json_string(monkeypatch):
    asyncio.run(_receiver_string_data(monkeypatch))


async def _receiver_string_data(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    raw = json.dumps({"data": json.dumps(_stripe_event("evt_str"))}).encode()
    out = await RCV.receive_event(
        FakeRequest({"Authorization": "Bearer books-bearer-1"}, raw))
    assert out["status"] == "received" and out["event_id"] == "evt_str"
    assert len(fake.integration_events.docs) == 1


def test_receiver_accepts_hmac_signed_books_alert(monkeypatch):
    asyncio.run(_receiver_hmac(monkeypatch))


async def _receiver_hmac(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.ALERTS_SECRET_ENV, "alert-secret")
    body = {"id": "alert-9", "type": "backup_stale", "summary": "Backup stale"}
    raw = json.dumps(body).encode()
    sig = hmac.new(b"alert-secret", raw, hashlib.sha256).hexdigest()
    req = FakeRequest({IE.SIGNATURE_HEADER: f"sha256={sig}"}, raw)
    out = await RCV.receive_event(req)
    assert out["status"] == "received"
    assert out["source"] == "books_alert"
    assert fake.integration_events.docs[0]["auth"] == "hmac"


def test_receiver_rejects_bad_auth_without_side_effects(monkeypatch):
    asyncio.run(_receiver_bad_auth(monkeypatch))


async def _receiver_bad_auth(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    monkeypatch.setenv(IE.ALERTS_SECRET_ENV, "alert-secret")
    raw = json.dumps({"data": _stripe_event()}).encode()
    with pytest.raises(HTTPException) as ei:
        await RCV.receive_event(
            FakeRequest({"Authorization": "Bearer wrong"}, raw))
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await RCV.receive_event(
            FakeRequest({IE.SIGNATURE_HEADER: "sha256=deadbeef"}, raw))
    assert ei.value.status_code == 401
    assert fake.integration_events.docs == []
    assert fake.audit_log.docs == []  # 401 = NO side effects


def test_receiver_unconfigured_is_503(monkeypatch):
    asyncio.run(_receiver_unconfigured(monkeypatch))


async def _receiver_unconfigured(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.delenv(IE.EVENTS_TOKEN_ENV, raising=False)
    monkeypatch.delenv(IE.ALERTS_SECRET_ENV, raising=False)
    raw = json.dumps({"data": _stripe_event()}).encode()
    with pytest.raises(HTTPException) as ei:
        await RCV.receive_event(
            FakeRequest({"Authorization": "Bearer anything"}, raw))
    assert ei.value.status_code == 503
    assert fake.integration_events.docs == []


def test_receiver_missing_event_id_is_422(monkeypatch):
    asyncio.run(_receiver_no_id(monkeypatch))


async def _receiver_no_id(monkeypatch):
    _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    raw = json.dumps({"data": {"type": "charge.succeeded"}}).encode()
    with pytest.raises(HTTPException) as ei:
        await RCV.receive_event(
            FakeRequest({"Authorization": "Bearer books-bearer-1"}, raw))
    assert ei.value.status_code == 422


def test_receiver_payment_failed_fans_out_once(monkeypatch):
    asyncio.run(_receiver_fanout(monkeypatch))


async def _receiver_fanout(monkeypatch):
    fake = _fake_db(monkeypatch)
    monkeypatch.setenv(IE.EVENTS_TOKEN_ENV, "books-bearer-1")
    calls = []

    async def fake_send(**kw):
        calls.append(kw)
        return {"sent": True, "alert_id": "a1"}

    monkeypatch.setattr(IE, "send_books_alert", fake_send)
    raw = json.dumps({"data": _stripe_event(
        "evt_fail", "invoice.payment_failed", "in_1")}).encode()

    def req():
        return FakeRequest({"Authorization": "Bearer books-bearer-1"}, raw)

    out = await RCV.receive_event(req())
    assert out["alert_fanout"] == {"sent": True, "alert_id": "a1"}
    assert calls and calls[0]["alert_type"] == "payment_failed"
    assert fake.integration_events.docs[0]["alert_fanout"]["sent"] is True
    await RCV.receive_event(req())  # redelivery: no second fan-out
    assert len(calls) == 1


# ------------------------------------------- item 3: the signed sender

class _CaptureClient:
    """httpx.AsyncClient stand-in: records the exact request."""

    def __init__(self, status=200, boom=False):
        self.status = status
        self.boom = boom
        self.calls = []

    async def post(self, url, content=None, headers=None):
        if self.boom:
            raise ConnectionError("instance down")
        self.calls.append({"url": url, "content": content,
                           "headers": headers})
        return SimpleNamespace(status_code=self.status)


def test_sender_signs_exactly_as_glue4_verifies(monkeypatch):
    asyncio.run(_sender_signs(monkeypatch))


async def _sender_signs(monkeypatch):
    monkeypatch.setenv(IE.ALERTS_URL_ENV, "https://ap.example/webhook/xyz")
    monkeypatch.setenv(IE.ALERTS_SECRET_ENV, "alert-secret")
    client = _CaptureClient()
    out = await IE.send_books_alert(
        alert_type="payment_failed", summary="Card declined",
        event_id="evt-fixed", _client=client)
    assert out["sent"] is True and out["alert_id"] == "evt-fixed"
    call = client.calls[0]
    raw = call["content"]
    # The GLUE-4 code step's verification, transcribed 1:1:
    expected = hmac.new(b"alert-secret", raw, hashlib.sha256).hexdigest()
    given = call["headers"][IE.SIGNATURE_HEADER].replace("sha256=", "")
    assert hmac.compare_digest(given, expected)
    body = json.loads(raw)
    assert body["id"] == "evt-fixed"
    assert body["type"] == "payment_failed"
    assert body["summary"] == "Card declined"


def test_sender_not_configured_reports_honestly(monkeypatch):
    asyncio.run(_sender_unconfigured(monkeypatch))


async def _sender_unconfigured(monkeypatch):
    monkeypatch.delenv(IE.ALERTS_URL_ENV, raising=False)
    monkeypatch.delenv(IE.ALERTS_SECRET_ENV, raising=False)
    out = await IE.send_books_alert(alert_type="x", summary="y")
    assert out == {"sent": False, "reason": "not_configured"}


def test_sender_failures_are_reported_never_raised(monkeypatch):
    asyncio.run(_sender_failures(monkeypatch))


async def _sender_failures(monkeypatch):
    monkeypatch.setenv(IE.ALERTS_URL_ENV, "https://ap.example/webhook/xyz")
    monkeypatch.setenv(IE.ALERTS_SECRET_ENV, "alert-secret")
    out = await IE.send_books_alert(alert_type="x", summary="y",
                                    _client=_CaptureClient(status=500))
    assert out["sent"] is False and out["reason"] == "http_500"
    out = await IE.send_books_alert(alert_type="x", summary="y",
                                    _client=_CaptureClient(boom=True))
    assert out["sent"] is False and out["reason"] == "transport_error"


def test_raise_alert_fans_out_only_when_configured(monkeypatch):
    asyncio.run(_raise_alert_fanout(monkeypatch))


async def _raise_alert_fanout(monkeypatch):
    fake = _fake_db(monkeypatch)
    calls = []

    async def fake_send(**kw):
        calls.append(kw)
        return {"sent": True, "alert_id": kw.get("event_id")}

    monkeypatch.setattr(IE, "send_books_alert", fake_send)
    # Unconfigured: in-app alert lands, no fan-out is even attempted.
    monkeypatch.delenv(IE.ALERTS_URL_ENV, raising=False)
    monkeypatch.delenv(IE.ALERTS_SECRET_ENV, raising=False)
    alert = await ALERTS.raise_alert("t1", "u1", "brute_force", "Many fails")
    assert fake.security_alerts.docs[0]["id"] == alert["id"]
    assert calls == []
    # Configured: the same alert fans out, keyed by the alert's id.
    monkeypatch.setenv(IE.ALERTS_URL_ENV, "https://ap.example/webhook/xyz")
    monkeypatch.setenv(IE.ALERTS_SECRET_ENV, "alert-secret")
    alert2 = await ALERTS.raise_alert("t1", "u1", "new_device", "New device")
    await asyncio.sleep(0.05)  # let the fire-and-forget task run
    assert len(calls) == 1
    assert calls[0]["alert_type"] == "new_device"
    assert calls[0]["event_id"] == alert2["id"]


# --------------------------------------- item 1: the entitlement read

def _seed_key(fake, tenant_id, **over):
    doc, full = DCA.new_key_doc(tenant_id=tenant_id, created_by="op@test",
                                label="surface key")
    doc.update(over)
    fake.dataclean_api_keys.docs.append(doc)
    return full


def _req(token=None, header="Authorization"):
    headers = {}
    if token is not None:
        headers[header] = f"Bearer {token}" if header == "Authorization" \
            else token
    return FakeRequest(headers)


def test_entitlements_mdck_bundle_grants_both(monkeypatch):
    asyncio.run(_ent_bundle(monkeypatch))


async def _ent_bundle(monkeypatch):
    fake = _fake_db(monkeypatch, tenants=[
        {"id": "t1", "dataclean_billing": {"product": "bundle",
                                           "status": "active"}}])
    full = _seed_key(fake, "t1")
    actor = await ENT.get_entitlement_actor(_req(full))
    out = await ENT.read_entitlements(actor)
    assert out["tenant_id"] == "t1" and out["credential"] == "dataclean_key"
    assert out["entitlements"]["dataclean"]["active"] is True
    assert out["entitlements"]["dataclean"]["basis"] == "subscription"
    assert out["entitlements"]["security"]["active"] is True
    assert out["entitlements"]["security"]["granted_by"] == "bundle"
    assert out["catalog"]["dataclean"]["sellable"] is True


def test_entitlements_lapsed_subscription_reads_inactive(monkeypatch):
    asyncio.run(_ent_lapsed(monkeypatch))


async def _ent_lapsed(monkeypatch):
    fake = _fake_db(monkeypatch, tenants=[
        {"id": "t1", "dataclean_billing": {"product": "dataclean",
                                           "status": "canceled"}}])
    full = _seed_key(fake, "t1")
    actor = await ENT.get_entitlement_actor(_req(full, header="X-API-Key"))
    out = await ENT.read_entitlements(actor)
    # The lapsed workspace can still READ its state (no entitlement
    # gate on this door) — and the state honestly says inactive.
    dc = out["entitlements"]["dataclean"]
    assert dc["active"] is False and dc["basis"] == "subscription_inactive"
    assert dc["record_status"] == "canceled"
    assert out["entitlements"]["security"]["active"] is False


def test_entitlements_no_record_dataclean_grandfathered_only(monkeypatch):
    asyncio.run(_ent_no_record(monkeypatch))


async def _ent_no_record(monkeypatch):
    fake = _fake_db(monkeypatch, tenants=[{"id": "t1"}])
    full = _seed_key(fake, "t1")
    actor = await ENT.get_entitlement_actor(_req(full))
    out = await ENT.read_entitlements(actor)
    dc = out["entitlements"]["dataclean"]
    assert dc["active"] is True and dc["basis"] == "grandfathered"
    sec = out["entitlements"]["security"]
    assert sec["active"] is False and sec["basis"] == "no_record"


def test_entitlements_revoked_or_unknown_key_is_401(monkeypatch):
    asyncio.run(_ent_bad_keys(monkeypatch))


async def _ent_bad_keys(monkeypatch):
    fake = _fake_db(monkeypatch)
    full = _seed_key(fake, "t1", revoked_at="2026-11-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as ei:
        await ENT.get_entitlement_actor(_req(full))
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await ENT.get_entitlement_actor(_req("mdck_doesnotexist"))
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException) as ei:
        await ENT.get_entitlement_actor(_req(None))
    assert ei.value.status_code == 401


def test_entitlements_mae1_tenant_credential_reads_own_tenant(monkeypatch):
    asyncio.run(_ent_mae1_tenant(monkeypatch))


async def _ent_mae1_tenant(monkeypatch):
    fake = _fake_db(monkeypatch, tenants=[
        {"id": "tA", "dataclean_billing": {"product": "security",
                                           "status": "active"}},
        {"id": "tB", "dataclean_billing": {"product": "dataclean",
                                           "status": "canceled"}}])
    doc, full = IA.new_credential_doc(tenant_id="tA", created_by="op@test",
                                      label="AI Office link",
                                      expires_in_days=365)
    fake.integration_credentials.docs.append(doc)
    actor = await ENT.get_entitlement_actor(_req(full))
    out = await ENT.read_entitlements(actor)
    # The answer is bound to the credential's tenant — tB's (different)
    # state is never reachable through tA's credential.
    assert out["tenant_id"] == "tA"
    assert out["credential"] == "integration_credential"
    assert out["entitlements"]["security"]["active"] is True
    assert out["entitlements"]["dataclean"]["active"] is False


def test_entitlements_mae1_platform_credential_is_403(monkeypatch):
    asyncio.run(_ent_mae1_platform(monkeypatch))


async def _ent_mae1_platform(monkeypatch):
    fake = _fake_db(monkeypatch)
    doc, full = IA.new_platform_credential_doc(
        platform="momo", created_by="op@test", label="referral sync",
        expires_in_days=365)
    fake.integration_credentials.docs.append(doc)
    with pytest.raises(HTTPException) as ei:
        await ENT.get_entitlement_actor(_req(full))
    assert ei.value.status_code == 403  # cross-class: refused, per house rule


# ------------------------------------------- item 4: combo lead delete

USER1 = {"id": "u1", "tenant_id": "t1", "role": "owner",
         "email": "owner@test", "name": "Owner"}


def test_combo_delete_lead_works_and_audits(monkeypatch):
    asyncio.run(_combo_delete_ok(monkeypatch))


async def _combo_delete_ok(monkeypatch):
    fake = _fake_db(monkeypatch, combo_leads=[
        {"id": "L1", "tenant_id": "t1", "name": "Pat", "stage": "new"}])
    out = await COMBO.delete_lead("L1", dict(USER1))
    assert out == {"ok": True}
    assert fake.combo_leads.docs == []
    actions = [r["action"] for r in fake.audit_log.docs]
    assert "combo.lead.delete" in actions


def test_combo_delete_lead_stays_tenant_scoped(monkeypatch):
    asyncio.run(_combo_delete_scope(monkeypatch))


async def _combo_delete_scope(monkeypatch):
    fake = _fake_db(monkeypatch, combo_leads=[
        {"id": "L2", "tenant_id": "t2", "name": "Sam", "stage": "new"}])
    with pytest.raises(HTTPException) as ei:
        await COMBO.delete_lead("L2", dict(USER1))  # t1 user, t2 lead
    assert ei.value.status_code == 404
    assert len(fake.combo_leads.docs) == 1  # untouched


def test_combo_delete_missing_lead_is_404(monkeypatch):
    asyncio.run(_combo_delete_missing(monkeypatch))


async def _combo_delete_missing(monkeypatch):
    _fake_db(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await COMBO.delete_lead("nope", dict(USER1))
    assert ei.value.status_code == 404


def test_combo_add_then_delete_round_trip(monkeypatch):
    asyncio.run(_combo_round_trip(monkeypatch))


async def _combo_round_trip(monkeypatch):
    fake = _fake_db(monkeypatch)
    doc = await COMBO.add_lead(
        COMBO.LeadBody(name="Jo", source="web"), dict(USER1))
    assert len(fake.combo_leads.docs) == 1
    out = await COMBO.delete_lead(doc["id"], dict(USER1))
    assert out == {"ok": True}
    assert fake.combo_leads.docs == []
    assert await COMBO.list_leads(user=dict(USER1)) == []
