"""Empire referral ledger — HTTP surface (B1–B3).

Three audiences:

* Reporting products (TC today, Momo later) authenticate with a
  PLATFORM-bound mae1 credential (`referrals:sync` scope) and use the
  ingest + read-back endpoints. A product may only speak for itself:
  the payload's product must match the credential's platform.
* The operator (platform owner, `ledger:admin`) mints platform
  credentials, reads balances, and runs payouts: the monthly Stripe
  Connect batch, manual recording (the backup rail), and the Melio
  queue for the two off-Stripe preferences (bank transfer = Melio
  ACH, paper check = Melio mailed check, $2.00 fee).
* Books' own subscription payments arrive through the checkout
  build's `referral_payment_events` outbox, drained by an operator
  action (and, later, a scheduler).

All computation lives in lib/referrals.py; this module is transport,
auth, and refusal translation (ReferralError -> 409).
"""
import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from db import db
from lib import referral_payouts as RP
from lib import referrals as R
from lib.audit import audit
from lib.auth import require
from lib.base import new_id, now_iso
from lib.integration_auth import (
    DEFAULT_EXPIRES_DAYS,
    MAX_EXPIRES_DAYS,
    PLATFORMS,
    format_credential,
    get_platform_actor,
    hash_secret,
    mint_secret,
    new_platform_credential_doc,
    public_view,
)

router = APIRouter(prefix="/referrals", tags=["referrals"])
logger = logging.getLogger(__name__)


def _conflict(err: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(err))


def _product_for(actor: dict, body_product: Optional[str]) -> str:
    product = body_product or actor.get("platform")
    if product != actor.get("platform"):
        raise HTTPException(
            status_code=403,
            detail="A platform credential may only report for its own "
                   "product")
    if product not in R.PRODUCTS:
        raise HTTPException(status_code=422, detail="Unknown product")
    return product


async def _person_for_readback(actor: dict, account_id: str = None,
                               email: str = None):
    """Resolve the member a platform credential is asking about.

    Account ids resolve product-scoped by construction. The email
    fallback is scoped too (security audit 2026-10-26, N-3): it
    resolves only when the person holds an account/attribution on the
    CALLER's product. An out-of-scope email gets the same 404 as an
    unknown one — no cross-product enumeration oracle."""
    person = None
    if account_id:
        person = await R.person_by_account(actor["platform"], account_id)
    if person is None and email:
        candidate = await R.person_by_email(email)
        if candidate is not None and await R.person_in_product(
                candidate, actor["platform"]):
            person = candidate
    if person is None:
        raise HTTPException(status_code=404,
                            detail="No referral member found")
    return person


# ------------------------------------------------------------- ingest (B1)

class AccountIn(BaseModel):
    product: Optional[str] = None
    account_id: str
    email: Optional[str] = None
    referral_code: Optional[str] = None
    tenant_id: Optional[str] = None
    standing: Optional[dict] = None
    at: Optional[str] = None


@router.post("/ingest/accounts")
async def ingest_account(body: AccountIn,
                         actor: dict = Depends(get_platform_actor)):
    """Register/attest a product account. Attribution is recorded
    once per account (first code wins); repeat calls refresh the
    account's standing attestation."""
    product = _product_for(actor, body.product)
    return await R.apply_attribution(
        product=product, account_id=body.account_id, email=body.email,
        referral_code=body.referral_code, tenant_id=body.tenant_id,
        standing=body.standing, at=body.at)


class PaymentIn(BaseModel):
    product: Optional[str] = None
    account_id: str
    payment_id: str
    amount_cents: int
    currency: str = "usd"
    paid_at: str
    charge: Optional[str] = None


