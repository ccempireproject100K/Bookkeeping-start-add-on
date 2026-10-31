"""AI expert guide (phase 3, owner ruling 2026-10-31): a
conversational layer over the existing SetupChecklist.

The guide interviews the tenant one question at a time — business
basics, entity type, fiscal year, opening balances — and writes every
answer into the SAME state the self-serve checklist and forms use:
the tenant doc, tenant settings, onboarding progress, the welcome
gift deliveries, the customers collection, and the ledger's opening
balances. It operates only the product's existing machinery (the
onboarding, gift, customer and ledger handlers are called directly);
accounting/legal questions beyond the scripted path are flagged for
human review, never improvised.

AI gating mirrors the copilot exactly: without AI_API_KEY the guide
endpoints answer the same honest 503 and the UI shows the same
unavailable state as the rest of the product's AI features — the
self-serve checklist is always available regardless.

Presentation: every response carries a `character_state` for the
guide's avatar slot (idle / walk / errand-run / talking / celebrate /
error — the shared empire mascot rig). The character itself is a
swappable skin over this conversation; bookkeeping's is the bear
(vest, pants, glasses — final art is the owner's ruling, pending).
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib import guide as G
from lib import niches
from lib import trial as trial_lib
from lib.audit import append_audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso
from routers import customers as customers_router
from routers import ledger as ledger_router
from routers import onboarding as onboarding_router
from routers import welcome_gift as gift_router

router = APIRouter(prefix="/guide", tags=["guide"])

CHARACTER = {
    "slot": "bookkeeping-guide",
    "skin": "bear",
    "description": "Full-body bear in a vest and pants, wearing "
                   "glasses — bookkeeping's guide character, on the "
                   "shared mascot rig. Final artwork pending the "
                   "owner's visual ruling; the UI ships with a "
                   "placeholder silhouette.",
    "states": ["idle", "walk", "errand-run", "talking", "celebrate",
               "error"],
}


def ai_configured() -> bool:
    """The copilot's gate, verbatim: AI_API_KEY presence."""
    return bool(os.environ.get("AI_API_KEY", "").strip())


def _require_ai():
    if not ai_configured():
        raise HTTPException(
            status_code=503,
            detail={"code": "ai_not_configured",
                    "message": "AI is not configured. Set AI_API_KEY on "
                               "the server to enable the guide — the "
                               "self-serve checklist works either way."})


def _sessions(user):
    return Scoped(db.guide_sessions, user["tenant_id"])


def _terms(tenant) -> dict:
    pack = niches.get_pack((tenant or {}).get("niche_id")) or {}
    return pack.get("terms") or {}


def _question_for(step_id: str, tenant: dict, session: dict) -> str:
    terms = _terms(tenant)
    customer = terms.get("customer", "customer")
    trade = (niches.get_pack((tenant or {}).get("niche_id")) or {}).get("trade")
    if step_id == "business_name":
        return "What's your business called? I'll put it on your books exactly as you type it."
    if step_id == "trade":
        return ("What kind of work do you do? Your books wear your "
                "trade's face — its own chart of accounts, services and "
                "words. Name your trade (for example: HVAC, plumbing, "
                "cleaning).")
    if step_id == "entity_type":
        return ("How is the business set up legally — sole proprietor, "
                "LLC, S corp, C corp, or partnership? This shapes how "
                "your taxes are figured later.")
    if step_id == "fiscal_year":
        return ("Which month does your fiscal year start? Most "
                f"{trade or 'small'} businesses use January (a calendar "
                "year). A month name or number is fine.")
    if step_id == "opening_balance":
        return ("Roughly how much money is in the business bank "
                "account today? That's your opening balance — I'll "
                "record it as the starting point of your books. A "
                "dollar amount, or say “skip.”")
    if step_id == "clean_gift":
        return ("Now your first welcome gift — the free file clean, "
                "for every new workspace. If you have a messy "
                f"{customer} list as a CSV, upload it here and I'll "
                "run it through the DataClean engine and hand it back "
                "cleaned, with a duplicate report. Or say “skip” and "
                "do it later from the gift page.")
    if step_id == "tax_profile":
        return ("Which state is the business based in? Two letters "
                "(like TX or CA). I'll save it to your tax profile.")
    if step_id == "estimate_gift":
        return ("Last gift: your first quarterly tax estimate, in "
                "writing. Roughly how much net profit has the business "
                "made so far this year, and any W-2 wages? Give me the "
                "profit number (W-2 wages optional).")
    if step_id == "first_customer":
        return (f"Who's your first {customer}? Give me a name and "
                f"I'll add them to your books — or say “skip.”")
    return ""


