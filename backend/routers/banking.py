"""Banking: bank/credit-card accounts, CSV import, categorization, bank rules,
matching, duplicate detection, transfers, and reconciliation. Every categorized
transaction posts exactly ONE balanced journal entry into the shared ledger."""
import csv
import io
import hashlib
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from db import db
from lib.base import Scoped, new_id, now_iso, today_str, to_cents, cents_to_dollars
from lib.auth import require, get_current_user
from lib.coa import NAME_OF, ACCOUNT_TYPE_OF
from lib import ledger as L
from lib import posting as P
from lib.currencies import normalize as normalize_currency
from lib.dataclean.normalize import normalize_date as _dc_date
from lib.dataclean.normalize import normalize_description as _dc_desc


def _iso_date(d):
    """Cleanup layer: bank CSV dates land as ISO when they parse."""
    if not d:
        return d
    res = _dc_date(d)
    return res["normalized"] if res["ok"] else d

router = APIRouter(prefix="/banking", tags=["banking"])

# GL code pools that can back a "bank account" register
BANK_CODES = ["1020", "1021", "1022", "1023", "1010", "1050", "1060"]
CARD_CODES = ["2050", "2051", "2052"]


def accounts(user):
    return Scoped(db.bank_accounts, user["tenant_id"])


def txns(user):
    return Scoped(db.bank_transactions, user["tenant_id"])


def rules(user):
    return Scoped(db.bank_rules, user["tenant_id"])


def recons(user):
    return Scoped(db.reconciliations, user["tenant_id"])


def _dedupe(bank_account_id, txn_date, amount_cents, description):
    raw = f"{bank_account_id}|{txn_date}|{amount_cents}|{(description or '')[:48].lower().strip()}"
    return hashlib.sha1(raw.encode()).hexdigest()


# ---------------- Bank accounts ----------------
class BankAccountBody(BaseModel):
    name: str
    kind: str = "bank"            # bank | credit_card
    gl_code: Optional[str] = None
    institution: str = ""
    currency: str = "USD"         # ISO 4217; validated on create


@router.get("/accounts")
async def list_accounts(user: dict = Depends(require("ledger:read"))):
    rows = await accounts(user).find_list(sort=[("created_at", 1)])
    bals = await L._balances(db, user["tenant_id"], as_of=today_str())
    for a in rows:
        v = bals.get(a["gl_code"], {"debit": 0, "credit": 0})
        a["gl_balance"] = cents_to_dollars(L._net_balance(a["gl_code"], v["debit"], v["credit"]))
        a["unreconciled_count"] = await txns(user).count({"bank_account_id": a["id"], "reconciled": {"$ne": True}})
    return rows


