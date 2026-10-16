"""AI copilot: a grounded chat plus approval-gated actions.

- POST /copilot/chat            grounded Q&A on the workspace's own numbers
- POST /copilot/actions/propose propose actions (overdue invoices -> reminders,
                                open bills -> draft payments) WITHOUT executing
- POST /copilot/actions         list proposed/executed actions
- POST /copilot/actions/{id}/approve executes an approved action
- POST /copilot/actions/{id}/reject  discards a proposed action

NOTHING executes until the owner explicitly approves it. Approval-gated
executions only ever create reminder records and draft payments — they never
move money or send mail.

LLM config (env; same style as the dead new-style ai.py):
  EMERGENT_LLM_KEY - API key for an OpenAI-compatible chat endpoint
  AI_MODEL         - model name (default: gpt-5-mini)

If the key is missing, chat returns 503 with a setup hint. Everything else
(overview numbers, proposals) works without any AI key.
"""

import asyncio
import logging
import os
from datetime import date
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars, to_cents
from lib.auth import require, get_current_user
from lib.pii import scrub_secrets

router = APIRouter(prefix="/copilot", tags=["copilot"])
logger = logging.getLogger(__name__)

ACTION_KINDS = ("chase_invoice", "pay_bill")
ACTION_STATUSES = ("pending", "approved", "rejected", "executed")


class ChatIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


def actions(user):
    return Scoped(db.copilot_actions, user["tenant_id"])


async def _audit(user, action, entity, entity_id, details=None):
    await db.audit_log.insert_one({
        "id": new_id(), "tenant_id": user["tenant_id"],
        "actor_id": user["id"], "actor_name": user.get("name"),
        "actor_role": user.get("role"), "action": action, "entity": entity,
        "entity_id": entity_id, "details": details or {}, "created_at": now_iso(),
    })


def _action_doc(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "kind", "title", "detail", "brief", "payload", "status",
                                 "created_at", "decided_at", "decided_by", "result")}


async def _cash_on_hand(tid: str) -> int:
    bals = await L._balances(db, tid, as_of=today_str())

    def bal(code):
        v = bals.get(code, {"debit": 0, "credit": 0})
        return L._net_balance(code, v["debit"], v["credit"])
    return bal("1010") + bal("1020") + bal("1021") + bal("1022") + bal("1023") + bal("1050")


async def _brief_for(user, kind: str, item: dict) -> dict:
    """AI brief on a proposed action: what happened, what the AI noticed,
    recommended action + reasoning, confidence. Grounded in real numbers;
    works without any LLM key."""
    tid = user["tenant_id"]
    if kind == "chase_invoice":
        client = item["client"]
        past = await Scoped(db.invoices, tid).find_list({"client_name": client}, limit=200)
        total_billed = sum(i.get("total_cents", 0) for i in past)
        n = len(past)
        # how often this client pays late (paid after due date, or still overdue)
        late = 0
        for i in past:
            if i["id"] == item["id"]:
                continue
            due = (i.get("due_date") or "")[:10]
            if not due:
                continue
            pay_dates = [p.get("date", "")[:10] for p in i.get("payments", []) if p.get("date")]
            last_pay = max(pay_dates) if pay_dates else ""
            if i.get("status") in ("issued", "partial") and due < today_str()[:10]:
                late += 1
            elif last_pay and last_pay > due:
                late += 1
        outstanding = sum(i["total_cents"] - i.get("paid_cents", 0)
                          for i in past if i["status"] in ("issued", "partial"))
        hist_note = (f"They've paid late {late} of {n} past invoices."
                     if n > 1 else "Not enough history to judge their payment pattern.")
        conf = "high" if n >= 5 else "medium" if n >= 2 else "low"
        return {
            "what_happened": f"Invoice #{item['id'][:8]} for ${item['balance']:.2f} to {client} "
                             f"is {item['days_overdue']} days past due (due {item['due_date']}).",
            "what_noticed": f"{hist_note} Total outstanding with {client}: "
                             f"${cents_to_dollars(outstanding):,.2f} across {n} invoices.",
            "recommendation": "Send the friendly reminder now.",
            "reasoning": ("Early nudges get paid fastest, and this is still in the friendly "
                          "window before late fees or firmer steps apply."),
            "confidence": conf,
            "confidence_basis": f"Based on {n} past invoices with this client.",
        }
    if kind == "pay_bill":
        cash = await _cash_on_hand(tid)
        bal = item["balance"]
        pct = round(bal / cash * 100, 1) if cash > 0 else 0
        return {
            "what_happened": f"Bill from {item['vendor']} for ${bal:.2f} "
                             f"is due {item['due_date'] or 'on receipt'}.",
            "what_noticed": f"Cash on hand is ${cents_to_dollars(cash):,.2f}; "
                             f"this bill is {pct}% of it.",
            "recommendation": "Draft the payment for review.",
            "reasoning": ("Drafting now keeps payables current; nothing moves until you "
                          "post the payment yourself."),
            "confidence": "high" if cash > bal * 2 else "medium",
            "confidence_basis": "Based on current cash position.",
        }
    return {"what_happened": "", "what_noticed": "", "recommendation": "",
            "reasoning": "", "confidence": "low", "confidence_basis": ""}


