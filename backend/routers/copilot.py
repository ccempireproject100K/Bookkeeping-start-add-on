"""AI copilot: a grounded chat plus approval-gated actions.

- POST /copilot/chat            grounded Q&A on the workspace's own numbers
- POST /copilot/actions/propose propose actions WITHOUT executing anything
- POST /copilot/actions         list proposed/executed actions
- POST /copilot/actions/{id}/approve executes an approved action
- POST /copilot/actions/{id}/reject  discards a proposed action

NOTHING executes until explicitly approved. Approval-gated executions only
ever run through the propose -> approve flow, and approval requires the same
permission the human would need to do the same thing by hand:

- chase_invoice / pay_bill / categorize_transaction / reconcile_match /
  send_reminder -> ops:manage (creates reminder records and draft payments,
  posts the categorize journal entry, records bank matches, sends collection
  emails)
- post_draft_entry -> ledger:approve (posts an existing DRAFT entry to the
  ledger — owner only, same gate as the manual Post button)

No action invents amounts: every amount comes from an existing record
(bank transaction, draft entry, invoice, bill).

LLM config (env; same style as the dead new-style ai.py):
  EMERGENT_LLM_KEY - API key for an OpenAI-compatible chat endpoint
  AI_MODEL         - model name (default: gpt-5-mini)

If the key is missing, chat returns 503 with a setup hint. Everything else
(overview numbers, proposals) works without any AI key.
"""

import asyncio
import logging
import os
from datetime import date, timedelta
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db import db
from lib.pii import scrub_secrets
from lib.base import Scoped, new_id, now_iso, today_str, cents_to_dollars, to_cents
from lib.auth import require, get_current_user, PERMISSIONS
from lib.coa import NAME_OF, ACCOUNT_TYPE_OF
from lib import ledger as L
from routers import banking as _banking
from routers import collections as _collections

router = APIRouter(prefix="/copilot", tags=["copilot"])
logger = logging.getLogger(__name__)

ACTION_KINDS = ("chase_invoice", "pay_bill", "categorize_transaction",
                "post_draft_entry", "reconcile_match", "send_reminder")
ACTION_STATUSES = ("pending", "approved", "rejected", "executed")

# Approval permission per action kind: whoever approves must hold the same
# permission as doing the equivalent thing by hand in the app.
APPROVAL_PERMISSION = {
    "chase_invoice": "ops:manage",          # reminder record (no email)
    "pay_bill": "ops:manage",               # draft payment (no money moves)
    "categorize_transaction": "ops:manage",  # posts the categorize JE (manual banking route gate)
    "post_draft_entry": "ledger:approve",   # posts a draft to the GL (manual ledger post gate)
    "reconcile_match": "ops:manage",        # records a bank/ledger match (manual banking route gate)
    "send_reminder": "ops:manage",          # sends a collection email (manual collections gate)
}

