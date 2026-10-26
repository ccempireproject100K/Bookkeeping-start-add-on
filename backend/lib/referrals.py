"""Empire referral ledger — the single host implementation of the
referral program law (owner-ruled 2026-10-24; see
~/workspace/empire-referral-program-scope-2026-10-24.md).

The law, ported from Trade Circle's `app/referrals.py` semantics:

* A referrer earns 25% of what each referred person actually pays,
  per referral, for a 6-month window (183 days) anchored at the
  referred person's first payment, net of refunds. An unpaid referral
  earns nothing; commissions accrue from referral #1.
* An earning becomes payable after 60 days of clearing, and only
  while the referrer is not frozen.
* Good standing (ONE empire rule): the referrer earns only while not
  disabled on any linked product account, not frozen, and a paying
  member on at least one product. Earnings recorded while ineligible
  are stored as `not_eligible` at zero — they still prove the referred
  member paid.
* A chargeback (dispute opened) claws back that month's earnings for
  the referrer across all products and freezes payouts; a won dispute
  releases them, a lost one stands and paid-out amounts become
  negative adjustments against the next payout. 3 disputes opened
  within 183 days flags the person for manual review (also a freeze).
* Payout unlock is PER SITE (owner ruling update): pots are computed
  per (referrer, product); 3 paying referrals on a site unlock THAT
  site's pot — the whole accrued pot, first referrals included.
  Recorded payouts are never undone by the lock.

Shape notes: books conventions (integer cents, ISO timestamps, `id`
fields). Ingest stores raw events immutably in `referral_events`
(deduped on a deterministic key); computation reads those events and
the person/attribution records — never the reverse. Earnings are
first-class documents with deterministic ids (`{product}:{payment_id}`)
exactly as TC keys one earning per invoice.
"""
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from db import db
from lib.base import new_id, now_iso

