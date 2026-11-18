"""Intercompany eliminations for the consolidated multi-entity view.

Read-only and computed live from the ledger, exactly like the rest of
entities.py: nothing here writes anything, and no entity's own books are
ever altered. Eliminations exist only inside the consolidated response.

How an intercompany deal is recognised, in priority order:

1. EXPLICIT FLAG + COUNTERPARTY LINK — a journal entry (or a single line)
   carries ``intercompany: true`` and ``counterparty_tenant_id`` naming a
   sister business owned by the same login (wired through
   lib/ledger.post_journal_entry and the ledger router). Lines inherit
   the entry's marking unless they set their own.
2. LEGACY ACCOUNT FLAG — the pre-existing heuristic in entities.py:
   accounts whose code starts with "14" or "21" *look* intercompany.
   Those amounts are reported, but with no named sister business they
   cannot be paired, so they land on the unmatched list with that reason
   — never force-matched, never dropped.

Pairing, per pair of sister businesses:
- balance class: one's flagged receivable (asset) against the other's
  flagged payable (liability), and the mirror direction;
- P&L class: one's flagged intercompany revenue against the other's
  flagged intercompany expense, and the mirror direction.
The eliminated amount is the smaller side; any remainder stays unmatched
with a plain reason. Flagged cash lines are listed as informational
"cash stayed inside the group" rows: combined cash is a balance, not a
flow, so an internal transfer cannot inflate it and its elimination is
honestly $0 — shown, not hidden.

Tenant switch: ``settings.eliminations_enabled`` on the tenant doc,
DEFAULT OFF (owner condition, 2026-11-14). OFF = the consolidated view is
exactly the flagged view it has always been; this module's detail and
unmatched lists are still computed and returned in both states.
"""
from lib.base import Scoped
from lib.coa import ACCOUNT_TYPE_OF, NAME_OF
from lib import ledger as L

CASH_CODES = {"1010", "1020", "1021", "1022", "1023", "1050", "1060"}
LEGACY_PREFIXES = ("14", "21")
_BALANCE_PAIRS = (("asset", "liability"), ("liability", "asset"))
_PNL_PAIRS = (("revenue", "expense"), ("expense", "revenue"))


def _name(code):
    return NAME_OF.get(code) or code


async def _collect(db, tid, date_from, date_to):
    """Flagged journal-line amounts for one entity, grouped by
    (counterparty, account). P&L amounts use the requested window;
    balance amounts are cumulative up to date_to (a balance sheet is a
    stock at a date, not a flow over a window) — the caller picks the
    window per account type by passing date_from=None for cumulative."""
    lines_s = Scoped(db.journal_lines, tid)
    entries_s = Scoped(db.journal_entries, tid)
    q = {"status": "posted", "entry_date": {"$lte": date_to}}
    if date_from:
        q["entry_date"]["$gte"] = date_from
    lines = await lines_s.find(q).to_list(length=100000)
    entries = await entries_s.find(q).to_list(length=100000)
    entry_by_id = {e["id"]: e for e in entries}
    groups = {}
    for ln in lines:
        entry = entry_by_id.get(ln.get("entry_id"), {})
        explicit = bool(ln.get("intercompany") or entry.get("intercompany"))
        counterparty = ln.get("counterparty_tenant_id") or entry.get("counterparty_tenant_id")
        code = ln.get("account_code", "")
        if explicit:
            source = "explicit"
        elif code.startswith(LEGACY_PREFIXES):
            source = "account_flag"
            counterparty = None
        else:
            continue
        key = (counterparty, code, source)
        g = groups.setdefault(key, {
            "tenant_id": tid, "counterparty_tenant_id": counterparty,
            "code": code, "name": _name(code),
            "type": ACCOUNT_TYPE_OF.get(code),
            "source": source, "debit": 0, "credit": 0, "entries": set(),
            "entry_amounts": {},
        })
        g["debit"] += int(ln.get("debit", 0) or 0)
        g["credit"] += int(ln.get("credit", 0) or 0)
        if entry.get("number"):
            g["entries"].add(entry["number"])
            ea = g["entry_amounts"].setdefault(
                entry["number"], {"date": ln.get("entry_date", ""), "amount": 0})
            ea["amount"] += L._net_balance(code, int(ln.get("debit", 0) or 0),
                                           int(ln.get("credit", 0) or 0))
    out = []
    for g in groups.values():
        g["amount_cents"] = L._net_balance(g["code"], g["debit"], g["credit"])
        g["remaining_cents"] = g["amount_cents"]
        g["entries"] = sorted(g["entries"])
        out.append(g)
    return out


