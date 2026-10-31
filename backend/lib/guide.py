"""The AI expert guide's script (phase 3, owner ruling 2026-10-31).

The guide INTERVIEWS instead of form-filling, but it is a SCRIPTED
path over the product's existing machinery — every answer lands in
exactly the state the self-serve checklist and forms use (tenant doc,
tenant settings, onboarding progress, gift deliveries, customers,
opening balances). It never improvises accounting or legal advice:
a question that leaves the scripted path is flagged for human review
and the interview carries on.

Honesty rules from the gift/trial phases carry over untouched: the
tax engine's NOT_IMPLEMENTED notes pass through verbatim, an unbuilt
engine is stated plainly, and the estimate step follows the card-door
gate — the guide explains the split instead of smoothing it over.
"""

# The interview, in order. `asks` is the character's working state
# while the question is on screen; steps that run an errand (the two
# gifts) ask in "errand-run" — he's off to fetch something.
SCRIPT = [
    {"id": "business_name", "asks": "talking"},
    {"id": "trade", "asks": "talking"},
    {"id": "entity_type", "asks": "talking"},
    {"id": "fiscal_year", "asks": "talking"},
    {"id": "opening_balance", "asks": "talking"},
    {"id": "clean_gift", "asks": "errand-run"},
    {"id": "tax_profile", "asks": "talking"},
    {"id": "estimate_gift", "asks": "errand-run"},
    {"id": "first_customer", "asks": "talking"},
]
STEP_IDS = [s["id"] for s in SCRIPT]

ENTITY_TYPES = {
    "sole proprietor": "sole_proprietor", "sole_prop": "sole_proprietor",
    "sole_proprietor": "sole_proprietor", "self employed": "sole_proprietor",
    "self-employed": "sole_proprietor",
    "llc": "llc", "s corp": "s_corp", "s-corp": "s_corp", "scorp": "s_corp",
    "s_corp": "s_corp", "c corp": "c_corp", "c-corp": "c_corp",
    "c_corp": "c_corp", "partnership": "partnership",
}
ENTITY_LABELS = {
    "sole_proprietor": "sole proprietor", "llc": "LLC", "s_corp": "S corp",
    "c_corp": "C corp", "partnership": "partnership",
}

MONTHS = {name: i + 1 for i, name in enumerate([
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december"])}

# Leaving the scripted path: advice-seeking language. An answer to a
# setup question never looks like this; a question that does is
# flagged for a human, never answered from the model's head.
QUESTION_MARKERS = (
    "should i", "should we", "can i deduct", "deduct", "write off",
    "write-off", "tax advice", "is it legal", "is that legal", "legal?",
    "liable", "liability", "sue", "lawsuit", "recommend", "advice",
    "what would you do", "am i allowed", "is it ok to", "is it okay to",
)

FLAG_MESSAGE = (
    "That's a question for a human expert — I won't improvise tax or "
    "legal advice. I've flagged it for review, and a person will "
    "follow up. Meanwhile, let's keep your setup moving."
)


def looks_like_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return any(m in t for m in QUESTION_MARKERS)


def explain_duplicates(dedupe: dict) -> str:
    """The duplicate report in plain words, built ONLY from what the
    engine actually found (counts, names, its own evidence strings)."""
    pairs = (dedupe or {}).get("pairs") or []
    if not pairs:
        return ("Good news — I scanned the cleaned file and found no "
                "likely duplicates. Every row looks like a different "
                "record.")
    lines = [
        f"I found {len(pairs)} likely duplicate pair(s) in the file. "
        "A duplicate pair means two rows that look like the same "
        "record written two ways — same person or business, entered "
        "twice with small differences.",
    ]
    for p in pairs[:3]:
        recs = p.get("records") or [{}, {}]
        a = recs[0].get("name") or recs[0].get("id") or "one row"
        b = recs[1].get("name") or recs[1].get("id") or "another row"
        ev = "; ".join(p.get("evidence") or [])
        lines.append(f"For example, “{a}” and “{b}” look like the same "
                     f"record — the engine's evidence: {ev}.")
    lines.append(
        "Nothing was merged or deleted — the cleaned file keeps every "
        "row, and the duplicate report (yours to download on the gift "
        "page) lists each pair so you can decide what to combine.")
    return " ".join(lines)


def character_for(step_id: str, outcome: str = "") -> str:
    """Map a script moment onto the shared mascot rig's states."""
    if outcome in ("delivered", "completed"):
        return "celebrate"
    if outcome in ("unavailable", "failed"):
        return "error"
    step = next((s for s in SCRIPT if s["id"] == step_id), None)
    return step["asks"] if step else "idle"