@router.post("/ingest/payments")
async def ingest_payment(body: PaymentIn,
                         actor: dict = Depends(get_platform_actor)):
    product = _product_for(actor, body.product)
    payload = body.model_dump()
    payload["product"] = product
    _, dup = await R.store_event("payment", payload)
    if dup:
        return {"ok": True, "duplicate": True}
    result = await R.apply_payment(
        product=product, account_id=body.account_id,
        payment_id=body.payment_id, amount_cents=body.amount_cents,
        currency=body.currency, paid_at=body.paid_at, charge=body.charge)
    return {"ok": True, **result}


class RefundIn(BaseModel):
    product: Optional[str] = None
    account_id: Optional[str] = None
    payment_id: str
    refunded_delta_cents: int
    refunded_total_cents: int


@router.post("/ingest/refunds")
async def ingest_refund(body: RefundIn,
                        actor: dict = Depends(get_platform_actor)):
    """Refund deltas (Addendum A convention): the body carries the
    delta being delivered and the cumulative refunded total the
    dedupe key derives from; the ledger recomputes from the total."""
    product = _product_for(actor, body.product)
    payload = body.model_dump()
    payload["product"] = product
    _, dup = await R.store_event("refund", payload)
    if dup:
        return {"ok": True, "duplicate": True}
    result = await R.apply_refund(
        product=product, payment_id=body.payment_id,
        refunded_delta_cents=body.refunded_delta_cents,
        refunded_total_cents=body.refunded_total_cents)
    return {"ok": True, **result}


class DisputeIn(BaseModel):
    product: Optional[str] = None
    dispute_id: str
    payment_id: Optional[str] = None
    charge: Optional[str] = None
    phase: str  # opened | closed
    outcome: Optional[str] = None  # won | lost (on closed)


@router.post("/ingest/disputes")
async def ingest_dispute(body: DisputeIn,
                         actor: dict = Depends(get_platform_actor)):
    product = _product_for(actor, body.product)
    if body.phase not in ("opened", "closed"):
        raise HTTPException(status_code=422,
                            detail="phase must be opened or closed")
    kind = "dispute_opened" if body.phase == "opened" else "dispute_closed"
    payload = body.model_dump()
    payload["product"] = product
    _, dup = await R.store_event(kind, payload)
    if dup:
        return {"ok": True, "duplicate": True}
    result = await R.apply_dispute(
        product=product, dispute_id=body.dispute_id,
        payment_id=body.payment_id, charge=body.charge,
        phase=body.phase, outcome=body.outcome)
    return {"ok": True, **result}


# ---------------------------------------------------- read-back + preference

@router.get("/summary")
async def readback_summary(account_id: str = None, email: str = None,
                           actor: dict = Depends(get_platform_actor)):
    """A referrer's per-site summary + empire totals, for the
    product's member-facing referral screen."""
    person = await _person_for_readback(actor, account_id, email)
    return await R.empire_summary(person)


@router.get("/payout-options")
async def payout_options(actor: dict = Depends(get_platform_actor)):
    """The three payout preferences and their terms — the paper-check
    fee disclosure lives here so every product shows the same words
    before a member chooses."""
    return {"preferences": [
        {"id": "stripe", "rail": "stripe_connect", "fee_cents": 0,
         "label": "Stripe",
         "note": "Paid to your connected Stripe account in the "
                 "monthly batch."},
        {"id": "bank_transfer", "rail": "melio_ach", "fee_cents": 0,
         "label": "Bank transfer (Melio)",
         "note": "Sent by ACH through Melio. You claim the payment "
                 "in Melio's flow and enter your own bank details — "
                 "we never hold them. Basic ACH is the free, slower "
                 "tier."},
        {"id": "paper_check", "rail": "paper_check_melio",
         "fee_cents": R.PAPER_CHECK_FEE_CENTS,
         "label": "Paper check by mail (Melio)",
         "note": "A paper check is mailed to you through Melio. "
                 "A $2.00 fee is deducted from each paper-check "
                 "payout."},
    ]}


class PreferenceIn(BaseModel):
    account_id: Optional[str] = None
    email: Optional[str] = None
    preference: str
    fee_acknowledged: bool = False


