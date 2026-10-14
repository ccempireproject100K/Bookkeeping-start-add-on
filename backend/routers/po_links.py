"""One-click approve/reject links for purchase orders (used in the Monday email).

Signed JWT (JWT_SECRET, purpose-scoped, 7-day expiry, single use). GET only shows a confirmation page — email security
scanners pre-open links, so the action happens on the POST from that page, never on the GET.
"""

import html
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from pymongo.errors import DuplicateKeyError

from lib.auth import principal_for_user
from lib.dates import now_iso
from lib.db import db
from lib.repo import Scoped

router = APIRouter()
PURPOSE = "po-action"


def public_base(request: Request) -> str:
    """Public origin for links: PUBLIC_APP_URL if set, else the forwarded host the request came in on."""
    env = os.environ.get("PUBLIC_APP_URL", "").rstrip("/")
    if env:
        return env
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto.split(',')[0]}://{host.split(',')[0]}"


def action_link(base: str, tenant_id: str, user_id: str, po_id: str, action: str) -> str:
    tok = jwt.encode({"p": PURPOSE, "t": tenant_id, "u": user_id, "po": po_id, "a": action, "jti": uuid.uuid4().hex,
                      "exp": datetime.now(timezone.utc) + timedelta(days=7)}, os.environ["JWT_SECRET"], algorithm="HS256")
    return f"{base}/api/po-action?t={tok}"


def _page(title: str, body: str, ok: bool = True) -> HTMLResponse:
    color = "#2563eb" if ok else "#dc2626"
    return HTMLResponse(f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>body{{font-family:system-ui,sans-serif;background:#0b1220;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0}}
.c{{background:#111a2e;border:1px solid #1e293b;border-radius:14px;padding:28px;max-width:420px;width:90%}}h1{{font-size:20px;margin:0 0 8px;color:{color}}}
p{{color:#94a3b8;font-size:14px;line-height:1.5}}button{{width:100%;padding:12px;border:0;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;color:#fff;background:{color}}}
a{{color:#60a5fa}}</style></head><body><div class="c"><h1>{html.escape(title)}</h1>{body}</div></body></html>""",
                        status_code=200 if ok else 400)


def _decode(t: str) -> dict | None:
    try:
        d = jwt.decode(t, os.environ["JWT_SECRET"], algorithms=["HS256"])
        return d if d.get("p") == PURPOSE and d.get("a") in ("approve", "reject") else None
    except jwt.PyJWTError:
        return None


async def _load(d: dict):
    user = await db.users.find_one({"id": d["u"], "tenant_id": d["t"], "role": "admin"}, {"_id": 0})
    p = await principal_for_user(user) if user else None
    po = await Scoped("purchase_orders", p).find_one({"id": d["po"]}) if p else None
    return p, po


@router.get("/po-action", response_class=HTMLResponse)
async def confirm_page(t: str = ""):
    d = _decode(t)
    if not d:
        return _page("Link expired", "<p>This approval link is invalid or older than 7 days. Open Procurement in the app instead.</p>", ok=False)
    p, po = await _load(d)
    if not po:
        return _page("Order not found", "<p>This order no longer exists.</p>", ok=False)
    verb = "Approve" if d["a"] == "approve" else "Reject"
    lines = "".join(f"<li>{l['quantity']} × {html.escape(l['name'])}</li>" for l in po["lines"])
    return _page(f"{verb} {po['number']}?", f"""<p>{html.escape(po['supplier_name'])} · <b>${po['total']:,.2f}</b> · status {po['status']}</p>
<ul style="color:#cbd5e1;font-size:13px">{lines}</ul>
<form method="post" action="/api/po-action"><input type="hidden" name="t" value="{html.escape(t)}"><button type="submit">{verb} order</button></form>""",
                 ok=d["a"] == "approve")


@router.post("/po-action", response_class=HTMLResponse)
async def do_action(t: str = Form(...)):
    d = _decode(t)
    if not d:
        return _page("Link expired", "<p>This approval link is invalid or expired.</p>", ok=False)
    p, po = await _load(d)
    if not po or not p:
        return _page("Order not found", "<p>This order no longer exists.</p>", ok=False)
    try:
        await db.used_action_tokens.insert_one({"_id": d["jti"], "at": now_iso()})
    except DuplicateKeyError:
        return _page("Already done", "<p>This link was already used.</p>", ok=False)
    if po["status"] != "draft":
        return _page("Nothing to do", f"<p>{po['number']} is already {po['status']}.</p>", ok=False)
    patch = ({"approved_by": p.name, "approved_at": now_iso()} if d["a"] == "approve"
             else {"status": "cancelled", "notes": (po.get("notes", "") + f"\nRejected by {p.name} from email").strip()})
    await Scoped("purchase_orders", p).update({"id": po["id"], "status": "draft"}, {"$set": patch})
    if d["a"] == "approve":
        return _page(f"{po['number']} approved", f"<p>Approved by {html.escape(p.name)}. It's ready to order or email to {html.escape(po['supplier_name'])} from Procurement.</p>")
    return _page(f"{po['number']} rejected", "<p>The draft was cancelled. Nothing was sent to the supplier.</p>", ok=False)