@router.post("/accounts")
async def create_account(body: BankAccountBody, user: dict = Depends(require("ledger:admin"))):
    try:
        currency = normalize_currency(body.currency)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unsupported currency: {body.currency!r}")
    pool = CARD_CODES if body.kind == "credit_card" else BANK_CODES
    used = {a["gl_code"] for a in await accounts(user).find_list()}
    gl = body.gl_code
    if gl and gl not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown GL account")
    if not gl:
        gl = next((c for c in pool if c not in used), None)
        if not gl:
            raise HTTPException(status_code=409, detail=f"No free {body.kind} GL accounts left")
    doc = {
        "id": new_id(), "name": body.name, "kind": body.kind, "gl_code": gl,
        "gl_name": NAME_OF.get(gl, gl), "institution": body.institution,
        "currency": currency,
        "last_reconciled_date": None, "last_reconciled_balance_cents": 0,
        "created_at": now_iso(),
    }
    await accounts(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


# ---------------- Transactions & import ----------------
class ManualTxn(BaseModel):
    bank_account_id: str
    txn_date: str
    description: str
    amount: float                 # signed: positive = money in, negative = money out


class ImportRow(BaseModel):
    txn_date: str
    description: str
    amount: float


class ImportBody(BaseModel):
    bank_account_id: str
    rows: Optional[List[ImportRow]] = None
    csv_text: Optional[str] = None
    # Bake-off #11: saved import profiles + dedupe choices
    profile_id: Optional[str] = None
    dedupe_mode: Optional[str] = None     # "hash" (default) | "external_id"
    allow_duplicates: bool = False        # deliberate import-anyway choice


def _parse_csv(text):
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
    date_key = next((fields[k] for k in ("date", "txn_date", "transaction date", "posted date") if k in fields), None)
    desc_key = next((fields[k] for k in ("description", "memo", "name", "payee", "details") if k in fields), None)
    amt_key = next((fields[k] for k in ("amount", "value") if k in fields), None)
    debit_key = next((fields[k] for k in ("debit", "withdrawal", "withdrawals") if k in fields), None)
    credit_key = next((fields[k] for k in ("credit", "deposit", "deposits") if k in fields), None)
    for r in reader:
        d = (r.get(date_key) or "").strip() if date_key else ""
        desc = (r.get(desc_key) or "").strip() if desc_key else ""
        if amt_key:
            raw = (r.get(amt_key) or "0").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw or 0)
            except ValueError:
                amt = 0
        else:
            dv = float((r.get(debit_key) or "0").replace("$", "").replace(",", "") or 0) if debit_key else 0
            cv = float((r.get(credit_key) or "0").replace("$", "").replace(",", "") or 0) if credit_key else 0
            amt = cv - dv
        if not d and not desc:
            continue
        rows.append({"txn_date": _iso_date(d) or today_str(), "description": desc or "Imported", "amount": amt})
    return rows


async def _apply_rules(user, description):
    desc = (description or "").lower()
    for r in await rules(user).find_list():
        if r.get("version") == 2:
            continue
        if (r.get("contains") or "").lower() in desc:
            return r
    return None


# ---------------- Bank rules engine v2 (bake-off #1) ----------------
# Multi-condition rules, implemented independently in our stack (behavior
# study: Firefly III docs — rules with combined triggers, ordered groups,
# stop-processing; see bookkeeping-cleanroom-bakeoff-2026-10-30.md).
# A v2 rule doc: {version: 2, name, priority, match_mode: all|any,
# conditions: [{field, op, value, negate}], actions: {category_code,
# vendor, tags}, stop_processing, active}. Legacy single-"contains" rules
# keep working and are evaluated after v2 rules.

def _eval_condition(cond, txn):
    field = cond.get("field")
    op = cond.get("op")
    val = cond.get("value")
    try:
        if field == "description":
            d = (txn.get("description") or "").lower()
            v = str(val or "").lower()
            res = {
                "contains": v in d,
                "starts_with": d.startswith(v),
                "ends_with": d.endswith(v),
                "exact": d == v,
            }.get(op, False)
        elif field in ("amount", "signed_amount"):
            cents = int(txn.get("amount_cents") or 0)
            if field == "amount":
                cents = abs(cents)
            if op == "between":
                lo, hi = (list(val) + [0, 0])[:2] if isinstance(val, (list, tuple)) else (0, 0)
                res = to_cents(lo) <= cents <= to_cents(hi)
            else:
                target = to_cents(val or 0)
                if field == "amount":
                    target = abs(target)
                res = {
                    "gt": cents > target, "gte": cents >= target,
                    "lt": cents < target, "lte": cents <= target,
                    "exact": cents == target,
                }.get(op, False)
        elif field == "bank_account":
            res = str(txn.get("bank_account_id") or "") == str(val or "") if op == "exact" \
                else str(txn.get("bank_account_id") or "") in [str(x) for x in (val or [])]
        elif field == "category":
            code = txn.get("category_code")
            res = {"is_set": bool(code), "is_unset": not code,
                   "exact": code == val}.get(op, False)
        elif field == "tag":
            res = str(val or "") in list(txn.get("tags") or [])
        else:
            res = False
    except Exception:
        res = False
    return (not res) if cond.get("negate") else res