def _attribute_entries(g):
    """Split a group's entry numbers into (eliminated, remainder) by
    attributing the group's eliminated total to its entries oldest
    first. Approximation note: a group nets all its entries on one
    account, so per-entry attribution follows the net — an entry whose
    amount was partly consumed by a later reversal attributes by date."""
    eliminated_total = g["amount_cents"] - g["remaining_cents"]
    elim_entries, rem_entries = [], []
    left = eliminated_total
    for number, ea in sorted(g["entry_amounts"].items(),
                             key=lambda kv: (kv[1]["date"], kv[0])):
        amt = ea["amount"]
        if amt > 0 and left > 0:
            take = min(amt, left)
            left -= take
            elim_entries.append(number)
            if take < amt:
                rem_entries.append(number)
        elif amt > 0:
            rem_entries.append(number)
    return elim_entries, rem_entries


def _allocate(a_groups, b_groups):
    """Two-pointer allocation between two sides' account groups (both
    pre-filtered to positive remaining amounts, sorted by code).
    Returns (chunks, total) where chunks are (a_group, b_group, cents)."""
    chunks = []
    i = j = 0
    a = sorted(a_groups, key=lambda g: g["code"])
    b = sorted(b_groups, key=lambda g: g["code"])
    while i < len(a) and j < len(b):
        take = min(a[i]["remaining_cents"], b[j]["remaining_cents"])
        if take > 0:
            chunks.append((a[i], b[j], take))
            a[i]["remaining_cents"] -= take
            b[j]["remaining_cents"] -= take
        if a[i]["remaining_cents"] <= 0:
            i += 1
        if j < len(b) and b[j]["remaining_cents"] <= 0:
            j += 1
    return chunks, sum(c[2] for c in chunks)


