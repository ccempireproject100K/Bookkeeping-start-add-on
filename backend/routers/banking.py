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
        rows.append({"txn_date": d or today_str(), "description": desc or "Imported", "amount": amt})
    return rows


async def _apply_rules(user, description):
    desc = (description or "").lower()
    for r in await rules(user).find_list():
        if r["contains"].lower() in desc:
            return r
    return None


async def _insert_txn(user, bank_account_id, txn_date, description, amount, batch):
    amount_cents = to_cents(amount)
    h = _dedupe(bank_account_id, txn_date, amount_cents, description)
    if await txns(user).find_one({"dedupe_hash": h}):
        return None
    rule = await _apply_rules(user, description)
    doc = {
        "id": new_id(), "bank_account_id": bank_account_id, "txn_date": txn_date,
        "description": description, "amount_cents": amount_cents, "dedupe_hash": h,
        "status": "unmatched", "category_code": rule["category_code"] if rule else None,
        "suggested_code": rule["category_code"] if rule else None,
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


@router.post("/import")
async def import_txns(body: ImportBody, user: dict = Depends(require("ops:manage"))):
    acc = await accounts(user).find_one({"id": body.bank_account_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    if body.csv_text:
        rows = _parse_csv(body.csv_text)
    elif body.rows:
        rows = [r.model_dump() for r in body.rows]
    else:
        raise HTTPException(status_code=422, detail="Provide csv_text or rows")
    batch = new_id()[:8]
    imported = 0
    duplicates = 0
    for r in rows:
        doc = await _insert_txn(user, body.bank_account_id, r["txn_date"], r["description"], r["amount"], batch)
        if doc:
            imported += 1
        else:
            duplicates += 1
    return {"imported": imported, "duplicates": duplicates, "total": len(rows), "batch": batch}


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
