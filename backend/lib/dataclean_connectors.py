"""DataClean CRM connectors (P4): HubSpot first, Salesforce second.

One shape for every connector (DESIGN §P4):

  OAuth per tenant -> READ-ONLY PULL (contacts/companies) into a
  stored list -> the customer cleans + dedupes locally through the
  existing engine and the Phase B queue -> WRITE-BACK of only the
  human-approved merges from that queue, field writes limited to the
  same fill-blanks-only rule as internal merges (the merge doc's
  recorded `fills` — never a field the human didn't approve, never a
  non-blank value). No connector ever auto-merges inside a CRM: the
  CRM-side merge call happens only as part of an approved write-back.

Honesty rules:
  * Unconfigured (no OAuth app credentials in env) answers
    not_configured — the connector is code-complete but off until the
    owner registers the OAuth apps and supplies the secrets through
    the secure route. No signups were made for this build.
  * Salesforce API access is EDITION-GATED on the customer's own
    Salesforce plan; several common editions have no API access at
    all. The connector probes and DECLINES with the reason printed
    rather than failing mysteriously, and its write-back is the
    merge-decision export only (the customer applies decisions in
    Salesforce themselves) — no silent partial sync.
  * A filled display name is never written back to HubSpot: contacts
    split first/last name, and splitting a display name would rewrite
    it. Skips are reported per merge, never silent.
  * OAuth tokens live only in the tenant-scoped
    dataclean_connector_tokens collection and are never returned by
    any view.
"""
import base64
import hashlib
import hmac
import os
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from db import db
from lib import dataclean_lists as lists_lib
from lib.base import Scoped, new_id, now_iso

PROVIDERS = ("hubspot", "salesforce")

_HS = "https://api.hubapi.com"
_SF_VERSION = "v59.0"


def http_json(method, url, token=None, payload=None, data=None,
              params=None):
    """Module-level HTTP seam (tests monkeypatch this; production
    uses requests, already a backend dependency). Returns
    (status_code, parsed_json_or_{})."""
    import requests
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, json=payload, data=data,
                            params=params, headers=headers, timeout=30)
    try:
        body = resp.json()
    except Exception:
        body = {}
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]  # Salesforce error shape: a list of error dicts
    return resp.status_code, body if isinstance(body, dict) else {}


# ------------------------------------------------------------------
# Configuration (env-gated, honest-off)
# ------------------------------------------------------------------

def provider_config(provider):
    if provider == "hubspot":
        return {
            "client_id": os.environ.get("DATACLEAN_HUBSPOT_CLIENT_ID"),
            "client_secret": os.environ.get(
                "DATACLEAN_HUBSPOT_CLIENT_SECRET"),
            "redirect_uri": os.environ.get(
                "DATACLEAN_HUBSPOT_REDIRECT_URI"),
            "authorize_url": "https://app.hubspot.com/oauth/authorize",
            "token_url": f"{_HS}/oauth/v1/token",
            "scopes": ("crm.objects.contacts.read "
                       "crm.objects.contacts.write "
                       "crm.objects.companies.read "
                       "crm.objects.companies.write oauth"),
        }
    if provider == "salesforce":
        return {
            "client_id": os.environ.get("DATACLEAN_SALESFORCE_CLIENT_ID"),
            "client_secret": os.environ.get(
                "DATACLEAN_SALESFORCE_CLIENT_SECRET"),
            "redirect_uri": os.environ.get(
                "DATACLEAN_SALESFORCE_REDIRECT_URI"),
            "authorize_url":
                "https://login.salesforce.com/services/oauth2/authorize",
            "token_url":
                "https://login.salesforce.com/services/oauth2/token",
            "scopes": "api refresh_token",
        }
    return None


def configured(provider):
    cfg = provider_config(provider)
    return bool(cfg and cfg["client_id"] and cfg["client_secret"]
                and cfg["redirect_uri"])


# ------------------------------------------------------------------
# OAuth state + token storage
# ------------------------------------------------------------------

def _state_for(actor, provider, cfg):
    raw = f"{actor['tenant_id']}:{provider}:{_secrets.token_urlsafe(12)}"
    sig = hmac.new(cfg["client_secret"].encode(), raw.encode(),
                   hashlib.sha256).hexdigest()
    return (base64.urlsafe_b64encode(raw.encode()).decode()
            + "." + sig)