REMINDER_STAGES = ("nudge", "firm", "final")


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
            "recommendation": "Create the reminder record now (no email is sent).",
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
    if kind == "categorize_transaction":
        return {
            "what_happened": f"Bank transaction '{item['description']}' of "
                             f"${item['amount']:.2f} on {item['txn_date']} is uncategorized.",
            "what_noticed": f"A bank rule suggests {item['category_name']} "
                             f"({item['category_code']}).",
            "recommendation": "Apply the suggested category — it posts exactly "
                              "like the manual Categorize button.",
            "reasoning": ("The category comes from an existing bank rule, not an AI guess; "
                          "you can re-categorize later if it's wrong."),
            "confidence": "high",
            "confidence_basis": "Matched an existing bank rule.",
        }
    if kind == "post_draft_entry":
        return {
            "what_happened": f"Draft journal entry {item.get('number') or '(unnumbered)'} "
                             f"dated {item['entry_date']}"
                             f"{': ' + item['memo'] if item.get('memo') else ''}.",
            "what_noticed": f"{item['lines']} lines; debits = credits = "
                             f"${item['total']:.2f}.",
            "recommendation": "Post it to make it official — posting is final "
                              "(undoing needs a reversal entry).",
            "reasoning": ("The draft already exists and balances; approving posts it "
                          "exactly like the manual Post button. Only existing drafts "
                          "can be posted — this never creates-and-posts in one step."),
            "confidence": "high",
            "confidence_basis": "The entry was reviewed when the draft was created.",
        }
    if kind == "reconcile_match":
        return {
            "what_happened": f"Bank transaction '{item['txn_desc']}' of "
                             f"${item['amount']:.2f} on {item['txn_date']} looks like it "
                             f"matches journal entry {item['entry_number']}"
                             f"{' (' + item['entry_memo'] + ')' if item.get('entry_memo') else ''}.",
            "what_noticed": f"Same amount; the entry is dated {item['entry_date']}.",
            "recommendation": "Record the match.",
            "reasoning": ("Matching links the bank record to the books — it changes no "
                          "amounts and posts no journal lines."),
            "confidence": item.get("confidence", "high"),
            "confidence_basis": "Amount match within a 7-day window.",
        }
    if kind == "send_reminder":
        return {
            "what_happened": f"Invoice #{item['id'][:8]} for ${item['balance']:.2f} to "
                             f"{item['client']} is {item['days_overdue']} days past due "
                             f"(due {item['due_date']}).",
            "what_noticed": f"Recipient: {item.get('recipient') or 'no email on file'}. "
                             f"Escalation stage: {item['stage']}.",
            "recommendation": "Approve only if you're ready to email the client right now.",
            "reasoning": ("Unlike a chase_invoice proposal (in-app record only), approving "
                          "this one SENDS an email to the client. Without SMTP configured "
                          "the reminder is recorded and queued instead — nothing is lost."),
            "confidence": "high",
            "confidence_basis": "Overdue balance comes from the invoice record.",
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

ACTIONS (approval-gated — you may suggest them, never execute them yourself):
- chase_invoice: propose an in-app reminder record for an overdue invoice (no email sent)
- pay_bill: propose a DRAFT payment for an unpaid bill (no money moves)
- categorize_transaction: propose posting a bank-transaction categorization as a
  journal entry (same effect as the manual Categorize button)
- post_draft_entry: propose posting an EXISTING draft journal entry to the ledger
  (never create-and-post in one step)
- reconcile_match: propose linking a bank transaction to a matching journal entry
  (records the match; changes no amounts)
- send_reminder: propose SENDING a collection email to the client (this one sends
  mail on approval; without SMTP configured the reminder is recorded and queued)

NOTHING executes until a human explicitly approves it. Approval requires the
same permission as doing it by hand: posting a draft to the ledger needs
ledger:approve (owner); reminders, draft payments, categorizing, matching, and
sending reminders need ops:manage. Every amount comes from an existing record —
never invent one.

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
def _reminder_stage(days: int) -> str:
    """Escalation stage for a reminder, mirroring the autopilot's ladder."""
    if days > 25:
        return "final"
    if days > 10:
        return "firm"
    return "nudge"


def _reminder_message(inv: dict, days: int, balance_dollars: float) -> str:
    """Plain-text reminder body built only from the invoice record."""
    ref = (inv.get("id") or "")[:8]
    name = inv.get("client_name") or "there"
    due = (inv.get("due_date") or "")[:10] or "recently"
    return (f"Hi {name},\n\n"
            f"A quick reminder that invoice #{ref} for ${balance_dollars:.2f} "
            f"was due {due} ({days} days ago). "
            "Please reply if you need anything to get this paid.\n\nThank you!")


async def _match_candidate(user, txn: dict):
    """Best posted-journal-entry candidate for a bank txn: same total amount,
    entry date within +/- 7 days. Returns (entry, lines) or None."""
    tid = user["tenant_id"]
    amt = abs(txn.get("amount_cents", 0))
    if amt == 0:
        return None
    try:
        base = date.fromisoformat((txn.get("txn_date") or "")[:10])
    except ValueError:
        return None
    window = [(base + timedelta(days=d)).isoformat() for d in range(-7, 8)]
    cands = await Scoped(db.journal_entries, tid).find_list(
        {"status": "posted", "entry_date": {"$in": window}}, limit=50)
    for e in cands:
        lines = await Scoped(db.journal_lines, tid).find_list(
            {"entry_id": e["id"]}, limit=200)
        total = sum(int(l.get("debit", 0) or 0) for l in lines)
        if total == amt:
            return e, lines
    return None


@router.post("/actions/propose")
async def propose_actions(user: dict = Depends(get_current_user)):
    """Build proposed actions from the workspace data WITHOUT executing anything.

    chase_invoice        -> one proposed reminder record per overdue invoice.
    pay_bill             -> one proposed draft payment per unpaid bill.
    categorize_transaction -> one proposed categorize per unmatched bank txn
                              that has a bank-rule suggestion.
    post_draft_entry     -> one proposed post per draft journal entry.
    reconcile_match      -> one proposed match per unmatched bank txn with a
                            same-amount posted entry within +/- 7 days.
    send_reminder        -> one proposed COLLECTION EMAIL per overdue invoice
                            (sends mail on approval).
    Existing pending/approved proposals are not duplicated. Proposing is open
    to anyone who can chat; approval is permission-gated per action kind.
    """
    tid = user["tenant_id"]
    snap = await _snapshot(user)
    open_docs = await actions(user).find_list(
        {"status": {"$in": ["pending", "approved"]}}, limit=200)
    seen = set()
    for a in open_docs:
        p = a.get("payload") or {}
        entity = (p.get("invoice_id") or p.get("bill_id") or p.get("txn_id")
                  or p.get("entry_id"))
        seen.add((a["kind"], entity))
    out = []

    async def _insert(doc):
        await actions(user).insert_one(doc)
        out.append(_action_doc(doc))

    # ---- chase_invoice: reminder records (no email) ----
    for o in snap["overdue_invoices"][:5]:
        if ("chase_invoice", o["id"]) in seen:
            continue
        await _insert({
            "id": new_id(), "kind": "chase_invoice",
            "title": f"Remind {o['client']} — ${o['balance']:.2f} overdue",
            "detail": f"Invoice {o['days_overdue']} day(s) past due (due {o['due_date']}). "
                      "Creates an in-app reminder record — nothing is sent.",
            "brief": await _brief_for(user, "chase_invoice", o),
            "payload": {"invoice_id": o["id"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })

    # ---- pay_bill: draft payments (no money moves) ----
    for b in snap["unpaid_bills"][:5]:
        if ("pay_bill", b["id"]) in seen:
            continue
        await _insert({
            "id": new_id(), "kind": "pay_bill",
            "title": f"Draft payment — {b['vendor']} (${b['balance']:.2f})",
            "detail": f"Bill due {b['due_date'] or 'n/a'}. Creates a DRAFT payment for review — "
                      "no money moves until you post it yourself.",
            "brief": await _brief_for(user, "pay_bill", b),
            "payload": {"bill_id": b["id"], "amount": b["balance"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })

    # ---- categorize_transaction: unmatched txns with a bank-rule suggestion ----
    txns = await Scoped(db.bank_transactions, tid).find_list(
        {"status": "unmatched"}, sort=[("txn_date", 1)], limit=25)
    n_cat = 0
    for t in txns:
        if n_cat >= 5:
            break
        code = t.get("suggested_code") or t.get("category_code")
        if not code or code not in ACCOUNT_TYPE_OF:
            continue  # never invent a category — only rule-based suggestions
        if ("categorize_transaction", t["id"]) in seen:
            continue
        acc = await Scoped(db.bank_accounts, tid).find_one(
            {"id": t.get("bank_account_id")})
        bank_gl = (acc or {}).get("gl_code") or "1020"
        amt = abs(t.get("amount_cents", 0))
        if amt == 0:
            continue
        name = NAME_OF.get(code, code)
        money = cents_to_dollars(amt)
        if t["amount_cents"] > 0:  # money in
            je = f"DR {bank_gl} ${money:.2f} / CR {name} ({code}) ${money:.2f}"
        else:                      # money out
            je = f"DR {name} ({code}) ${money:.2f} / CR {bank_gl} ${money:.2f}"
        await _insert({
            "id": new_id(), "kind": "categorize_transaction",
            "title": f"Categorize '{(t.get('description') or 'transaction')[:40]}' → {name}",
            "detail": f"Bank transaction of ${money:.2f} on {t.get('txn_date') or 'n/a'} "
                      f"matches a bank rule → {name} ({code}). On approve, posts journal "
                      f"entry [{je}] dated {t.get('txn_date') or 'today'} and marks the "
                      "transaction 'cleared'. Same effect as the manual Categorize button.",
            "brief": await _brief_for(user, "categorize_transaction", {
                "description": t.get("description"), "txn_date": t.get("txn_date"),
                "amount": money, "category_code": code, "category_name": name}),
            "payload": {"txn_id": t["id"], "category_code": code}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })
        n_cat += 1

    # ---- post_draft_entry: existing draft journal entries ----
    drafts = await Scoped(db.journal_entries, tid).find_list(
        {"status": "draft"}, sort=[("entry_date", 1)], limit=5)
    for e in drafts:
        if ("post_draft_entry", e["id"]) in seen:
            continue
        lines = await Scoped(db.journal_lines, tid).find_list(
            {"entry_id": e["id"]}, limit=200)
        total = sum(int(l.get("debit", 0) or 0) for l in lines)
        memo = (e.get("memo") or "").strip()
        await _insert({
            "id": new_id(), "kind": "post_draft_entry",
            "title": f"Post draft entry {e.get('number') or '(unnumbered)'} — "
                     f"${cents_to_dollars(total):.2f}",
            "detail": f"Draft journal entry dated {e.get('entry_date') or 'n/a'} "
                      f"({len(lines)} line(s), debits = credits = "
                      f"${cents_to_dollars(total):.2f})"
                      f"{': ' + memo if memo else ''}. On approve, posts it to the ledger "
                      "exactly like the manual Post button — balances update and the entry "
                      "becomes official. Posting is final; undoing needs a reversal entry.",
            "brief": await _brief_for(user, "post_draft_entry", {
                "number": e.get("number"), "entry_date": e.get("entry_date"),
                "memo": memo, "lines": len(lines), "total": cents_to_dollars(total)}),
            "payload": {"entry_id": e["id"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })

    # ---- reconcile_match: unmatched txns with a same-amount posted entry ----
    n_match = 0
    for t in txns:
        if n_match >= 5:
            break
        if ("reconcile_match", t["id"]) in seen:
            continue
        found = await _match_candidate(user, t)
        if not found:
            continue
        e, _lines = found
        money = cents_to_dollars(abs(t.get("amount_cents", 0)))
        e_memo = (e.get("memo") or "").strip()
        await _insert({
            "id": new_id(), "kind": "reconcile_match",
            "title": f"Match bank txn ${money:.2f} ↔ JE {e.get('number') or '(unnumbered)'}",
            "detail": f"Bank transaction '{t.get('description') or 'n/a'}' of ${money:.2f} "
                      f"on {t.get('txn_date') or 'n/a'} matches posted journal entry "
                      f"{e.get('number') or e['id'][:8]}"
                      f"{' (' + e_memo + ')' if e_memo else ''} (same amount, entry dated "
                      f"{e.get('entry_date') or 'n/a'}). On approve, records the match and "
                      "marks the transaction 'matched'. Changes no amounts, posts nothing.",
            "brief": await _brief_for(user, "reconcile_match", {
                "txn_desc": t.get("description"), "txn_date": t.get("txn_date"),
                "amount": money, "entry_number": e.get("number") or e["id"][:8],
                "entry_memo": e_memo, "entry_date": e.get("entry_date"),
                "confidence": "high"}),
            "payload": {"txn_id": t["id"], "entry_id": e["id"]}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })
        n_match += 1

    # ---- send_reminder: collection EMAILS for overdue invoices ----
    for o in snap["overdue_invoices"][:3]:
        if ("send_reminder", o["id"]) in seen:
            continue
        inv = await Scoped(db.invoices, tid).find_one({"id": o["id"]})
        if not inv:
            continue
        stage = _reminder_stage(o["days_overdue"])
        recipient = await _collections._reminder_recipient(user, inv)
        shop = await _collections._shop_name(user)
        message = _reminder_message(inv, o["days_overdue"], o["balance"])
        subject, _body = _collections._build_reminder_email(
            message, inv["id"], o["days_overdue"], stage, shop)
        to_note = (f"to {recipient}" if recipient
                   else "(no email on file — will be recorded as no_recipient)")
        await _insert({
            "id": new_id(), "kind": "send_reminder",
            "title": f"Email {o['client']} — ${o['balance']:.2f} overdue ({stage})",
            "detail": f"On approve, SENDS a collection email {to_note} with subject "
                      f"'{subject}'. Unlike a chase reminder (in-app record only), this "
                      "one actually emails the client. If SMTP is not configured, the "
                      "reminder is recorded and queued for a later run instead of sending.",
            "brief": await _brief_for(user, "send_reminder", {
                "id": o["id"], "client": o["client"], "balance": o["balance"],
                "due_date": o["due_date"], "days_overdue": o["days_overdue"],
                "recipient": recipient, "stage": stage}),
            "payload": {"invoice_id": o["id"], "stage": stage}, "status": "pending",
            "created_at": now_iso(), "decided_at": None, "decided_by": None, "result": None,
        })

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
async def approve_action(action_id: str, user: dict = Depends(get_current_user)):
    """Approve AND execute a proposed action.

    Approval is permission-gated per action kind (APPROVAL_PERMISSION): the
    approver must hold the same permission they'd need to do it by hand —
    e.g. posting a draft entry needs ledger:approve (owner), while reminders,
    draft payments, categorizing, matching, and sending reminder emails need
    ops:manage. Execution never happens any other way.
    """
    doc = await actions(user).find_one({"id": action_id})
    if not doc:
        raise HTTPException(404, "Action not found")
    if doc["status"] != "pending":
        raise HTTPException(409, "Action already decided")
    if doc["kind"] not in ACTION_KINDS:
        raise HTTPException(400, "Unknown action kind")

    perm = APPROVAL_PERMISSION.get(doc["kind"])
    if not perm or user.get("role") not in PERMISSIONS.get(perm, set()):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user.get('role')}' cannot approve a '{doc['kind']}' "
                   f"action (requires {perm or 'a known action kind'})")

    executor = _EXECUTORS[doc["kind"]]
    stamp = now_iso()
    await actions(user).update_one({"id": action_id}, {"$set": {
        "status": "approved", "decided_at": stamp, "decided_by": user.get("email")}})

    try:
        result = await executor(user, doc.get("payload") or {})
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


async def _exec_categorize_transaction(user, payload: dict) -> str:
    """Post the bank-transaction categorization as a journal entry.

    Reuses the real banking categorize path (posts exactly one balanced JE and
    marks the transaction 'cleared') — the same effect as the manual
    Categorize button.
    """
    txn_id = str(payload.get("txn_id") or "")
    category_code = str(payload.get("category_code") or "")
    res = await _banking.categorize(
        txn_id,
        _banking.CategorizeBody(category_code=category_code,
                                memo=payload.get("memo")),
        user)
    return (f"Categorized transaction and posted journal entry "
            f"{res.get('entry_number') or '(unnumbered)'} — same as manual categorize")


async def _exec_post_draft_entry(user, payload: dict) -> str:
    """Post an existing DRAFT journal entry to the ledger.

    Reuses the same path as the manual Post button (L.post_draft): only
    existing drafts can be posted — never create-and-post in one step.
    """
    entry_id = str(payload.get("entry_id") or "")
    entry = await L.post_draft(db, user["tenant_id"], user, entry_id)
    return (f"Posted draft journal entry {entry.get('number') or entry_id} "
            f"— balances are now official")


async def _exec_reconcile_match(user, payload: dict) -> str:
    """Record a bank-transaction ↔ journal-entry match.

    Reuses the real banking match path. Re-verifies the transaction is still
    unmatched at approve time; changes no amounts and posts nothing.
    """
    txn_id = str(payload.get("txn_id") or "")
    entry_id = str(payload.get("entry_id") or "")
    t = await Scoped(db.bank_transactions, user["tenant_id"]).find_one({"id": txn_id})
    if not t or t.get("status") != "unmatched":
        raise HTTPException(409, "Transaction is no longer available to match")
    res = await _banking.match(txn_id, entry_id, user)
    return (f"Matched bank transaction to journal entry "
            f"{res.get('entry_number') or entry_id} — no amounts changed")


async def _exec_send_reminder(user, payload: dict) -> str:
    """Send a collection reminder email for an overdue invoice.

    Reuses the merged collections pattern: key-gated lib.mailer.send_email and
    the email_status convention (sent|queued|failed|no_recipient). Without SMTP
    configured, the reminder is recorded and queued for a later run — never a
    crash, never a fake 'sent'. The message body is rebuilt from the invoice
    record at approve time, so amounts are always current.
    """
    inv_id = str(payload.get("invoice_id") or "")
    inv = await Scoped(db.invoices, user["tenant_id"]).find_one({"id": inv_id})
    if not inv or inv.get("status") not in ("issued", "partial"):
        raise HTTPException(409, "Invoice is no longer open")
    bal = inv["total_cents"] - inv.get("paid_cents", 0)
    if bal <= 0:
        raise HTTPException(409, "Invoice balance is zero")
    days = _collections._days_overdue(inv.get("due_date"), today_str())
    if days <= 0:
        raise HTTPException(409, "Invoice is no longer overdue")
    stage = str(payload.get("stage") or "nudge")
    if stage not in REMINDER_STAGES:
        stage = "nudge"
    message = _reminder_message(inv, days, cents_to_dollars(bal))
    rid = await _collections._record_reminder(user, inv, message, stage, days)
    rec = await Scoped(db.payment_reminders, user["tenant_id"]).find_one({"id": rid})
    status = (rec or {}).get("email_status")
    ref = inv_id[:8]
    if status == "sent":
        return f"Reminder email sent to {(rec or {}).get('email_to')} for invoice #{ref}"
    if status == "queued":
        return f"Reminder recorded and queued (SMTP not configured) for invoice #{ref}"
    if status == "no_recipient":
        return f"Reminder recorded but no customer email on file for invoice #{ref}"
    return f"Reminder recorded for invoice #{ref} (email status: {status or 'not sent'})"


_EXECUTORS = {
    "chase_invoice": _exec_chase_invoice,
    "pay_bill": _exec_pay_bill,
    "categorize_transaction": _exec_categorize_transaction,
    "post_draft_entry": _exec_post_draft_entry,
    "reconcile_match": _exec_reconcile_match,
    "send_reminder": _exec_send_reminder,
}