@router.post("/preference")
async def set_preference(body: PreferenceIn,
                         actor: dict = Depends(get_platform_actor)):
    if body.preference not in R.PAYOUT_PREFERENCES:
        raise HTTPException(status_code=422,
                            detail="Unknown payout preference")
    if body.preference == "paper_check" and not body.fee_acknowledged:
        raise HTTPException(
            status_code=422,
            detail="Paper check carries a $2.00 fee deducted from each "
                   "payout. The member must see and acknowledge that "
                   "fee before this preference can be set.")
    # Same scoping as read-back (audit N-3): a platform credential may
    # set a preference only for a member of its OWN product — resolved
    # by its product-scoped account id, or by email when the person
    # holds an account/attribution on the caller's product. An
    # out-of-scope or unknown email is a 404, and this path never
    # creates a person (the old ensure_person fallback did, for any
    # email any credential supplied).
    person = None
    if body.account_id:
        person = await R.person_by_account(actor["platform"],
                                           body.account_id)
    if person is None and body.email:
        candidate = await R.person_by_email(body.email)
        if candidate is not None and await R.person_in_product(
                candidate, actor["platform"]):
            person = candidate
    if person is None:
        raise HTTPException(status_code=404,
                            detail="No referral member found")
    await db.empire_persons.update_one(
        {"id": person["id"]},
        {"$set": {"payout_preference": body.preference}})
    return {"empire_id": person["id"],
            "payout_preference": body.preference,
            "paper_check_fee_cents": R.PAPER_CHECK_FEE_CENTS}


# ------------------------------------------- operator: platform credentials

class PlatformIssueBody(BaseModel):
    platform: str
    label: Optional[str] = None
    expires_in_days: Optional[int] = DEFAULT_EXPIRES_DAYS


@router.post("/admin/platform-credentials")
async def issue_platform_credential(
        body: PlatformIssueBody,
        user: dict = Depends(require("ledger:admin"))):
    """Mint a platform-bound referrals:sync credential for a
    reporting product. Operator-only: these credentials speak for a
    whole product, not a workspace. Secret shown ONCE."""
    if body.platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail="Unknown platform")
    days = body.expires_in_days or DEFAULT_EXPIRES_DAYS
    if days < 1 or days > MAX_EXPIRES_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"expires_in_days must be between 1 and {MAX_EXPIRES_DAYS}")
    doc, full = new_platform_credential_doc(
        platform=body.platform, created_by=user.get("email", ""),
        label=body.label or "", expires_in_days=days)
    await db.integration_credentials.insert_one(doc)
    await audit(user, "referral.platform_credential_issued",
                "integration_credential", doc["id"],
                {"client_id": doc["client_id"], "platform": body.platform})
    out = public_view(doc)
    out["credential"] = full
    return out


@router.get("/admin/platform-credentials")
async def list_platform_credentials(
        user: dict = Depends(require("ledger:admin"))):
    rows = await db.integration_credentials.find(
        {"kind": "platform"}, {"_id": 0}).sort(
            "created_at", -1).to_list(100)
    return [public_view(r) for r in rows]


async def _find_platform_credential(cred_id: str):
    cred = await db.integration_credentials.find_one(
        {"id": cred_id, "kind": "platform"})
    if not cred:
        raise HTTPException(status_code=404,
                            detail="Platform credential not found")
    return cred


@router.post("/admin/platform-credentials/{cred_id}/revoke")
async def revoke_platform_credential(
        cred_id: str, user: dict = Depends(require("ledger:admin"))):
    cred = await _find_platform_credential(cred_id)
    if cred.get("revoked_at"):
        return {"ok": True, "id": cred_id,
                "revoked_at": cred["revoked_at"]}
    await db.integration_credentials.update_one(
        {"id": cred_id}, {"$set": {"revoked_at": now_iso()}})
    await audit(user, "referral.platform_credential_revoked",
                "integration_credential", cred_id,
                {"client_id": cred.get("client_id")})
    return {"ok": True, "id": cred_id, "revoked_at": now_iso()}