def _verify_state(actor, provider, cfg, state):
    try:
        raw_b64, sig = state.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64.encode()).decode()
    except Exception:
        return False
    expected = hmac.new(cfg["client_secret"].encode(), raw.encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    tenant, prov, _nonce = raw.split(":", 2)
    return tenant == actor["tenant_id"] and prov == provider


def authorize_url(actor, provider):
    cfg = provider_config(provider)
    state = _state_for(actor, provider, cfg)
    from urllib.parse import urlencode
    q = urlencode({"client_id": cfg["client_id"],
                   "redirect_uri": cfg["redirect_uri"],
                   "scope": cfg["scopes"], "state": state,
                   "response_type": "code"})
    return f"{cfg['authorize_url']}?{q}"


def _tokens(actor):
    return Scoped(db.dataclean_connector_tokens, actor["tenant_id"])


async def _store_token(actor, provider, tok, extra=None):
    expires_in = int(tok.get("expires_in") or 0)
    expires_at = None
    if expires_in:
        expires_at = (datetime.now(timezone.utc)
                      + timedelta(seconds=expires_in)).isoformat()
    existing = await _tokens(actor).find_one({"provider": provider})
    doc = {"id": (existing or {}).get("id") or new_id(),
           "provider": provider, "status": "connected",
           "access_token": tok.get("access_token"),
           "refresh_token": tok.get("refresh_token")
           or (existing or {}).get("refresh_token"),
           "expires_at": expires_at,
           "updated_at": now_iso()}
    if extra:
        doc.update(extra)
    if existing:
        await _tokens(actor).update_one(
            {"id": doc["id"]},
            {"$set": {k: v for k, v in doc.items() if k != "id"}})
    else:
        await _tokens(actor).insert_one(doc)
    return doc


async def handle_callback(actor, provider, code, state):
    """Exchange the OAuth code and store the token. Returns (view,
    error_code). The view NEVER carries token material."""
    cfg = provider_config(provider)
    if not _verify_state(actor, provider, cfg, state or ""):
        return None, "invalid_state"
    status, tok = http_json(
        "POST", cfg["token_url"],
        data={"grant_type": "authorization_code", "code": code,
              "client_id": cfg["client_id"],
              "client_secret": cfg["client_secret"],
              "redirect_uri": cfg["redirect_uri"]})
    if status != 200 or not tok.get("access_token"):
        return None, "token_exchange_failed"
    extra = {}
    if provider == "salesforce":
        extra["instance_url"] = tok.get("instance_url")
    if provider == "hubspot":
        extra["portal_id"] = tok.get("hub_id")
    doc = await _store_token(actor, provider, tok, extra)
    return {"provider": provider, "status": doc["status"],
            "connected_at": doc["updated_at"]}, None


async def connection_view(actor, provider):
    doc = await _tokens(actor).find_one({"provider": provider})
    return {"provider": provider,
            "configured": configured(provider),
            "connected": bool(doc and doc.get("status") == "connected"),
            "status": (doc or {}).get("status") or "not_connected",
            "updated_at": (doc or {}).get("updated_at")}


async def _access_token(actor, provider):
    """The stored access token, refreshed when expired. None when not
    connected (or when the refresh failed — the doc is then marked
    expired so the status view tells the truth)."""
    doc = await _tokens(actor).find_one({"provider": provider})
    if not doc or doc.get("status") != "connected":
        return None, doc
    exp = doc.get("expires_at")
    if exp and exp <= now_iso() and doc.get("refresh_token"):
        cfg = provider_config(provider)
        data = {"grant_type": "refresh_token",
                "refresh_token": doc["refresh_token"],
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"]}
        status, tok = http_json("POST", cfg["token_url"], data=data)
        if status == 200 and tok.get("access_token"):
            doc = await _store_token(actor, provider, tok)
        else:
            await _tokens(actor).update_one(
                {"id": doc["id"]}, {"$set": {"status": "expired"}})
            return None, doc
    return doc.get("access_token"), doc


# ------------------------------------------------------------------
# HubSpot
# ------------------------------------------------------------------

_HS_CONTACT_PROPS = ("firstname,lastname,email,phone,address,city,"
                     "state,zip,country,company")
_HS_COMPANY_PROPS = "name,domain,phone,address,city,state,zip,country"


def _hs_address(props):
    parts = [props.get("address"), props.get("city"),
             props.get("state"), props.get("zip"),
             props.get("country")]
    return ", ".join(str(p) for p in parts if p) or None


def _hs_contact_record(obj):
    p = obj.get("properties") or {}
    name = " ".join(x for x in (p.get("firstname"), p.get("lastname"))
                    if x).strip() or p.get("company") or ""
    rec = {"name": name, "external_id": obj.get("id"),
           "external_source": "hubspot:contacts"}
    if p.get("email"):
        rec["email"] = p["email"]
    if p.get("phone"):
        rec["phone"] = p["phone"]
    addr = _hs_address(p)
    if addr:
        rec["address"] = addr
    return rec


def _hs_company_record(obj):
    p = obj.get("properties") or {}
    rec = {"name": p.get("name") or "", "external_id": obj.get("id"),
           "external_source": "hubspot:companies"}
    if p.get("phone"):
        rec["phone"] = p["phone"]
    addr = _hs_address(p)
    if addr:
        rec["address"] = addr
    return rec


async def pull_hubspot(actor, obj="contacts", limit=200):
    token, _doc = await _access_token(actor, "hubspot")
    if not token:
        return None, "not_connected"
    mapper = _hs_contact_record if obj == "contacts" else _hs_company_record
    props = _HS_CONTACT_PROPS if obj == "contacts" else _HS_COMPANY_PROPS
    records, after = [], None
    while len(records) < limit:
        params = {"limit": min(100, limit - len(records)),
                  "properties": props}
        if after:
            params["after"] = after
        status, body = http_json(
            "GET", f"{_HS}/crm/v3/objects/{obj}",
            token=token, params=params)
        if status != 200:
            return None, f"hubspot_pull_failed_{status}"
        for o in body.get("results") or []:
            records.append(mapper(o))
        after = ((body.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    kind = "customers" if obj == "contacts" else "vendors"
    lst = await lists_lib.create_list(
        actor, f"HubSpot {obj} pull", kind=kind, source="hubspot")
    await lists_lib.append_records(actor, lst, records)
    return {"list_id": lst["id"], "pulled": len(records),
            "object": obj}, None


_HS_FILL_PROPS = {"email": "email", "phone": "phone", "address": "address"}


async def writeback_hubspot(actor, list_id, obj="contacts"):
    """Apply APPROVED merges to HubSpot: for each active merge in the
    list not yet written back — PATCH the survivor's approved blank
    fills, then call HubSpot's native merge. Returns a per-merge
    report; unapproved pairs are never touched."""
    token, _doc = await _access_token(actor, "hubspot")
    if not token:
        return None, "not_connected"
    lst = await lists_lib.get_list(actor, list_id)
    if not lst:
        return None, "list_not_found"
    records = lists_lib._records(actor)
    merges = await lists_lib.list_merges(actor, list_id)
    results, written = [], 0
    for m in merges:
        if m.get("status") != "active" or m.get("connector_writeback"):
            continue
        surv = await records.find_one({"id": m["survivor_id"]})
        abso = await records.find_one({"id": m["absorbed_id"]})
        s_ext = (surv or {}).get("external_id")
        a_ext = (abso or {}).get("external_id")
        entry = {"merge_id": m["id"]}
        if not s_ext or not a_ext:
            entry["status"] = "skipped"
            entry["reason"] = "record_not_from_hubspot"
            results.append(entry)
            continue
        fills = {prop: m["fills"][f] for f, prop in _HS_FILL_PROPS.items()
                 if m.get("fills", {}).get(f)}
        skipped_fields = [f for f in (m.get("fills") or {})
                          if f not in _HS_FILL_PROPS]
        if fills:
            status, _b = http_json(
                "PATCH", f"{_HS}/crm/v3/objects/{obj}/{s_ext}",
                token=token, payload={"properties": fills})
            if status not in (200, 204):
                entry["status"] = "failed"
                entry["reason"] = f"hubspot_patch_{status}"
                results.append(entry)
                continue
        status, _b = http_json(
            "POST", f"{_HS}/crm/v3/objects/{obj}/merge",
            token=token,
            payload={"primaryObjectId": s_ext,
                     "objectIdToMerge": a_ext})
        if status not in (200, 204):
            entry["status"] = "failed"
            entry["reason"] = f"hubspot_merge_{status}"
            results.append(entry)
            continue
        entry["status"] = "written"
        entry["patched_fields"] = sorted(fills)
        if skipped_fields:
            # A filled display name has no honest HubSpot home
            # (first/last split) — reported, never guessed.
            entry["skipped_fields"] = sorted(skipped_fields)
        await lists_lib._merges(actor).update_one(
            {"id": m["id"]}, {"$set": {"connector_writeback": {
                "provider": "hubspot", "object": obj, "at": now_iso(),
                "patched_fields": entry["patched_fields"],
                "skipped_fields": entry.get("skipped_fields", [])}}})
        written += 1
        results.append(entry)
    return {"provider": "hubspot", "list_id": list_id,
            "written": written, "results": results}, None


# ------------------------------------------------------------------
# Salesforce (probe-and-decline + pull + decision-export write-back)
# ------------------------------------------------------------------

SF_API_DECLINE = (
    "Salesforce API access appears unavailable for this org — API "
    "access is edition-gated on the customer's Salesforce plan, and "
    "several common editions include none. The connector declines "
    "rather than half-syncing: enable API access (or upgrade the "
    "edition) and reconnect, or use the merge-decision export, which "
    "needs no API access.")


async def probe_salesforce(actor):
    """Detect the no-API edition honestly: a data query is the only
    reliable test (the OAuth handshake itself can succeed)."""
    token, doc = await _access_token(actor, "salesforce")
    if not token or not (doc or {}).get("instance_url"):
        return {"available": False, "connected": False,
                "reason": "not connected"}
    inst = doc["instance_url"].rstrip("/")
    status, body = http_json(
        "GET", f"{inst}/services/data/{_SF_VERSION}/query",
        token=token, params={"q": "SELECT Id FROM Account LIMIT 1"})
    if status == 200:
        return {"available": True, "connected": True, "reason": None}
    return {"available": False, "connected": True,
            "reason": SF_API_DECLINE,
            "provider_status": status,
            "provider_error": (body.get("errorCode")
                               or body.get("message") or "")[:200]}


_SF_FIELDS = {
    "Account": ("Id, Name, Phone, BillingStreet, BillingCity, "
                "BillingState, BillingPostalCode"),
    "Contact": ("Id, FirstName, LastName, Email, Phone, MailingStreet, "
                "MailingCity, MailingState, MailingPostalCode"),
}


def _sf_record(obj_name, row):
    if obj_name == "Contact":
        name = " ".join(x for x in (row.get("FirstName"),
                                    row.get("LastName")) if x).strip()
        street, city = row.get("MailingStreet"), row.get("MailingCity")
        state, zipc = row.get("MailingState"), row.get("MailingPostalCode")
        rec = {"name": name, "external_id": row.get("Id"),
               "external_source": "salesforce:Contact"}
        if row.get("Email"):
            rec["email"] = row["Email"]
    else:
        rec = {"name": row.get("Name") or "",
               "external_id": row.get("Id"),
               "external_source": "salesforce:Account"}
        street, city = row.get("BillingStreet"), row.get("BillingCity")
        state, zipc = row.get("BillingState"), row.get("BillingPostalCode")
    if row.get("Phone"):
        rec["phone"] = row["Phone"]
    addr = ", ".join(str(p) for p in (street, city, state, zipc) if p)
    if addr:
        rec["address"] = addr
    return rec


async def pull_salesforce(actor, obj="Account", limit=200):
    probe = await probe_salesforce(actor)
    if not probe["available"]:
        return None, probe
    token, doc = await _access_token(actor, "salesforce")
    inst = doc["instance_url"].rstrip("/")
    soql = (f"SELECT {_SF_FIELDS[obj]} FROM {obj} "
            f"ORDER BY Id LIMIT {int(limit)}")
    status, body = http_json(
        "GET", f"{inst}/services/data/{_SF_VERSION}/query",
        token=token, params={"q": soql})
    if status != 200:
        return None, {"available": False, "reason": SF_API_DECLINE,
                      "provider_status": status}
    records = [_sf_record(obj, r) for r in body.get("records") or []]
    kind = "customers" if obj == "Contact" else "vendors"
    lst = await lists_lib.create_list(
        actor, f"Salesforce {obj} pull", kind=kind, source="salesforce")
    await lists_lib.append_records(actor, lst, records)
    return {"list_id": lst["id"], "pulled": len(records),
            "object": obj}, None


async def writeback_salesforce(actor, list_id):
    """Salesforce write-back is the merge-decision EXPORT, by design:
    the customer applies the approved decisions inside Salesforce
    (where its own dedupe tooling and permissions live). No PATCH or
    merge call is made from here."""
    lst = await lists_lib.get_list(actor, list_id)
    if not lst:
        return None, "list_not_found"
    export = await lists_lib.export_decisions(actor, list_id)
    return {"provider": "salesforce", "mode": "decision_export",
            "note": ("Decisions only — apply them inside Salesforce; "
                     "this connector never writes to Salesforce."),
            "export": export}, None
