"""Financial-sync fix track (audit 2026-10-17) — books side.

Regression tests for the new sync-surface payment-refund endpoint
POST /api/invoices/{invoice_id}/refund. Before this branch the
endpoint did not exist: Momo processed Stripe refunds locally and
books kept showing the invoice as paid (Track E, Confirmed Broken #2).
The only refund surface was the owner-JWT customer-CREDIT refund,
which settles a credit balance (2210) and cannot reflect a payment
refund against an invoice.

Same method as Track A: the real router functions against Track A's
FakeDB (unique-index emulation), actors resolved through the real
get_sync_actor. Synthetic data only.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from test_integration_sync import (
    ACCOUNT_CODES, FakeCollection, FakeDB, T1, T2, bearer, make_user, req,
)

import lib.auth as auth_mod
import lib.audit as audit_mod
import lib.idempotency as idem
import lib.integration_auth as iauth
import routers.customers as customers_r
import routers.integrations as integrations_r
import routers.invoices as invoices_r


@pytest.fixture()
def fdb(monkeypatch):
    fake = FakeDB()
    for tenant in (T1, T2):
        fake._colls.setdefault("accounts", FakeCollection()).docs.extend(
            {"id": f"acct-{tenant}-{c}", "tenant_id": tenant, "code": c,
             "archived": False} for c in ACCOUNT_CODES)
        fake._colls.setdefault("tenants", FakeCollection()).docs.append(
            {"id": tenant, "name": f"Shop {tenant}", "settings": {}})
        fake._colls.setdefault("users", FakeCollection()).docs.append(
            make_user(tenant))
    for mod in (customers_r, invoices_r, integrations_r, idem, iauth,
                auth_mod, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def issue_credential(fdb, tenant=T1):
    owner = make_user(tenant)

    async def _go():
        return await integrations_r.issue_credential(
            integrations_r.IssueBody(label="Momo sync"), user=owner)
    return run(_go())


def sync_actor_for(credential, path="/api/invoices"):
    async def _go():
        return await iauth.get_sync_actor(req(bearer(credential), path))
    return run(_go())


def issue_invoice(actor, key=None, amount=100.0, client="Acme Co"):
    headers = {"Idempotency-Key": key} if key else {}
    body = invoices_r.InvoiceBody(
        client_name=client,
        lines=[invoices_r.InvLine(name="Service", amount=amount)], tax=0)

    async def _go():
        return await invoices_r.issue_invoice(
            body, user=actor, request=req(headers, "/api/invoices"))
    return run(_go())


def pay_invoice(actor, invoice_id, key=None, amount=100.0, method="card"):
    headers = {"Idempotency-Key": key} if key else {}
    body = invoices_r.PayBody(payment_method=method, amount=amount)

    async def _go():
        return await invoices_r.pay_invoice(
            invoice_id, body, user=actor,
            request=req(headers, f"/api/invoices/{invoice_id}/pay"))
    return run(_go())


def refund_invoice(actor, invoice_id, key=None, amount=100.0, method="card"):
    headers = {"Idempotency-Key": key} if key else {}
    body = invoices_r.InvoiceRefundBody(amount=amount, payment_method=method)

    async def _go():
        return await invoices_r.refund_invoice_payment(
            invoice_id, body, user=actor,
            request=req(headers, f"/api/invoices/{invoice_id}/refund"))
    return run(_go())


def docs(fdb, name, tenant=None):
    out = fdb._colls[name].docs
    if tenant:
        out = [d for d in out if d.get("tenant_id") == tenant]
    return out


def balances(fdb, tenant):
    bal = {}
    for ln in docs(fdb, "journal_lines", tenant):
        code = ln.get("account_code")
        bal[code] = bal.get(code, 0) + int(ln.get("debit") or 0) \
            - int(ln.get("credit") or 0)
    return bal


def paid_invoice(fdb, amount=100.0):
    """Issue + fully pay an invoice through the sync surface."""
    actor = sync_actor_for(issue_credential(fdb)["credential"])
    inv = issue_invoice(actor, key="fx-inv", amount=amount)
    res = pay_invoice(actor, inv["id"], key="fx-pay", amount=amount)
    assert res["status"] == "paid"
    return actor, inv["id"]


class TestSyncRefundEndpoint:
    def test_full_refund_reopens_balance(self, fdb):
        actor, inv_id = paid_invoice(fdb)
        out = refund_invoice(actor, inv_id, key="fx-ref-1", amount=100.0)
        assert out["paid_cents"] == 0
        assert out["balance_cents"] == 10000
        assert out["status"] == "issued"
        assert out["refund_cents"] == 10000
        assert out["entry_number"]
        stored = docs(fdb, "invoices", T1)[0]
        assert stored["paid_cents"] == 0 and stored["status"] == "issued"
        refunds = [p for p in stored["payments"] if p.get("kind") == "refund"]
        assert len(refunds) == 1 and refunds[0]["amount_cents"] == -10000
        entries = [e for e in docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_refund"]
        assert len(entries) == 1
        bal = balances(fdb, T1)
        assert sum(bal.values()) == 0, bal          # books stay balanced
        assert bal.get("1200", 0) == 10000, bal     # AR reopened in full
        assert bal.get("1050", 0) == 0, bal         # card cash in, then out
        assert bal.get("4020", 0) == -10000, bal    # revenue untouched

    def test_partial_refund_adjusts(self, fdb):
        actor, inv_id = paid_invoice(fdb)
        out = refund_invoice(actor, inv_id, key="fx-ref-2", amount=30.0)
        assert out["paid_cents"] == 7000
        assert out["balance_cents"] == 3000
        assert out["status"] == "partial"
        bal = balances(fdb, T1)
        assert sum(bal.values()) == 0, bal
        assert bal.get("1200", 0) == 3000, bal      # only $30 owed again
        assert bal.get("1050", 0) == 7000, bal      # $70 of the cash stays

    def test_duplicate_refund_delivery_posts_exactly_once(self, fdb):
        actor, inv_id = paid_invoice(fdb)
        first = refund_invoice(actor, inv_id, key="fx-ref-3", amount=40.0)
        second = refund_invoice(actor, inv_id, key="fx-ref-3", amount=40.0)
        replayed = json.loads(bytes(second.body)) \
            if hasattr(second, "body") else second
        assert replayed == first
        entries = [e for e in docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_refund"]
        assert len(entries) == 1
        stored = docs(fdb, "invoices", T1)[0]
        refunds = [p for p in stored["payments"] if p.get("kind") == "refund"]
        assert len(refunds) == 1
        assert stored["paid_cents"] == 6000         # reduced exactly once

    def test_refund_crash_window_stamp_adopted(self, fdb):
        """Outcome record lost after the refund landed: the retry with
        the same key is answered from the payment stamp, not re-posted."""
        actor, inv_id = paid_invoice(fdb)
        first = refund_invoice(actor, inv_id, key="fx-ref-4", amount=25.0)
        fdb._colls["idempotency_records"].docs = [
            r for r in fdb._colls["idempotency_records"].docs
            if r.get("key") != "fx-ref-4"]
        second = refund_invoice(actor, inv_id, key="fx-ref-4", amount=25.0)
        assert second["entry_number"] == first["entry_number"]
        entries = [e for e in docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_refund"]
        assert len(entries) == 1

    def test_refund_exceeding_paid_is_422(self, fdb):
        actor, inv_id = paid_invoice(fdb)
        with pytest.raises(HTTPException) as ei:
            refund_invoice(actor, inv_id, key="fx-ref-5", amount=150.0)
        assert ei.value.status_code == 422
        entries = [e for e in docs(fdb, "journal_entries", T1)
                   if e.get("source") == "invoice_refund"]
        assert entries == []

    def test_refund_on_unpaid_invoice_is_422(self, fdb):
        actor = sync_actor_for(issue_credential(fdb)["credential"])
        inv = issue_invoice(actor, key="fx-inv-6")
        with pytest.raises(HTTPException) as ei:
            refund_invoice(actor, inv["id"], key="fx-ref-6", amount=10.0)
        assert ei.value.status_code == 422

    def test_same_key_different_amount_is_409(self, fdb):
        actor, inv_id = paid_invoice(fdb)
        refund_invoice(actor, inv_id, key="fx-ref-7", amount=10.0)
        with pytest.raises(HTTPException) as ei:
            refund_invoice(actor, inv_id, key="fx-ref-7", amount=20.0)
        assert ei.value.status_code == 409

    def test_cross_tenant_refund_is_404(self, fdb):
        actor1, inv_id = paid_invoice(fdb)
        actor2 = sync_actor_for(issue_credential(fdb, tenant=T2)["credential"],
                                path="/api/invoices")
        with pytest.raises(HTTPException) as ei:
            refund_invoice(actor2, inv_id, key="fx-ref-8", amount=10.0)
        assert ei.value.status_code == 404
        stored = docs(fdb, "invoices", T1)[0]
        assert stored["paid_cents"] == 10000        # untouched

    def test_owner_jwt_actor_can_refund(self, fdb):
        """The JWT path of get_sync_actor (ops:manage user) may refund
        too — same endpoint, same enforcement, per the contract table's
        two-auth-path design."""
        actor, inv_id = paid_invoice(fdb)
        owner = make_user(T1)
        token = auth_mod.create_access_token(owner["id"], owner["email"], 0)

        async def _go():
            return await iauth.get_sync_actor(
                req(bearer(token), "/api/invoices"))
        jwt_actor = run(_go())
        out = refund_invoice(jwt_actor, inv_id, key="fx-ref-9", amount=100.0)
        assert out["paid_cents"] == 0
