"""DataClean/Security entitlement read for empire surfaces.

GET /api/v1/platform/entitlements

Why this exists (Integration Plan section 3, item 4): a surface that
SELLS the DataClean + Security bundle (AI Office first) needs to SHOW
bundle state — active / not — and the state lives here, on the books
host, the platform of record for both products. Before this endpoint
the only readers of that state were the host's own gates.

Auth follows the house machine-credential discipline exactly
(lib/integration_auth.py + lib/dataclean_api.py): the credential is
minted per surface in the operator console, only its SHA-256 hash is
stored, and the workspace ALWAYS comes from the credential record —
never from the request — so a credential can only ever read its own
binding:

* `mdck_...` DataClean API key (X-API-Key or Authorization: Bearer)
  -> the key's workspace. NOTE: this read deliberately does NOT pass
  through authenticate_key's entitlement gate — a workspace whose
  subscription lapsed must still be able to READ that it lapsed; the
  gate would 403 the very answer this endpoint exists to give.
* `mae1....` TENANT integration credential -> its bound workspace.
* `mae1....` PLATFORM credential (referrals:sync) -> 403. That class
  speaks for a product's referral reporting, not for any workspace;
  there is no workspace answer to give it, and letting it read one
  would break the cross-product rule the referrals surface enforces
  in the other direction (get_platform_actor refuses tenant
  credentials there). User JWTs are likewise not accepted here —
  this is a machine surface, like the integration health endpoint.

The answer, per product:
* dataclean — the exact rule the API door enforces
  (lib.dataclean_billing.entitlement_from_record): a granting
  subscription (dataclean | bundle, active | past_due) is active; no
  billing record is grandfathered-active until the owner turns
  strict mode on (DATACLEAN_BILLING_ENFORCED=1).
* security — granted only by a security | bundle subscription in an
  entitled status. There is no grandfathering for Security: it never
  shipped to pre-billing workspaces, so "no record" honestly reads
  inactive. The catalog block separately reports whether Security is
  built / available / sellable at all (SECURITY_PRODUCT_BUILT), so a
  surface can distinguish "not subscribed" from "not yet a product".
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from db import db
from lib import dataclean_api as dca
from lib import dataclean_billing as dcb
from lib import integration_auth

router = APIRouter(prefix="/v1/platform", tags=["platform-entitlements"])

SECURITY_GRANTING_PRODUCTS = ("security", "bundle")


async def _resolve_dataclean_key(token: str) -> str:
    """Workspace for an mdck_ key WITHOUT the entitlement gate (see
    module docstring). Same storage + revoked semantics as
    lib.dataclean_api.authenticate_key; one generic 401 throughout."""
    digest = dca.hash_key(token)
    doc = await db.dataclean_api_keys.find_one({"key_hash": digest})
    if not doc or doc.get("revoked_at"):
        raise HTTPException(status_code=401, detail={
            "code": "invalid_api_key", "message": "Invalid API key"})
    return doc["tenant_id"]


async def get_entitlement_actor(request: Request) -> dict:
    """Dependency: resolve the presented machine credential to the
    ONE workspace it is bound to. Returns {tenant_id, credential}."""
    token = request.headers.get("X-API-Key") or ""
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
    token = token.strip()
    if token.startswith(dca.KEY_PREFIX):
        tenant_id = await _resolve_dataclean_key(token)
        return {"tenant_id": tenant_id, "credential": "dataclean_key"}
    if integration_auth.is_integration_token(token):
        actor = await integration_auth.authenticate_integration_token(token)
        if actor.get("kind") != "tenant":
            raise HTTPException(
                status_code=403,
                detail="A platform credential cannot read workspace "
                       "entitlements; use the surface's own workspace "
                       "credential (its DataClean key)")
        return {"tenant_id": actor.get("tenant_id"),
                "credential": "integration_credential"}
    raise HTTPException(status_code=401,
                        detail="Machine credential required")


def _product_state(rec, *, granting_products, grandfather: bool) -> dict:
    """One product's entitlement state from the workspace's
    dataclean_billing record (the record the Stripe webhook alone
    writes). `basis` names WHY, so a selling surface never has to
    guess what "active" rests on."""
    if rec:
        granted = (rec.get("product") in granting_products
                   and rec.get("status") in dcb.ENTITLED_STATUSES)
        return {
            "active": bool(granted),
            "basis": "subscription" if granted else "subscription_inactive",
            "record_status": rec.get("status"),
            "granted_by": rec.get("product") if granted else None,
        }
    if grandfather and not dcb.enforcement_on():
        return {"active": True, "basis": "grandfathered",
                "record_status": None, "granted_by": None}
    return {"active": False,
            "basis": "strict_mode_no_record" if grandfather else "no_record",
            "record_status": None, "granted_by": None}


@router.get("/entitlements")
async def read_entitlements(
        actor: dict = Depends(get_entitlement_actor)):
    tenant_id = actor["tenant_id"]
    tenants = getattr(db, "tenants", None)
    tenant = None
    if tenants is not None:
        tenant = await tenants.find_one({"id": tenant_id})
    rec = (tenant or {}).get("dataclean_billing")

    catalog = {p["id"]: {"built": p["built"], "available": p["available"],
                         "sellable": p["sellable"], "status": p["status"]}
               for p in dca.products_config()}
    return {
        "tenant_id": tenant_id,
        "credential": actor["credential"],
        "billing_enforced": dcb.enforcement_on(),
        "entitlements": {
            "dataclean": _product_state(
                rec, granting_products=dcb.GRANTING_PRODUCTS,
                grandfather=True),
            "security": _product_state(
                rec, granting_products=SECURITY_GRANTING_PRODUCTS,
                grandfather=False),
        },
        "catalog": catalog,
    }