async def _progress(user) -> dict:
    state = await onboarding_router.get_onboarding(user)
    total = state["total"] or 1
    return {"done": state["done"], "total": state["total"],
            "percent": round(100 * state["done"] / total)}


async def _respond(user, session, message, outcome="", flagged=False):
    tenant = await db.tenants.find_one({"id": user["tenant_id"]},
                                       {"_id": 0}) or {}
    step = session.get("step")
    return {
        "session_id": session["id"],
        "step": step,
        "message": message,
        "character_state": "talking" if flagged else
            G.character_for(step, outcome) if step else
            ("celebrate" if outcome == "completed" else "idle"),
        "flagged": flagged,
        "done": bool(session.get("done")),
        "progress": await _progress(user),
        "tenant_name": tenant.get("name"),
    }


@router.get("/status")
async def guide_status(user: dict = Depends(require("ops:manage"))):
    """Never 503s: the onboarding surface renders the honest
    unavailable state from this when the AI key is absent."""
    return {"ai_configured": ai_configured(), "character": CHARACTER}


@router.post("/start")
async def start(user: dict = Depends(require("ops:manage"))):
    _require_ai()
    tenant = await db.tenants.find_one({"id": user["tenant_id"]},
                                       {"_id": 0}) or {}
    session = {"id": new_id(), "step": G.STEP_IDS[0], "answers": {},
               "outcomes": {}, "done": False, "created_at": now_iso()}
    await _sessions(user).insert_one(session)
    intro = (
        "Hi — I'm your setup guide. I'll ask one question at a time "
        "and fill in your setup checklist as we go; nothing here "
        "replaces the checklist, and you can stop and use it yourself "
        "whenever you like. First: ")
    return await _respond(user, session,
                          intro + _question_for(G.STEP_IDS[0], tenant, session))


class AnswerIn(BaseModel):
    session_id: str
    text: Optional[str] = None
    payload: Optional[dict] = None    # gift steps carry structured data


async def _advance(user, session, outcome=""):
    idx = G.STEP_IDS.index(session["step"])
    if idx + 1 >= len(G.STEP_IDS):
        session["done"] = True
        session["step"] = None
        await _sessions(user).update_one(
            {"id": session["id"]},
            {"$set": {"done": True, "step": None,
                      "outcomes": session.get("outcomes", {})}})
        state = await onboarding_router.get_onboarding(user)
        return await _respond(
            user, session,
            "That's the guided setup done — "
            f"{state['done']} of {state['total']} checklist steps "
            "are complete, and everything I set up is in the same "
            "places the checklist itself uses. Anything left is on "
            "your dashboard checklist whenever you're ready.",
            outcome="completed")
    session["step"] = G.STEP_IDS[idx + 1]
    await _sessions(user).update_one(
        {"id": session["id"]},
        {"$set": {"step": session["step"], "answers": session.get("answers", {}),
                  "outcomes": session.get("outcomes", {})}})
    tenant = await db.tenants.find_one({"id": user["tenant_id"]},
                                       {"_id": 0}) or {}
    return await _respond(user, session,
                          _question_for(session["step"], tenant, session),
                          outcome="walk")


def _skip(text: str) -> bool:
    return (text or "").strip().lower() in ("skip", "later", "no", "none", "pass")