# ---------------- workspace snapshot (grounding data) ----------------
async def _snapshot(user) -> dict:
    """Compact workspace facts: totals, overdue invoices, unpaid bills, low stock."""
    tid = user["tenant_id"]
    today = today_str()

    invs = await Scoped(db.invoices, tid).find_list({"status": "issued"}, limit=2000)
    overdue = []
    total_ar_cents = 0
    for i in invs:
        bal = i["total_cents"] - i.get("paid_cents", 0)
        if bal <= 0:
            continue
        total_ar_cents += bal
        due = (i.get("due_date") or "")[:10]
        if due and due < today:
            try:
                days = (date.fromisoformat(today) - date.fromisoformat(due)).days
            except ValueError:
                days = 0
            overdue.append({
                "id": i["id"], "client": i.get("client_name"), "due_date": due,
                "days_overdue": days, "balance": cents_to_dollars(bal),
            })
    overdue.sort(key=lambda o: -o["days_overdue"])

    bills = await Scoped(db.bills, tid).find_list(
        {"status": {"$in": ["open", "partial"]}}, sort=[("due_date", 1)], limit=500)
    unpaid = []
    total_ap_cents = 0
    for b in bills:
        bal = b["amount_cents"] - b.get("paid_cents", 0)
        if bal <= 0:
            continue
        total_ap_cents += bal
        unpaid.append({
            "id": b["id"], "vendor": b.get("vendor"), "due_date": b.get("due_date"),
            "balance": cents_to_dollars(bal),
        })

    prods = await Scoped(db.products, tid).find_list(limit=5000)
    low_stock = [
        {"name": p.get("name"), "on_hand": p.get("quantity_on_hand"),
         "reorder_point": p.get("reorder_point")}
        for p in prods
        if int(p.get("reorder_point") or 0) > 0
        and int(p.get("quantity_on_hand") or 0) <= int(p.get("reorder_point") or 0)
    ]

    return {
        "today": today,
        "accounts_receivable": cents_to_dollars(total_ar_cents),
        "accounts_payable": cents_to_dollars(total_ap_cents),
        "overdue_invoices": overdue,
        "unpaid_bills": unpaid,
        "low_stock": low_stock,
    }


# ---------------- LLM (OpenAI-compatible, key-gated) ----------------
def _llm_key() -> str:
    key = os.environ.get("EMERGENT_LLM_KEY", "").strip()
    if not key:
        raise HTTPException(
            503,
            "AI is not configured. Set EMERGENT_LLM_KEY on the server to an "
            "OpenAI-compatible API key to enable the copilot chat.")
    return key


async def _llm_chat(system: str, user_text: str) -> str:
    key = _llm_key()
    model = os.environ.get("AI_MODEL", "gpt-5-mini").strip()

    def _do():
        return requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user_text}],
                  "temperature": 0.3},
            timeout=60)

    try:
        r = await asyncio.to_thread(_do)
    except Exception as exc:
        logger.error("copilot LLM request failed: %s", exc)
        raise HTTPException(502, "The AI service could not be reached — try again in a minute")
    if r.status_code != 200:
        logger.error("copilot LLM error %s: %s", r.status_code, scrub_secrets(r.text[:300]))
        raise HTTPException(502, "The AI service returned an error — try again in a minute")
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        raise HTTPException(502, "The AI service returned an unexpected response")


CHAT_SYSTEM = """You are the copilot inside a small-business bookkeeping app.
Answer ONLY from the business data JSON below; if the data doesn't contain the
answer, say so plainly. Never invent numbers. Be brief and practical (owners
read this on a phone). Use $ amounts with 2 decimals. Use short bullet points
when listing. Plain text, no markdown tables.

WORKSPACE DATA (JSON):
"""


