"""Sales-tax automation v1 tests (parity audit §D2).

Covers: stacked-component calculation + snapshots, exempt customers,
refund / credit-note tax reversals, liability keyed on issue_date
(incl. POS + legacy rows), rate history (past never rewritten), the
return-preparation worksheet + record-payment flow, the GL 2100
reconciliation, the nexus monitor, and CPA-package inclusion.

No live Mongo needed — an in-memory fake db (operator-aware, with a
minimal aggregate for the ledger/CPA paths) is monkeypatched into
every module that holds a `db` reference, following the conventions of
tests/test_integration_sync.py.

Run from the backend directory with:
    python -m pytest tests/test_sales_tax_v1.py -n 0
"""
import asyncio
import copy
import io
import os
import sys
import zipfile
from datetime import datetime

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_sales_tax_v1")
os.environ.setdefault("JWT_SECRET", "sales-tax-v1-test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

import lib.audit as audit_mod  # noqa: E402
import lib.auth as auth_mod  # noqa: E402
import lib.idempotency as idem  # noqa: E402
import lib.integration_auth as iauth  # noqa: E402
from lib.base import new_id, now_iso, today_str  # noqa: E402
from lib.coa import ACCOUNT_TYPE_OF  # noqa: E402
from lib import sales_tax_engine as STE  # noqa: E402
import routers.cpa as cpa_r  # noqa: E402
import routers.customers as customers_r  # noqa: E402
import routers.invoices as invoices_r  # noqa: E402
import routers.pos as pos_r  # noqa: E402
import routers.products as products_r  # noqa: E402
import routers.sales_tax as st_r  # noqa: E402

T1 = "tenant-1"
OWNER = {"id": "u-owner", "email": "owner@shop.test", "tenant_id": T1,
         "role": "owner"}
NOW = datetime.now()
YEAR, MONTH = NOW.year, NOW.month
PERIOD = f"{YEAR:04d}-{MONTH:02d}"


def run(coro):
    return asyncio.run(coro)


# ---------------- fake in-memory db (operator-aware) ----------------

def _match_value(actual, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, val in cond.items():
            if op == "$gte" and not (actual is not None and actual >= val):
                return False
            if op == "$lte" and not (actual is not None and actual <= val):
                return False
            if op == "$gt" and not (actual is not None and actual > val):
                return False
            if op == "$lt" and not (actual is not None and actual < val):
                return False
            if op == "$ne" and actual == val:
                return False
            if op == "$in" and actual not in val:
                return False
            if op == "$exists" and (actual is not None) != bool(val):
                return False
        return True
    return actual == cond


def _matches(doc, q):
    for k, v in (q or {}).items():
        if k == "$or":
            if not any(_matches(doc, c) for c in v):
                return False
        elif k == "$and":
            if not all(_matches(doc, c) for c in v):
                return False
        elif not _match_value(doc.get(k), v):
            return False
    return True


def _project(doc, projection):
    if not projection:
        return doc
    inclusion = any(v == 1 for k, v in projection.items() if k != "_id")
    if inclusion:
        out = {k: doc[k] for k, v in projection.items() if v == 1 and k in doc}
        if projection.get("_id", 1) and "_id" in doc:
            out["_id"] = doc["_id"]
        return out
    out = dict(doc)
    for k, v in projection.items():
        if v == 0:
            out.pop(k, None)
    return out


class _Result:
    def __init__(self, matched=0, modified=0):
        self.matched_count = matched
        self.modified_count = modified
        self.inserted_id = None


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        specs = key if isinstance(key, list) else [(key, direction)]
        for k, d in reversed(specs):
            self._docs.sort(key=lambda doc: (doc.get(k) is None, str(doc.get(k))),
                            reverse=(d < 0))
        return self

    def limit(self, n):
        if n:
            self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return copy.deepcopy(docs)

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield copy.deepcopy(d)
        return _gen()


class FakeCollection:
    def __init__(self, unique_keys=()):
        self.docs = []
        self.unique_keys = [tuple(u) for u in unique_keys]
        self._seq = 0

    def _check_unique(self, doc, ignore=None):
        for keys in self.unique_keys:
            if any(doc.get(k) is None for k in keys):
                continue
            for existing in self.docs:
                if existing is ignore:
                    continue
                if all(existing.get(k) == doc.get(k) for k in keys):
                    raise DuplicateKeyError(f"unique index violation on {keys}")

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        self._check_unique(doc)
        self._seq += 1
        doc.setdefault("_id", f"oid-{self._seq}-{id(self)}")
        self.docs.append(doc)
        res = _Result(0, 0)
        res.inserted_id = doc["_id"]
        return res

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return _project(copy.deepcopy(d), projection)
        return None

    def find(self, q=None, projection=None):
        return FakeCursor([_project(copy.deepcopy(d), projection)
                           for d in self.docs if _matches(d, q)])

    def _apply(self, doc, update):
        for op, fields in update.items():
            if op == "$set":
                for k, v in fields.items():
                    doc[k] = copy.deepcopy(v)
            elif op == "$inc":
                for k, v in fields.items():
                    doc[k] = doc.get(k, 0) + v
            elif op == "$push":
                for k, v in fields.items():
                    doc.setdefault(k, []).append(copy.deepcopy(v))
            elif op == "$unset":
                for k in fields:
                    doc.pop(k, None)
            else:
                doc.update(copy.deepcopy(update))
                return

    async def update_one(self, q, update):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return _Result(1, 1)
        return _Result(0, 0)

    async def update_many(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                n += 1
        return _Result(n, n)

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=False, **kw):
        for d in self.docs:
            if _matches(d, q):
                self._apply(d, update)
                return copy.deepcopy(d)
        if upsert:
            doc = dict(q or {})
            self._apply(doc, update)
            self._seq += 1
            doc.setdefault("_id", f"oid-{self._seq}-{id(self)}")
            self.docs.append(doc)
            return copy.deepcopy(doc)
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))

    async def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                del self.docs[i]
                return _Result(1, 1)
        return _Result(0, 0)

    def aggregate(self, pipeline):
        docs = [copy.deepcopy(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = stage["$group"]
                id_expr = spec.get("_id")
                groups, order = {}, []
                for d in docs:
                    key = (d.get(id_expr[1:]) if isinstance(id_expr, str)
                           and id_expr.startswith("$") else id_expr)
                    if key not in groups:
                        groups[key] = {"_id": key}
                        for f in spec:
                            if f != "_id":
                                groups[key][f] = 0
                        order.append(key)
                    for f, acc in spec.items():
                        if f == "_id" or not isinstance(acc, dict):
                            continue
                        expr = acc.get("$sum")
                        if isinstance(expr, str) and expr.startswith("$"):
                            groups[key][f] += d.get(expr[1:], 0) or 0
                        elif isinstance(expr, (int, float)):
                            groups[key][f] += expr
                docs = [groups[k] for k in order]
            elif "$sort" in stage:
                for k, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda d: (d.get(k) is None, str(d.get(k))),
                              reverse=(direction < 0))
            elif "$limit" in stage:
                docs = docs[: stage["$limit"]]
            elif "$project" in stage:
                docs = [_project(d, stage["$project"]) for d in docs]
        return FakeCursor(docs)


class FakeDB:
    def __init__(self):
        self._colls = {
            "idempotency_records": FakeCollection(
                unique_keys=[("tenant_id", "key")]),
        }

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    for mod in (st_r, invoices_r, customers_r, pos_r, products_r, cpa_r,
                idem, iauth, auth_mod, audit_mod):
        monkeypatch.setattr(mod, "db", fake)
    fake.tenants.docs.append({
        "id": T1, "name": "Test Shop", "currency": "USD",
        "settings": {"sales_tax_rate": 0.0725}, "niche": None})
    fake.users.docs.append({"id": OWNER["id"], "tenant_id": T1,
                            "role": "owner", "email": OWNER["email"]})
    for code, typ in ACCOUNT_TYPE_OF.items():
        fake.accounts.docs.append({
            "id": f"acct-{code}", "tenant_id": T1, "code": code,
            "name": f"Account {code}", "type": typ, "active": True,
            "system": False})
    return fake


# ---------------- helpers ----------------

async def _tx_jur():
    return await st_r.create_jurisdiction(st_r.JurisdictionIn(
        name="Austin, TX", state="TX", components=[
            st_r.ComponentIn(level="state", name="Texas", rate_pct=6.25),
            st_r.ComponentIn(level="county", name="Travis County", rate_pct=1.0),
            st_r.ComponentIn(level="city", name="Austin", rate_pct=1.0)]),
        user=OWNER)


async def _issue(**kw):
    return await invoices_r.issue_invoice(
        invoices_r.InvoiceBody(**kw), user=OWNER, request=None)


def _gl(fake, code, side):
    return sum(int(l[side]) for l in fake.journal_lines.docs
               if l.get("account_code") == code and l.get("status") == "posted")


def _raw_invoice(fake, state, subtotal, facilitated=0, when=None):
    doc = {"id": new_id(), "tenant_id": T1, "status": "issued",
           "issue_date": when or today_str(), "created_at": now_iso(),
           "subtotal_cents": subtotal, "tax_cents": 0,
           "total_cents": subtotal, "paid_cents": 0,
           "tax_jurisdiction_id": None,
           "tax_snapshot": {"engine": "manual-plus", "state": state,
                            "taxable_sales_cents": subtotal - facilitated,
                            "exempt_sales_cents": 0,
                            "nontaxable_sales_cents": 0,
                            "facilitated_taxable_cents": facilitated,
                            "tax_cents": 0}}
    fake.invoices.docs.append(doc)
    return doc


# ---------------- engine units ----------------

def test_engine_components_round_per_component():
    comps = [{"level": "state", "name": "S", "rate_pct": 6.25},
             {"level": "city", "name": "C", "rate_pct": 1.0}]
    out = STE.calculate_tax(
        lines=[{"amount_cents": 333, "tax_category": "general_goods"}],
        components=comps, state="TX", customer_exempt=False)
    # 333 * 6.25% = 20.8125 -> 21 ; 333 * 1% = 3.33 -> 3
    assert out["tax_cents"] == 24
    assert out["lines"][0]["components"][0]["tax_cents"] == 21
    assert out["lines"][0]["components"][1]["tax_cents"] == 3


def test_engine_matrix_services_states():
    comps = [{"level": "state", "name": "S", "rate_pct": 5.0}]
    labor = [{"amount_cents": 10000, "tax_category": "labor_service"}]
    calc = lambda lines, state: STE.calculate_tax(
        lines=lines, components=comps, state=state)["tax_cents"]
    assert calc(labor, "TX") == 0
    assert calc(labor, "HI") == 500
    food = [{"amount_cents": 10000, "tax_category": "food_grocery"}]
    assert calc(food, "TX") == 0
    s, e, key = STE.period_bounds(2026, quarter=1)
    assert (s, e, key) == ("2026-01-01", "2026-04-01", "2026-Q1")


# ---------------- calculation + snapshot ----------------

def test_stacked_jurisdiction_invoice(env):
    run(_stacked(env))


async def _stacked(env):
    jur = await _tx_jur()
    assert jur["rate_pct"] == 8.25
    assert len(jur["components"]) == 3
    assert len(jur["rate_history"]) == 1
    cust = await customers_r.create_customer(customers_r.CustomerBody(
        name="Acme",
        ship_to=customers_r.StructuredAddress(state="TX", city="Austin",
                                              zip5="78701")),
        user=OWNER, request=None)
    inv = await _issue(
        client_name="Acme", customer_id=cust["id"],
        tax_jurisdiction_id=jur["id"], tax=99.99,
        lines=[invoices_r.InvLine(name="Widget", revenue_code="4100",
                                  amount=100.0, tax_category="general_goods"),
               invoices_r.InvLine(name="Install", revenue_code="4020",
                                  amount=50.0, tax_category="labor_service")])
    # goods: 10000 * (6.25 + 1.0 + 1.0)% = 825 ; labor is not taxable in TX
    assert inv["tax_cents"] == 825
    snap = inv["tax_snapshot"]
    assert snap["engine"] == "manual-plus"
    assert snap["taxable_sales_cents"] == 10000
    assert snap["nontaxable_sales_cents"] == 5000
    assert snap["combined_rate_pct"] == 8.25
    assert snap["client_estimate_cents"] == 9999  # frontend estimate demoted
    line0 = snap["lines"][0]
    assert [c["tax_cents"] for c in line0["components"]] == [625, 100, 100]
    assert line0["taxability_reason"] == "matrix"
    assert snap["lines"][1]["tax_cents"] == 0
    # GL accrual: 2100 credited exactly the computed tax
    assert _gl(env, "2100", "credit") == 825
    # liability for the month sees it, keyed on issue_date
    liab = await st_r.liability(year=YEAR, month=MONTH, user=OWNER)
    bucket = liab["jurisdictions"][0]
    assert bucket["taxable_sales_cents"] == 10000
    assert bucket["nontaxable_sales_cents"] == 5000
    assert bucket["tax_collected_cents"] == 825
    assert bucket["net_tax_cents"] == 825


def test_ship_to_auto_resolution(env):
    run(_auto(env))


async def _auto(env):
    ok = await st_r.create_jurisdiction(st_r.JurisdictionIn(
        name="Oklahoma", state="OK", rate_pct=4.5), user=OWNER)
    assert ok["components"][0]["rate_pct"] == 4.5  # flat rate synthesized
    cust = await customers_r.create_customer(customers_r.CustomerBody(
        name="Okie",
        ship_to=customers_r.StructuredAddress(state="OK", zip5="73102")),
        user=OWNER, request=None)
    inv = await _issue(client_name="Okie", customer_id=cust["id"],
                       lines=[invoices_r.InvLine(
                           name="Goods", revenue_code="4100", amount=200.0)])
    assert inv["tax_cents"] == 900
    assert inv["tax_snapshot"]["jurisdiction_id"] == ok["id"]
    assert inv["tax_snapshot"]["state"] == "OK"


def test_exempt_sale(env):
    run(_exempt(env))


async def _exempt(env):
    jur = await _tx_jur()
    cust = await customers_r.create_customer(customers_r.CustomerBody(
        name="Church", tax_exempt=True,
        exemption=customers_r.ExemptionInfo(
            type="nonprofit", certificate_id="CERT-77", state="TX")),
        user=OWNER, request=None)
    inv = await _issue(client_name="Church", customer_id=cust["id"],
                       tax_jurisdiction_id=jur["id"],
                       lines=[invoices_r.InvLine(
                           name="Goods", revenue_code="4100", amount=300.0)])
    assert inv["tax_cents"] == 0
    snap = inv["tax_snapshot"]
    assert snap["exempt_sales_cents"] == 30000
    assert snap["exemption"]["certificate_id"] == "CERT-77"
    assert _gl(env, "2100", "credit") == 0
    ws = await st_r.build_worksheet(T1, "TX", *STE.period_bounds(YEAR, month=MONTH)[:2],
                                    STE.period_bounds(YEAR, month=MONTH)[2])
    assert ws["lines"]["less_exempt_sales_cents"] == 30000
    assert ws["lines"]["taxable_sales_cents"] == 0
    assert ws["lines"]["amount_due_cents"] == 0


def test_manual_fallback_without_jurisdiction(env):
    run(_manual(env))


async def _manual(env):
    inv = await _issue(client_name="Walk-in", tax=12.34,
                       lines=[invoices_r.InvLine(
                           name="Goods", revenue_code="4100", amount=100.0)])
    assert inv["tax_cents"] == 1234
    assert inv["tax_snapshot"]["engine"] == "manual"
    assert inv["tax_snapshot"]["taxable_sales_cents"] == 10000


# ---------------- reversals ----------------

def test_refund_reverses_tax(env):
    run(_refund(env))


async def _refund(env):
    jur = await _tx_jur()
    inv = await _issue(client_name="Acme", tax_jurisdiction_id=jur["id"],
                       lines=[invoices_r.InvLine(
                           name="Goods", revenue_code="4100", amount=100.0)])
    assert inv["tax_cents"] == 825
    await invoices_r.pay_invoice(inv["id"], invoices_r.PayBody(
        payment_method="bank"), user=OWNER, request=None)
    # reversal beyond the tax actually charged is refused
    with pytest.raises(HTTPException) as exc:
        await invoices_r.refund_invoice_payment(
            inv["id"], invoices_r.InvoiceRefundBody(
                amount=50.0, payment_method="bank", tax_reversal=9.00),
            user=OWNER, request=None)
    assert exc.value.status_code == 422
    out = await invoices_r.refund_invoice_payment(
        inv["id"], invoices_r.InvoiceRefundBody(
            amount=108.25, payment_method="bank", tax_reversal=8.25),
        user=OWNER, request=None)
    assert out["tax_reversal_cents"] == 825
    doc = await env.invoices.find_one({"id": inv["id"]})
    assert doc["tax_reversed_cents"] == 825
    assert doc["tax_adjustments"][0]["kind"] == "payment_refund"
    assert _gl(env, "2100", "debit") == 825
    liab = await st_r.liability(year=YEAR, month=MONTH, user=OWNER)
    assert liab["jurisdictions"][0]["tax_reversed_cents"] == 825
    assert liab["jurisdictions"][0]["net_tax_cents"] == 0


def test_credit_note_reverses_tax(env):
    run(_credit(env))


async def _credit(env):
    jur = await _tx_jur()
    inv = await _issue(client_name="Acme", tax_jurisdiction_id=jur["id"],
                       lines=[invoices_r.InvLine(
                           name="Goods", revenue_code="4100", amount=200.0)])
    assert inv["tax_cents"] == 1650
    note = await invoices_r.issue_credit_note(invoices_r.CreditNoteBody(
        client_name="Acme", amount=200.0, revenue_code="4100",
        invoice_id=inv["id"], tax_jurisdiction_id=jur["id"],
        sales_tax_reversal=16.50), user=OWNER)
    assert note["tax_reversal_cents"] == 1650
    assert note["amount_cents"] == 20000
    assert note["remaining_cents"] == 21650  # face + reversed tax owed back
    assert _gl(env, "2100", "debit") == 1650
    assert _gl(env, "2210", "credit") == 21650
    ws = await st_r.worksheet(state="TX", year=YEAR, month=MONTH,
                              quarter=None, user=OWNER)
    assert ws["lines"]["less_returns_sales_cents"] == 20000
    assert ws["lines"]["less_tax_reversed_cents"] == 1650
    assert ws["lines"]["taxable_sales_cents"] == 0
    assert ws["lines"]["amount_due_cents"] == 0


# ---------------- worksheet + payments + reconciliation ----------------

def test_worksheet_payments_reconciliation(env):
    run(_worksheet(env))


async def _worksheet(env):
    jur = await _tx_jur()
    await _issue(client_name="Acme", tax_jurisdiction_id=jur["id"],
                 lines=[invoices_r.InvLine(
                     name="Goods", revenue_code="4100", amount=100.0)])
    prod = await products_r.create_product(products_r.ProductBody(
        name="Shampoo", price=20.0, unit_cost=5.0, quantity_on_hand=10),
        user=OWNER)
    sale = await pos_r.sell(pos_r.SellBody(
        product_items=[pos_r.ProductItem(product_id=prod["id"], qty=2)],
        payment_method="cash", tax_jurisdiction_id=jur["id"]), user=OWNER)
    assert sale["tax_cents"] == 330  # 4000 * 8.25%
    assert sale["sale_date"] == today_str()
    assert sale["tax_snapshot"]["engine"] == "manual-plus"

    ws = await st_r.worksheet(state="TX", year=YEAR, month=MONTH,
                              quarter=None, user=OWNER)
    ln = ws["lines"]
    assert ln["gross_sales_cents"] == 14000
    assert ln["taxable_sales_cents"] == 14000
    assert ln["tax_collected_cents"] == 1155
    assert ln["amount_due_cents"] == 1155
    assert len(ws["drill_down"]["invoices"]) == 1
    assert len(ws["drill_down"]["pos_sales"]) == 1
    assert ws["drill_down"]["invoices"][0]["tax_cents"] == 825
    assert ws["registered"] is True
    assert "does not file returns" in ws["disclaimer"]

    await st_r.record_payment(st_r.PaymentIn(
        state="TX", period_key=PERIOD, amount=1.55, kind="prepayment"),
        user=OWNER)
    ws = await st_r.worksheet(state="TX", year=YEAR, month=MONTH,
                              quarter=None, user=OWNER)
    assert ws["lines"]["less_prepayments_cents"] == 155
    assert ws["lines"]["amount_due_cents"] == 1000

    pay = await st_r.record_payment(st_r.PaymentIn(
        state="TX", period_key=PERIOD, amount=10.00,
        kind="return_payment", payment_method="bank"), user=OWNER)
    assert "did not file or remit" in pay["note"]
    ws = await st_r.worksheet(state="TX", year=YEAR, month=MONTH,
                              quarter=None, user=OWNER)
    assert ws["lines"]["amount_due_cents"] == 0
    assert _gl(env, "2100", "debit") == 1155

    rec = await st_r.reconciliation(year=YEAR, month=MONTH, user=OWNER)
    assert rec["ties_out"] is True
    assert rec["variance_cents"] == 0
    assert rec["computed"]["tax_collected_cents"] == 1155
    assert rec["computed"]["payments_cents"] == 1155
    assert rec["gl"]["closing_cents"] == 0

    csv_resp = await st_r.worksheet_csv(state="TX", year=YEAR, month=MONTH,
                                        quarter=None, user=OWNER)
    assert csv_resp.media_type == "text/csv"
    assert "NOT a filed return" in csv_resp.body.decode()


def test_pos_legacy_flat_rate(env):
    run(_pos_legacy(env))


async def _pos_legacy(env):
    prod = await products_r.create_product(products_r.ProductBody(
        name="Gel", price=20.0, unit_cost=5.0, quantity_on_hand=5), user=OWNER)
    sale = await pos_r.sell(pos_r.SellBody(
        product_items=[pos_r.ProductItem(product_id=prod["id"], qty=1)],
        payment_method="cash"), user=OWNER)
    assert sale["tax_cents"] == 145  # tenant flat 7.25% on products
    assert sale["tax_snapshot"]["engine"] == "pos-flat"
    assert sale["sale_date"] == today_str()
    doc = await env.products.find_one({"id": prod["id"]})
    assert doc["quantity_on_hand"] == 4


# ---------------- liability keying + rate history ----------------

def test_liability_keys_on_issue_date_and_flags_legacy(env):
    run(_liab_dates(env))


async def _liab_dates(env):
    # created last year, issued this month: belongs to THIS month
    doc = _raw_invoice(env, "TX", 5000, when=today_str())
    doc["created_at"] = "2025-01-15T00:00:00Z"
    doc["tax_snapshot"] = None  # pre-engine row
    doc["tax_cents"] = 400
    liab = await st_r.liability(year=YEAR, month=MONTH, user=OWNER)
    bucket = [b for b in liab["jurisdictions"]
              if b["jurisdiction_id"] == "unassigned"][0]
    assert bucket["legacy_count"] == 1
    assert bucket["gross_sales_cents"] == 5000
    assert bucket["tax_collected_cents"] == 400
    old = await st_r.liability(year=2025, month=1, user=OWNER)
    assert old["totals"]["gross_sales_cents"] == 0


def test_rate_history_never_rewrites_past(env):
    run(_history(env))


async def _history(env):
    jur = await st_r.create_jurisdiction(st_r.JurisdictionIn(
        name="Flat TX", state="TX", rate_pct=5.0), user=OWNER)
    inv1 = await _issue(client_name="A", tax_jurisdiction_id=jur["id"],
                        lines=[invoices_r.InvLine(
                            name="Goods", revenue_code="4100", amount=100.0)])
    assert inv1["tax_cents"] == 500
    upd = await st_r.update_jurisdiction(jur["id"], st_r.JurisdictionPatch(
        rate_pct=6.0, effective_from=today_str()), user=OWNER)
    assert upd["rate_pct"] == 6.0
    assert len(upd["rate_history"]) == 2
    inv2 = await _issue(client_name="A", tax_jurisdiction_id=jur["id"],
                        lines=[invoices_r.InvLine(
                            name="Goods", revenue_code="4100", amount=100.0)])
    assert inv2["tax_cents"] == 600
    old = await env.invoices.find_one({"id": inv1["id"]})
    assert old["tax_snapshot"]["combined_rate_pct"] == 5.0  # past untouched
    hist = await st_r.jurisdiction_rate_history(jur["id"], user=OWNER)
    assert sorted(h["rate_pct"] for h in hist["history"]) == [5.0, 6.0]
    liab = await st_r.liability(year=YEAR, month=MONTH, user=OWNER)
    assert liab["jurisdictions"][0]["tax_collected_cents"] == 1100


# ---------------- nexus + CPA ----------------

def test_nexus_monitor_warns(env):
    run(_nexus(env))


async def _nexus(env):
    await _tx_jur()  # TX registered
    _raw_invoice(env, "OK", 9_000_000)  # $90k of $100k threshold
    res = await st_r.nexus_monitor(user=OWNER)
    by_state = {s["state"]: s for s in res["states"]}
    assert by_state["OK"]["status"] == "approaching"
    assert by_state["OK"]["registered"] is False
    assert by_state["TX"]["status"] == "registered"
    assert "not a determination" in by_state["OK"]["warning"]
    _raw_invoice(env, "OK", 2_000_000)
    res = await st_r.nexus_monitor(user=OWNER)
    by_state = {s["state"]: s for s in res["states"]}
    assert by_state["OK"]["status"] == "threshold_crossed"
    assert by_state["OK"]["direct_sales_cents"] == 11_000_000
    assert by_state["OK"]["transactions"] == 2


def test_cpa_package_includes_worksheets(env):
    run(_cpa(env))


async def _cpa(env):
    jur = await _tx_jur()
    await _issue(client_name="Acme", tax_jurisdiction_id=jur["id"],
                 lines=[invoices_r.InvLine(
                     name="Goods", revenue_code="4100", amount=100.0)])
    resp = await cpa_r.cpa_package(year=str(YEAR), user=OWNER)
    zf = zipfile.ZipFile(io.BytesIO(resp.body))
    names = zf.namelist()
    assert "sales_tax_worksheets.csv" in names
    csv_text = zf.read("sales_tax_worksheets.csv").decode()
    assert "TX" in csv_text and "Taxable Sales" in csv_text
    assert "8.25" in csv_text  # the $8.25 of tax collected on the $100 sale
    readme = zf.read("README.txt").decode()
    assert "sales_tax_worksheets.csv" in readme
