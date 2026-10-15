"""Tax + 1099s: contractor tracking, W-9 status, year summaries, a 1099-NEC
data preview, and e-filing through the TaxBandits API.

SCOPE LIMITS:
- No in-house IRS e-filing. Filing goes through the TaxBandits API only.
- Without TAXBANDITS_* env keys, e-file endpoints return 503 with setup
  instructions (see TAXBANDITS_SETUP.md at the repo root).
- Contractor tax IDs are Fernet-encrypted at rest (`tax_id_enc`); only the
  last 4 (`tax_id_last4`) is stored in cleartext. Full values decrypt
  server-side for `pii:read` holders and TaxBandits e-filing only.
  The field is write-only through the API: responses expose only the last 4.
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib.pii import last4, pii_decrypt, pii_encrypt

router = APIRouter(prefix="/tax", tags=["tax"])

THRESHOLD_CENTS = 600_00          # IRS reporting threshold, $600
EXEMPT_ENTITIES = {"c_corp", "s_corp"}   # corporations generally don't receive 1099-NEC


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


def contractors(user):
    return Scoped(db.contractors, user["tenant_id"])


def payments(user):
    return Scoped(db.contractor_payments, user["tenant_id"])


# ---------------------------------------------------------------- contractors

class AddressIn(BaseModel):
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""


class ContractorIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=40)
    business_name: str = Field(default="", max_length=160)
    entity_type: str = Field(default="individual",
                             pattern=r"^(individual|sole_prop|llc|c_corp|s_corp|partnership|trust)$")
    # SSN/EIN. Stored in `tax_id_enc` (see module docstring); write-only via the API.
    tax_id: str = Field(default="", max_length=20)
    address: AddressIn = AddressIn()


class W9In(BaseModel):
    received: bool


def _tax_id_fields(tax_id: str) -> dict:
    """Encrypt the tax ID; only the last 4 is ever stored in cleartext."""
    tax_id = (tax_id or "").strip()
    if not tax_id:
        return {"tax_id_enc": "", "tax_id_last4": ""}
    return {"tax_id_enc": pii_encrypt(tax_id), "tax_id_last4": last4(tax_id)}


def _strip(c: dict, total_cents: int = 0) -> dict:
    return {
        "id": c["id"], "name": c["name"], "email": c.get("email", ""),
        "phone": c.get("phone", ""), "business_name": c.get("business_name", ""),
        "entity_type": c.get("entity_type", "individual"),
        # tax_id_enc is write-only; only the last 4 ever leave the server.
        "tax_id_last4": c.get("tax_id_last4", ""),
        "address": c.get("address", {}),
        "w9_received": c.get("w9_received", False),
        "w9_received_date": c.get("w9_received_date"),
        "year_total_cents": total_cents,
        "year_total": cents_to_dollars(total_cents),
        "needs_1099": total_cents >= THRESHOLD_CENTS
                      and c.get("entity_type") not in EXEMPT_ENTITIES,
        "efile": c.get("efile", {}),
    }


async def _year_total_cents(user, contractor_id: str, year: int) -> int:
    rows = await payments(user).aggregate([
        {"$match": {"contractor_id": contractor_id,
                    "date": {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount_cents"}}},
    ])
    return int(rows[0]["total"]) if rows else 0


@router.get("/contractors")
async def list_contractors(year: int = Query(datetime.now(timezone.utc).year),
                           user: dict = Depends(require("tax:read"))):
    out = []
    for c in await contractors(user).find_list(sort=[("name", 1)]):
        out.append(_strip(c, await _year_total_cents(user, c["id"], year)))
    return out


@router.post("/contractors")
async def create_contractor(body: ContractorIn, user: dict = Depends(require("tax:write"))):
    data = body.model_dump()
    tax_id = data.pop("tax_id", "")
    doc = {
        "id": new_id(),
        "name": data["name"], "email": data["email"], "phone": data["phone"],
        "business_name": data["business_name"], "entity_type": data["entity_type"],
        **_tax_id_fields(tax_id),
        "address": data["address"],
        "w9_received": False, "w9_received_date": None, "efile": {},
        "created_at": now_iso(),
    }
    await contractors(user).insert_one(doc)
    await _audit(user, "tax.contractor.create", "contractor", doc["id"], {"name": doc["name"]})
    return _strip(doc)


@router.patch("/contractors/{cid}")
async def update_contractor(cid: str, body: ContractorIn, user: dict = Depends(require("tax:write"))):
    data = body.model_dump()
    tax_id = data.pop("tax_id", "")
    patch = {
        "name": data["name"], "email": data["email"], "phone": data["phone"],
        "business_name": data["business_name"], "entity_type": data["entity_type"],
        "address": data["address"],
    }
    if tax_id:
        # Only overwrite the encrypted tax ID when a new one is supplied.
        patch.update(_tax_id_fields(tax_id))
    res = await contractors(user).update_one({"id": cid}, {"$set": patch})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contractor not found")
    await _audit(user, "tax.contractor.update", "contractor", cid, {})
    doc = await contractors(user).find_one({"id": cid})
    return _strip(doc)


@router.get("/contractors/{cid}/tax-id")
async def get_contractor_tax_id(cid: str, user: dict = Depends(require("pii:read"))):
    """Full tax ID — pii:read holders only. Audited, never logged."""
    c = await contractors(user).find_one({"id": cid})
    if not c:
        raise HTTPException(status_code=404, detail="Contractor not found")
    await _audit(user, "tax.tax_id.view", "contractor", cid, {})
    enc = c.get("tax_id_enc") or ""
    return {"contractor_id": cid, "tax_id": pii_decrypt(enc) if enc else ""}


@router.delete("/contractors/{cid}")
async def delete_contractor(cid: str, user: dict = Depends(require("tax:write"))):
    res = await contractors(user).c.delete_one(
        {"tenant_id": user["tenant_id"], "id": cid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Contractor not found")
    await _audit(user, "tax.contractor.delete", "contractor", cid, {})
    return {"ok": True}


@router.post("/contractors/{cid}/w9")
async def set_w9(cid: str, body: W9In, user: dict = Depends(require("tax:write"))):
    res = await contractors(user).update_one(
        {"id": cid},
        {"$set": {"w9_received": body.received,
                  "w9_received_date": datetime.now(timezone.utc).date().isoformat()
                  if body.received else None}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Contractor not found")
    await _audit(user, "tax.w9", "contractor", cid, {"received": body.received})
    doc = await contractors(user).find_one({"id": cid})
    return _strip(doc)


# ---------------------------------------------------------------- payments

class PaymentIn(BaseModel):
    date: str
    amount: float = Field(gt=0)
    description: str = Field(default="", max_length=200)


@router.get("/contractors/{cid}/payments")
async def list_payments(cid: str, year: int = Query(datetime.now(timezone.utc).year),
                        user: dict = Depends(require("tax:read"))):
    return await payments(user).find_list(
        {"contractor_id": cid, "date": {"$gte": f"{year}-01-01", "$lte": f"{year}-12-31"}},
        sort=[("date", 1)], limit=2000)


@router.post("/contractors/{cid}/payments")
async def add_payment(cid: str, body: PaymentIn, user: dict = Depends(require("tax:write"))):
    if not await contractors(user).find_one({"id": cid}):
        raise HTTPException(status_code=404, detail="Contractor not found")
    amount_cents = to_cents(body.amount)
    if amount_cents <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    doc = {"id": new_id(), "contractor_id": cid, "date": body.date,
           "amount_cents": amount_cents, "amount": cents_to_dollars(amount_cents),
           "description": body.description, "created_at": now_iso()}
    await payments(user).insert_one(doc)
    await _audit(user, "tax.payment.add", "contractor", cid,
                 {"amount_cents": amount_cents, "date": body.date})
    return doc


@router.delete("/payments/{pid}")
async def delete_payment(pid: str, user: dict = Depends(require("tax:write"))):
    res = await payments(user).c.delete_one(
        {"tenant_id": user["tenant_id"], "id": pid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Payment not found")
    await _audit(user, "tax.payment.delete", "contractor_payment", pid, {})
    return {"ok": True}


# ---------------------------------------------------------------- year summary

@router.get("/summary")
async def year_summary(year: int = Query(datetime.now(timezone.utc).year),
                       user: dict = Depends(require("tax:read"))):
    rows = []
    reportable_cents = 0
    missing_w9 = []
    for c in await contractors(user).find_list(sort=[("name", 1)]):
        total = await _year_total_cents(user, c["id"], year)
        row = _strip(c, total)
        rows.append(row)
        if row["needs_1099"]:
            reportable_cents += total
            if not row["w9_received"]:
                missing_w9.append(row["name"])
    return {"year": year, "threshold_cents": THRESHOLD_CENTS,
            "threshold": cents_to_dollars(THRESHOLD_CENTS),
            "contractors": rows,
            "required_count": sum(1 for r in rows if r["needs_1099"]),
            "reportable_total_cents": reportable_cents,
            "reportable_total": cents_to_dollars(reportable_cents),
            "missing_w9": missing_w9}


# ---------------------------------------------------------------- 1099-NEC preview

@router.get("/1099-preview")
async def preview_1099(year: int = Query(datetime.now(timezone.utc).year),
                       user: dict = Depends(require("tax:write"))):
    """1099-NEC data shaped for e-filing. Carries full tax IDs, so it requires
    tax:write. The payer's legal name/EIN/address live in TaxBandits itself;
    the app sends only recipient + amounts (see TAXBANDITS_SETUP.md)."""
    recipients = []
    for c in await contractors(user).find_list(sort=[("name", 1)]):
        total = await _year_total_cents(user, c["id"], year)
        row = _strip(c, total)
        if not row["needs_1099"]:
            continue
        enc = c.get("tax_id_enc") or ""
        recipients.append({
            "contractor_id": c["id"], "name": c["name"],
            "business_name": c.get("business_name", ""),
            "entity_type": c.get("entity_type", "individual"),
            # Decrypted server-side only; this preview already requires tax:write.
            "tin": pii_decrypt(enc) if enc else "",
            "email": c.get("email", ""),
            "address": c.get("address", {}),
            "w9_received": row["w9_received"],
            "box1_cents": total,
            "box1": cents_to_dollars(total),
        })
    return {"year": year, "form": "1099-NEC", "recipients": recipients,
            "note": "Draft data only — no IRS filing happens in-house. "
                    "E-filing creates a draft in TaxBandits; review and Transmit there."}


# ---------------------------------------------------------------- TaxBandits e-file

def _tb_conf() -> dict:
    conf = {k: os.environ.get(k, "").strip() for k in
            ("TAXBANDITS_CLIENT_ID", "TAXBANDITS_CLIENT_SECRET",
             "TAXBANDITS_USER_TOKEN", "TAXBANDITS_BUSINESS_ID")}
    if not all(conf.values()):
        raise HTTPException(
            status_code=503,
            detail="TaxBandits is not configured. Set TAXBANDITS_CLIENT_ID, "
                   "TAXBANDITS_CLIENT_SECRET, TAXBANDITS_USER_TOKEN and "
                   "TAXBANDITS_BUSINESS_ID in the server environment — see "
                   "TAXBANDITS_SETUP.md at the repo root. Tracking and year "
                   "summaries work without it; only e-filing is disabled.")
    sandbox = os.environ.get("TAXBANDITS_SANDBOX", "true").lower() != "false"
    conf["base"] = "https://testapi.taxbandits.com/v2" if sandbox else "https://api.taxbandits.com/v2"
    conf["sandbox"] = sandbox
    return conf


def _tb_token_sync(conf: dict) -> str:
    """OAuth2 client-assertion flow: JWT signed with the client secret,
    exchanged at POST {base}/OAuth/Token for a Bearer token.

    See https://developer.taxbandits.com/docs/OAuth2.0Authentication.
    TaxBandits-side errors are surfaced verbatim so credential/plan issues
    are visible."""
    now = int(datetime.now(timezone.utc).timestamp())
    assertion = jwt.encode(
        {"iss": conf["TAXBANDITS_CLIENT_ID"], "sub": conf["TAXBANDITS_CLIENT_ID"],
         "aud": f"{conf['base']}/OAuth/Token", "iat": now, "exp": now + 300},
        conf["TAXBANDITS_CLIENT_SECRET"], algorithm="HS256")
    r = requests.post(f"{conf['base']}/OAuth/Token", timeout=30, json={
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "user_token": conf["TAXBANDITS_USER_TOKEN"]})
    if r.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"TaxBandits auth failed ({r.status_code}): {r.text[:300]}")
    try:
        token = r.json().get("AccessToken") or r.json().get("access_token")
    except Exception:
        token = None
    if not token:
        raise HTTPException(status_code=502,
                            detail=f"TaxBandits auth returned no token: {r.text[:300]}")
    return token


def _tb_request_sync(method: str, url: str, token: str, payload: Optional[dict] = None) -> dict:
    r = requests.request(method, url, timeout=60, json=payload,
                         headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise HTTPException(status_code=502,
                            detail=f"TaxBandits {url.split('/v2')[-1]} failed "
                                   f"({r.status_code}): {r.text[:500]}")
    return r.json()


@router.get("/taxbandits/status")
async def tb_status(user: dict = Depends(require("tax:read"))):
    try:
        conf = _tb_conf()
    except HTTPException:
        return {"configured": False, "sandbox": True}
    return {"configured": True, "sandbox": conf["sandbox"]}


@router.post("/efile-1099/{cid}")
async def efile_1099(cid: str, year: int = Query(datetime.now(timezone.utc).year),
                     user: dict = Depends(require("tax:write"))):
    """Create a 1099-NEC in TaxBandits for this contractor (does NOT transmit —
    review and Transmit inside TaxBandits)."""
    c = await contractors(user).find_one({"id": cid})
    if not c:
        raise HTTPException(status_code=404, detail="Contractor not found")
    total = await _year_total_cents(user, cid, year)
    if total < THRESHOLD_CENTS:
        raise HTTPException(status_code=400,
                            detail=f"Total {cents_to_dollars(total):,.2f} is under the "
                                   f"${THRESHOLD_CENTS // 100:,.0f} reporting threshold")
    if not c.get("w9_received"):
        raise HTTPException(status_code=400,
                            detail="Collect the contractor's W-9 before e-filing")
    conf = _tb_conf()
    token = await asyncio.to_thread(_tb_token_sync, conf)
    addr = c.get("address") or {}
    payload = {
        "BusinessId": conf["TAXBANDITS_BUSINESS_ID"],
        "TaxYear": year,
        "Recipient": {
            "Name": c["name"],
            # Decrypted server-side only, at send time. Never stored or logged in plaintext.
            "TIN": pii_decrypt(c.get("tax_id_enc") or "") if c.get("tax_id_enc") else "",
            "Email": c.get("email", ""),
            "Address": {"Address1": addr.get("street", ""), "City": addr.get("city", ""),
                        "State": addr.get("state", ""), "ZipCd": addr.get("zip", "")},
        },
        "Form1099NEC": {"Box1 NEC": cents_to_dollars(total)},
    }
    resp = await asyncio.to_thread(_tb_request_sync, "POST",
                                   f"{conf['base']}/Form1099NEC/Create",
                                   token, payload)
    rec = (resp.get("Form1099Records") or [{}])[0]
    efile = {"submission_id": resp.get("SubmissionId"),
             "record_id": rec.get("RecordId"),
             "status": rec.get("RecordStatus") or resp.get("Status", "created"),
             "year": year, "amount_cents": total,
             "sandbox": conf["sandbox"], "updated_at": now_iso()}
    await contractors(user).update_one({"id": cid}, {"$set": {"efile": efile}})
    await _audit(user, "tax.efile.create", "contractor", cid,
                 {"year": year, "amount_cents": total})
    return {"ok": True, "efile": efile,
            "note": "Created in TaxBandits — review and Transmit inside TaxBandits "
                    "to file with the IRS."}


@router.get("/efile-1099/{cid}/status")
async def efile_status(cid: str, user: dict = Depends(require("tax:read"))):
    c = await contractors(user).find_one({"id": cid})
    if not c:
        raise HTTPException(status_code=404, detail="Contractor not found")
    efile = c.get("efile") or {}
    if not efile.get("submission_id"):
        return {"efile": efile}
    conf = _tb_conf()
    token = await asyncio.to_thread(_tb_token_sync, conf)
    resp = await asyncio.to_thread(
        _tb_request_sync, "GET",
        f"{conf['base']}/Form1099NEC/Status?SubmissionId={efile['submission_id']}",
        token, None)
    efile["status"] = resp.get("Status") or efile.get("status")
    efile["updated_at"] = now_iso()
    await contractors(user).update_one({"id": cid}, {"$set": {"efile": efile}})
    return {"efile": efile}