@router.post("/admin/platform-credentials/{cred_id}/rotate")
async def rotate_platform_credential(
        cred_id: str, user: dict = Depends(require("ledger:admin"))):
    cred = await _find_platform_credential(cred_id)
    if cred.get("revoked_at"):
        raise HTTPException(status_code=409,
                            detail="Cannot rotate a revoked credential")
    secret = mint_secret()
    full = format_credential(cred["client_id"], secret)
    await db.integration_credentials.update_one(
        {"id": cred_id},
        {"$set": {"secret_hash": hash_secret(secret),
                  "rotated_at": now_iso()}})
    await audit(user, "referral.platform_credential_rotated",
                "integration_credential", cred_id,
                {"client_id": cred.get("client_id")})
    out = public_view(cred)
    out["rotated_at"] = now_iso()
    out["credential"] = full
    return out


# ------------------------------------------------- operator: balances/views

async def _display(person: dict) -> dict:
    name = None
    acct = next((a for a in person.get("product_accounts", [])
                 if a.get("product") == "books"), None)
    if acct:
        owner = await db.users.find_one(
            {"tenant_id": acct.get("tenant_id") or acct.get("account_id"),
             "role": "owner"}, {"_id": 0, "name": 1})
        name = (owner or {}).get("name")
    emails = person.get("emails", [])
    return {"empire_id": person["id"], "code": person.get("code"),
            "email": emails[0] if emails else None, "name": name,
            "payout_preference": person.get("payout_preference", "stripe")}


@router.get("/admin/balances")
async def admin_balances(user: dict = Depends(require("ledger:admin"))):
    """Every referrer with referral activity: per-site pots with
    locked/frozen state, empire payable, and payout preference."""
    ids = set(await db.referral_earnings.distinct("referrer_id"))
    async for att in db.referral_attributions.find(
            {"referrer_id": {"$ne": None}}, {"referrer_id": 1}):
        ids.add(att["referrer_id"])
    rows = []
    for rid in sorted(ids):
        person = await R.person_by_id(rid)
        if person is None:
            continue
        summary = await R.empire_summary(person)
        row = await _display(person)
        row.update({"sites": summary["sites"],
                    "payable": summary["payable"],
                    "frozen": summary["frozen"],
                    "under_review": summary["under_review"],
                    "eligible": summary["eligible"]})
        rows.append(row)
    return sorted(rows, key=lambda r: (-r["payable"], r["email"] or ""))


@router.get("/admin/payouts")
async def admin_payouts(referrer_id: str = None, product: str = None,
                        user: dict = Depends(require("ledger:admin"))):
    q = {}
    if referrer_id:
        q["referrer_id"] = referrer_id
    if product:
        q["product"] = product
    rows = await db.referral_payouts.find(q, {"_id": 0}).sort(
        "at", -1).to_list(500)
    return rows


@router.post("/admin/referrers/{person_id}/clear-review")
async def clear_review(person_id: str,
                       user: dict = Depends(require("ledger:admin"))):
    result = await db.empire_persons.update_one(
        {"id": person_id, "review": True},
        {"$set": {"review": False, "review_cleared_by": user.get("id"),
                  "review_cleared_at": now_iso()}})
    if not getattr(result, "matched_count", 0):
        raise HTTPException(status_code=404, detail="Not under review")
    await audit(user, "referral.review_cleared", "empire_person",
                person_id, {})
    return {"under_review": False}


@router.post("/admin/drain-books-outbox")
async def drain_books_outbox(
        user: dict = Depends(require("ledger:admin"))):
    result = await R.drain_books_outbox()
    await audit(user, "referral.books_outbox_drained", "referral_event",
                "", result)
    return result


class TcImportIn(BaseModel):
    members: List[dict] = []
    earnings: List[dict] = []
    payouts: List[dict] = []
    adjustments: List[dict] = []
    disputes: List[dict] = []