async def compute_eliminations(db, tids, names, date_from, date_to):
    """Pair flagged intercompany amounts across the owner's entities.

    Returns {detail, unmatched, elim_by_code, totals, cash_movement_cents}.
    Pure read: no collection is written."""
    owned = set(tids)
    window_groups, cumul_groups = [], []
    for tid in tids:
        for g in await _collect(db, tid, date_from, date_to):
            if g["type"] in ("revenue", "expense"):
                window_groups.append(g)
        for g in await _collect(db, tid, None, date_to):
            if (g["type"] in ("asset", "liability", "equity")
                    or g["code"] in CASH_CODES or g["source"] == "account_flag"):
                cumul_groups.append(g)
    groups = window_groups + cumul_groups

    detail, unmatched = [], []
    elim_by_code = {}
    totals = {"revenue_cents": 0, "expense_cents": 0, "balance_cents": 0}
    cash_movement = 0

    def name_of(tid):
        return names.get(tid, tid)

    # --- classify groups that can never pair, first -------------------
    pairable = []
    for g in groups:
        cp = g["counterparty_tenant_id"]
        if g["code"] in CASH_CODES and g["source"] == "explicit" and cp in owned and cp != g["tenant_id"]:
            cash_movement += abs(g["amount_cents"])
            detail.append({
                "kind": "cash_transfer",
                "entity_a_id": g["tenant_id"], "entity_a_name": name_of(g["tenant_id"]),
                "account_a_code": g["code"], "account_a_name": g["name"],
                "entity_b_id": cp, "entity_b_name": name_of(cp),
                "account_b_code": None, "account_b_name": None,
                "amount_cents": g["amount_cents"], "eliminated_cents": 0,
                "entries_a": g["entries"], "entries_b": [],
                "note": "Cash moved between your own businesses — it stayed "
                        "inside the group, so combined cash does not change.",
            })
            g["remaining_cents"] = 0
            continue
        if g["source"] == "account_flag":
            unmatched.append(_unmatched(g, name_of,
                f"Flagged by its account code ({g['code']} looks intercompany) "
                "but the entry names no sister business, so it cannot be paired."))
            g["remaining_cents"] = 0
            continue
        if cp is None:
            unmatched.append(_unmatched(g, name_of,
                "Marked intercompany but no sister business is named on the entry."))
            g["remaining_cents"] = 0
            continue
        if cp == g["tenant_id"] or cp not in owned:
            unmatched.append(_unmatched(g, name_of,
                "Names a business outside this owner's group, so it cannot be "
                "eliminated in this view."))
            g["remaining_cents"] = 0
            continue
        if g["type"] not in ("asset", "liability", "revenue", "expense"):
            unmatched.append(_unmatched(g, name_of,
                f"{(g['type'] or 'Unknown').capitalize()} accounts are not "
                "eliminated in the consolidated view."))
            g["remaining_cents"] = 0
            continue
        if g["amount_cents"] <= 0:
            unmatched.append(_unmatched(g, name_of,
                "This side's balance is not in its normal direction "
                "(zero or reversed), so there is nothing to pair."))
            g["remaining_cents"] = 0
            continue
        pairable.append(g)

    # --- pair, per unordered entity pair, per class/direction ---------
    tids_sorted = sorted(tids)
    for x in range(len(tids_sorted)):
        for y in range(x + 1, len(tids_sorted)):
            A, B = tids_sorted[x], tids_sorted[y]
            for kind, pairs in (("balance", _BALANCE_PAIRS), ("pnl", _PNL_PAIRS)):
                for a_type, b_type in pairs:
                    a_groups = [g for g in pairable
                                if g["tenant_id"] == A and g["counterparty_tenant_id"] == B
                                and g["type"] == a_type and g["remaining_cents"] > 0]
                    b_groups = [g for g in pairable
                                if g["tenant_id"] == B and g["counterparty_tenant_id"] == A
                                and g["type"] == b_type and g["remaining_cents"] > 0]
                    if not a_groups or not b_groups:
                        continue
                    chunks, total = _allocate(a_groups, b_groups)
                    for ga, gb, cents in chunks:
                        detail.append({
                            "kind": kind,
                            "entity_a_id": A, "entity_a_name": name_of(A),
                            "account_a_code": ga["code"], "account_a_name": ga["name"],
                            "entity_b_id": B, "entity_b_name": name_of(B),
                            "account_b_code": gb["code"], "account_b_name": gb["name"],
                            "amount_cents": cents, "eliminated_cents": cents,
                            "entries_a": [], "entries_b": [],
                            "_ga": ga, "_gb": gb,
                            "note": None,
                        })
                        elim_by_code[ga["code"]] = elim_by_code.get(ga["code"], 0) + cents
                        elim_by_code[gb["code"]] = elim_by_code.get(gb["code"], 0) + cents
                        if kind == "pnl":
                            for g in (ga, gb):
                                if g["type"] == "revenue":
                                    totals["revenue_cents"] += cents
                                else:
                                    totals["expense_cents"] += cents
                        else:
                            totals["balance_cents"] += cents

    # --- attribute entry numbers now that pairing is final ------------
    for d in detail:
        ga, gb = d.pop("_ga", None), d.pop("_gb", None)
        if ga is not None:
            d["entries_a"] = _attribute_entries(ga)[0]
            d["entries_b"] = _attribute_entries(gb)[0]

    # --- whatever is left over is unmatched, with the reason ----------
    for g in pairable:
        if g["remaining_cents"] <= 0:
            continue
        cp = g["counterparty_tenant_id"]
        other_side = [o for o in pairable
                      if o["tenant_id"] == cp and o["counterparty_tenant_id"] == g["tenant_id"]]
        if not other_side:
            reason = (f"No matching intercompany entry was found in "
                      f"{name_of(cp)} — the other side of this deal is not in "
                      "its books, or is not flagged intercompany.")
        else:
            reason = (f"{name_of(cp)} recorded less on the other side than "
                      "this business did — the matched part is eliminated; "
                      "this remainder has no match.")
        u = _unmatched(g, name_of, reason)
        u["amount_cents"] = g["remaining_cents"]
        u["entries"] = _attribute_entries(g)[1]
        unmatched.append(u)

    detail.sort(key=lambda d: (d["kind"], d["entity_a_name"], d["account_a_code"] or ""))
    unmatched.sort(key=lambda u: (u["entity_name"], u["account_code"]))
    return {"detail": detail, "unmatched": unmatched,
            "elim_by_code": elim_by_code, "totals": totals,
            "cash_movement_cents": cash_movement}


def _unmatched(g, name_of, reason):
    cp = g["counterparty_tenant_id"]
    return {
        "entity_id": g["tenant_id"], "entity_name": name_of(g["tenant_id"]),
        "account_code": g["code"], "account_name": g["name"],
        "amount_cents": g["amount_cents"],
        "counterparty_tenant_id": cp,
        "counterparty_name": name_of(cp) if cp else None,
        "source": g["source"], "entries": g["entries"],
        "reason": reason,
    }