@router.post("/answer")
async def answer(body: AnswerIn, user: dict = Depends(require("ops:manage"))):
    _require_ai()
    session = await _sessions(user).find_one({"id": body.session_id})
    if not session or session.get("done"):
        raise HTTPException(status_code=404,
                            detail="Guide session not found or finished")
    step = session["step"]
    text = (body.text or "").strip()
    payload = body.payload or {}
    tenant = await db.tenants.find_one({"id": user["tenant_id"]},
                                       {"_id": 0}) or {}

    # Out-of-path questions are flagged for a human, never improvised,
    # and the interview does not advance. (Structured gift payloads
    # aren't free text, so they skip this check.)
    if text and not payload and G.looks_like_question(text):
        flag = {"id": new_id(), "tenant_id": user["tenant_id"],
                "session_id": session["id"], "step": step,
                "question": text, "flagged_at": now_iso(),
                "status": "needs_human_review"}
        await db.guide_flags.insert_one(flag)
        await append_audit(db, {
            "id": new_id(), "tenant_id": user["tenant_id"],
            "actor_id": user["id"], "actor_name": user.get("name"),
            "actor_role": user.get("role"),
            "action": "guide.question_flagged", "entity": "guide_flag",
            "entity_id": flag["id"],
            "details": {"step": step}, "created_at": now_iso()})
        return await _respond(
            user, session,
            G.FLAG_MESSAGE + " " + _question_for(step, tenant, session),
            flagged=True)

    answers = session.setdefault("answers", {})
    outcomes = session.setdefault("outcomes", {})

    if step == "business_name":
        if len(text) < 1:
            return await _respond(user, session, "I need the business name to set up your books — what should I put?")
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"name": text}})
        answers[step] = text
        return await _advance(user, session)

    if step == "trade":
        packs = niches.all_packs()
        match = next((p for p in packs
                      if p["id"] == text.lower()
                      or p.get("trade", "").lower() == text.lower()), None)
        if not match:
            names = ", ".join(p.get("trade", p["id"]) for p in packs[:8])
            return await _respond(user, session,
                                  f"I don't have a pack called “{text}” — the trades I know include {names}… Which is closest?")
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"niche_id": match["id"]}})
        answers[step] = match["id"]
        resp = await _advance(user, session)
        resp["message"] = (f"{match.get('product_name', match['trade'])} it is — "
                           "your books now wear that face. " + resp["message"])
        return resp

    if step == "entity_type":
        ent = G.ENTITY_TYPES.get(text.lower())
        if not ent:
            return await _respond(user, session, "I can record: sole proprietor, LLC, S corp, C corp, or partnership. Which one is it?")
        settings = dict(tenant.get("settings") or {})
        settings["entity_type"] = ent
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"settings": settings}})
        answers[step] = ent
        return await _advance(user, session)

    if step == "fiscal_year":
        month = G.MONTHS.get(text.lower())
        if month is None and text.isdigit() and 1 <= int(text) <= 12:
            month = int(text)
        if month is None and text.lower() in ("calendar", "january", "jan"):
            month = 1
        if month is None:
            return await _respond(user, session, "A month name or a number from 1 to 12 — which month starts your fiscal year?")
        settings = dict(tenant.get("settings") or {})
        settings["fiscal_year_start_month"] = month
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"settings": settings}})
        answers[step] = month
        return await _advance(user, session)

    if step == "opening_balance":
        if _skip(text):
            outcomes[step] = "skipped"
            return await _advance(user, session)
        try:
            amount = float(text.replace("$", "").replace(",", ""))
            assert amount >= 0
        except (ValueError, AssertionError):
            return await _respond(user, session, "A dollar amount like 2500, or “skip” — how much is in the business account today?")
        try:
            await ledger_router.opening_balances(
                ledger_router.OpeningBody(cash_bank=amount), user)
            outcomes[step] = "recorded"
        except HTTPException as e:
            if e.status_code == 409:
                outcomes[step] = "already_recorded"
            else:
                outcomes[step] = "failed"
                return await _respond(user, session,
                                      "I couldn't record that opening balance — the books said: "
                                      f"{e.detail}. We can fix it later from the ledger; let's keep going.",
                                      outcome="failed")
        answers[step] = amount
        return await _advance(user, session)

    if step == "clean_gift":
        if _skip(text) and not payload:
            outcomes[step] = "skipped"
            return await _advance(user, session)
        try:
            out = await gift_router.gift_clean(
                gift_router.GiftCleanIn(
                    kind=payload.get("kind", "customers"),
                    csv_text=payload.get("csv_text", "")), user)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else (e.detail or {}).get("message", "the clean could not run")
            return await _respond(user, session,
                                  f"The clean couldn't run: {detail}. You can retry right here, or do it later from the gift page.",
                                  outcome="failed")
        outcomes[step] = "delivered"
        answers[step] = {"rows": out["rows"],
                         "pairs": len(out["dedupe"]["pairs"])}
        resp = await _advance(user, session, outcome="delivered")
        resp["character_state"] = "celebrate"
        resp["message"] = (
            f"Done — {out['rows']} rows cleaned, "
            f"{len(out['changes'])} fixes made, and the cleaned file "
            "is yours to download on the gift page. "
            + G.explain_duplicates(out["dedupe"]) + " " + resp["message"])
        resp["gift_result"] = {"rows": out["rows"],
                               "changes": len(out["changes"]),
                               "pairs": len(out["dedupe"]["pairs"])}
        return resp

    if step == "tax_profile":
        code = text.upper()
        if len(code) != 2 or not code.isalpha():
            return await _respond(user, session, "Two letters — like TX or CA. Which state is the business based in?")
        settings = dict(tenant.get("settings") or {})
        settings["tax_home_state"] = code
        await db.tenants.update_one({"id": user["tenant_id"]},
                                    {"$set": {"settings": settings}})
        await onboarding_router.complete_step(
            onboarding_router.CompleteIn(step="tax_profile"), user)
        answers[step] = code
        return await _advance(user, session)

    if step == "estimate_gift":
        if not trial_lib.gift_estimate_unlocked(tenant):
            outcomes[step] = "skipped_gate"
            resp = await _advance(user, session)
            resp["message"] = (
                "This gift rides the card trial door, and this "
                "workspace is on the no-card door — so the estimate "
                "stays locked here, plainly: everyone gets the file "
                "clean; the tax estimate in writing (and half off "
                "your first month) comes with the card opt-in. You "
                "can switch doors any time in Settings. " + resp["message"])
            return resp
        profit = payload.get("net_profit", text)
        try:
            profit = float(str(profit).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            return await _respond(user, session, "A number is all I need — roughly how much net profit so far this year?")
        settings = tenant.get("settings") or {}
        try:
            out = await gift_router.gift_estimate(
                gift_router.GiftEstimateIn(
                    net_profit=profit,
                    filing_status=payload.get("filing_status", "single"),
                    state=settings.get("tax_home_state"),
                    w2_wages=payload.get("w2_wages")), user)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            if detail.get("code") == "engine_not_built":
                outcomes[step] = "unavailable"
                resp = await _advance(user, session, outcome="unavailable")
                resp["character_state"] = "error"
                resp["message"] = (
                    "Honest answer: the tax estimate engine isn't "
                    "switched on in this environment yet, so I can't "
                    "write that packet — and I won't make up a number. "
                    "The gift stays yours to claim from the gift page "
                    "once it's on. " + resp["message"])
                return resp
            raise
        outcomes[step] = "delivered"
        result = out["result"]
        total = (result.get("totalLiabilityCents") or 0) / 100
        notes = result.get("notes") or []
        narration = (
            f"Your estimate is written: ${total:,.2f} total estimated "
            f"liability, split across {len(result.get('schedule') or [])} "
            "quarterly payments. The full packet — breakdown, schedule "
            "and workpapers — is on the gift page, yours to keep.")
        if notes:
            narration += " In the engine's own words: " + " ".join(notes)
        resp = await _advance(user, session, outcome="delivered")
        resp["character_state"] = "celebrate"
        resp["message"] = narration + " " + resp["message"]
        resp["gift_result"] = {"status": out["status"],
                               "total_liability_cents": out["total_liability_cents"]}
        return resp

    if step == "first_customer":
        if _skip(text):
            outcomes[step] = "skipped"
            return await _advance(user, session)
        await customers_router.create_customer(
            customers_router.CustomerBody(name=text), user)
        answers[step] = text
        return await _advance(user, session)

    raise HTTPException(status_code=409, detail=f"Unknown guide step {step}")