@router.post("/admin/migration/tc-import")
async def tc_import(body: TcImportIn,
                    user: dict = Depends(require("ledger:admin"))):
    """B2 migration path: import Trade Circle's exported referral
    records as trade_circle-site history. Idempotent — a re-import
    skips everything already landed."""
    counts = await R.import_tc_export(body.model_dump())
    await audit(user, "referral.tc_import", "empire_person", "", counts)
    return counts


# ------------------------------------------------- operator: manual payout

class ManualPayoutIn(BaseModel):
    referrer_id: str
    product: str
    note: str  # how it was paid, e.g. "Zelle 10/24"


@router.post("/admin/payouts/manual")
async def manual_payout(body: ManualPayoutIn,
                        user: dict = Depends(require("ledger:admin"))):
    """The backup rail: record a payout made outside the platform.
    Locked and frozen pots are refused with the reason, every rail,
    no exceptions."""
    if body.product not in R.PRODUCTS:
        raise HTTPException(status_code=422, detail="Unknown product")
    try:
        payout = await R.record_manual_payout(
            body.referrer_id, body.product, note=body.note,
            by=user.get("email", ""))
    except R.ReferralError as e:
        raise _conflict(e)
    await audit(user, "referral.payout_recorded", "empire_person",
                body.referrer_id,
                {"payout_id": payout["id"], "rail": "manual",
                 "product": body.product, "cents": payout["net_cents"]})
    return {"payout_id": payout["id"], "cents": payout["net_cents"],
            "rail": "manual"}


# --------------------------------------------- operator: Stripe batch (B3)

class BatchIn(BaseModel):
    period: Optional[str] = None  # YYYY-MM; defaults to this month
    referrer_ids: Optional[List[str]] = None


