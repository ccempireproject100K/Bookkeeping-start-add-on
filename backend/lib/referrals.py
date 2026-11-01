"""Empire referral ledger — the single host implementation of the
referral program law (owner build word 2026-11-01; adopted schedule
R-1, free month R-2, friend benefit R-3).

The law implemented here:

* Tiered schedule per referral, by whole months from the referral's
  schedule anchor: months 1-6 = 25%, 7-12 = 10%, 13-24 = 2%,
  25-36 = 1% of what the referred customer actually paid. Months
  37-48 = 1% ONLY for referrals whose `bonus_year_locked` was stamped
  at their first payment (referrer at/above the 250 active-paying
  threshold at that moment). Beyond the entitlement end: nothing
  (CONFIG tail_open_ended=False).
* Qualification: `qualified_at` is set exactly once, per site, when
  the referrer's 4th distinct paying referred person makes their
  first payment. Prospective only — payments before qualification are
  stored as `pre_qualification` audit entries at zero share (the old
  retroactive backfill is gone). First four referrals' anchors are
  `qualified_at`; later referrals anchor at their own first payment.
  The qualification month counts in full as month 1 of the first
  four's clocks (anchors at `qualified_at`, no proration of earnings
  from the qualification day forward) — that is NOT retroactive:
  payments made before `qualified_at` never earn, in any month, and
  stored `pre_qualification` entries never convert.
* Free month at the 3rd paying referral (R-2): one month of the
  referrer's current paid plan on that site (free member -> Starter
  month), recorded as a grant; not cash, no clock effects; stacks
  sequentially behind an already-active free month on that site.
* Thresholds: 250 active paying referrals stamps the bonus lock
  (active = a payment within CONFIG active_window_days, not cancelled
  after); 100 is a milestone only. Grace (3 months on dips) does NOT
  stamp new locks (grace_locks_bonus=False); locks are never
  retro-fitted (bonus_retrofit=False).
* Clocks are wall-clock continuous: cancel stops earnings, return
  resumes at the current month index; the anchor never moves.
* Friend benefit (R-3) is recorded on the relationship at signup
  through a link: tier_up (one tier above the signup plan for 14 days
  from plan start) or, for top-tier signups, half_off for the first
  two paid months. Books ladder: light -> medium -> heavy (top).
* Payouts trigger only on money actually arrived; 60-day clearing;
  rails unchanged (Stripe Connect batch, Melio queue with the $2
  paper-check fee disclosed, manual recording, pay-file export) —
  only the gating input changed from the old unlock count to
  `qualified_at`. Refund recompute uses the ORIGINAL payment's band
  rate. Paid-out money is never auto-clawed-back; a refund on a
  paid-out earning books a negative adjustment, as before.
* Earned-free-month cash eligibility: a referrer who is not a paying
  member earns cash only for referral payments in a calendar month
  overlapping one of their earned free-month windows on that product
  (standing attestations), credited at whole-month granularity.
* Good standing (ONE empire rule) otherwise unchanged: not disabled
  on any linked product account, not frozen. A chargeback claws back
  that month's earnings and freezes payouts; 3 disputes opened within
  183 days flags the person for manual review.

Shape notes: books conventions (integer cents, ISO timestamps, `id`
fields). Ingest stores raw events immutably in `referral_events`
(deduped on a deterministic key); computation reads those events and
the person/attribution records — never the reverse. Earnings are
first-class documents with deterministic ids (`{product}:{payment_id}`).
"""
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from db import db
from lib.base import new_id, now_iso

# ------------------------------------------------------------------ config
# ONE config block for every program number (draft change 9). Nothing
# below may hard-code a rate, duration, or threshold: read CONFIG.
CONFIG = {
    "qualify_at": 4,
    # (last month of band, rate) — months are 1-based whole months
    # from the referral's schedule anchor. Adopted schedule (R-1).
    "bands": [(6, 0.25), (12, 0.10), (24, 0.02), (36, 0.01)],
    "bonus_band": (48, 0.01),          # months 37-48, locked referrals only
    "bonus_threshold": 250,            # active paying referrals -> lock
    "milestone_threshold": 100,        # milestone only; confers nothing
    "grace_months": 3,                 # dip grace under the threshold
    "clearing_days": 60,
    "review_chargebacks": 3,
    "review_window_days": 183,
    "qualification_scope": "per_site",
    "active_window_days": 45,          # "active paying" for thresholds
    "tail_open_ended": False,          # earnings stop at entitlement end
    "grace_locks_bonus": False,        # grace does NOT stamp new locks
    "bonus_retrofit": False,           # locks never applied retroactively
    "free_track_counts_toward_qualification": True,
    "free_month_stacking": "sequential",
    "free_month_counts_toward_portable_gate": False,
    "friend_benefit_requires_plan_signup": True,
    "friend_benefit_days": 14,
    "friend_benefit_half_off_months": 2,
    "paper_check_fee_cents": 200,
    # Books plan ladder for the friend benefit (R-3): light $18.99 /
    # medium $39.99 standalone; heavy is top (volume/custom priced).
    "books_plan_ladder": ["light", "medium", "heavy"],
    "books_plan_prices_cents": {"light": 1899, "medium": 3999,
                                "heavy": None},
    # Referrer's current-plan fallback for the R-2 free month when the
    # referrer is a free member on the site: a Starter month.
    "free_month_free_member_plan": "starter",
}

