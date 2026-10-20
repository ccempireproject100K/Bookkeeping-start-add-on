"""Live bank feeds via Plaid: connect a bank once, transactions flow in automatically.

Writes into the same `bank_transactions` staging collection the CSV import uses,
so the Banking page (categorize / match / reconcile) needs no changes. Plaid
access tokens are Fernet-encrypted at rest and never returned by the API.

Setup (env vars; see repo-root PLAID_SETUP.md):
  PLAID_CLIENT_ID, PLAID_SECRET   - from the Plaid dashboard
  PLAID_ENV                        - sandbox | development | production (default: sandbox)
  TOKEN_ENCRYPTION_KEY             - Fernet key; generate with:
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Until the keys are set, GET /bankfeed/status reports configured=false and the
UI shows setup instructions instead of a Connect button.
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Optional

import requests
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, to_cents
from lib.auth import require
from lib.plans import require_feature
from lib.plaid_webhook import verify_plaid_webhook
from lib.coa import NAME_OF, ACCOUNT_TYPE_OF
from routers.banking import (
    accounts as _accounts, txns as _txns, _apply_rules,
    BANK_CODES, CARD_CODES,
)

router = APIRouter(prefix="/bankfeed", tags=["bankfeed"], dependencies=[Depends(require_feature("bank_feeds"))])
logger = logging.getLogger(__name__)

PLAID_BASES = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


class ExchangeIn(BaseModel):
    public_token: str = Field(min_length=1, max_length=500)


class SyncIn(BaseModel):
    connection_id: Optional[str] = ""


def connections(user):
    return Scoped(db.bank_connections, user["tenant_id"])


# ---------------- config / crypto ----------------
def _plaid_cfg():
    cid = os.environ.get("PLAID_CLIENT_ID", "").strip()
    sec = os.environ.get("PLAID_SECRET", "").strip()
    env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    return cid, sec, env, PLAID_BASES.get(env, PLAID_BASES["sandbox"])


def _require_plaid():
    cid, sec, env, base = _plaid_cfg()
    if not cid or not sec:
        raise HTTPException(
            503, "Plaid is not configured. Set PLAID_CLIENT_ID and PLAID_SECRET (see PLAID_SETUP.md).")
    return cid, sec, env, base


def _fernet() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise HTTPException(
            503, "TOKEN_ENCRYPTION_KEY is not set. Generate one and set it (see PLAID_SETUP.md).")
    try:
        return Fernet(key.encode())
    except Exception:
        raise HTTPException(500, "TOKEN_ENCRYPTION_KEY is not a valid Fernet key.")


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


async def _plaid_post(base: str, path: str, payload: dict, cid: str, sec: str) -> dict:
    def _do():
        return requests.post(base + path, json={**payload, "client_id": cid, "secret": sec}, timeout=30)

    r = await asyncio.to_thread(_do)
    if r.status_code != 200:
        detail = r.text[:300]
        try:
            detail = r.json().get("error_message", detail)
        except Exception:
            pass
        raise HTTPException(502, f"Plaid error: {detail}")
    return r.json()


# ---------------- txn staging (same collection/shape as the CSV import) ----------------
async def _insert_plaid_txn_doc(user, bank_account_id, plaid_txn_id, txn_date, description,
                               amount_cents, plaid_account_id, plaid_category, pending):
    """Insert one synced txn; dedupe on the Plaid transaction id."""
    dedupe_hash = hashlib.sha1(f"plaid|{plaid_txn_id}".encode()).hexdigest()
    if await _txns(user).find_one({"dedupe_hash": dedupe_hash}):
        return None
    rule = await _apply_rules(user, description)
    doc = {
        "id": new_id(), "bank_account_id": bank_account_id, "txn_date": txn_date,
        "description": description, "amount_cents": amount_cents, "dedupe_hash": dedupe_hash,
        "status": "unmatched", "category_code": rule["category_code"] if rule else None,
        "suggested_code": rule["category_code"] if rule else None,
        "entry_id": None, "matched_entry_id": None, "reconciled": False,
        "import_batch": "plaid", "source": "plaid",
        "plaid_account_id": plaid_account_id, "plaid_category": plaid_category,
        "pending": bool(pending), "created_at": now_iso(),
    }
    await _txns(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


async def _ensure_local_account(user, acct: dict, institution: str):
    """Map a Plaid account onto a local bank_accounts register (creates it once)."""
    existing = await _accounts(user).find_one({"plaid_account_id": acct.get("account_id")})
    if existing:
        return existing
    pool = CARD_CODES if (acct.get("type") or "").lower() == "credit" else BANK_CODES
    used = {a["gl_code"] for a in await _accounts(user).find_list()}
    gl = next((c for c in pool if c not in used), None)
    if not gl:
        raise HTTPException(409, "No free bank GL accounts left for Plaid accounts")
    doc = {
        "id": new_id(),
        "name": acct.get("name") or f"{institution} ••{acct.get('mask') or ''}".strip(),
        "kind": "credit_card" if (acct.get("type") or "").lower() == "credit" else "bank",
        "gl_code": gl, "gl_name": NAME_OF.get(gl, gl),
        "institution": institution, "plaid_account_id": acct.get("account_id"),
        "last_reconciled_date": None, "last_reconciled_balance_cents": 0,
        "created_at": now_iso(),
    }
    await _accounts(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- routes ----------------
@router.get("/status")
async def status(user: dict = Depends(require("ledger:read"))):
    cid, sec, env, _ = _plaid_cfg()
    conns = await connections(user).find_list(sort=[("created_at", -1)], limit=50)
    for c in conns:
        c.pop("access_token_enc", None)
    return {"configured": bool(cid and sec), "env": env, "connections": conns}


@router.post("/link-token")
async def link_token(user: dict = Depends(require("ops:manage"))):
    cid, sec, env, base = _require_plaid()
    data = await _plaid_post(base, "/link/token/create", {
        "client_name": "Ledger",
        "country_codes": ["US"],
        "language": "en",
        "user": {"client_user_id": f"{user['tenant_id']}:{user['id']}"},
        "products": ["transactions"],
    }, cid, sec)
    return {"link_token": data["link_token"], "env": env}


@router.post("/exchange")
async def exchange(body: ExchangeIn, user: dict = Depends(require("ops:manage"))):
    cid, sec, env, base = _require_plaid()
    f = _fernet()
    data = await _plaid_post(base, "/item/public_token/exchange",
                             {"public_token": body.public_token}, cid, sec)
    access_token, item_id = data["access_token"], data["item_id"]
    accts = await _plaid_post(base, "/accounts/get", {"access_token": access_token}, cid, sec)
    institution = ((accts.get("item") or {}).get("institution_id")) or ""
    plaid_accounts = [
        {"account_id": a.get("account_id"), "name": a.get("name"), "mask": a.get("mask"),
         "type": a.get("type"), "subtype": a.get("subtype")}
        for a in accts.get("accounts", [])
    ]
    conn_id = new_id()
    await connections(user).insert_one({
        "id": conn_id, "item_id": item_id,
        "access_token_enc": f.encrypt(access_token.encode()).decode(),
        "institution": institution, "accounts": plaid_accounts,
        "cursor": "", "last_sync": "", "created_at": now_iso(),
    })
    # Map every Plaid account onto a local bank register so synced txns land in
    # bank_transactions and flow through the normal categorize/reconcile UI.
    linked = []
    for a in plaid_accounts:
        acc = await _ensure_local_account(user, a, institution)
        linked.append({"plaid_account_id": a.get("account_id"), "bank_account_id": acc["id"]})
    await connections(user).update_one({"id": conn_id}, {"$set": {"linked": linked}})
    await _audit(user, "bankfeed.connect", "bank_connection", conn_id,
                 {"accounts": len(plaid_accounts)})
    return {"connection_id": conn_id, "accounts": linked}


async def sync_connections(user, connection_id: str = "") -> dict:
    """Sync transactions for one tenant's Plaid connection(s) — the shared
    core behind the manual /sync endpoint, the scheduler's periodic bank
    sync, and webhook-triggered syncs. Idempotent: the Plaid cursor only
    advances once a page is fully processed, and inserts dedupe on the
    Plaid transaction id."""
    cid, sec, env, base = _require_plaid()
    f = _fernet()
    q = {"id": connection_id} if connection_id else {}
    conns = await connections(user).find_list(q, limit=50)
    if not conns:
        raise HTTPException(404, "No bank connection found. Connect a bank first.")
    added = dups = 0
    for conn in conns:
        try:
            access_token = f.decrypt(conn["access_token_enc"].encode()).decode()
        except Exception:
            raise HTTPException(
                500, "Could not decrypt a stored bank token. Check TOKEN_ENCRYPTION_KEY.")
        link = {l["plaid_account_id"]: l["bank_account_id"] for l in (conn.get("linked") or [])}
        cursor = conn.get("cursor") or ""
        while True:
            data = await _plaid_post(base, "/transactions/sync",
                                     {"access_token": access_token, "cursor": cursor, "count": 500},
                                     cid, sec)
            for t in data.get("added", []) + data.get("modified", []):
                # Plaid convention: positive amount = money OUT. Ours: positive = money IN.
                amt = to_cents(-float(t.get("amount") or 0))
                d = (t.get("date") or "")[:10]
                desc = (t.get("merchant_name") or t.get("name") or "").strip() or "Plaid transaction"
                bank_account_id = link.get(t.get("account_id"))
                if not bank_account_id:
                    continue  # account was removed or never linked
                pfc = t.get("personal_finance_category") or {}
                doc = await _insert_plaid_txn_doc(
                    user, bank_account_id, t.get("transaction_id"), d, desc, amt,
                    t.get("account_id"),
                    pfc.get("primary") if isinstance(pfc, dict) else None,
                    t.get("pending"))
                if doc:
                    added += 1
                else:
                    dups += 1
            cursor = data.get("next_cursor", cursor)
            if not data.get("has_more"):
                break
        await connections(user).update_one(
            {"id": conn["id"]}, {"$set": {"cursor": cursor, "last_sync": now_iso()}})
    await _audit(user, "bankfeed.sync", "tenant", user["tenant_id"],
                 {"added": added, "duplicates": dups})
    return {"added": added, "duplicates": dups}


@router.post("/sync")
async def sync(body: SyncIn, user: dict = Depends(require("ops:manage"))):
    return await sync_connections(user, body.connection_id or "")


async def sync_item(item_id: str) -> dict:
    """Sync the tenant that owns a Plaid item (webhook / scheduler entry
    point). Honours the same plan gate the router enforces: a tenant whose
    plan no longer includes bank_feeds is skipped, not synced."""
    conn = await db.bank_connections.find_one({"item_id": item_id}, {"_id": 0})
    if not conn:
        return {"synced": False, "reason": "unknown_item"}
    tenant = await db.tenants.find_one({"id": conn["tenant_id"]}, {"_id": 0})
    from lib.plans import effective_plan, plan_includes
    if not plan_includes(effective_plan(tenant or {}), "bank_feeds"):
        return {"synced": False, "reason": "plan_gate"}
    user = {"id": "plaid-sync", "tenant_id": conn["tenant_id"],
            "email": "plaid-sync@system", "name": "Plaid sync", "role": "system"}
    res = await sync_connections(user)
    return {"synced": True, **res}


_pending_syncs: set = set()


async def _webhook_sync(item_id: str):
    try:
        res = await sync_item(item_id)
        logger.info("bankfeed webhook sync for item %s: %s", item_id, res)
    except Exception:
        logger.exception("bankfeed webhook sync failed for item %s", item_id)


def _kick_item_sync(item_id: str):
    """Fire-and-forget a webhook-triggered sync, keeping a strong reference
    so the task isn't garbage-collected mid-flight."""
    task = asyncio.create_task(_webhook_sync(item_id))
    _pending_syncs.add(task)
    task.add_done_callback(_pending_syncs.discard)
    return task


