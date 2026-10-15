"""One-click books cleanup: "fix my messy QuickBooks import" as an onboarding superpower.

POST /cleanup/scan   — detect issues across bank transactions, invoices, bills, inventory
POST /cleanup/apply  — apply selected (or all safe) fixes, fully audited

Detectors:
  uncategorized_txn   — bank transactions with no category; auto-fixable when a
                        categorization rule matches the description
  duplicate_txn       — same amount + near date + similar description; fix = mark duplicate
  missing_payee       — placeholder descriptions ("Imported"); needs human info
  negative_inventory  — products with negative on-hand; needs human review
  empty_invoice       — invoices with no lines or zero total; needs human review
  vendorless_bill     — bills with no vendor; needs human review
  stale_unmatched     — unmatched transactions older than 60 days; suggest archive

Only "safe" fixes (rule-based categorization, duplicate marking) are one-click;
everything else is flagged for the human. Nothing is ever deleted.
"""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db import db
from lib import ledger as L
from lib.audit import audit
from lib.auth import require
from lib.base import Scoped, new_id, now_iso, today_str, to_cents

router = APIRouter(prefix="/cleanup", tags=["cleanup"])

SAFE = "safe"      # one-click auto-fix
REVIEW = "review"  # human needed


def _txns(user):
    return Scoped(db.bank_transactions, user["tenant_id"])


async def _scan(user) -> list:
    tid = user["tenant_id"]
    today = today_str()[:10]
    issues = []

    # --- bank transactions ---
    txns = await _txns(user).find_list(limit=5000)
    from routers.banking import _apply_rules
    seen = {}
    for t in txns:
        amt = abs(t.get("amount_cents", 0))
        d = (t.get("txn_date") or "")[:10]
        desc = (t.get("description") or "").strip().lower()
        # duplicates
        key = (amt, d)
        if key in seen and desc and seen[key].lower()[:20] == desc[:20]:
            issues.append({"type": "duplicate_txn", "severity": "medium",
                           "fix_kind": SAFE, "txn_id": t["id"],
                           "title": f"Possible duplicate: {t.get('description')} "
                                    f"${amt/100:,.2f} on {d}",
                           "fix": "Mark as duplicate (excluded from books)"})
        else:
            seen[key] = desc
        # uncategorized
        if t.get("status") == "unmatched" and not t.get("category_code"):
            rule = await _apply_rules(user, t.get("description") or "")
            if rule:
                issues.append({"type": "uncategorized_txn", "severity": "low",
                               "fix_kind": SAFE, "txn_id": t["id"],
                               "category_code": rule["category_code"],
                               "title": f"Uncategorized: {t.get('description')} "
                                        f"${amt/100:,.2f}",
                               "fix": f"Categorize as {rule['category_code']} (rule match)"})
            else:
                issues.append({"type": "uncategorized_txn", "severity": "medium",
                               "fix_kind": REVIEW, "txn_id": t["id"],
                               "title": f"Uncategorized: {t.get('description')} "
                                        f"${amt/100:,.2f}",
                               "fix": "Pick a category manually"})
        # missing payee
        if (t.get("description") or "").strip().lower() in ("imported", "", "n/a"):
            issues.append({"type": "missing_payee", "severity": "low",
                           "fix_kind": REVIEW, "txn_id": t["id"],
                           "title": f"Missing description on ${amt/100:,.2f} ({d})",
                           "fix": "Add who this was with"})
        # stale unmatched
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(d)).days if d else 0
        except ValueError:
            age = 0
        if t.get("status") == "unmatched" and age > 60:
            issues.append({"type": "stale_unmatched", "severity": "low",
                           "fix_kind": REVIEW, "txn_id": t["id"],
                           "title": f"Unmatched for {age} days: {t.get('description')}",
                           "fix": "Categorize or archive"})

    # --- inventory ---
    prods = await Scoped(db.products, tid).find_list(limit=5000)
    for p in prods:
        if int(p.get("quantity_on_hand") or 0) < 0:
            issues.append({"type": "negative_inventory", "severity": "high",
                           "fix_kind": REVIEW, "product_id": p["id"],
                           "title": f"Negative stock: {p.get('name')} "
                                    f"({p.get('quantity_on_hand')})",
                           "fix": "Count and adjust to actual"})

    # --- invoices / bills ---
    invs = await Scoped(db.invoices, tid).find_list(limit=5000)
    for i in invs:
        if not i.get("lines") or not i.get("total_cents"):
            issues.append({"type": "empty_invoice", "severity": "medium",
                           "fix_kind": REVIEW, "invoice_id": i["id"],
                           "title": f"Invoice {i.get('number', i['id'][:8])} has no lines/total",
                           "fix": "Add lines or void it"})
    bills = await Scoped(db.bills, tid).find_list(limit=2000)
    for b in bills:
        if not (b.get("vendor") or "").strip():
            issues.append({"type": "vendorless_bill", "severity": "medium",
                           "fix_kind": REVIEW, "bill_id": b["id"],
                           "title": f"Bill {b['id'][:8]} has no vendor",
                           "fix": "Add the vendor name"})

    for n, is_ in enumerate(issues):
        is_["id"] = f"iss-{n}"
    return issues


@router.get("/scan")
async def scan(user: dict = Depends(require("ledger:read"))):
    issues = await _scan(user)
    return {"issues": issues,
            "safe_count": sum(1 for i in issues if i["fix_kind"] == SAFE),
            "review_count": sum(1 for i in issues if i["fix_kind"] == REVIEW)}


class ApplyBody(BaseModel):
    issue_ids: Optional[List[str]] = None  # None = all safe fixes
    apply_all_safe: bool = True


@router.post("/apply")
async def apply_fixes(body: ApplyBody, user: dict = Depends(require("ops:manage"))):
    from routers.banking import categorize, CategorizeBody
    issues = await _scan(user)
    targets = [i for i in issues if i["fix_kind"] == SAFE]
    if body.issue_ids is not None:
        targets = [i for i in targets if i["id"] in body.issue_ids]
    applied, failed = [], []
    for is_ in targets:
        try:
            if is_["type"] == "uncategorized_txn":
                await categorize(is_["txn_id"],
                                 CategorizeBody(category_code=is_["category_code"]), user)
            elif is_["type"] == "duplicate_txn":
                await _txns(user).update_one(
                    {"id": is_["txn_id"]}, {"$set": {"status": "duplicate",
                                                    "duplicate_of": True}})
            applied.append(is_["id"])
        except Exception as exc:
            failed.append({"id": is_["id"], "error": str(exc)[:120]})
    await audit(user, "cleanup.apply", "tenant", user["tenant_id"],
                {"applied": len(applied), "failed": len(failed)})
    return {"applied": applied, "failed": failed,
            "remaining_review": sum(1 for i in issues if i["fix_kind"] == REVIEW)}