CLEARING_DAYS = CONFIG["clearing_days"]
REVIEW_CHARGEBACKS = CONFIG["review_chargebacks"]
REVIEW_WINDOW_DAYS = CONFIG["review_window_days"]
PAPER_CHECK_FEE_CENTS = CONFIG["paper_check_fee_cents"]
# Legacy names kept as config-derived aliases for existing importers.
SHARE = CONFIG["bands"][0][1]
UNLOCK_REFERRALS = CONFIG["qualify_at"]
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



# ------------------------------------------------------- schedule engine

def _whole_months(anchor: datetime, at: datetime) -> int:
    """Whole months elapsed from anchor to `at` (0 = first month)."""
    months = (at.year - anchor.year) * 12 + (at.month - anchor.month)
    if at.day < anchor.day:
        months -= 1
    return max(0, months)


def month_index(anchor_iso, at) -> int:
    """1-based schedule month of `at` on a clock anchored at anchor_iso."""
    return _whole_months(_parse(anchor_iso), _parse(at)) + 1


def band_for(month_idx: int, bonus_locked: bool):
    """(rate, last_month) for a 1-based schedule month, or None when
    the entitlement has ended (tail is NOT open-ended, per config)."""
    for last, rate in CONFIG["bands"]:
        if month_idx <= last:
            return rate
    last, rate = CONFIG["bonus_band"]
    if bonus_locked and month_idx <= last:
        return rate
    return None


def friend_benefit_for(signup_plan: str = None) -> dict:
    """The R-3 friend benefit for a signup on `signup_plan` (books
    ladder). Top tier has no tier above: half off the first two paid
    months instead of the 14-day tier-up."""
    ladder = CONFIG["books_plan_ladder"]
    plan = (signup_plan or "").strip().lower() or None
    if plan not in ladder:
        # Unknown/no plan named: the benefit attaches at plan signup;
        # default to a tier-up from the ladder's first rung.
        plan = ladder[0]
    idx = ladder.index(plan)
    if idx == len(ladder) - 1:
        return {"type": "half_off", "signup_plan": plan,
                "months": CONFIG["friend_benefit_half_off_months"],
                "percent_off": 50}
    return {"type": "tier_up", "signup_plan": plan,
            "tier": ladder[idx + 1],
            "days": CONFIG["friend_benefit_days"],
            "starts": "plan_start"}


def qualified_at_for(person: dict, product: str):
    """This site's qualified_at (ISO) or None. Scope is per_site per
    config; the map lives on the person record."""
    if CONFIG["qualification_scope"] == "per_site":
        return (person.get("qualified_at_by_product") or {}).get(product)
    return person.get("qualified_at")


async def _set_qualified(person: dict, product: str, at_iso: str):
    by = dict(person.get("qualified_at_by_product") or {})
    if by.get(product):
        return person  # set exactly once
    by[product] = at_iso
    fields = {"qualified_at_by_product": by}
    if not person.get("qualified_at"):
        fields["qualified_at"] = at_iso
    await db.empire_persons.update_one(
        {"id": person["id"]}, {"$set": fields})
    person.update(fields)
    return person


async def _attributions_of_referrer(referrer_id: str, product: str):
    return await db.referral_attributions.find(
        {"referrer_id": referrer_id, "product": product}).to_list(10000)


async def _distinct_paying_ids(referrer_id: str, product: str):
    """Distinct referred persons with at least one stored earning of
    any cash/audit status and positive net on this site."""
    ids = set()
    async for e in db.referral_earnings.find(
            {"referrer_id": referrer_id, "product": product}):
        if (e.get("revenue_cents", 0) - (e.get("refunded_cents") or 0)) > 0:
            ids.add(e["referred_id"])
    return ids