def rule_matches(rule, txn):
    """Pure matcher: does a v2 rule fire for this transaction dict?"""
    if not rule.get("active", True):
        return False
    conds = rule.get("conditions") or []
    if not conds:
        return False
    results = [_eval_condition(c, txn) for c in conds]
    return any(results) if rule.get("match_mode") == "any" else all(results)


def _merge_actions(into, rule):
    acts = rule.get("actions") or {}
    if acts.get("category_code") and not into.get("category_code"):
        into["category_code"] = acts["category_code"]
    if acts.get("vendor") and not into.get("vendor"):
        into["vendor"] = acts["vendor"]
    for t in acts.get("tags") or []:
        if t not in into.setdefault("tags", []):
            into["tags"].append(t)
    return into


async def _match_v2_rules(user, txn):
    """Evaluate the tenant's v2 rules in (priority, created_at) order.
    Returns (actions_dict, [matched_rule_ids]) — empty actions when none fire."""
    v2 = [r for r in await rules(user).find_list() if r.get("version") == 2]
    v2.sort(key=lambda r: (r.get("priority", 100), r.get("created_at", "")))
    actions, matched = {}, []
    for r in v2:
        if rule_matches(r, txn):
            matched.append(r["id"])
            _merge_actions(actions, r)
            if r.get("stop_processing", True):
                break
    return actions, matched