@router.post("/chat")
async def chat(body: ChatIn, user: dict = Depends(get_current_user)):
    """Grounded chat: the workspace snapshot goes to the model, the prompt asks."""
    snap = await _snapshot(user)
    import json as _json
    reply = await _llm_chat(
        CHAT_SYSTEM + _json.dumps(snap, default=str),
        f"Today is {snap['today']}. User question: {body.prompt}")
    await _audit(user, "copilot.chat", "tenant", user["tenant_id"],
                 {"prompt": body.prompt[:200]})
    return {"reply": reply}


@router.get("/snapshot")
async def snapshot(user: dict = Depends(get_current_user)):
    """The raw grounding data (useful for debugging and for the UI summary)."""
    return await _snapshot(user)


# ---------------- approval-gated actions ----------------
@router.post("/actions/propose")
async def propose_actions(user: dict = Depends(require("ops:manage"))):
    """Build proposed actions from the workspace data WITHOUT executing anything.

    chase_invoice -> one proposed reminder per overdue invoice (oldest first).
    pay_bill      -> one proposed draft payment per unpaid bill.
    Existing pending proposals are not duplicated.
    """
    snap = await _snapshot(user)
    open_docs = await actions(user).find_list(
        {"status": {"$in": ["pending", "approved"]}}, limit=200)
    seen = {(a["kind"], (a.get("payload") or {}).get("invoice_id")
             or (a.get("payload") or {}).get("bill_id")) for a in open_docs}
    out = []

    for o in snap["overdue_invoices"][:5]:
        if ("chase_invoice", o["id"]) in seen:
            continue
        doc = {
            "id": new_id(), "kind": "chase_invoice",
            "title": f"Remind {o['client']} — ${o['balance']:.2f} overdue",
            "detail": f"Invoice {o['days_overdue']} day(s) past due (due {o['due_date']}). "
                      "Creates a reminder record — nothing is sent automatically.",
            "brief": await _brief_for(user, "chase_invoice", o),
            "payload": {"invoice_id": o["id"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        }
        await actions(user).insert_one(doc)
        out.append(_action_doc(doc))

    for b in snap["unpaid_bills"][:5]:
        if ("pay_bill", b["id"]) in seen:
            continue
        doc = {
            "id": new_id(), "kind": "pay_bill",
            "title": f"Draft payment — {b['vendor']} (${b['balance']:.2f})",
            "detail": f"Bill due {b['due_date'] or 'n/a'}. Creates a DRAFT payment for review — "
                      "no money moves until you post it yourself.",
            "brief": await _brief_for(user, "pay_bill", b),
            "payload": {"bill_id": b["id"], "amount": b["balance"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        }
        await actions(user).insert_one(doc)
        out.append(_action_doc(doc))

    await _audit(user, "copilot.actions.propose", "tenant", user["tenant_id"],
                 {"proposed": len(out)})
    return out


@router.get("/actions")
async def list_actions(user: dict = Depends(get_current_user)):
    docs = await actions(user).find_list(sort=[("created_at", -1)], limit=50)
    out = []
    for d in docs:
        if not d.get("brief") and d.get("status") == "pending":
            # backfill the brief for actions proposed before briefs existed
            item = None
            if d["kind"] == "chase_invoice":
                inv = await Scoped(db.invoices, user["tenant_id"]).find_one(
                    {"id": (d.get("payload") or {}).get("invoice_id")})
                if inv:
                    bal = inv["total_cents"] - inv.get("paid_cents", 0)
                    item = {"id": inv["id"], "client": inv.get("client_name"),
                            "balance": cents_to_dollars(bal),
                            "due_date": (inv.get("due_date") or "")[:10],
                            "days_overdue": max(0, (date.fromisoformat(today_str()[:10]) -
                                date.fromisoformat((inv.get("due_date") or today_str())[:10])).days)}
            elif d["kind"] == "pay_bill":
                bill = await Scoped(db.bills, user["tenant_id"]).find_one(
                    {"id": (d.get("payload") or {}).get("bill_id")})
                if bill:
                    item = {"vendor": bill.get("vendor"),
                            "balance": cents_to_dollars(
                                bill.get("amount_cents", 0) - bill.get("paid_cents", 0)),
                            "due_date": bill.get("due_date")}
            if item:
                d["brief"] = await _brief_for(user, d["kind"], item)
        out.append(_action_doc(d))
    return out


@router.post("/actions/{action_id}/reject")
async def reject_action(action_id: str, user: dict = Depends(require("ops:manage"))):
    doc = await actions(user).find_one({"id": action_id})
    if not doc:
        raise HTTPException(404, "Action not found")
    if doc["status"] != "pending":
        raise HTTPException(409, "Action already decided")
    stamp = now_iso()
    await actions(user).update_one({"id": action_id}, {"$set": {
        "status": "rejected", "decided_at": stamp, "decided_by": user.get("email")}})
    await _audit(user, "copilot.action.reject", "copilot_action", action_id,
                 {"kind": doc["kind"]})
    doc.update(status="rejected", decided_at=stamp, decided_by=user.get("email"))
    return _action_doc(doc)


@router.post("/actions/{action_id}/approve")
async def approve_action(action_id: str, user: dict = Depends(require("ops:manage"))):
    """Approve AND execute a proposed action. The acting permission is the same
    ops:manage gate as proposing — but execution never happens any other way."""
    doc = await actions(user).find_one({"id": action_id})
    if not doc:
        raise HTTPException(404, "Action not found")
    if doc["status"] != "pending":
        raise HTTPException(409, "Action already decided")
    if doc["kind"] not in ACTION_KINDS:
        raise HTTPException(400, "Unknown action kind")

    stamp = now_iso()
    await actions(user).update_one({"id": action_id}, {"$set": {
        "status": "approved", "decided_at": stamp, "decided_by": user.get("email")}})

    try:
        if doc["kind"] == "chase_invoice":
            result = await _exec_chase_invoice(user, doc["payload"])
        else:  # pay_bill
            result = await _exec_pay_bill(user, doc["payload"])
    except HTTPException:
        # execution failed: roll back to pending so it can be retried or rejected
        await actions(user).update_one({"id": action_id}, {"$set": {"status": "pending",
                                                                   "decided_at": None,
                                                                   "decided_by": None}})
        raise
    except Exception as exc:
        logger.error("copilot execute failed: %s", exc)
        await actions(user).update_one({"id": action_id}, {"$set": {"status": "pending",
                                                                   "decided_at": None,
                                                                   "decided_by": None}})
        raise HTTPException(502, "The action failed to execute — nothing was changed")

    done = now_iso()
    await actions(user).update_one({"id": action_id}, {"$set": {
        "status": "executed", "decided_at": done, "decided_by": user.get("email"),
        "result": result}})
    await _audit(user, "copilot.action.execute", "copilot_action", action_id,
                 {"kind": doc["kind"], "result": result})
    doc.update(status="executed", decided_at=done, decided_by=user.get("email"), result=result)
    return _action_doc(doc)


async def _exec_chase_invoice(user, payload: dict) -> str:
    """Create a reminder record for an overdue invoice (no email is sent)."""
    inv_id = str(payload.get("invoice_id") or "")
    inv = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": inv_id})
    if not inv or inv.get("status") != "issued":
        raise HTTPException(409, "Invoice is no longer open")
    bal = inv["total_cents"] - inv.get("paid_cents", 0)
    if bal <= 0:
        raise HTTPException(409, "Invoice is no longer overdue")
    rec = {
        "id": new_id(), "type": "invoice_chase", "invoice_id": inv_id,
        "client_name": inv.get("client_name"), "balance_cents": bal,
        "note": f"Follow up on ${cents_to_dollars(bal):.2f} (due {inv.get('due_date') or 'n/a'})",
        "status": "pending", "created_by": user.get("email"), "created_at": now_iso(),
    }
    await Scoped(db.reminders, user["tenant_id"]).insert_one(rec)
    return f"Reminder created for {inv.get('client_name')} — ${cents_to_dollars(bal):.2f} overdue"


async def _exec_pay_bill(user, payload: dict) -> str:
    """Create a DRAFT payment for a bill (no money moves)."""
    bill_id = str(payload.get("bill_id") or "")
    bill = await Scoped(db.bills, user["tenant_id"]).find_one({"id": bill_id})
    if not bill or bill.get("status") not in ("open", "partial"):
        raise HTTPException(409, "Bill is no longer unpaid")
    bal = bill["amount_cents"] - bill.get("paid_cents", 0)
    amt = to_cents(payload.get("amount") or 0)
    if amt <= 0 or amt > bal:
        raise HTTPException(400, "Payment amount is no longer valid for this bill")
    draft = {
        "id": new_id(), "kind": "bill_draft", "bill_id": bill_id,
        "vendor": bill.get("vendor"), "amount_cents": amt,
        "status": "draft", "created_by": user.get("email"), "created_at": now_iso(),
    }
    await Scoped(db.draft_payments, user["tenant_id"]).insert_one(draft)
    return f"Draft payment of ${cents_to_dollars(amt):.2f} to {bill.get('vendor')} created — review and post it yourself"