async def active_paying_count(referrer_id: str, product: str,
                              at: datetime) -> int:
    """Active paying referrals at `at`: distinct referred persons with
    a payment within CONFIG active_window_days before `at` whose
    account standing is not cancelled after that payment (standing is
    attested via link_account's disabled flag — a cancelled account is
    attested disabled; absent an attestation the payment stands)."""
    window_start = at - timedelta(days=CONFIG["active_window_days"])
    ids = set()
    async for e in db.referral_earnings.find(
            {"referrer_id": referrer_id, "product": product}):
        earned = e.get("earned_at")
        if earned and _parse(earned) >= window_start and _parse(earned) <= at:
            if (e.get("revenue_cents", 0)
                    - (e.get("refunded_cents") or 0)) > 0:
                ids.add(e["referred_id"])
    # Exclude anyone whose account is attested cancelled/disabled now.
    live = set()
    for rid in ids:
        person = await person_by_id(rid)
        facts = standing_facts(person) if person else {}
        if not facts.get("disabled_any"):
            live.add(rid)
    return len(live)


async def threshold_state(referrer_id: str, product: str,
                          at: datetime = None) -> dict:
    """Derived threshold state for a site pot: current active count,
    whether the bonus threshold is currently met, milestone flag, and
    the tracked 3-month grace on dips below the bonus threshold."""
    at = at or _now()
    count = await active_paying_count(referrer_id, product, at)
    person = await person_by_id(referrer_id)
    state = ((person or {}).get("threshold_state") or {}).get(product, {})
    above = count >= CONFIG["bonus_threshold"]
    grace_until = state.get("grace_until")
    in_grace = bool(grace_until and _parse(grace_until) >= at)
    return {"active_paying_referrals": count,
            "bonus_threshold": CONFIG["bonus_threshold"],
            "above_bonus_threshold": above,
            "milestone_reached":
                count >= CONFIG["milestone_threshold"],
            "milestone_threshold": CONFIG["milestone_threshold"],
            "grace_until": grace_until if in_grace else None,
            "in_grace": in_grace and not above}


