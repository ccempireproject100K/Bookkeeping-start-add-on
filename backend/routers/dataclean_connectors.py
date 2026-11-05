"""DataClean connectors surface (P4): /v1/dataclean/connectors.

OAuth connect (per tenant, mdck_-key authenticated like the rest of
the external surface), read-only pulls into stored lists, and
write-back of human-approved merges. Everything honest-off: a
provider whose OAuth app credentials are not in env answers
503 not_configured, and no endpoint ever returns token material.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from lib import dataclean_api as dca
from lib import dataclean_connectors as conn
from routers.dataclean_api import _err

router = APIRouter(prefix="/v1/dataclean/connectors",
                   tags=["dataclean-connectors"])


def _provider_or_404(provider):
    if provider not in conn.PROVIDERS:
        raise _err(404, "unknown_provider",
                   "provider must be hubspot or salesforce")
    if not conn.configured(provider):
        raise _err(503, "not_configured",
                   f"the {provider} connector is not configured on "
                   "this deployment (OAuth app credentials not set)")


@router.get("")
async def list_connectors(actor: dict = Depends(dca.get_dataclean_actor)):
    return {"connectors": [await conn.connection_view(actor, p)
                           for p in conn.PROVIDERS]}


@router.get("/{provider}/authorize")
async def authorize(provider: str,
                    actor: dict = Depends(dca.get_dataclean_actor)):
    _provider_or_404(provider)
    return {"provider": provider,
            "authorize_url": conn.authorize_url(actor, provider)}


class CallbackIn(BaseModel):
    code: str
    state: str


@router.post("/{provider}/callback")
async def callback(provider: str, body: CallbackIn,
                   actor: dict = Depends(dca.get_dataclean_actor)):
    _provider_or_404(provider)
    view, err = await conn.handle_callback(actor, provider,
                                           body.code, body.state)
    if err:
        raise _err(400, err, "the OAuth callback could not be "
                             "completed")
    return view


class PullIn(BaseModel):
    object: Optional[str] = None
    limit: int = 200


@router.post("/{provider}/pull")
async def pull(provider: str, body: PullIn,
               actor: dict = Depends(dca.get_dataclean_actor)):
    _provider_or_404(provider)
    if provider == "hubspot":
        obj = body.object or "contacts"
        if obj not in ("contacts", "companies"):
            raise _err(422, "unknown_object",
                       "object must be contacts or companies")
        out, err = await conn.pull_hubspot(actor, obj,
                                           limit=min(body.limit, 1000))
    else:
        obj = body.object or "Account"
        if obj not in ("Account", "Contact"):
            raise _err(422, "unknown_object",
                       "object must be Account or Contact")
        out, err = await conn.pull_salesforce(actor, obj,
                                              limit=min(body.limit, 1000))
    if err:
        if isinstance(err, dict):  # salesforce decline carries detail
            raise _err(422, "salesforce_api_unavailable",
                       err.get("reason") or "salesforce declined")
        raise _err(400, str(err), "the pull could not be completed")
    return out


class WritebackIn(BaseModel):
    list_id: str
    object: Optional[str] = None


@router.post("/{provider}/writeback")
async def writeback(provider: str, body: WritebackIn,
                    actor: dict = Depends(dca.get_dataclean_actor)):
    _provider_or_404(provider)
    if provider == "hubspot":
        out, err = await conn.writeback_hubspot(
            actor, body.list_id, obj=body.object or "contacts")
    else:
        out, err = await conn.writeback_salesforce(actor, body.list_id)
    if err:
        raise _err(400, str(err), "the write-back could not be "
                                  "completed")
    return out


@router.post("/salesforce/probe")
async def salesforce_probe(actor: dict = Depends(dca.get_dataclean_actor)):
    _provider_or_404("salesforce")
    return await conn.probe_salesforce(actor)