SHARE = 0.25
WINDOW_DAYS = 183           # about 6 months, anchored at first payment
CLEARING_DAYS = 60
REVIEW_CHARGEBACKS = 3
REVIEW_WINDOW_DAYS = 183
UNLOCK_REFERRALS = 3        # paying referrals per site (owner ruling)
PAPER_CHECK_FEE_CENTS = 200  # $2.00, deducted from paper-check payouts
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I (TC's)
PRODUCTS = ("trade_circle", "books", "momo")
PAYOUT_PREFERENCES = ("stripe", "bank_transfer", "paper_check")


class ReferralError(Exception):
    """A business-rule refusal (locked / frozen / nothing payable).
    The router turns it into a 409 with this message."""


# ---------------------------------------------------------------- time

def _parse(value) -> datetime:
    """Accept ISO strings, epoch seconds, or datetimes; return aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_code() -> str:
    return "".join(_secrets.choice(ALPHABET) for _ in range(8))


# ---------------------------------------------------------------- persons

async def _insert_person(email: str = None, source: str = "ingest") -> dict:
    """Create a person with a freshly issued ledger code (retrying on
    the unique-code index, TC's code_for pattern)."""
    for _ in range(5):
        person = {
            "id": new_id(),
            "code": new_code(),
            "emails": [email] if email else [],
            "product_accounts": [],
            "first_paid_at": None,
            "review": False,
            "review_at": None,
            "payout_preference": "stripe",
            "created_at": now_iso(),
            "source": source,
        }
        try:
            await db.empire_persons.insert_one(person)
            return person
        except DuplicateKeyError:
            continue
    raise RuntimeError("Could not issue a unique referral code")


async def person_by_id(person_id: str):
    if not person_id:
        return None
    return await db.empire_persons.find_one({"id": person_id})


async def person_by_email(email: str):
    if not email:
        return None
    return await db.empire_persons.find_one({"emails": email.strip().lower()})


async def person_by_code(code: str):
    if not code:
        return None
    return await db.empire_persons.find_one({"code": code.strip().upper()})


async def person_by_account(product: str, account_id: str):
    if not account_id:
        return None
    att = await db.referral_attributions.find_one(
        {"id": f"{product}:{account_id}"})
    if att:
        return await person_by_id(att["person_id"])
    return await db.empire_persons.find_one(
        {"product_accounts": {"$elemMatch": {
            "product": product, "account_id": account_id}}})


async def person_in_product(person: dict, product: str) -> bool:
    """True when the person holds an account or an attribution on
    `product`. This is the scoping rule for the cross-product
    read-back/preference surface (security audit 2026-10-26, N-3): a
    product may resolve a person by email only when the person is one
    of ITS OWN members in this sense — never by email alone."""
    if not person:
        return False
    if any(a.get("product") == product
           for a in person.get("product_accounts", [])):
        return True
    att = await db.referral_attributions.find_one(
        {"person_id": person["id"], "product": product}, {"_id": 1})
    return att is not None


async def ensure_person(email: str = None, source: str = "ingest") -> dict:
    """The person for an email (normalized-email join key), creating
    one when none exists. Email matching only ever links accounts of
    the same human — it never creates or moves attribution."""
    email = (email or "").strip().lower() or None
    if email:
        found = await person_by_email(email)
        if found:
            return found
    return await _insert_person(email, source=source)


async def link_account(person: dict, *, product: str, account_id: str,
                       tenant_id: str = None, email: str = None,
                       standing: dict = None) -> dict:
    """Attach (or refresh) a product account on a person record."""
    email = (email or "").strip().lower() or None
    accounts = [dict(a) for a in person.get("product_accounts", [])]
    acct = next((a for a in accounts
                 if a["product"] == product and a["account_id"] == account_id),
                None)
    if acct is None:
        acct = {"product": product, "account_id": account_id,
                "tenant_id": tenant_id, "email": email,
                "disabled": False, "paying": False}
        accounts.append(acct)
    if tenant_id:
        acct["tenant_id"] = tenant_id
    if email:
        acct["email"] = email
    if standing:
        if "disabled" in standing:
            acct["disabled"] = bool(standing["disabled"])
        if "paying" in standing:
            acct["paying"] = bool(standing["paying"])
    acct["updated_at"] = now_iso()
    emails = list(person.get("emails", []))
    if email and email not in emails:
        emails.append(email)
    await db.empire_persons.update_one(
        {"id": person["id"]},
        {"$set": {"product_accounts": accounts, "emails": emails}})
    person["product_accounts"] = accounts
    person["emails"] = emails
    return person


# ---------------------------------------------------------------- standing

async def is_frozen(person_id: str) -> bool:
    """Frozen = an open dispute anywhere, or a manual-review flag.
    One freeze per person, empire-wide (scope doc fraud section)."""
    if await db.referral_disputes.find_one(
            {"referrer_id": person_id, "status": "open"}, {"_id": 1}):
        return True
    person = await person_by_id(person_id)
    return bool(person and person.get("review"))


def standing_facts(person: dict) -> dict:
    accounts = (person or {}).get("product_accounts", [])
    return {
        "disabled_any": any(a.get("disabled") for a in accounts),
        "paying_any": any(a.get("paying") for a in accounts),
        "review": bool((person or {}).get("review")),
    }


def good_standing(person: dict, frozen: bool) -> bool:
    """The ONE empire rule (owner-confirmed): not disabled or frozen
    anywhere, and a paying member on at least one product."""
    facts = standing_facts(person)
    return (not facts["disabled_any"] and not frozen
            and facts["paying_any"])


# ---------------------------------------------------------------- raw ingest

def _dedupe_key(kind: str, payload: dict) -> str:
    product = payload["product"]
    if kind == "attribution":
        return f"{product}:attribution:{payload['account_id']}"
    if kind == "payment":
        return f"{product}:payment:{payload['payment_id']}"
    if kind == "refund":
        # Addendum A convention: the client sends the delta, and the
        # dedupe key derives from the CUMULATIVE refunded total, so
        # replayed deliveries collapse and sequential partial refunds
        # each land under a distinct key.
        return (f"{product}:refund:{payload['payment_id']}:"
                f"{int(payload['refunded_total_cents'])}")
    if kind in ("dispute_opened", "dispute_closed"):
        return f"{product}:{kind}:{payload['dispute_id']}"
    raise ValueError(f"Unknown referral event kind: {kind!r}")


async def store_event(kind: str, payload: dict):
    """Insert the raw event immutably; return (event, is_duplicate).
    A duplicate delivery is a no-op the caller reports as such."""
    key = _dedupe_key(kind, payload)
    existing = await db.referral_events.find_one({"dedupe_key": key})
    if existing:
        return existing, True
    event = {
        "id": new_id(),
        "dedupe_key": key,
        "product": payload["product"],
        "kind": kind,
        "account_id": payload.get("account_id"),
        "payment_id": payload.get("payment_id"),
        "dispute_id": payload.get("dispute_id"),
        "payload": dict(payload),
        "received_at": now_iso(),
    }
    try:
        await db.referral_events.insert_one(event)
    except DuplicateKeyError:
        return await db.referral_events.find_one({"dedupe_key": key}), True
    return event, False


# ---------------------------------------------------------------- attribution

async def _resolve_referrer(att: dict):
    """Fill an attribution's referrer from its code. Returns the
    updated attribution (status linked / self / code_unknown)."""
    if att.get("referrer_id"):
        return att
    code = (att.get("referral_code") or "").strip().upper()
    if not code:
        return att
    owner = await person_by_code(code)
    if owner is None:
        att["status"] = "code_unknown"
    elif owner["id"] == att["person_id"]:
        att["status"] = "self"  # links the accounts, earns nothing
    else:
        att["referrer_id"] = owner["id"]
        att["status"] = "linked"
    await db.referral_attributions.update_one(
        {"id": att["id"]},
        {"$set": {"status": att["status"],
                  "referrer_id": att.get("referrer_id")}})
    return att


async def apply_attribution(*, product: str, account_id: str,
                            email: str = None, referral_code: str = None,
                            tenant_id: str = None, standing: dict = None,
                            at=None) -> dict:
    """Register/attest a product account: link it to its person
    (email join key) and, when a referral code is presented, record
    the attribution — once per account; the first code wins."""
    person = await ensure_person(email)
    person = await link_account(
        person, product=product, account_id=account_id,
        tenant_id=tenant_id, email=email, standing=standing)
    att_id = f"{product}:{account_id}"
    att = await db.referral_attributions.find_one({"id": att_id})
    if att is None:
        att = {
            "id": att_id,
            "product": product,
            "account_id": account_id,
            "tenant_id": tenant_id,
            "email": (email or "").strip().lower() or None,
            "person_id": person["id"],
            "referral_code": (referral_code or "").strip().upper() or None,
            "referrer_id": None,
            "status": "none",
            "at": _iso(_parse(at)) if at else now_iso(),
        }
        try:
            await db.referral_attributions.insert_one(att)
        except DuplicateKeyError:
            att = await db.referral_attributions.find_one({"id": att_id})
    if att.get("referral_code") and not att.get("referrer_id"):
        att = await _resolve_referrer(att)
        if att.get("referrer_id"):
            await _backfill_earnings(att)
    return {"person_id": person["id"], "attribution_status": att["status"],
            "referrer_id": att.get("referrer_id")}


async def _attribution_for(product: str, account_id: str):
    return await db.referral_attributions.find_one(
        {"id": f"{product}:{account_id}"})


async def _backfill_earnings(att: dict):
    """A referrer resolved late (code imported after payments were
    reported): create the earnings those stored payments should have
    produced, in paid_at order so the window anchor is exact."""
    events = await db.referral_events.find(
        {"product": att["product"], "kind": "payment",
         "account_id": att["account_id"]}).sort("received_at", 1).to_list(1000)
    payloads = sorted((e["payload"] for e in events),
                      key=lambda p: _parse(p["paid_at"]))
    for p in payloads:
        await _apply_payment(att, p)


# ---------------------------------------------------------------- payments

async def _apply_payment(att: dict, payload: dict):
    amount = int(payload.get("amount_cents") or 0)
    if amount <= 0:
        return None
    paid_at = _parse(payload["paid_at"])
    referred = await person_by_id(att["person_id"])
    # The payer demonstrably pays: their account counts as paying.
    referred = await link_account(
        referred, product=payload["product"],
        account_id=payload["account_id"], standing={"paying": True})
    first = referred.get("first_paid_at")
    first_dt = _parse(first) if first else None
    if first_dt is None or paid_at < first_dt:
        first_dt = paid_at
        await db.empire_persons.update_one(
            {"id": referred["id"]},
            {"$set": {"first_paid_at": _iso(paid_at)}})
    if not att.get("referrer_id"):
        return None  # raw event stored; no one to credit (yet)
    if paid_at > first_dt + timedelta(days=WINDOW_DAYS):
        return None  # the 6 months are over
    referrer = await person_by_id(att["referrer_id"])
    eligible = (referrer is not None
                and good_standing(referrer, await is_frozen(referrer["id"])))
    earning = {
        "id": f"{payload['product']}:{payload['payment_id']}",
        "referrer_id": att["referrer_id"],
        "referred_id": att["person_id"],
        "product": payload["product"],
        "account_id": payload["account_id"],
        "payment_id": payload["payment_id"],
        "charge": payload.get("charge"),
        "revenue_cents": amount,
        "refunded_cents": 0,
        "share_cents": round(amount * SHARE) if eligible else 0,
        "status": "pending" if eligible else "not_eligible",
        "month": paid_at.strftime("%Y-%m"),
        "earned_at": _iso(paid_at),
    }
    existing = await db.referral_earnings.find_one({"id": earning["id"]})
    if existing:
        return existing
    try:
        await db.referral_earnings.insert_one(earning)
    except DuplicateKeyError:
        return await db.referral_earnings.find_one({"id": earning["id"]})
    return earning


async def apply_payment(*, product: str, account_id: str, payment_id: str,
                        amount_cents: int, currency: str = "usd",
                        paid_at=None, charge: str = None) -> dict:
    att = await _attribution_for(product, account_id)
    if att is None:
        # Payment for an account never attested: register it bare so
        # the payment still lands on a person record (no attribution).
        await apply_attribution(product=product, account_id=account_id)
        att = await _attribution_for(product, account_id)
    earning = await _apply_payment(att, {
        "product": product, "account_id": account_id,
        "payment_id": payment_id, "amount_cents": amount_cents,
        "currency": currency, "paid_at": _iso(_parse(paid_at)),
        "charge": charge})
    return {"earning_id": earning["id"] if earning else None,
            "attributed": bool(att.get("referrer_id"))}


async def apply_refund(*, product: str, payment_id: str,
                       refunded_delta_cents: int,
                       refunded_total_cents: int) -> dict:
    """Recompute the earning's share from net, TC's refund semantics:
    the cumulative refunded total is the truth, however the deltas
    arrived. A replay (dedupe) never reaches here."""
    earning = await db.referral_earnings.find_one(
        {"id": f"{product}:{payment_id}"})
    if earning is None:
        return {"earning_id": None}
    total = max(int(earning.get("refunded_cents") or 0),
                int(refunded_total_cents))
    net = max(0, earning["revenue_cents"] - total)
    share = (round(net * SHARE)
             if earning["status"] != "not_eligible" else 0)
    await db.referral_earnings.update_one(
        {"id": earning["id"]},
        {"$set": {"refunded_cents": total, "share_cents": share}})
    if earning["status"] == "paid":
        clawed = int(earning.get("share_cents") or 0) - share
        if clawed > 0:
            # The money already went out the door. Rewriting the
            # share alone leaves the member's summary permanently
            # short of the payout record — load probe P6 measured a
            # 225¢ gap with no balancing entry, and the refunded
            # share was never charged against future pots. Book the
            # difference as a negative adjustment, the dispute path's
            # mechanism: the summary folds it into the next pot, and
            # payout records reconcile with summaries to the cent.
            await db.referral_adjustments.insert_one({
                "id": new_id(), "referrer_id": earning["referrer_id"],
                "product": earning["product"], "cents": -clawed,
                "reason": f"Refund on payment {payment_id}",
                "earning_id": earning["id"], "status": "pending",
                "at": now_iso()})
    return {"earning_id": earning["id"], "net_cents": net,
            "share_cents": share}


# ---------------------------------------------------------------- disputes

async def _find_dispute_earning(product: str, payment_id: str = None,
                                charge: str = None):
    if payment_id:
        found = await db.referral_earnings.find_one(
            {"id": f"{product}:{payment_id}"})
        if found:
            return found
    if charge:
        return await db.referral_earnings.find_one(
            {"product": product, "charge": charge})
    return None


async def apply_dispute(*, product: str, dispute_id: str,
                        payment_id: str = None, charge: str = None,
                        phase: str, outcome: str = None) -> dict:
    earning = await _find_dispute_earning(product, payment_id, charge)
    disp_id = f"{product}:{dispute_id}"
    if earning is None:
        # A close event may carry only the dispute id: resolve the
        # earning through the dispute record opened earlier.
        disp = await db.referral_disputes.find_one({"id": disp_id})
        if disp and disp.get("earning_id"):
            earning = await db.referral_earnings.find_one(
                {"id": disp["earning_id"]})
    if earning is None:
        return {"earning_id": None}
    referrer = earning["referrer_id"]
    if phase == "opened":
        await db.referral_disputes.update_one(
            {"id": disp_id},
            {"$setOnInsert": {
                "id": disp_id, "referrer_id": referrer,
                "earning_id": earning["id"], "month": earning["month"],
                "status": "open", "opened_at": now_iso()}},
            upsert=True)
        # Claw back the month (all products — the month is the
        # referrer's empire month): unpaid earnings are withheld;
        # paid ones come off the next payout for that site's pot.
        async for e in db.referral_earnings.find(
                {"referrer_id": referrer, "month": earning["month"],
                 "status": {"$in": ["pending", "paid"]}}):
            await db.referral_earnings.update_one(
                {"id": e["id"]},
                {"$set": {"status": "clawed_back", "was": e["status"],
                          "clawed_by": disp_id}})
            if e["status"] == "paid":
                await db.referral_adjustments.insert_one({
                    "id": new_id(), "referrer_id": referrer,
                    "product": e["product"], "cents": -e["share_cents"],
                    "reason": f"Chargeback in {e['month']}",
                    "dispute": disp_id, "status": "pending",
                    "at": now_iso()})
        since = _iso(_now() - timedelta(days=REVIEW_WINDOW_DAYS))
        count = await db.referral_disputes.count_documents(
            {"referrer_id": referrer, "opened_at": {"$gte": since}})
        if count >= REVIEW_CHARGEBACKS:
            await db.empire_persons.update_one(
                {"id": referrer},
                {"$set": {"review": True, "review_at": now_iso()}})
        return {"earning_id": earning["id"], "clawed_back": True}
    # closed
    won = outcome == "won"
    result = await db.referral_disputes.update_one(
        {"id": disp_id, "status": "open"},
        {"$set": {"status": "won" if won else "lost",
                  "closed_at": now_iso()}})
    if not getattr(result, "modified_count", 0) and \
            not getattr(result, "matched_count", 0):
        return {"earning_id": earning["id"], "released": False}
    if not won:
        return {"earning_id": earning["id"], "released": False}
    async for e in db.referral_earnings.find(
            {"clawed_by": disp_id, "status": "clawed_back"}):
        await db.referral_earnings.update_one(
            {"id": e["id"]},
            {"$set": {"status": e.get("was", "pending")},
             "$unset": {"clawed_by": "", "was": ""}})
    await db.referral_adjustments.update_many(
        {"dispute": disp_id, "status": "pending"},
        {"$set": {"status": "cancelled"}})
    return {"earning_id": earning["id"], "released": True}


# ---------------------------------------------------------------- summaries

async def paying_referral_count(referrer_id: str, product: str) -> int:
    """Distinct referred persons with real money on THIS site: at
    least one earning here that still stands (not clawed back) with
    net revenue above zero. Read from the earnings ledger, never a
    counter that could drift — TC's rule, per site."""
    ids = set()
    async for e in db.referral_earnings.find(
            {"referrer_id": referrer_id, "product": product,
             "status": {"$in": ["pending", "paid", "not_eligible"]}},
            {"referred_id": 1, "revenue_cents": 1, "refunded_cents": 1}):
        if e["revenue_cents"] - e.get("refunded_cents", 0) > 0:
            ids.add(e["referred_id"])
    return len(ids)


async def site_summary(referrer_id: str, product: str,
                       now: datetime = None) -> dict:
    """TC's summary buckets for one site pot."""
    now = now or _now()
    cutoff = _iso(now - timedelta(days=CLEARING_DAYS))
    totals = {"pending": 0, "available": 0, "paid": 0, "clawed_back": 0}
    async for e in db.referral_earnings.find(
            {"referrer_id": referrer_id, "product": product}):
        if e["status"] == "pending":
            bucket = "available" if e["earned_at"] <= cutoff else "pending"
            totals[bucket] += e["share_cents"]
        elif e["status"] in ("paid", "clawed_back"):
            totals[e["status"]] += e["share_cents"]
    adjust = 0
    async for a in db.referral_adjustments.find(
            {"referrer_id": referrer_id, "product": product,
             "status": "pending"}):
        adjust += a["cents"]
    frozen = await is_frozen(referrer_id)
    paying = await paying_referral_count(referrer_id, product)
    locked = paying < UNLOCK_REFERRALS
    cleared = max(0, totals["available"] + adjust)
    return {
        "product": product,
        **totals,
        "adjustments": adjust,
        "earned_cents": totals["pending"] + totals["available"]
                        + totals["paid"],
        "paying_referrals": paying,
        "unlock_at": UNLOCK_REFERRALS,
        "locked": locked,
        # What the lock itself holds: the cleared pot that would be
        # payable today if this site were unlocked.
        "locked_cents": cleared if locked else 0,
        "payable": 0 if (frozen or locked) else cleared,
        "frozen": frozen,
        "clearing_days": CLEARING_DAYS,
    }


async def empire_summary(person: dict, now: datetime = None) -> dict:
    """The read-back shape products render: TC's member-screen fields,
    per site, plus empire totals and payout state."""
    rid = person["id"]
    sites = {}
    for product in PRODUCTS:
        has = await db.referral_earnings.count_documents(
            {"referrer_id": rid, "product": product})
        has_att = await db.referral_attributions.count_documents(
            {"referrer_id": rid, "product": product})
        if has or has_att:
            sites[product] = await site_summary(rid, product, now=now)
    frozen = await is_frozen(rid)
    totals = {"pending": 0, "available": 0, "paid": 0, "clawed_back": 0,
              "earned_cents": 0, "payable": 0}
    for s in sites.values():
        for k in totals:
            totals[k] += s[k]
    preference = person.get("payout_preference", "stripe")
    payouts = []
    async for p in db.referral_payouts.find(
            {"referrer_id": rid}).sort("at", -1).limit(50):
        payouts.append({
            "id": p["id"], "product": p["product"], "rail": p["rail"],
            "gross_cents": p["gross_cents"], "fee_cents": p["fee_cents"],
            "net_cents": p["net_cents"], "currency": p.get("currency", "usd"),
            "reference": p.get("reference"), "at": p["at"],
        })
    return {
        "empire_id": rid,
        "code": person.get("code"),
        "eligible": good_standing(person, frozen),
        "under_review": bool(person.get("review")),
        "frozen": frozen,
        "payout_preference": preference,
        "paper_check_fee_cents": PAPER_CHECK_FEE_CENTS,
        "sites": sites,
        "totals": totals,
        "payable": totals["payable"],
        "payouts": payouts,
        "terms": {"share_percent": int(SHARE * 100), "months": 6,
                  "clearing_days": CLEARING_DAYS,
                  "review_chargebacks": REVIEW_CHARGEBACKS,
                  "unlock_referrals": UNLOCK_REFERRALS,
                  "unlock_is_per_site": True},
    }


# ---------------------------------------------------------------- payouts

async def _record_payout_now(db_, referrer_id: str, product: str, *,
                             rail: str, note: str, by: str,
                             reference: str = None, fee_cents: int = 0,
                             period: str = None, batch_key: str = None,
                             currency: str = "usd",
                             now: datetime = None) -> dict:
    """Mark this site's cleared earnings paid and write the payout
    record. Caller has already done the locked/frozen checks (or is
    completing a queue item that did). Gross is the site's current
    cleared payable; the fee (paper checks) comes off the top."""
    now = now or _now()
    s = await site_summary(referrer_id, product, now=now)
    gross = max(0, s["available"] + s["adjustments"])
    if gross <= 0:
        raise ReferralError("Nothing is payable yet.")
    payout = {
        "id": new_id(),
        "referrer_id": referrer_id,
        "product": product,
        "period": period or now.strftime("%Y-%m"),
        "rail": rail,
        "gross_cents": gross,
        "fee_cents": int(fee_cents),
        "net_cents": gross - int(fee_cents),
        "currency": currency,
        "reference": reference,
        "note": note or "",
        "by": by,
        "at": _iso(now),
        "batch_key": batch_key or new_id(),
    }
    await db_.referral_payouts.insert_one(payout)
    await mark_cleared_paid(referrer_id, product, payout["id"], now=now)
    return payout


async def mark_cleared_paid(referrer_id: str, product: str,
                            payout_id: str, now: datetime = None):
    """Move this site's cleared pending earnings to paid under a
    payout, and apply its pending adjustments to the same payout."""
    now = now or _now()
    cutoff = _iso(now - timedelta(days=CLEARING_DAYS))
    await db.referral_earnings.update_many(
        {"referrer_id": referrer_id, "product": product,
         "status": "pending", "earned_at": {"$lte": cutoff}},
        {"$set": {"status": "paid", "payout_id": payout_id}})
    await db.referral_adjustments.update_many(
        {"referrer_id": referrer_id, "product": product,
         "status": "pending"},
        {"$set": {"status": "applied", "payout_id": payout_id}})


async def payable_or_refuse(referrer_id: str, product: str,
                            now: datetime = None) -> dict:
    """The gate every rail passes through: frozen, locked, and
    empty pots are refused with the reason (TC's payout refusals)."""
    person = await person_by_id(referrer_id)
    if person is None:
        raise ReferralError("Unknown referrer.")
    s = await site_summary(referrer_id, product, now=now)
    if s["frozen"]:
        raise ReferralError(
            "Payouts are frozen for this member (open chargeback or review).")
    if s["locked"]:
        raise ReferralError(
            f"Payouts are locked until a member has {UNLOCK_REFERRALS} "
            f"paying referrals on {product} — this member has "
            f"{s['paying_referrals']}. Their earnings keep accruing and "
            f"unlock in full at {UNLOCK_REFERRALS}.")
    if s["payable"] <= 0:
        raise ReferralError("Nothing is payable yet.")
    return s


async def record_manual_payout(referrer_id: str, product: str, *,
                               note: str, by: str,
                               now: datetime = None) -> dict:
    """The manual backup rail: a human paid off-platform; record it.
    Exactly TC's moderator payout semantics, per site."""
    await payable_or_refuse(referrer_id, product, now=now)
    return await _record_payout_now(
        db, referrer_id, product, rail="manual", note=note, by=by,
        reference=note, now=now)


# ---------------------------------------------------------------- books outbox

async def drain_books_outbox(limit: int = 500) -> dict:
    """Consume the checkout build's `referral_payment_events` outbox
    into the ledger as books-product ingest. The outbox row is never
    mutated; idempotency is the ledger's own (product, payment_id)
    dedupe, so re-draining is a no-op. Tenants stamped with a
    referral code get their attribution from the stamp."""
    processed = skipped = 0
    rows = await db.referral_payment_events.find(
        {"product": "books"}).sort("emitted_at", 1).to_list(limit)
    for row in rows:
        tenant_id = row.get("tenant_id") or row.get("referred_account_id")
        code = row.get("referral_code")
        if code:
            existing = await _attribution_for("books", tenant_id)
            if existing is None:
                await apply_attribution(
                    product="books", account_id=tenant_id,
                    email=row.get("referred_email"), referral_code=code,
                    tenant_id=tenant_id)
        payload = {
            "product": "books", "account_id": tenant_id,
            "payment_id": row["payment_id"],
            "amount_cents": row.get("amount_cents"),
            "currency": row.get("currency", "usd"),
            "paid_at": row.get("paid_at"),
        }
        _, dup = await store_event("payment", payload)
        if dup:
            skipped += 1
            continue
        await apply_payment(
            product="books", account_id=tenant_id,
            payment_id=row["payment_id"],
            amount_cents=row.get("amount_cents") or 0,
            currency=row.get("currency", "usd"),
            paid_at=row.get("paid_at"))
        processed += 1
    return {"processed": processed, "duplicates": skipped}


# ---------------------------------------------------------------- TC migration

async def import_tc_export(payload: dict) -> dict:
    """Import Trade Circle's referral records as trade_circle-site
    history (B2 migration path). Idempotent: persons merge on email,
    earnings/payouts land under deterministic ids and existing rows
    are skipped, never overwritten."""
    counts = {"persons": 0, "attributions": 0, "earnings": 0,
              "payouts": 0, "adjustments": 0, "disputes": 0,
              "skipped": 0}
    tc_to_empire = {}
    for m in payload.get("members", []):
        person = await ensure_person(m.get("email"), source="tc_migration")
        if m.get("code") and person.get("code") != m["code"].strip().upper():
            clash = await person_by_code(m["code"])
            if clash is None:
                await db.empire_persons.update_one(
                    {"id": person["id"]},
                    {"$set": {"code": m["code"].strip().upper()}})
                person["code"] = m["code"].strip().upper()
        if m.get("referral_review"):
            await db.empire_persons.update_one(
                {"id": person["id"]}, {"$set": {"review": True}})
        person = await link_account(
            person, product="trade_circle",
            account_id=m["tc_user_id"], email=m.get("email"),
            standing={"paying": bool(m.get("paying"))})
        tc_to_empire[m["tc_user_id"]] = person["id"]
        counts["persons"] += 1
    for m in payload.get("members", []):
        ref_tc = m.get("referred_by_tc_user_id")
        if not ref_tc:
            continue
        referrer_code = None
        for src in payload.get("members", []):
            if src["tc_user_id"] == ref_tc:
                referrer_code = src.get("code")
        att = {
            "id": f"trade_circle:{m['tc_user_id']}",
            "product": "trade_circle",
            "account_id": m["tc_user_id"],
            "tenant_id": None,
            "email": (m.get("email") or "").strip().lower() or None,
            "person_id": tc_to_empire[m["tc_user_id"]],
            "referral_code": (referrer_code or "").strip().upper() or None,
            "referrer_id": tc_to_empire.get(ref_tc),
            "status": "linked" if ref_tc in tc_to_empire else "code_unknown",
            "at": m.get("referred_at") or now_iso(),
        }
        if await db.referral_attributions.find_one({"id": att["id"]}):
            counts["skipped"] += 1
            continue
        await db.referral_attributions.insert_one(att)
        counts["attributions"] += 1
        if m.get("referral_first_paid_at"):
            person = await person_by_id(att["person_id"])
            cur = person.get("first_paid_at")
            if not cur or m["referral_first_paid_at"] < cur:
                await db.empire_persons.update_one(
                    {"id": att["person_id"]},
                    {"$set": {"first_paid_at":
                               m["referral_first_paid_at"]}})
    for e in payload.get("earnings", []):
        doc = {
            "id": f"trade_circle:{e['invoice_id']}",
            "referrer_id": tc_to_empire.get(e["referrer_tc_user_id"]),
            "referred_id": tc_to_empire.get(e["referred_tc_user_id"]),
            "product": "trade_circle",
            "account_id": e["referred_tc_user_id"],
            "payment_id": e["invoice_id"],
            "charge": e.get("charge"),
            "revenue_cents": e.get("revenue_cents", 0),
            "refunded_cents": e.get("refunded_cents", 0),
            "share_cents": e.get("share_cents", 0),
            "status": e.get("status", "pending"),
            "month": e.get("month"),
            "earned_at": e.get("earned_at"),
            "payout_id": e.get("payout_id"),
        }
        if not doc["referrer_id"] or \
                await db.referral_earnings.find_one({"id": doc["id"]}):
            counts["skipped"] += 1
            continue
        await db.referral_earnings.insert_one(doc)
        counts["earnings"] += 1
    for p in payload.get("payouts", []):
        doc = {
            "id": f"tc:{p['id']}",
            "referrer_id": tc_to_empire.get(p["referrer_tc_user_id"]),
            "product": "trade_circle",
            "period": (p.get("at") or "")[:7] or None,
            "rail": "manual",
            "gross_cents": p.get("cents", 0),
            "fee_cents": 0,
            "net_cents": p.get("cents", 0),
            "currency": "usd",
            "reference": p.get("note"),
            "note": p.get("note") or "",
            "by": "tc_migration",
            "at": p.get("at") or now_iso(),
            "batch_key": f"tc-migration:{p['id']}",
        }
        if not doc["referrer_id"] or \
                await db.referral_payouts.find_one({"id": doc["id"]}):
            counts["skipped"] += 1
            continue
        await db.referral_payouts.insert_one(doc)
        counts["payouts"] += 1
    for a in payload.get("adjustments", []):
        doc = {
            "id": f"tc:{a['id']}",
            "referrer_id": tc_to_empire.get(a["referrer_tc_user_id"]),
            "product": "trade_circle",
            "cents": a.get("cents", 0),
            "reason": a.get("reason", ""),
            "dispute": (f"trade_circle:{a['dispute']}"
                        if a.get("dispute") else None),
            "status": a.get("status", "pending"),
            "at": a.get("at") or now_iso(),
        }
        if not doc["referrer_id"] or \
                await db.referral_adjustments.find_one({"id": doc["id"]}):
            counts["skipped"] += 1
            continue
        await db.referral_adjustments.insert_one(doc)
        counts["adjustments"] += 1
    for d in payload.get("disputes", []):
        doc = {
            "id": f"trade_circle:{d['id']}",
            "referrer_id": tc_to_empire.get(d["referrer_tc_user_id"]),
            "earning_id": (f"trade_circle:{d['invoice_id']}"
                           if d.get("invoice_id") else None),
            "month": d.get("month"),
            "status": d.get("status", "open"),
            "opened_at": d.get("opened_at") or now_iso(),
            "closed_at": d.get("closed_at"),
        }
        if not doc["referrer_id"] or \
                await db.referral_disputes.find_one({"id": doc["id"]}):
            counts["skipped"] += 1
            continue
        await db.referral_disputes.insert_one(doc)
        counts["disputes"] += 1
    return counts


# ------------------------------------------------------- Melio payout queue
#
# Off-Stripe payouts consolidate into Melio (owner ruling 2026-10-24):
# bank transfer = Melio ACH (the member claims in Melio's flow and
# enters their own bank details — we never custody them), paper check
# = Melio mailed check with the $2.00 fee deducted from the payout.
# There is NO Melio API integration: the operator executes in Melio
# (the business's own account) and mirrors the state here. An item is
# a snapshot of one site pot for one Melio-preference member.
# States: queued -> sent_to_melio -> (ACH: awaiting_claim -> claimed)
# -> completed | failed. Unclaimed or failed items stay visible in
# the queue with their state; nothing is silently dropped. A payout
# is recorded only at completion — money has not moved before that.

QUEUE_OPEN_STATES = ("queued", "sent_to_melio", "awaiting_claim", "claimed")
QUEUE_TRANSITIONS = {
    "queued": {"sent_to_melio", "failed"},
    "sent_to_melio": {"awaiting_claim", "claimed", "completed", "failed"},
    "awaiting_claim": {"claimed", "completed", "failed"},
    "claimed": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}
RAIL_BY_PREFERENCE = {"bank_transfer": "melio_ach",
                      "paper_check": "paper_check_melio"}


async def queue_create(referrer_id: str, product: str, *, by: str,
                       now: datetime = None) -> dict:
    """Snapshot a member's site pot into the Melio queue. Refuses
    (ReferralError) for stripe-preference members, locked/frozen
    pots, duplicate open items, and paper-check pots the $2 fee
    would swallow."""
    person = await person_by_id(referrer_id)
    if person is None:
        raise ReferralError("Unknown referrer.")
    preference = person.get("payout_preference", "stripe")
    if preference not in RAIL_BY_PREFERENCE:
        raise ReferralError(
            "This member's payout preference is Stripe — they are not "
            "paid through the Melio queue.")
    s = await payable_or_refuse(referrer_id, product, now=now)
    existing = await db.referral_payout_queue.find_one(
        {"referrer_id": referrer_id, "product": product,
         "status": {"$in": list(QUEUE_OPEN_STATES)}})
    if existing:
        raise ReferralError(
            "An open queue item already exists for this member and site.")
    fee = PAPER_CHECK_FEE_CENTS if preference == "paper_check" else 0
    gross = s["payable"]
    if gross - fee <= 0:
        raise ReferralError(
            "The payable amount does not cover the $2.00 paper-check fee.")
    item = {
        "id": new_id(),
        "referrer_id": referrer_id,
        "product": product,
        "preference": preference,
        "rail": RAIL_BY_PREFERENCE[preference],
        "gross_cents": gross,
        "fee_cents": fee,
        "net_cents": gross - fee,
        "currency": "usd",
        "status": "queued",
        "batch_id": None,
        "payout_id": None,
        "note": "",
        "created_by": by,
        "created_at": _iso(now) if now else now_iso(),
        "updated_at": _iso(now) if now else now_iso(),
        "completed_at": None,
    }
    await db.referral_payout_queue.insert_one(item)
    return item


async def _release_completion_claim(item_id: str, token: str):
    """Give back a completion claim when recording failed, so the
    item stays open and retryable (the refusal semantics below)."""
    await db.referral_payout_queue.update_one(
        {"id": item_id, "completion_claim": token},
        {"$unset": {"completion_claim": "",
                    "completion_claim_at": ""}})


async def queue_complete(item: dict, *, by: str, reference: str = None,
                         now: datetime = None) -> dict:
    """Record the payout for a queue item whose money has moved in
    Melio, and mark it completed. Re-validates the pot at completion
    (locked/frozen/empty refuse, leaving the item open and visible)
    and pays the CURRENT cleared payable, exactly like the manual
    rail — the snapshot is the operator's Melio instruction, the
    ledger pays what is actually payable.

    Concurrency (load probe P4): two operators — or one double-click
    — completing the same batch at once both passed the status check
    and each recorded a payout (150¢ of records against a 75¢ pot).
    Completion therefore starts with an atomic claim, the F-1
    pattern: a conditional update that only one caller can land. The
    loser re-reads the item — completed by the winner means return
    it as-is; a live claim means refuse honestly. A claim older than
    ten minutes is a crashed completer and may be taken over. The
    payout itself carries a deterministic batch_key under the
    unique index, so even a slipped claim cannot write a second
    payout record for one queue item."""
    if item["status"] == "completed":
        return item
    await payable_or_refuse(item["referrer_id"], item["product"], now=now)
    token = new_id()
    stale = _iso((now or _now()) - timedelta(minutes=10))
    claim = await db.referral_payout_queue.update_one(
        {"id": item["id"], "status": {"$ne": "completed"},
         "$or": [{"completion_claim": None},
                 {"completion_claim_at": {"$lte": stale}}]},
        {"$set": {"completion_claim": token,
                  "completion_claim_at": _iso(now) if now else now_iso(),
                  "updated_at": _iso(now) if now else now_iso()}})
    if not getattr(claim, "modified_count", 0):
        fresh = await db.referral_payout_queue.find_one({"id": item["id"]})
        if fresh and fresh.get("status") == "completed":
            return fresh
        raise ReferralError(
            "This payout is already being completed by another "
            "operator.")
    fee = (PAPER_CHECK_FEE_CENTS
           if item["preference"] == "paper_check" else 0)
    batch_key = (f"melio-batch:{item['batch_id']}:{item['referrer_id']}"
                 f":{item['product']}" if item.get("batch_id")
                 else f"melio-queue:{item['id']}")
    try:
        payout = await _record_payout_now(
            db, item["referrer_id"], item["product"],
            rail=item["rail"], note=f"Melio {item['preference']}",
            by=by, reference=reference, fee_cents=fee,
            batch_key=batch_key, now=now)
    except DuplicateKeyError:
        # This item's payout already exists — a claim raced, or an
        # earlier attempt died after recording. Adopt the record and
        # finish marking the pot paid under it; never record twice.
        payout = await db.referral_payouts.find_one(
            {"batch_key": batch_key})
        if payout is None:
            await _release_completion_claim(item["id"], token)
            raise
        await mark_cleared_paid(item["referrer_id"], item["product"],
                                payout["id"], now=now)
    except Exception:
        await _release_completion_claim(item["id"], token)
        raise
    await db.referral_payout_queue.update_one(
        {"id": item["id"], "completion_claim": token},
        {"$set": {"status": "completed", "payout_id": payout["id"],
                  "gross_cents": payout["gross_cents"],
                  "fee_cents": payout["fee_cents"],
                  "net_cents": payout["net_cents"],
                  "completed_at": _iso(now) if now else now_iso(),
                  "updated_at": _iso(now) if now else now_iso()},
         "$unset": {"completion_claim": "",
                    "completion_claim_at": ""}})
    item.update({"status": "completed", "payout_id": payout["id"],
                 "gross_cents": payout["gross_cents"],
                 "net_cents": payout["net_cents"]})
    return item


async def queue_advance(item_id: str, to_status: str, *, by: str,
                        reference: str = None, note: str = None,
                        now: datetime = None) -> dict:
    """Move a queue item along the Melio state machine. `completed`
    routes through queue_complete (which records the payout); every
    other state is a pure mirror of what the operator saw in Melio."""
    item = await db.referral_payout_queue.find_one({"id": item_id})
    if item is None:
        raise ReferralError("Queue item not found.")
    allowed = QUEUE_TRANSITIONS.get(item["status"], set())
    if to_status not in allowed:
        raise ReferralError(
            f"Cannot move a {item['status']} item to {to_status}.")
    if to_status == "awaiting_claim" and item["preference"] != "bank_transfer":
        raise ReferralError(
            "Only bank-transfer (ACH) payouts wait for a member claim.")
    if to_status == "completed":
        return await queue_complete(item, by=by, reference=reference,
                                    now=now)
    fields = {"status": to_status,
              "updated_at": _iso(now) if now else now_iso()}
    if note:
        fields["note"] = note
    if reference:
        fields["reference"] = reference
    await db.referral_payout_queue.update_one(
        {"id": item_id}, {"$set": fields})
    item.update(fields)
    return item