@router.post("/admin/payouts/batch")
async def stripe_batch(body: BatchIn,
                       user: dict = Depends(require("ledger:admin"))):
    """The monthly Stripe Connect batch: pay every unlocked,
    unfrozen stripe-preference site pot to the member's connected
    account. Idempotent per (referrer, site, period): a re-run
    reports already-paid pots and never transfers twice (the claim
    record plus Stripe's own idempotency key). Melio-preference
    members are skipped — they run through the Melio queue — and
    members without a payable Connect account are skipped and
    listed, payable via the manual path."""
    if not RP.configured():
        raise HTTPException(
            status_code=503,
            detail="Stripe payouts are not configured. Set "
                   "STRIPE_SECRET_KEY to enable them (see "
                   "REFERRAL_HOST_BUILD.md).")
    from datetime import datetime, timezone
    period = body.period or datetime.now(timezone.utc).strftime("%Y-%m")
    paid, skipped, failed = [], [], []
    ids = set(await db.referral_earnings.distinct("referrer_id"))
    if body.referrer_ids:
        ids &= set(body.referrer_ids)
    for rid in sorted(ids):
        person = await R.person_by_id(rid)
        if person is None:
            continue
        preference = person.get("payout_preference", "stripe")
        for product in R.PRODUCTS:
            s = await R.site_summary(rid, product)
            if s["earned_cents"] <= 0 and s["payable"] <= 0:
                continue
            entry = {"referrer_id": rid, "product": product}
            if preference != "stripe":
                skipped.append({**entry, "reason":
                                f"payout preference is {preference} "
                                f"(Melio queue)"})
                continue
            if s["frozen"]:
                skipped.append({**entry, "reason": "frozen"})
                continue
            if s["locked"]:
                skipped.append({**entry, "reason":
                                f"locked ({s['paying_referrals']} of "
                                f"{R.UNLOCK_REFERRALS} paying referrals "
                                f"on {product})"})
                continue
            if s["payable"] <= 0:
                continue
            dest, why = await RP.connect_destination(db, person)
            if dest is None:
                skipped.append({**entry, "reason":
                                f"no payable Stripe account: {why}"})
                continue
            batch_key = f"stripe-batch:{period}:{rid}:{product}"
            claim = await db.referral_payouts.find_one(
                {"batch_key": batch_key})
            if claim and claim.get("status") == "recorded":
                skipped.append({**entry, "reason": "already paid",
                                "payout_id": claim["id"]})
                continue
            if claim is None:
                claim = {
                    "id": new_id(), "referrer_id": rid,
                    "product": product, "period": period,
                    "rail": "stripe_connect", "status": "processing",
                    "gross_cents": s["payable"], "fee_cents": 0,
                    "net_cents": s["payable"], "currency": "usd",
                    "reference": None, "note": "Monthly Stripe batch",
                    "by": user.get("email", ""), "at": now_iso(),
                    "batch_key": batch_key,
                }
                await db.referral_payouts.insert_one(claim)
            try:
                transfer = RP.create_transfer(
                    destination=dest, amount_cents=s["payable"],
                    currency="usd", idempotency_key=batch_key,
                    description=f"Empire referral payout {period} "
                                f"({product})")
            except Exception as e:
                await db.referral_payouts.update_one(
                    {"id": claim["id"]},
                    {"$set": {"status": "failed",
                              "error": str(e)[:300],
                              "updated_at": now_iso()}})
                logger.warning("referral batch transfer failed "
                               "referrer=%s product=%s: %s",
                               rid, product, type(e).__name__)
                failed.append({**entry, "error": "Stripe transfer failed"})
                continue
            await db.referral_payouts.update_one(
                {"id": claim["id"]},
                {"$set": {"status": "recorded",
                          "reference": transfer.get("id"),
                          "gross_cents": s["payable"],
                          "net_cents": s["payable"],
                          "at": now_iso()}})
            await R.mark_cleared_paid(rid, product, claim["id"])
            paid.append({**entry, "cents": s["payable"],
                         "payout_id": claim["id"],
                         "transfer_id": transfer.get("id")})
    await audit(user, "referral.batch_run", "referral_payout", "",
                {"period": period, "paid": len(paid),
                 "skipped": len(skipped), "failed": len(failed)})
    return {"period": period, "paid": paid, "skipped": skipped,
            "failed": failed}


# --------------------------------------------- operator: Melio queue (B3b)
#
# Bank transfer and paper check both execute in Melio (the business's
# own Melio account — operator-driven, no API integration). The queue
# mirrors Melio's reality: queued -> sent_to_melio -> (ACH)
# awaiting_claim -> claimed -> completed, with failed/unclaimed items
# staying visible. A payout records only at completion.

class QueueIn(BaseModel):
    referrer_id: str
    product: str


@router.post("/admin/queue")
async def queue_create(body: QueueIn,
                       user: dict = Depends(require("ledger:admin"))):
    if body.product not in R.PRODUCTS:
        raise HTTPException(status_code=422, detail="Unknown product")
    try:
        item = await R.queue_create(body.referrer_id, body.product,
                                    by=user.get("email", ""))
    except R.ReferralError as e:
        raise _conflict(e)
    await audit(user, "referral.queue_created", "empire_person",
                body.referrer_id,
                {"item_id": item["id"], "product": body.product,
                 "rail": item["rail"], "net_cents": item["net_cents"]})
    # Strip Mongo's _id, as the list endpoint does: the raw item
    # carries an ObjectId FastAPI cannot serialize — the create
    # succeeded but the operator saw HTTP 500 (load probe, §5.3).
    return {k: v for k, v in item.items() if k != "_id"}


@router.get("/admin/queue")
async def queue_list(status: str = None, preference: str = None,
                     user: dict = Depends(require("ledger:admin"))):
    """The Melio queue — every item with its state, including
    unclaimed and failed items (never silently dropped)."""
    q = {}
    if status:
        q["status"] = status
    if preference:
        q["preference"] = preference
    rows = []
    async for item in db.referral_payout_queue.find(q).sort(
            "created_at", -1).limit(500):
        person = await R.person_by_id(item["referrer_id"])
        row = {k: v for k, v in item.items() if k != "_id"}
        if person:
            row.update({"referrer_email": (person.get("emails") or [None])[0],
                        "referrer_code": person.get("code")})
        rows.append(row)
    return rows