async def _track_threshold(referrer_id: str, product: str, at: datetime):
    """Persist above_since / grace_until for the bonus threshold so a
    dip starts the 3-month grace clock (tracked, per settled law)."""
    person = await person_by_id(referrer_id)
    if person is None:
        return
    count = await active_paying_count(referrer_id, product, at)
    all_state = dict(person.get("threshold_state") or {})
    st = dict(all_state.get(product) or {})
    if count >= CONFIG["bonus_threshold"]:
        if not st.get("above_since"):
            st["above_since"] = _iso(at)
        st.pop("grace_until", None)
    elif st.get("above_since") and not st.get("grace_until"):
        grace = at.replace()
        # +grace_months calendar months, clamped to month end.
        m = grace.month - 1 + CONFIG["grace_months"]
        year, month = grace.year + m // 12, m % 12 + 1
        day = min(grace.day, [31, 29 if year % 4 == 0 and
                (year % 100 != 0 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        st["grace_until"] = _iso(grace.replace(year=year, month=month,
                                               day=day))
    all_state[product] = st
    await db.empire_persons.update_one(
        {"id": referrer_id}, {"$set": {"threshold_state": all_state}})


# ------------------------------------------------- free months (R-2 + C-7)

async def record_free_month_window(referrer_id: str, product: str, *,
                                   start: str, end: str,
                                   source: str = "attestation",
                                   plan: str = None) -> dict:
    """Store an earned-free-month window (standing attestation). Used
    by the R-2 grant at the 3rd paying referral and by products
    reporting Cheers-bought grant months to the host."""
    doc = {"id": new_id(), "referrer_id": referrer_id,
           "product": product, "start": start, "end": end,
           "source": source, "plan": plan, "created_at": now_iso()}
    await db.referral_free_months.insert_one(doc)
    return doc


async def free_month_windows(referrer_id: str, product: str):
    return await db.referral_free_months.find(
        {"referrer_id": referrer_id, "product": product}).to_list(100)


def _month_overlaps_window(paid_at: datetime, window: dict) -> bool:
    """Whole-month rule: a payment counts when its calendar month
    overlaps the window at all."""
    start, end = _parse(window["start"]), _parse(window["end"])
    month_start = paid_at.replace(day=1, hour=0, minute=0, second=0,
                                  microsecond=0)
    if paid_at.month == 12:
        next_month = month_start.replace(year=paid_at.year + 1, month=1)
    else:
        next_month = month_start.replace(month=paid_at.month + 1)
    return start < next_month and end >= month_start


async def in_free_month(referrer_id: str, product: str,
                        paid_at: datetime) -> bool:
    for w in await free_month_windows(referrer_id, product):
        if _month_overlaps_window(paid_at, w):
            return True
    return False


async def _grant_referral_free_month(referrer: dict, product: str,
                                     at: datetime):
    """R-2: at the 3rd paying referral, grant one month of the
    referrer's current paid plan on this site (free member -> the
    configured Starter month). Sequential stacking: when another free
    month is already active on the site, the grant queues behind it.
    Idempotent per (referrer, product) via a deterministic id."""
    gid = f"grant3:{referrer['id']}:{product}"
    if await db.referral_free_months.find_one({"id": gid}):
        return None
    plan = None
    for a in referrer.get("product_accounts", []):
        if a.get("product") == product and a.get("paying"):
            plan = a.get("plan") or "paid"
    if plan is None:
        plan = CONFIG["free_month_free_member_plan"]
    start = at
    for w in await free_month_windows(referrer["id"], product):
        wend = _parse(w["end"])
        if wend > start:
            start = wend  # sequential: begins when the active one ends
    # One calendar month from start.
    m = start.month
    year, month = (start.year + (1 if m == 12 else 0),
                   1 if m == 12 else m + 1)
    day = min(start.day, [31, 29 if year % 4 == 0 and
            (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    end = start.replace(year=year, month=month, day=day)
    doc = {"id": gid, "referrer_id": referrer["id"], "product": product,
           "start": _iso(start), "end": _iso(end),
           "source": "third_paying_referral", "plan": plan,
           "counts_toward_portable_gate":
               CONFIG["free_month_counts_toward_portable_gate"],
           "created_at": now_iso()}
    await db.referral_free_months.insert_one(doc)
    return doc


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
                            at=None, signup_plan: str = None) -> dict:
    """Register/attest a product account: link it to its person
    (email join key) and, when a referral code is presented, record
    the attribution — once per account; the first code wins. A signup
    through a link records the R-3 friend benefit on the relationship
    (from `signup_plan`, or standing["signup_plan"]). Standing
    attestations may also carry earned-free-month windows
    (standing["free_month_windows"]: [{start, end, source?}])."""
    person = await ensure_person(email)
    person = await link_account(
        person, product=product, account_id=account_id,
        tenant_id=tenant_id, email=email, standing=standing)
    att_id = f"{product}:{account_id}"
    att = await db.referral_attributions.find_one({"id": att_id})
    if att is None:
        plan = signup_plan or (standing or {}).get("signup_plan")
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
            "schedule_anchor_at": None,
            "bonus_year_locked": False,
            "friend_benefit": (friend_benefit_for(plan)
                               if referral_code and plan else None),
        }
        try:
            await db.referral_attributions.insert_one(att)
        except DuplicateKeyError:
            att = await db.referral_attributions.find_one({"id": att_id})
    if att.get("referral_code") and not att.get("referrer_id"):
        att = await _resolve_referrer(att)
        # NOTE: no retroactive backfill. Payments reported before the
        # referrer was resolved stay as raw events; under the new law
        # pre-qualification payments earn nothing in any case, and any
        # payment that should earn arrives (or is replayed) through
        # apply_payment after the link exists.
    if standing and standing.get("free_month_windows") \
            and att.get("referrer_id"):
        for w in standing["free_month_windows"]:
            await record_free_month_window(
                att["referrer_id"], product, start=w["start"],
                end=w["end"], source=w.get("source", "attestation"),
                plan=w.get("plan"))
    return {"person_id": person["id"], "attribution_status": att["status"],
            "referrer_id": att.get("referrer_id"),
            "friend_benefit": att.get("friend_benefit")}


async def _attribution_for(product: str, account_id: str):
    return await db.referral_attributions.find_one(
        {"id": f"{product}:{account_id}"})


# ---------------------------------------------------------------- payments

async def _apply_payment(att: dict, payload: dict):
    amount = int(payload.get("amount_cents") or 0)
    if amount <= 0:
        return None
    paid_at = _parse(payload["paid_at"])
    product = payload["product"]
    referred = await person_by_id(att["person_id"])
    # The payer demonstrably pays: their account counts as paying.
    referred = await link_account(
        referred, product=product,
        account_id=payload["account_id"], standing={"paying": True})
    first = referred.get("first_paid_at")
    first_dt = _parse(first) if first else None
    if first_dt is None or paid_at < first_dt:
        first_dt = paid_at
        await db.empire_persons.update_one(
            {"id": referred["id"]},
            {"$set": {"first_paid_at": _iso(paid_at)}})
    if not att.get("referrer_id"):
        return None  # raw event stored; no one to credit
    referrer = await person_by_id(att["referrer_id"])
    if referrer is None:
        return None
    earning_id = f"{product}:{payload['payment_id']}"
    if await db.referral_earnings.find_one({"id": earning_id}):
        return await db.referral_earnings.find_one({"id": earning_id})

    # Is this the referred person's first stored payment on this site?
    prior = await db.referral_earnings.find_one(
        {"referrer_id": referrer["id"], "product": product,
         "referred_id": att["person_id"]})
    is_first_for_referral = prior is None

    qualified_at = qualified_at_for(referrer, product)

    if is_first_for_referral:
        # Anchor + bonus lock are stamped at the first payment.
        if qualified_at:
            # Referrer already qualified: this referral anchors at its
            # own first payment (it is #5 or later in arrival order of
            # qualification counting when qualified_at precedes it).
            anchor = paid_at
        else:
            anchor = None  # first four: anchor becomes qualified_at
        # Bonus lock: stamped iff the referrer's active-paying count
        # (including this payer) is at/above the threshold right now.
        # Grace does NOT stamp locks (config grace_locks_bonus=False).
        active = await active_paying_count(referrer["id"], product,
                                           paid_at)
        locked = (active + 1) >= CONFIG["bonus_threshold"]
        fields = {"bonus_year_locked": bool(locked)}
        if anchor is not None:
            fields["schedule_anchor_at"] = _iso(anchor)
            att["schedule_anchor_at"] = _iso(anchor)
        att["bonus_year_locked"] = bool(locked)
        await db.referral_attributions.update_one(
            {"id": att["id"]}, {"$set": fields})

    # Distinct paying count including this payment (net > 0 so far is
    # approximated by stored earnings + this one; refunds adjust the
    # ledger, and qualification once set is never unset).
    paying_ids = await _distinct_paying_ids(referrer["id"], product)
    if is_first_for_referral:
        paying_ids.add(att["person_id"])
    paying_now = len(paying_ids)

    # R-2 free month at the 3rd paying referral.
    if is_first_for_referral and paying_now == 3:
        await _grant_referral_free_month(referrer, product, paid_at)

    # Qualification at the 4th distinct paying referred person.
    if qualified_at is None and paying_now >= CONFIG["qualify_at"]:
        qualified_at = _iso(paid_at)
        referrer = await _set_qualified(referrer, product, qualified_at)
        # First four referrals anchor at qualified_at.
        for other in await _attributions_of_referrer(referrer["id"],
                                                     product):
            if not other.get("schedule_anchor_at"):
                await db.referral_attributions.update_one(
                    {"id": other["id"]},
                    {"$set": {"schedule_anchor_at": qualified_at}})
                if other["id"] == att["id"]:
                    att["schedule_anchor_at"] = qualified_at
        # No conversion: stored pre_qualification entries stay at
        # zero forever, regardless of their calendar month (settled
        # law — nothing is paid retroactively).

    anchor_iso = att.get("schedule_anchor_at")
    if qualified_at is not None and anchor_iso is None:
        anchor_iso = qualified_at  # defensive: first-four anchor
    # A payment made before the qualification day never earns, in
    # any month; payments on/after qualified_at earn normally.
    pre_qual = qualified_at is None or paid_at < _parse(qualified_at)

    if pre_qual:
        earning = {
            "id": earning_id,
            "referrer_id": referrer["id"],
            "referred_id": att["person_id"],
            "product": product,
            "account_id": payload["account_id"],
            "payment_id": payload["payment_id"],
            "charge": payload.get("charge"),
            "revenue_cents": amount,
            "refunded_cents": 0,
            "share_cents": 0,
            "rate": 0,
            "month_index": None,
            "schedule_anchor_at": anchor_iso,
            "bonus_year_locked": bool(att.get("bonus_year_locked")),
            "status": "pre_qualification",
            "month": paid_at.strftime("%Y-%m"),
            "earned_at": _iso(paid_at),
        }
        try:
            await db.referral_earnings.insert_one(earning)
        except DuplicateKeyError:
            return await db.referral_earnings.find_one({"id": earning_id})
        await _track_threshold(referrer["id"], product, paid_at)
        return earning

    midx = month_index(anchor_iso, paid_at)
    rate = band_for(midx, bool(att.get("bonus_year_locked")))
    if rate is None:
        status, share, rate_v = "beyond_entitlement", 0, 0
    else:
        rate_v = rate
        # Cash eligibility: good standing, where a non-paying referrer
        # earns cash only inside an earned free-month window.
        facts = standing_facts(referrer)
        frozen = await is_frozen(referrer["id"])
        if facts["disabled_any"] or frozen:
            status, share = "not_eligible", 0
        elif facts["paying_any"]:
            status, share = "pending", round(amount * rate)
        elif await in_free_month(referrer["id"], product, paid_at):
            status, share = "pending", round(amount * rate)
        else:
            status, share = "not_eligible", 0
    earning = {
        "id": earning_id,
        "referrer_id": referrer["id"],
        "referred_id": att["person_id"],
        "product": product,
        "account_id": payload["account_id"],
        "payment_id": payload["payment_id"],
        "charge": payload.get("charge"),
        "revenue_cents": amount,
        "refunded_cents": 0,
        "share_cents": share,
        "rate": rate_v,
        "month_index": midx,
        "schedule_anchor_at": anchor_iso,
        "bonus_year_locked": bool(att.get("bonus_year_locked")),
        "status": status,
        "month": paid_at.strftime("%Y-%m"),
        "earned_at": _iso(paid_at),
    }
    try:
        await db.referral_earnings.insert_one(earning)
    except DuplicateKeyError:
        return await db.referral_earnings.find_one({"id": earning_id})
    await _track_threshold(referrer["id"], product, paid_at)
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
    # Recompute at the ORIGINAL payment's band rate — stored on the
    # earning at creation, never today's band.
    rate = earning.get("rate")
    if rate is None:  # legacy rows from before the schedule engine
        rate = SHARE if earning.get("status") == "pending" else 0
    share = (round(net * rate)
             if earning["status"] not in ("not_eligible",
                                          "pre_qualification",
                                          "beyond_entitlement") else 0)
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

def _bucket_sums(cutoff: str) -> dict:
    """The site pot's per-status share sums as $group accumulators,
    splitting pending earnings at the clearing cutoff. earned_at and
    the cutoff are both `_iso` strings, so the lexicographic compare
    is the chronological compare — the same test the original
    cursor-iterating implementation made in Python."""
    def _sum_when(cond):
        return {"$sum": {"$cond": [cond, "$share_cents", 0]}}
    return {
        "pending": _sum_when({"$and": [
            {"$eq": ["$status", "pending"]},
            {"$gt": ["$earned_at", cutoff]}]}),
        "available": _sum_when({"$and": [
            {"$eq": ["$status", "pending"]},
            {"$lte": ["$earned_at", cutoff]}]}),
        "paid": _sum_when({"$eq": ["$status", "paid"]}),
        "clawed_back": _sum_when({"$eq": ["$status", "clawed_back"]}),
    }


def _paying_stages(match: dict, person_key) -> list:
    """The paying-referral count as aggregation stages: the best
    (largest) net per referred person under `match`, then a count of
    the persons whose best net is positive. `person_key` is the
    per-person $group _id — "$referred_id" when the match already
    pins one (referrer, product), or a key carrying them when the
    caller regroups per site afterwards."""
    return [
        {"$match": match},
        {"$group": {"_id": person_key,
                    "max_net": {"$max": {"$subtract": [
                        "$revenue_cents",
                        {"$ifNull": ["$refunded_cents", 0]}]}}}},
        {"$group": {"_id": None,
                    "n": {"$sum": {"$cond": [
                        {"$gt": ["$max_net", 0]}, 1, 0]}}}},
    ]


async def paying_referral_count(referrer_id: str, product: str) -> int:
    """Distinct referred persons with real money on THIS site: at
    least one earning here that still stands (not clawed back) with
    net revenue above zero. Read from the earnings ledger, never a
    counter that could drift — TC's rule, per site."""
    rows = await db.referral_earnings.aggregate(_paying_stages(
        {"referrer_id": referrer_id, "product": product,
         "status": {"$in": ["pending", "paid", "not_eligible",
                            "pre_qualification"]}},
        "$referred_id")).to_list(1)
    return int(rows[0]["n"]) if rows else 0


async def _site_parts(referrer_id: str, product: str, cutoff: str):
    """(totals, pending_adjustments, paying_count) for one site pot,
    from grouped aggregations — one small result row per query, so
    the read is bounded by the answer, not by the ledger."""
    totals = {"pending": 0, "available": 0, "paid": 0, "clawed_back": 0}
    rows = await db.referral_earnings.aggregate([
        {"$match": {"referrer_id": referrer_id, "product": product}},
        {"$group": {"_id": None, **_bucket_sums(cutoff)}},
    ]).to_list(1)
    if rows:
        for k in totals:
            totals[k] = rows[0].get(k) or 0
    adj = await db.referral_adjustments.aggregate([
        {"$match": {"referrer_id": referrer_id, "product": product,
                    "status": "pending"}},
        {"$group": {"_id": None, "total": {"$sum": "$cents"}}},
    ]).to_list(1)
    adjust = (adj[0].get("total") or 0) if adj else 0
    paying = await paying_referral_count(referrer_id, product)
    return totals, adjust, paying


def _summary_dict(product: str, totals: dict, adjust: int,
                  frozen: bool, paying: int,
                  qualified_at: str = None) -> dict:
    """The site-summary shape, assembled from precomputed parts.
    site_summary and the empire-wide admin read share this so the
    two paths can never drift apart. The pot's gate is qualification
    (qualified_at set), not the old per-site unlock count."""
    locked = qualified_at is None
    cleared = max(0, totals["available"] + adjust)
    return {
        "product": product,
        **totals,
        "adjustments": adjust,
        "earned_cents": totals["pending"] + totals["available"]
                        + totals["paid"],
        "paying_referrals": paying,
        "qualified": not locked,
        "qualified_at": qualified_at,
        "qualify_at": CONFIG["qualify_at"],
        # Legacy aliases (same values) for older readers.
        "unlock_at": CONFIG["qualify_at"],
        "locked": locked,
        # What the gate itself holds: the cleared pot that would be
        # payable today if this site were qualified.
        "locked_cents": cleared if locked else 0,
        "payable": 0 if (frozen or locked) else cleared,
        "frozen": frozen,
        "clearing_days": CLEARING_DAYS,
    }


async def site_summary(referrer_id: str, product: str,
                       now: datetime = None) -> dict:
    """TC's summary buckets for one site pot."""
    now = now or _now()
    cutoff = _iso(now - timedelta(days=CLEARING_DAYS))
    totals, adjust, paying = await _site_parts(referrer_id, product,
                                               cutoff)
    frozen = await is_frozen(referrer_id)
    person = await person_by_id(referrer_id)
    return _summary_dict(product, totals, adjust, frozen, paying,
                         qualified_at=qualified_at_for(person or {},
                                                       product))


async def empire_buckets(now: datetime = None):
    """Every site pot's raw parts for the whole empire, from grouped
    aggregations — the read behind the admin balances page, whose
    cost used to be O(the entire ledger) in Python per page load
    (load test 2026-10-26 §5.1: 4.5 s at 6,445 earnings).

    Returns (earn_totals, adjustments, paying, att_products, frozen):
      earn_totals / adjustments / paying are keyed by
      (referrer_id, product); att_products is the set of
      (referrer_id, product) pairs holding an attribution;
      frozen is the set of referrer ids with an open dispute
      anywhere (the review-flag half of is_frozen is per-person and
      stays with the caller, which holds the person records).
    Site inclusion still follows empire_summary's rule — a product
    appears for a referrer when they have earnings or an attribution
    there — so the adjustments/paying maps only ever supply values
    for included sites, exactly as the per-site reads did."""
    now = now or _now()
    cutoff = _iso(now - timedelta(days=CLEARING_DAYS))
    earn_totals = {}
    async for row in db.referral_earnings.aggregate([
            {"$group": {"_id": {"r": "$referrer_id", "p": "$product"},
                        **_bucket_sums(cutoff)}}]):
        totals = {"pending": 0, "available": 0, "paid": 0,
                  "clawed_back": 0}
        for k in totals:
            totals[k] = row.get(k) or 0
        earn_totals[(row["_id"]["r"], row["_id"]["p"])] = totals
    adjustments = {}
    async for row in db.referral_adjustments.aggregate([
            {"$match": {"status": "pending"}},
            {"$group": {"_id": {"r": "$referrer_id", "p": "$product"},
                        "total": {"$sum": "$cents"}}}]):
        adjustments[(row["_id"]["r"], row["_id"]["p"])] = \
            row.get("total") or 0
    paying = {}
    stages = _paying_stages(
        {"status": {"$in": ["pending", "paid", "not_eligible",
                            "pre_qualification"]}},
        {"r": "$referrer_id", "p": "$product", "d": "$referred_id"})
    # Replace the bare-count tail with a per-site regroup.
    stages[-1] = {"$group": {
        "_id": {"r": "$_id.r", "p": "$_id.p"},
        "n": {"$sum": {"$cond": [{"$gt": ["$max_net", 0]}, 1, 0]}}}}
    async for row in db.referral_earnings.aggregate(stages):
        paying[(row["_id"]["r"], row["_id"]["p"])] = int(row["n"])
    att_products = set()
    async for row in db.referral_attributions.aggregate([
            {"$match": {"referrer_id": {"$ne": None}}},
            {"$group": {"_id": {"r": "$referrer_id",
                                "p": "$product"}}}]):
        att_products.add((row["_id"]["r"], row["_id"]["p"]))
    frozen = set(await db.referral_disputes.distinct(
        "referrer_id", {"status": "open"}))
    return earn_totals, adjustments, paying, att_products, frozen


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
    referrals = []
    async for att in db.referral_attributions.find(
            {"referrer_id": rid}):
        anchor = att.get("schedule_anchor_at")
        band = None
        if anchor:
            rate = band_for(month_index(anchor, now or _now()),
                            bool(att.get("bonus_year_locked")))
            band = rate
        referrals.append({
            "product": att["product"],
            "account_id": att["account_id"],
            "referred_id": att["person_id"],
            "schedule_anchor_at": anchor,
            "current_band_rate": band,
            "bonus_year_locked": bool(att.get("bonus_year_locked")),
            "friend_benefit": att.get("friend_benefit"),
        })
    free_months = []
    async for w in db.referral_free_months.find({"referrer_id": rid}):
        free_months.append({"product": w["product"], "start": w["start"],
                            "end": w["end"], "source": w["source"],
                            "plan": w.get("plan")})
    return {
        "empire_id": rid,
        "code": person.get("code"),
        "eligible": good_standing(person, frozen),
        "under_review": bool(person.get("review")),
        "frozen": frozen,
        "qualified_at": person.get("qualified_at"),
        "qualified_at_by_product":
            person.get("qualified_at_by_product") or {},
        "payout_preference": preference,
        "paper_check_fee_cents": PAPER_CHECK_FEE_CENTS,
        "sites": sites,
        "totals": totals,
        "payable": totals["payable"],
        "payouts": payouts,
        "referrals": referrals,
        "free_months": free_months,
        "terms": terms_payload(),
    }


def terms_payload() -> dict:
    """Everything a product screen may state about the program,
    served from CONFIG so screens never hard-code numbers."""
    return {
        "bands": [{"through_month": last, "rate": rate}
                  for last, rate in CONFIG["bands"]],
        "bonus_band": {"through_month": CONFIG["bonus_band"][0],
                       "rate": CONFIG["bonus_band"][1],
                       "threshold": CONFIG["bonus_threshold"]},
        "months": CONFIG["bands"][-1][0],
        "qualify_at": CONFIG["qualify_at"],
        "qualification_scope": CONFIG["qualification_scope"],
        "bonus_threshold": CONFIG["bonus_threshold"],
        "milestone_threshold": CONFIG["milestone_threshold"],
        "grace_months": CONFIG["grace_months"],
        "clearing_days": CONFIG["clearing_days"],
        "review_chargebacks": CONFIG["review_chargebacks"],
        "review_window_days": CONFIG["review_window_days"],
        "paper_check_fee_cents": PAPER_CHECK_FEE_CENTS,
        "friend_benefit": {"days": CONFIG["friend_benefit_days"],
                           "half_off_months":
                               CONFIG["friend_benefit_half_off_months"],
                           "books_plan_ladder":
                               CONFIG["books_plan_ladder"]},
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
            f"Payouts wait until a member qualifies with "
            f"{CONFIG['qualify_at']} paying referrals on {product} — "
            f"this member has {s['paying_referrals']}. Commissions "
            f"start on the qualification day; nothing is paid "
            f"retroactively.")
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
                tenant = await db.tenants.find_one({"id": tenant_id})
                await apply_attribution(
                    product="books", account_id=tenant_id,
                    email=row.get("referred_email"), referral_code=code,
                    tenant_id=tenant_id,
                    signup_plan=(tenant or {}).get("plan"))
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
