"""DataClean paid-data verification (P9 email / P10 phone / P11
address) — CODE ONLY in this build: env-gated, honest-off, no live
calls were made and no provider accounts exist yet. Activation waits
on the owner's provider picks + keys through the secure route
(DESIGN §8.1).

The shape, identical for all three lanes (DESIGN §3):

  * A lookup result is attached to the record as EVIDENCE with a
    source and a timestamp (`record["verifications"][kind]`). It
    never edits a field, never deletes a record, never fires
    silently: `undeliverable` is a flag for the human, and a
    provider-suggested ZIP+4 rides along as a suggestion only.
  * Every run is preceded by a PRE-FLIGHT ESTIMATE — distinct
    uncached values x the published per-lookup rate — that the
    customer confirms before a single lookup fires (verify jobs pause
    in `awaiting_confirmation`; the sync endpoint answers the
    estimate until `confirm_estimate` is set).
  * Results are CACHED per value (SHA-256 of the normalized value,
    ~30-day TTL, tenant-scoped): the same value is never paid for
    twice across a customer's lists.
  * A provider outage marks lookups `unavailable` and the job still
    completes — never faked, never blocking the clean.
  * Every confirmed run meters through dataclean_api_usage.

Published entry rates (vendor pricing pages, read 2026-11-03 for the
design): NeverBounce $0.008/email (unknowns billed), ZeroBounce
~$0.008 (unknowns free — the selection criterion is the owner's),
Twilio Lookup line-type $0.008, Smarty ~$0.017/lookup at entry
($17 per 1,000/mo). These are COST rates for the estimate, not
retail prices — the retail shape is an owner decision.

Provider roster (all env-gated, honest-off):
  * email   — NeverBounce / ZeroBounce (DATACLEAN_EMAIL_VERIFY_
              PROVIDER + the provider's DATACLEAN_* key), Bouncer
              (same selector + BOUNCER_API_KEY; single + batch).
  * phone   — Twilio Lookup (DATACLEAN_TWILIO_*).
  * address — Smarty (DATACLEAN_SMARTY_*) or Google Address
              Validation (GOOGLE_ADDRESS_VALIDATION_API_KEY),
              selected by DATACLEAN_ADDRESS_VERIFY_PROVIDER.
A provider outage / auth failure (including Bouncer's team-
verification hold answering 401/403) marks lookups `unavailable` —
never a faked verdict, never "clean".

Markup: the owner's ruling is pass-through + a TINY markup on
verification lookups. The mechanism lives here: DATACLEAN_VERIFY_
MARKUP_BPS (basis points, integer >= 0) is applied on top of the
provider cost rate in the estimate, and the estimate payload
surfaces cost, markup and marked-up price side by side. DEFAULT 0
— pure pass-through until the owner stamps a markup size.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

from db import db
from lib import dataclean_api as dca
from lib import dataclean_jobs as jobs_lib
from lib import dataclean_lists as lists_lib
from lib.base import Scoped, new_id, now_iso

KINDS = ("email", "phone", "address")
FIELD_FOR_KIND = {"email": "email", "phone": "phone",
                  "address": "address"}
RATES = {"email": 0.008, "phone": 0.008, "address": 0.017}
CACHE_TTL_DAYS = 30


def http_json(method, url, params=None, headers=None, auth=None,
              json_body=None):
    """Module-level HTTP seam (tests monkeypatch this; production
    uses requests). Returns (status, parsed_body_dict)."""
    import requests
    resp = requests.request(method, url, params=params,
                            headers=headers, auth=auth, json=json_body,
                            timeout=20)
    try:
        body = resp.json()
    except Exception:
        body = {}
    if isinstance(body, list):
        body = {"results": body}
    return resp.status_code, body if isinstance(body, dict) else {}


# ------------------------------------------------------------------
# Provider configuration (env-gated, honest-off)
# ------------------------------------------------------------------

def email_provider():
    name = (os.environ.get("DATACLEAN_EMAIL_VERIFY_PROVIDER")
            or "").strip().lower()
    if name == "neverbounce" and os.environ.get(
            "DATACLEAN_NEVERBOUNCE_API_KEY"):
        return "neverbounce"
    if name == "zerobounce" and os.environ.get(
            "DATACLEAN_ZEROBOUNCE_API_KEY"):
        return "zerobounce"
    if name == "bouncer" and os.environ.get("BOUNCER_API_KEY"):
        return "bouncer"
    return None


def google_address_key():
    return (os.environ.get("GOOGLE_ADDRESS_VALIDATION_API_KEY")
            or os.environ.get(
                "DATACLEAN_GOOGLE_ADDRESS_VALIDATION_API_KEY"))


def address_provider():
    sel = (os.environ.get("DATACLEAN_ADDRESS_VERIFY_PROVIDER")
           or "").strip().lower()
    smarty = bool(os.environ.get("DATACLEAN_SMARTY_AUTH_ID")
                  and os.environ.get("DATACLEAN_SMARTY_AUTH_TOKEN"))
    google = bool(google_address_key())
    if sel in ("google", "google_address_validation"):
        return "google_address_validation" if google else None
    if sel == "smarty":
        return "smarty" if smarty else None
    if sel:
        return None
    if smarty:
        return "smarty"
    if google:
        return "google_address_validation"
    return None


def provider_for(kind):
    if kind == "email":
        return email_provider()
    if kind == "phone":
        if (os.environ.get("DATACLEAN_TWILIO_ACCOUNT_SID")
                and os.environ.get("DATACLEAN_TWILIO_AUTH_TOKEN")):
            return "twilio_lookup"
        return None
    if kind == "address":
        return address_provider()
    return None


def markup_bps():
    """Owner-ruled markup on verification lookups, in basis points
    (DATACLEAN_VERIFY_MARKUP_BPS, integer >= 0). DEFAULT 0: pure
    pass-through — the mechanism never invents a markup size; a
    malformed or negative value is treated as 0, never an error."""
    try:
        bps = int((os.environ.get("DATACLEAN_VERIFY_MARKUP_BPS")
                   or "0").strip())
    except (TypeError, ValueError):
        return 0
    return max(bps, 0)


def marked_up_rate(kind, bps=None):
    """The customer-facing per-lookup rate: provider cost + markup."""
    bps = markup_bps() if bps is None else bps
    return RATES[kind] * (1 + bps / 10000.0)


def configured_kinds(kinds=None):
    kinds = kinds or KINDS
    return [k for k in kinds if provider_for(k)]


def any_configured(kinds=None):
    return bool(configured_kinds(kinds))


# ------------------------------------------------------------------
# Value normalization + cache
# ------------------------------------------------------------------

def normalize_value(kind, value):
    v = str(value or "").strip()
    if kind == "email":
        return v.lower()
    if kind == "phone":
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits
    return " ".join(v.lower().split())


def _value_hash(kind, normalized):
    return hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()


def _cache(actor):
    return Scoped(db.dataclean_lookup_cache, actor["tenant_id"])


async def _cache_get(actor, kind, normalized):
    h = _value_hash(kind, normalized)
    doc = await _cache(actor).find_one({"kind": kind, "value_hash": h})
    if doc and (doc.get("expires_at") or "") > now_iso():
        return doc.get("result")
    return None


async def _cache_put(actor, kind, normalized, result, provider):
    h = _value_hash(kind, normalized)
    expires = (datetime.now(timezone.utc)
               + timedelta(days=CACHE_TTL_DAYS)).isoformat()
    existing = await _cache(actor).find_one({"kind": kind,
                                             "value_hash": h})
    doc = {"id": (existing or {}).get("id") or new_id(),
           "kind": kind, "value_hash": h, "result": result,
           "provider": provider, "created_at": now_iso(),
           "expires_at": expires}
    if existing:
        await _cache(actor).update_one(
            {"id": doc["id"]},
            {"$set": {k: v for k, v in doc.items() if k != "id"}})
    else:
        await _cache(actor).insert_one(doc)


# ------------------------------------------------------------------
# Provider adapters (recorded-response shapes; no live calls in CI)
# ------------------------------------------------------------------

_EMAIL_MAP = {
    "valid": "deliverable", "invalid": "undeliverable",
    "disposable": "risky", "catchall": "risky", "catch-all": "risky",
    "unknown": "unknown", "abuse": "risky", "do_not_mail": "risky",
    "spamtrap": "risky",
}


_BOUNCER_MAP = {"deliverable": "deliverable",
                "undeliverable": "undeliverable",
                "risky": "risky", "unknown": "unknown"}
BOUNCER_BATCH_MIN = 2
_BATCH_POLLS = 5
_BATCH_POLL_SECONDS = 2


def _bouncer_result(item):
    raw = str(item.get("status") or "unknown").lower()
    out = {"status": _BOUNCER_MAP.get(raw, "unknown"),
           "provider_status": raw}
    if item.get("reason"):
        out["reason"] = str(item["reason"])
    return out


def _lookup_email_bouncer(normalized):
    from urllib.parse import quote
    status, body = http_json(
        "GET", "https://api.usebouncer.com/v1.1/email/verify/"
        + quote(normalized, safe=""),
        headers={"x-api-key": os.environ["BOUNCER_API_KEY"]})
    if status != 200:
        # Auth failure, the account's team-verification hold, or an
        # outage: surfaces as unavailable, NEVER as a verdict.
        return None
    return _bouncer_result(body)


def _bouncer_batch_results(normalized_values):
    """One Bouncer batch for many emails -> {normalized: result},
    or None on any failure (the caller falls back to the ordinary
    per-value path, which applies the same honesty rules)."""
    import time
    headers = {"x-api-key": os.environ["BOUNCER_API_KEY"]}
    status, body = http_json(
        "POST", "https://api.usebouncer.com/v1.1/email/verify/batch",
        headers=headers,
        json_body={"emails": list(normalized_values)})
    if status not in (200, 201, 202):
        return None
    results = body.get("results")
    batch_id = body.get("id") or body.get("batch_id")
    polls = 0
    while results is None and batch_id and polls < _BATCH_POLLS:
        status, body = http_json(
            "GET", "https://api.usebouncer.com/v1.1/email/verify/"
            f"batch/{batch_id}", headers=headers)
        if status != 200:
            return None
        results = body.get("results")
        if results is None:
            polls += 1
            if polls < _BATCH_POLLS:
                time.sleep(_BATCH_POLL_SECONDS)
    if results is None:
        return None
    out = {}
    for item in results:
        em = normalize_value("email", item.get("email"))
        if em:
            out[em] = _bouncer_result(item)
    return out


def _lookup_email(normalized):
    provider = email_provider()
    if provider == "bouncer":
        return _lookup_email_bouncer(normalized)
    if provider == "neverbounce":
        status, body = http_json(
            "GET", "https://api.neverbounce.com/v4/single/check",
            params={"key": os.environ["DATACLEAN_NEVERBOUNCE_API_KEY"],
                    "email": normalized})
        if status != 200:
            return None
        raw = str(body.get("result") or "unknown").lower()
    else:
        status, body = http_json(
            "GET", "https://api.zerobounce.net/v2/validate",
            params={"api_key": os.environ["DATACLEAN_ZEROBOUNCE_API_KEY"],
                    "email": normalized})
        if status != 200:
            return None
        raw = str(body.get("status") or "unknown").lower()
    return {"status": _EMAIL_MAP.get(raw, "unknown"),
            "provider_status": raw}


def _lookup_phone(normalized):
    sid = os.environ["DATACLEAN_TWILIO_ACCOUNT_SID"]
    token = os.environ["DATACLEAN_TWILIO_AUTH_TOKEN"]
    e164 = "+1" + normalized if len(normalized) == 10 else "+" + normalized
    status, body = http_json(
        "GET", f"https://lookups.twilio.com/v2/PhoneNumbers/{e164}",
        params={"Fields": "line_type_intelligence"},
        auth=(sid, token))
    if status != 200:
        return None
    lti = body.get("line_type_intelligence") or {}
    return {"status": "valid" if body.get("valid") else "invalid",
            "line_type": lti.get("type"),
            "carrier": lti.get("carrier_name")}


def _lookup_address_google(normalized, raw):
    status, body = http_json(
        "POST",
        "https://addressvalidation.googleapis.com/v1:validateAddress",
        params={"key": google_address_key()},
        json_body={"address": {"regionCode": "US",
                               "addressLines": [str(raw or "")]}})
    if status != 200:
        return None
    result = body.get("result") or {}
    address = result.get("address") or {}
    if not address:
        return {"status": "not_found", "validation_granularity": None}
    verdict = result.get("verdict") or {}
    granularity = verdict.get("validationGranularity")
    if (granularity in ("PREMISE", "SUB_PREMISE")
            and verdict.get("addressComplete")):
        mapped = "deliverable"
    elif granularity in ("PREMISE", "SUB_PREMISE", "ROUTE"):
        mapped = "partial"
    else:
        mapped = "not_found"
    out = {"status": mapped, "validation_granularity": granularity,
           "address_complete": bool(verdict.get("addressComplete"))}
    postal = (address.get("postalAddress") or {}).get("postalCode")
    if postal:
        # Same rule as Smarty's ZIP+4: a SUGGESTION riding as
        # evidence, never written into the record's fields.
        out["suggested_postal_code"] = postal
    return out


def _lookup_address(normalized, raw):
    if provider_for("address") == "google_address_validation":
        return _lookup_address_google(normalized, raw)
    return _lookup_address_smarty(normalized, raw)


def _lookup_address_smarty(normalized, raw):
    status, body = http_json(
        "GET", "https://us-street.api.smarty.com/street-address",
        params={"auth-id": os.environ["DATACLEAN_SMARTY_AUTH_ID"],
                "auth-token": os.environ["DATACLEAN_SMARTY_AUTH_TOKEN"],
                "candidates": 1, "street": str(raw or ""),
                "match": "enhanced"})
    if status != 200:
        return None
    results = body.get("results") or []
    if not results:
        return {"status": "not_found", "dpv_match_code": None}
    top = results[0]
    analysis = top.get("analysis") or {}
    code = analysis.get("dpv_match_code")
    mapped = {"Y": "deliverable", "S": "partial", "D": "partial",
              "N": "not_found"}.get(code or "", "unknown")
    out = {"status": mapped, "dpv_match_code": code,
           "dpv_footnotes": analysis.get("dpv_footnotes")}
    components = top.get("components") or {}
    if components.get("zipcode"):
        # A provider correction is a SUGGESTION riding as evidence —
        # it is never written into the record's fields from here.
        out["suggested_postal_code"] = (
            components["zipcode"]
            + (f"-{components['plus4_code']}"
               if components.get("plus4_code") else ""))
    return out


_LOOKUPS = {"email": lambda n, raw: _lookup_email(n),
            "phone": lambda n, raw: _lookup_phone(n),
            "address": _lookup_address}


async def lookup_value(actor, kind, raw):
    """One value through cache -> provider. Returns (result_dict,
    origin) where origin is 'cache' | 'provider' | 'unavailable'.
    The result is evidence-shaped: {status, source, at, ...}."""
    normalized = normalize_value(kind, raw)
    if not normalized:
        return None, "empty"
    cached = await _cache_get(actor, kind, normalized)
    if cached is not None:
        return cached, "cache"
    provider = provider_for(kind)
    try:
        raw_result = _LOOKUPS[kind](normalized, raw)
    except Exception:
        raw_result = None
    if raw_result is None:
        # Outage / provider refusal: marked, never faked, not cached
        # (the next run may succeed).
        return {"status": "unavailable", "source": provider,
                "at": now_iso()}, "unavailable"
    result = {**raw_result, "source": provider, "at": now_iso()}
    await _cache_put(actor, kind, normalized, result, provider)
    return result, "provider"


# ------------------------------------------------------------------
# Estimates (pre-flight — the honesty feature)
# ------------------------------------------------------------------

async def _distinct_values(actor, list_id, kinds):
    rows = await lists_lib.list_records(actor, list_id, limit=1000000)
    values = {k: {} for k in kinds}  # kind -> normalized -> raw sample
    for r in rows:
        if r.get("status") in ("merged", "deleted"):
            continue
        for k in kinds:
            raw = r.get(FIELD_FOR_KIND[k])
            if raw:
                n = normalize_value(k, raw)
                if n:
                    values[k].setdefault(n, raw)
    return values


async def estimate_for_list(actor, list_id, kinds=None):
    """The pre-flight: per kind, distinct values, how many are
    already cached (free), how many would be looked up, and the cost
    at the published entry rate. NO provider calls are made here."""
    kinds = [k for k in (kinds or KINDS) if provider_for(k)]
    values = await _distinct_values(actor, list_id, kinds)
    bps = markup_bps()
    per_kind, total_lookups, total_cost, total_markup = {}, 0, 0.0, 0.0
    for k in kinds:
        cached = 0
        for n in values[k]:
            if await _cache_get(actor, k, n) is not None:
                cached += 1
        lookups = len(values[k]) - cached
        cost = round(lookups * RATES[k], 4)
        price = round(lookups * marked_up_rate(k, bps), 4)
        markup = round(price - cost, 4)
        per_kind[k] = {"provider": provider_for(k),
                       "distinct_values": len(values[k]),
                       "cached": cached, "lookups": lookups,
                       "rate_usd": RATES[k], "cost_usd": cost,
                       "markup_bps": bps,
                       "price_rate_usd": round(marked_up_rate(k, bps), 6),
                       "markup_usd": markup, "price_usd": price}
        total_lookups += lookups
        total_cost += cost
        total_markup += markup
    return {"currency": "USD", "kinds": per_kind,
            "total_lookups": total_lookups,
            "total_cost_usd": round(total_cost, 4),
            "markup_bps": bps,
            "total_markup_usd": round(total_markup, 4),
            "total_price_usd": round(total_cost + total_markup, 4),
            "note": ("Estimate at published entry rates; cached values "
                     "are never re-billed. Actual provider billing "
                     "depends on the provider's unknowns policy. "
                     "total_cost_usd is the provider cost; "
                     "total_price_usd adds the configured markup "
                     f"({bps} bps; 0 = pure pass-through).")}


# ------------------------------------------------------------------
# Job integration (the Phase B skeleton's verify contract)
# ------------------------------------------------------------------

async def prepare_verify_job(actor, job):
    """process_job's verify branch: compute the estimate, stash it on
    the job doc (and the passed dict — process_job re-reads it), and
    pause. Returns (counts, stats)."""
    kinds = [k for k in (job.get("options") or {}).get("kinds", KINDS)
             if provider_for(k)]
    estimate = await estimate_for_list(actor, job["list_id"], kinds)
    job["estimate"] = estimate
    await jobs_lib._mark(actor, job["id"], estimate=estimate)
    counts = {"distinct_values": sum(
        v["distinct_values"] for v in estimate["kinds"].values()),
        "cached": sum(v["cached"] for v in estimate["kinds"].values()),
        "lookups_needed": estimate["total_lookups"]}
    return counts, {"kinds": kinds, "estimate": estimate}


async def _prewarm_bouncer_batch(actor, rows, kinds):
    """When the email lane runs on Bouncer, verify the distinct
    uncached emails in ONE provider batch and seed the per-value
    cache; the per-record pass then reads them back as ordinary
    cache hits. Any batch failure simply leaves the per-value
    single-lookup path to cope under the same honesty rules."""
    if "email" not in kinds or email_provider() != "bouncer":
        return
    wanted = {}
    for r in rows:
        if r.get("status") in ("merged", "deleted"):
            continue
        raw = r.get(FIELD_FOR_KIND["email"])
        if raw:
            n = normalize_value("email", raw)
            if n:
                wanted.setdefault(n, raw)
    pending = [n for n in wanted
               if await _cache_get(actor, "email", n) is None]
    if len(pending) < BOUNCER_BATCH_MIN:
        return
    try:
        results = _bouncer_batch_results(pending)
    except Exception:
        results = None
    if not results:
        return
    for n, raw_result in results.items():
        result = {**raw_result, "source": "bouncer", "at": now_iso()}
        await _cache_put(actor, "email", n, result, "bouncer")


async def confirm_verify_job(actor, job_id):
    """Run the lookups for an awaiting_confirmation verify job:
    evidence onto each record (values never edited), one artifact per
    verified record, usage metered, job completed. Returns
    (job_view_counts, error|None)."""
    job = await jobs_lib.get_job(actor, job_id)
    if not job or job.get("kind") != "verify":
        return None, "not_a_verify_job"
    if job.get("status") != "awaiting_confirmation":
        return None, f"job is {job.get('status')}"
    kinds = [k for k in (job.get("options") or {}).get("kinds", KINDS)
             if provider_for(k)]
    rows = await lists_lib.list_records(actor, job["list_id"],
                                        limit=1000000)
    await _prewarm_bouncer_batch(actor, rows, kinds)
    records = lists_lib._records(actor)
    artifacts = jobs_lib._artifacts(actor)
    counts = {"records_verified": 0, "lookups_made": 0,
              "cache_hits": 0, "unavailable": 0}
    by_status = {}
    seq = 0
    for r in rows:
        if r.get("status") in ("merged", "deleted"):
            continue
        results = {}
        for k in kinds:
            raw = r.get(FIELD_FOR_KIND[k])
            if not raw:
                continue
            result, origin = await lookup_value(actor, k, raw)
            if result is None:
                continue
            results[k] = result
            if origin == "provider":
                counts["lookups_made"] += 1
            elif origin == "cache":
                counts["cache_hits"] += 1
            if result.get("status") == "unavailable":
                counts["unavailable"] += 1
            by_status[f"{k}:{result.get('status')}"] = (
                by_status.get(f"{k}:{result.get('status')}", 0) + 1)
        if results:
            merged_evidence = dict(r.get("verifications") or {})
            merged_evidence.update(results)
            await records.update_one(
                {"id": r["id"]},
                {"$set": {"verifications": merged_evidence}})
            seq += 1
            await artifacts.insert_one(
                {"id": new_id(), "job_id": job_id,
                 "kind": "verification", "seq": seq,
                 "record_id": r["id"], "results": results})
            counts["records_verified"] += 1
    counts["by_status"] = by_status
    await jobs_lib._mark(actor, job_id, status="complete",
                         finished_at=now_iso(), result_counts=counts)
    await dca.record_usage(actor, "jobs/verify",
                           job.get("rows_total", 0), "ok")
    return counts, None