class QueueAdvanceIn(BaseModel):
    status: str  # sent_to_melio | awaiting_claim | claimed | completed | failed
    reference: Optional[str] = None  # e.g. the Melio payment reference
    note: Optional[str] = None


@router.post("/admin/queue/{item_id}/advance")
async def queue_advance(item_id: str, body: QueueAdvanceIn,
                        user: dict = Depends(require("ledger:admin"))):
    try:
        item = await R.queue_advance(
            item_id, body.status, by=user.get("email", ""),
            reference=body.reference, note=body.note)
    except R.ReferralError as e:
        raise _conflict(e)
    await audit(user, "referral.queue_advanced",
                "referral_payout_queue", item_id,
                {"status": item["status"], "rail": item["rail"],
                 "payout_id": item.get("payout_id")})
    # Same _id strip as create/list (see queue_create): the fetched
    # item carries Mongo's ObjectId, which 500s the response after
    # the state change already landed.
    return {k: v for k, v in item.items() if k != "_id"}


# --------------------------------------- operator: Melio pay-file (B3a)
#
# The export's job: generate the batch file FROM the ledger in a
# shape suited to Melio's bulk import (generic CSV: payee, amount,
# method — the exact Melio import format is confirmed at Melio
# account setup; see REFERRAL_HOST_BUILD.md), so the operator never
# hand-types a payout. Executing happens in Melio; marking the batch
# done records the payouts (rail melio_ach / paper_check_melio, fee
# on checks only).

class ExportItemIn(BaseModel):
    referrer_id: str
    product: str


class ExportIn(BaseModel):
    period: str  # YYYY-MM
    items: List[ExportItemIn]


def _csv_for_batch(items: list, period: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["payee_name", "payee_email", "referrer_id", "product",
                "method", "gross_cents", "fee_cents", "amount_cents",
                "currency", "period"])
    for it in items:
        w.writerow([it.get("payee_name") or "", it.get("payee_email") or "",
                    it["referrer_id"], it["product"],
                    "ach" if it["preference"] == "bank_transfer" else "check",
                    it["gross_cents"], it["fee_cents"], it["net_cents"],
                    it["currency"], period])
    return buf.getvalue()


@router.post("/admin/payouts/export")
async def export_batch(body: ExportIn,
                       user: dict = Depends(require("ledger:admin"))):
    """Build a Melio batch from selected site pots: each selection
    becomes (or reuses) an open queue item, snapshotted at its
    current payable. All-or-nothing — a locked, frozen, or
    ineligible selection refuses the whole export with the reason,
    so the file always matches what the ledger can record."""
    staged = []
    for sel in body.items:
        if sel.product not in R.PRODUCTS:
            raise HTTPException(status_code=422, detail="Unknown product")
        existing = await db.referral_payout_queue.find_one(
            {"referrer_id": sel.referrer_id, "product": sel.product,
             "status": {"$in": list(R.QUEUE_OPEN_STATES)}})
        if existing and existing.get("batch_id"):
            raise _conflict(R.ReferralError(
                "A selected payout is already in another open batch."))
        staged.append((sel, existing))
    items = []
    for sel, existing in staged:
        try:
            item = existing or await R.queue_create(
                sel.referrer_id, sel.product, by=user.get("email", ""))
        except R.ReferralError as e:
            raise _conflict(e)
        items.append(item)
    batch = {
        "id": new_id(), "period": body.period, "status": "exported",
        "item_ids": [it["id"] for it in items],
        "created_by": user.get("email", ""), "created_at": now_iso(),
        "reference": None, "done_at": None,
    }
    await db.referral_pay_batches.insert_one(batch)
    for it in items:
        fields = {"batch_id": batch["id"], "updated_at": now_iso()}
        if it["status"] == "queued":
            fields["status"] = "sent_to_melio"
        await db.referral_payout_queue.update_one(
            {"id": it["id"]}, {"$set": fields})
        person = await R.person_by_id(it["referrer_id"])
        disp = await _display(person) if person else {}
        it.update(fields)
        it["payee_name"] = disp.get("name")
        it["payee_email"] = disp.get("email")
    await audit(user, "referral.batch_exported", "referral_pay_batch",
                batch["id"], {"period": body.period,
                              "items": len(items)})
    return Response(
        content=_csv_for_batch(items, body.period),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="melio-payouts-{body.period}.csv"',
            "X-Pay-Batch-Id": batch["id"],
        })