@router.post("/webhook")
async def webhook(request: Request):
    """Plaid transaction webhooks (TRANSACTIONS SYNC_UPDATES etc.). Logged
    here, and a TRANSACTIONS webhook immediately kicks a background sync for
    the owning tenant (plan-gated; the scheduler's periodic sweep remains as
    a backstop). No session auth: Plaid calls this server-to-server, so
    authenticity is established via the `Plaid-Verification` JWT signature
    instead (see lib/plaid_webhook.py).

    The signature is computed over the RAW request body, so the body is read
    as bytes first and only parsed to JSON after verification. Verification is
    skipped gracefully when Plaid is not configured (nothing is configured to
    send webhooks then); any verification failure is a hard 403 — Plaid
    redelivers, so a key-fetch outage only delays, never loses, delivery."""
    raw = await request.body()
    cid, sec, _env, base = _plaid_cfg()
    if cid and sec:
        ok, reason = await asyncio.to_thread(
            verify_plaid_webhook, raw, request.headers, cid, sec, base)
        if not ok:
            logger.warning("bankfeed webhook: rejected (%s)", reason)
            raise HTTPException(403, "Webhook signature verification failed")
    else:
        logger.warning(
            "bankfeed webhook: Plaid not configured; skipping signature verification")
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    await db.bank_webhooks.insert_one({
        "webhook_type": body.get("webhook_type"), "webhook_code": body.get("webhook_code"),
        "item_id": body.get("item_id"), "received_at": now_iso(), "raw": str(body)[:2000]})
    if body.get("webhook_type") == "TRANSACTIONS" and body.get("item_id"):
        _kick_item_sync(body["item_id"])
    return {"ok": True}


@router.delete("/connection/{conn_id}")
async def disconnect(conn_id: str, user: dict = Depends(require("ops:manage"))):
    conn = await connections(user).find_one({"id": conn_id})
    if not conn:
        raise HTTPException(404, "Connection not found")
    try:  # best-effort: revoke at Plaid too
        cid, sec, env, base = _require_plaid()
        f = _fernet()
        await _plaid_post(base, "/item/remove",
                          {"access_token": f.decrypt(conn["access_token_enc"].encode()).decode()},
                          cid, sec)
    except Exception:
        logger.warning("bankfeed disconnect: Plaid /item/remove failed (best-effort)", exc_info=True)
    await connections(user).c.delete_one({"tenant_id": user["tenant_id"], "id": conn_id})
    await _audit(user, "bankfeed.disconnect", "bank_connection", conn_id, {})
    return {"ok": True}