async def _insert_txn(user, bank_account_id, txn_date, description, amount, batch,
                      dedupe_mode="hash", external_ref=None, allow_duplicates=False):
    amount_cents = to_cents(amount)
    if dedupe_mode == "external_id" and external_ref:
        h = hashlib.sha1(f"ext|{bank_account_id}|{external_ref}".encode()).hexdigest()
    else:
        h = _dedupe(bank_account_id, txn_date, amount_cents, description)
    if not allow_duplicates and await txns(user).find_one({"dedupe_hash": h}):
        return None
    probe = {"description": description, "amount_cents": amount_cents,
             "bank_account_id": bank_account_id, "category_code": None, "tags": []}
    actions, matched_ids = await _match_v2_rules(user, probe)
    # Cleanup layer: rules also get a chance to match on the NORMALIZED
    # description (lowercased, punctuation collapsed, legal suffixes
    # dropped) so "ACME  SUPPLY,  INC." can match a rule written for
    # "acme supply". Raw-description matches always win.
    ndesc = _dc_desc(description)
    if not actions.get("category_code") and ndesc \
            and ndesc != (description or "").lower():
        a2, m2 = await _match_v2_rules(user, {**probe, "description": ndesc})
        if a2:
            merged = dict(a2)
            for k, v in actions.items():
                if v:
                    merged[k] = v
            actions = merged
            matched_ids = matched_ids + [m for m in m2 if m not in matched_ids]
    rule = None
    if not actions.get("category_code"):
        rule = await _apply_rules(user, description)
        if not rule and ndesc and ndesc != (description or ""):
            rule = await _apply_rules(user, ndesc)
    category = actions.get("category_code") or (rule["category_code"] if rule else None)
    doc = {
        "id": new_id(), "bank_account_id": bank_account_id, "txn_date": txn_date,
        "description": description,
        "description_normalized": ndesc or None,
        "amount_cents": amount_cents, "dedupe_hash": h,
        "status": "unmatched", "category_code": category,
        "suggested_code": category,
        "vendor": actions.get("vendor"), "tags": actions.get("tags", []),
        "external_ref": external_ref,
        "rule_ids": matched_ids,
        "entry_id": None, "matched_entry_id": None, "reconciled": False,
        "import_batch": batch, "created_at": now_iso(),
    }
    await txns(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/transactions")
async def list_txns(bank_account_id: Optional[str] = None, status: Optional[str] = None,
                    user: dict = Depends(require("ledger:read"))):
    q = {}
    if bank_account_id:
        q["bank_account_id"] = bank_account_id
    if status:
        q["status"] = status
    return await txns(user).find_list(q, sort=[("txn_date", -1), ("created_at", -1)], limit=1000)


@router.post("/transactions")
async def add_txn(body: ManualTxn, user: dict = Depends(require("ops:manage"))):
    acc = await accounts(user).find_one({"id": body.bank_account_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    doc = await _insert_txn(user, body.bank_account_id, body.txn_date, body.description, body.amount, "manual")
    if not doc:
        raise HTTPException(status_code=409, detail="Duplicate transaction detected")
    return doc


def _parse_csv_with_profile(text, profile):
    """Parse CSV using a saved profile's column map + vendor-name value maps.
    Returns rows shaped like _parse_csv rows, plus external_ref when the
    profile designates an identifier column."""
    cmap = profile.get("column_map") or {}
    vmaps = profile.get("value_maps") or {}
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}

    def pick(role, fallbacks):
        want = (cmap.get(role) or "").strip().lower()
        if want and want in fields:
            return fields[want]
        return next((fields[k] for k in fallbacks if k in fields), None)

    date_key = pick("date", ("date", "txn_date", "transaction date", "posted date"))
    desc_key = pick("description", ("description", "memo", "name", "payee", "details"))
    amt_key = pick("amount", ("amount", "value"))
    debit_key = pick("debit", ("debit", "withdrawal", "withdrawals"))
    credit_key = pick("credit", ("credit", "deposit", "deposits"))
    ext_key = None
    ext_col = (profile.get("external_id_column") or "").strip().lower()
    if ext_col and ext_col in fields:
        ext_key = fields[ext_col]
    rows = []
    for r in reader:
        d = (r.get(date_key) or "").strip() if date_key else ""
        desc = (r.get(desc_key) or "").strip() if desc_key else ""
        for raw_frag, clean in vmaps.items():
            if raw_frag and raw_frag.lower() in desc.lower():
                desc = clean
                break
        if amt_key:
            raw = (r.get(amt_key) or "0").replace("$", "").replace(",", "").strip()
            try:
                amt = float(raw or 0)
            except ValueError:
                amt = 0
        else:
            dv = float((r.get(debit_key) or "0").replace("$", "").replace(",", "") or 0) if debit_key else 0
            cv = float((r.get(credit_key) or "0").replace("$", "").replace(",", "") or 0) if credit_key else 0
            amt = cv - dv
        if not d and not desc:
            continue
        rows.append({"txn_date": _iso_date(d) or today_str(), "description": desc or "Imported",
                     "amount": amt,
                     "external_ref": (r.get(ext_key) or "").strip() if ext_key else None})
    return rows


@router.post("/import")
async def import_txns(body: ImportBody, user: dict = Depends(require("ops:manage"))):
    acc = await accounts(user).find_one({"id": body.bank_account_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    profile = None
    if body.profile_id:
        profile = await profiles(user).find_one({"id": body.profile_id})
        if not profile:
            raise HTTPException(status_code=404, detail="Import profile not found")
    dedupe_mode = body.dedupe_mode or (profile or {}).get("dedupe_mode") or "hash"
    if dedupe_mode not in ("hash", "external_id"):
        raise HTTPException(status_code=422, detail="dedupe_mode must be hash or external_id")
    if body.csv_text:
        rows = _parse_csv_with_profile(body.csv_text, profile) if profile else _parse_csv(body.csv_text)
    elif body.rows:
        rows = [r.model_dump() for r in body.rows]
    else:
        raise HTTPException(status_code=422, detail="Provide csv_text or rows")
    batch = new_id()[:8]
    imported = 0
    duplicates = 0
    for r in rows:
        doc = await _insert_txn(user, body.bank_account_id, r["txn_date"], r["description"],
                                r["amount"], batch, dedupe_mode=dedupe_mode,
                                external_ref=r.get("external_ref"),
                                allow_duplicates=body.allow_duplicates)
        if doc:
            imported += 1
        else:
            duplicates += 1
    return {"imported": imported, "duplicates": duplicates, "total": len(rows),
            "batch": batch, "dedupe_mode": dedupe_mode,
            "profile_id": body.profile_id}


# ---------------- Categorize (post to GL) ----------------
class CategorizeBody(BaseModel):
    category_code: str
    memo: Optional[str] = None


@router.post("/transactions/{txn_id}/categorize")
async def categorize(txn_id: str, body: CategorizeBody, user: dict = Depends(require("ops:manage"))):
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if t["status"] in ("cleared", "matched"):
        raise HTTPException(status_code=409, detail="Transaction already posted or matched")
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account")
    acc = await accounts(user).find_one({"id": t["bank_account_id"]})
    bank_gl = acc["gl_code"]
    amt = abs(t["amount_cents"])
    if amt == 0:
        raise HTTPException(status_code=422, detail="Zero-amount transaction")
    if t["amount_cents"] > 0:   # money in
        jl = [P.dr(bank_gl, amt, "Deposit"), P.cr(body.category_code, amt, t["description"])]
    else:                        # money out
        jl = [P.dr(body.category_code, amt, t["description"]), P.cr(bank_gl, amt, "Payment")]
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": t["txn_date"], "source": "bank_txn", "source_ref": txn_id,
        "memo": body.memo or f"Bank: {t['description']}", "lines": jl,
    }, status="posted", idempotency_key=f"bank:{txn_id}")
    await txns(user).update_one({"id": txn_id}, {"$set": {
        "status": "cleared", "category_code": body.category_code, "entry_id": entry["id"],
        "entry_number": entry["number"],
    }})
    return {"status": "cleared", "entry_number": entry["number"]}


@router.post("/transactions/{txn_id}/match")
async def match(txn_id: str, entry_id: str, user: dict = Depends(require("ops:manage"))):
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    e = await Scoped(db.journal_entries, user["tenant_id"]).find_one({"id": entry_id})
    if not e:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    await txns(user).update_one({"id": txn_id}, {"$set": {
        "status": "matched", "matched_entry_id": entry_id, "entry_number": e.get("number"),
    }})
    return {"status": "matched", "entry_number": e.get("number")}


@router.delete("/transactions/{txn_id}")
async def delete_txn(txn_id: str, user: dict = Depends(require("ops:manage"))):
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if t["status"] == "cleared":
        raise HTTPException(status_code=409, detail="Reverse the posted entry before deleting")
    await txns(user).c.delete_one({"tenant_id": user["tenant_id"], "id": txn_id})
    return {"deleted": True}


# ---------------- Bank rules ----------------
class RuleBody(BaseModel):
    contains: str
    category_code: str


@router.get("/rules")
async def list_rules(user: dict = Depends(require("ledger:read"))):
    return await rules(user).find_list(sort=[("created_at", -1)])


@router.post("/rules")
async def create_rule(body: RuleBody, user: dict = Depends(require("ops:manage"))):
    if body.category_code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account")
    doc = {"id": new_id(), "contains": body.contains, "category_code": body.category_code,
           "category_name": NAME_OF.get(body.category_code), "created_at": now_iso()}
    await rules(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, user: dict = Depends(require("ops:manage"))):
    await rules(user).c.delete_one({"tenant_id": user["tenant_id"], "id": rule_id})
    return {"deleted": True}


class RuleV2Body(BaseModel):
    name: str
    conditions: List[dict]
    match_mode: str = "all"               # all | any
    actions: dict = {}                    # {category_code, vendor, tags}
    priority: int = 100
    stop_processing: bool = True
    active: bool = True


def _validate_v2(body: RuleV2Body):
    if body.match_mode not in ("all", "any"):
        raise HTTPException(status_code=422, detail="match_mode must be all or any")
    if not body.conditions:
        raise HTTPException(status_code=422, detail="A rule needs at least one condition")
    fields = {"description", "amount", "signed_amount", "bank_account", "category", "tag"}
    for c in body.conditions:
        if c.get("field") not in fields:
            raise HTTPException(status_code=422, detail=f"Unknown condition field: {c.get('field')}")
    code = (body.actions or {}).get("category_code")
    if code and code not in ACCOUNT_TYPE_OF:
        raise HTTPException(status_code=422, detail="Unknown category account in actions")


@router.post("/rules/v2")
async def create_rule_v2(body: RuleV2Body, user: dict = Depends(require("ops:manage"))):
    _validate_v2(body)
    doc = {"id": new_id(), "version": 2, **body.model_dump(), "created_at": now_iso()}
    await rules(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.patch("/rules/{rule_id}")
async def update_rule_v2(rule_id: str, body: RuleV2Body, user: dict = Depends(require("ops:manage"))):
    existing = await rules(user).find_one({"id": rule_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    _validate_v2(body)
    await rules(user).update_one({"id": rule_id},
                                 {"$set": {**body.model_dump(), "version": 2}})
    return {"id": rule_id, "version": 2, **body.model_dump()}


class RulePreviewBody(BaseModel):
    rule: Optional[dict] = None           # inline v2 rule draft
    rule_id: Optional[str] = None         # or an existing rule
    samples: List[dict]                   # txn-shaped dicts to test against


@router.post("/rules/preview")
async def preview_rule(body: RulePreviewBody, user: dict = Depends(require("ledger:read"))):
    """Preview matches before saving: run one rule against sample transactions."""
    rule = body.rule
    if body.rule_id:
        rule = await rules(user).find_one({"id": body.rule_id})
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
    if not rule:
        raise HTTPException(status_code=422, detail="Provide rule or rule_id")
    out = []
    for sample in body.samples:
        hit = rule_matches(rule, sample) if rule.get("version") == 2 else \
            (str(rule.get("contains") or "").lower() in (sample.get("description") or "").lower())
        out.append({"sample": sample, "matches": bool(hit),
                    "actions": (rule.get("actions") or {"category_code": rule.get("category_code")}) if hit else {}})
    return {"results": out, "matched": sum(1 for r in out if r["matches"])}


class ApplyRulesBody(BaseModel):
    rule_id: Optional[str] = None         # None = all v2 rules


@router.post("/rules/apply-existing")
async def apply_rules_existing(body: ApplyRulesBody, user: dict = Depends(require("ops:manage"))):
    """Re-run rules over unmatched transactions (suggestions only — nothing posts).
    Sets category suggestion, vendor and tags where a rule fires."""
    only = None
    if body.rule_id:
        only = await rules(user).find_one({"id": body.rule_id})
        if not only:
            raise HTTPException(status_code=404, detail="Rule not found")
    updated = 0
    pending = await txns(user).find_list({"status": "unmatched"}, limit=5000)
    for t in pending:
        if only is not None:
            if only.get("version") == 2:
                if not rule_matches(only, t):
                    continue
                actions = dict(only.get("actions") or {})
            else:
                if (only.get("contains") or "").lower() not in (t.get("description") or "").lower():
                    continue
                actions = {"category_code": only.get("category_code")}
        else:
            actions, _ids = await _match_v2_rules(user, t)
            if not actions:
                legacy = await _apply_rules(user, t.get("description") or "")
                if legacy:
                    actions = {"category_code": legacy["category_code"]}
        if not actions:
            continue
        upd = {}
        if actions.get("category_code"):
            upd["category_code"] = actions["category_code"]
            upd["suggested_code"] = actions["category_code"]
        if actions.get("vendor"):
            upd["vendor"] = actions["vendor"]
        if actions.get("tags"):
            upd["tags"] = sorted(set(list(t.get("tags") or []) + list(actions["tags"])))
        if upd:
            await txns(user).update_one({"id": t["id"]}, {"$set": upd})
            updated += 1
    return {"examined": len(pending), "updated": updated}


class TagsBody(BaseModel):
    tags: List[str]


@router.patch("/transactions/{txn_id}/tags")
async def set_txn_tags(txn_id: str, body: TagsBody, user: dict = Depends(require("ops:manage"))):
    """Job tags on a bank transaction (bake-off #7: category says what kind of
    cost, tag says which job)."""
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    tags = sorted({str(x).strip() for x in body.tags if str(x).strip()})
    await txns(user).update_one({"id": txn_id}, {"$set": {"tags": tags}})
    return {"id": txn_id, "tags": tags}


# ---------------- Transfers ----------------
class TransferBody(BaseModel):
    from_account_id: str
    to_account_id: str
    amount: float
    txn_date: Optional[str] = None
    memo: Optional[str] = None


@router.post("/transfer")
async def transfer(body: TransferBody, user: dict = Depends(require("ops:manage"))):
    fa = await accounts(user).find_one({"id": body.from_account_id})
    ta = await accounts(user).find_one({"id": body.to_account_id})
    if not fa or not ta:
        raise HTTPException(status_code=404, detail="Account not found")
    amt = to_cents(body.amount)
    if amt <= 0:
        raise HTTPException(status_code=422, detail="Amount must be positive")
    entry = await L.post_journal_entry(db, user["tenant_id"], user, {
        "entry_date": body.txn_date or today_str(), "source": "transfer", "source_ref": None,
        "memo": body.memo or f"Transfer {fa['name']} → {ta['name']}",
        "lines": [P.dr(ta["gl_code"], amt, "Transfer in"), P.cr(fa["gl_code"], amt, "Transfer out")],
    }, status="posted")
    return {"entry_number": entry["number"]}


# ---------------- Reconciliation ----------------
class ReconBody(BaseModel):
    bank_account_id: str
    statement_date: str
    statement_ending_balance: float
    txn_ids: List[str]


@router.get("/reconciliations")
async def list_recons(bank_account_id: Optional[str] = None, user: dict = Depends(require("ledger:read"))):
    q = {"bank_account_id": bank_account_id} if bank_account_id else {}
    return await recons(user).find_list(q, sort=[("created_at", -1)], limit=200)


@router.post("/reconciliations")
async def reconcile(body: ReconBody, user: dict = Depends(require("ledger:admin"))):
    acc = await accounts(user).find_one({"id": body.bank_account_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    selected = await txns(user).find_list({"id": {"$in": body.txn_ids}, "bank_account_id": body.bank_account_id})
    cleared_sum = sum(t["amount_cents"] for t in selected)
    opening = acc.get("last_reconciled_balance_cents", 0)
    reconciled_balance = opening + cleared_sum
    ending = to_cents(body.statement_ending_balance)
    difference = ending - reconciled_balance
    status = "completed" if difference == 0 else "in_progress"
    doc = {
        "id": new_id(), "bank_account_id": body.bank_account_id, "bank_account_name": acc["name"],
        "statement_date": body.statement_date, "statement_ending_cents": ending,
        "opening_cents": opening, "cleared_cents": cleared_sum,
        "reconciled_balance_cents": reconciled_balance, "difference_cents": difference,
        "txn_count": len(selected), "status": status, "created_by": user["email"], "created_at": now_iso(),
    }
    await recons(user).insert_one(doc)
    if status == "completed":
        await txns(user).c.update_many(
            {"tenant_id": user["tenant_id"], "id": {"$in": body.txn_ids}},
            {"$set": {"reconciled": True}})
        await accounts(user).update_one({"id": body.bank_account_id}, {"$set": {
            "last_reconciled_date": body.statement_date,
            "last_reconciled_balance_cents": reconciled_balance,
        }})
    doc.pop("_id", None)
    return doc


# ---------------- Split transactions (bake-off #6) ----------------
class SplitPart(BaseModel):
    amount: float                    # dollars; same sign as the original
    category_code: Optional[str] = None
    memo: Optional[str] = None
    tags: List[str] = []


class SplitBody(BaseModel):
    parts: List[SplitPart]


@router.post("/transactions/{txn_id}/split")
async def split_txn(txn_id: str, body: SplitBody, user: dict = Depends(require("ops:manage"))):
    """Break one bank line into coded parts summing EXACTLY to the original.
    The original is parked as status "split" (never posts); each part is a
    normal transaction that categorizes/posts through the existing flow."""
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if t["status"] in ("cleared", "matched", "split"):
        raise HTTPException(status_code=409, detail=f"Transaction is {t['status']} and cannot be split")
    if len(body.parts) < 2:
        raise HTTPException(status_code=422, detail="A split needs at least two parts")
    total = 0
    for part in body.parts:
        cents = to_cents(part.amount)
        if cents == 0:
            raise HTTPException(status_code=422, detail="Split parts cannot be zero")
        if (cents > 0) != (t["amount_cents"] > 0):
            raise HTTPException(status_code=422, detail="Split parts must keep the original's direction")
        if part.category_code and part.category_code not in ACCOUNT_TYPE_OF:
            raise HTTPException(status_code=422, detail="Unknown category account")
        total += cents
    if total != t["amount_cents"]:
        raise HTTPException(status_code=422,
                            detail=f"Parts sum to {total} cents, original is {t['amount_cents']} cents")
    parts = []
    for i, part in enumerate(body.parts):
        cents = to_cents(part.amount)
        doc = {
            "id": new_id(), "bank_account_id": t["bank_account_id"],
            "txn_date": t["txn_date"],
            "description": part.memo or t["description"],
            "amount_cents": cents,
            "dedupe_hash": _dedupe(t["id"], str(i), cents, part.memo or t["description"]),
            "status": "unmatched",
            "category_code": part.category_code, "suggested_code": part.category_code,
            "vendor": t.get("vendor"), "tags": sorted({x.strip() for x in part.tags if x.strip()}),
            "split_parent_id": txn_id,
            "entry_id": None, "matched_entry_id": None, "reconciled": False,
            "import_batch": t.get("import_batch"), "created_at": now_iso(),
        }
        await txns(user).insert_one(doc)
        doc.pop("_id", None)
        parts.append(doc)
    await txns(user).update_one({"id": txn_id}, {"$set": {"status": "split"}})
    return {"original_id": txn_id, "parts": parts}


@router.post("/transactions/{txn_id}/unsplit")
async def unsplit_txn(txn_id: str, user: dict = Depends(require("ops:manage"))):
    """Reverse a split (only while no part has posted)."""
    t = await txns(user).find_one({"id": txn_id})
    if not t:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if t["status"] != "split":
        raise HTTPException(status_code=409, detail="Transaction is not split")
    parts = await txns(user).find_list({"split_parent_id": txn_id})
    if any(p["status"] in ("cleared", "matched") for p in parts):
        raise HTTPException(status_code=409, detail="A split part has already posted; reverse it first")
    for p in parts:
        await txns(user).c.delete_one({"tenant_id": user["tenant_id"], "id": p["id"]})
    await txns(user).update_one({"id": txn_id}, {"$set": {"status": "unmatched"}})
    return {"id": txn_id, "status": "unmatched", "parts_removed": len(parts)}


# ---------------- Saved import profiles (bake-off #11) ----------------
def profiles(user):
    return Scoped(db.import_profiles, user["tenant_id"])


class ImportProfileBody(BaseModel):
    name: str
    column_map: dict = {}          # role -> csv header (date/description/amount/debit/credit)
    value_maps: dict = {}          # raw description fragment -> clean name
    dedupe_mode: str = "hash"      # hash | external_id
    external_id_column: Optional[str] = None


@router.get("/import-profiles")
async def list_import_profiles(user: dict = Depends(require("ledger:read"))):
    return await profiles(user).find_list(sort=[("name", 1)], limit=200)


@router.post("/import-profiles")
async def create_import_profile(body: ImportProfileBody, user: dict = Depends(require("ops:manage"))):
    if body.dedupe_mode not in ("hash", "external_id"):
        raise HTTPException(status_code=422, detail="dedupe_mode must be hash or external_id")
    if body.dedupe_mode == "external_id" and not body.external_id_column:
        raise HTTPException(status_code=422, detail="external_id_column is required for external_id dedupe")
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await profiles(user).insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.delete("/import-profiles/{profile_id}")
async def delete_import_profile(profile_id: str, user: dict = Depends(require("ops:manage"))):
    await profiles(user).c.delete_one({"tenant_id": user["tenant_id"], "id": profile_id})
    return {"deleted": True}