@router.get("/admin/payouts/batches/{batch_id}")
async def get_batch(batch_id: str,
                    user: dict = Depends(require("ledger:admin"))):
    batch = await db.referral_pay_batches.find_one(
        {"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items = []
    for item_id in batch.get("item_ids", []):
        item = await db.referral_payout_queue.find_one(
            {"id": item_id}, {"_id": 0})
        if item:
            items.append(item)
    return {**batch, "items": items}


class BatchDoneIn(BaseModel):
    reference: Optional[str] = None  # e.g. the Melio batch reference


@router.post("/admin/payouts/batches/{batch_id}/done")
async def batch_done(batch_id: str, body: BatchDoneIn,
                     user: dict = Depends(require("ledger:admin"))):
    """After the operator executes the batch in Melio: mark it done.
    Every open item completes and records its payout with its rail
    (melio_ach / paper_check_melio, fee on checks). Idempotent —
    completed items are reported, never re-paid; an item whose pot
    became locked or empty in the meantime refuses and stays open
    and visible."""
    batch = await db.referral_pay_batches.find_one({"id": batch_id})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    recorded, refused, already = [], [], []
    for item_id in batch.get("item_ids", []):
        item = await db.referral_payout_queue.find_one({"id": item_id})
        if item is None:
            continue
        if item["status"] == "completed":
            already.append({"item_id": item_id,
                            "payout_id": item.get("payout_id")})
            continue
        if item["status"] == "failed":
            refused.append({"item_id": item_id,
                            "reason": "item is marked failed"})
            continue
        try:
            done = await R.queue_complete(
                item, by=user.get("email", ""),
                reference=body.reference or batch_id)
        except R.ReferralError as e:
            # A concurrent completion of this same batch may have
            # finished the item while this call was refused by the
            # completion claim: report what is true now, not the
            # mid-race refusal.
            fresh = await db.referral_payout_queue.find_one(
                {"id": item_id})
            if fresh and fresh.get("status") == "completed":
                already.append({"item_id": item_id,
                                "payout_id": fresh.get("payout_id")})
            else:
                refused.append({"item_id": item_id, "reason": str(e)})
            continue
        recorded.append({"item_id": item_id,
                         "payout_id": done.get("payout_id"),
                         "rail": done["rail"],
                         "net_cents": done["net_cents"]})
    open_left = await db.referral_payout_queue.count_documents(
        {"id": {"$in": batch.get("item_ids", [])},
         "status": {"$in": list(R.QUEUE_OPEN_STATES)}})
    if open_left == 0:
        await db.referral_pay_batches.update_one(
            {"id": batch_id},
            {"$set": {"status": "done", "done_at": now_iso(),
                      "reference": body.reference or batch_id}})
    await audit(user, "referral.batch_done", "referral_pay_batch",
                batch_id, {"recorded": len(recorded),
                           "refused": len(refused),
                           "already": len(already)})
    return {"batch_id": batch_id, "recorded": recorded,
            "already_recorded": already, "refused": refused,
            "batch_status": "done" if open_left == 0 else "exported"}
