"""CFDI 4.0 (Mexico) — WS-4 of the Mexico entry playbook.

Fiscal invoicing for MX-market workspaces ONLY: every endpoint below is
gated on the tenant's market being MX (settings.market == "MX", set when
the owner saves a Mexican fiscal profile). Non-MX tenants get a 404 on
the whole surface and none of their data or flows change.

Flows (playbook §4.1 WS-4):
- PUT  /cfdi/fiscal-profile        emisor capture (Constancia-exact, §3.2)
- POST /cfdi/invoices              ingreso CFDI (PUE or PPD) — creates the
                                   books invoice and posts Dr AR / Cr
                                   revenue + Cr IVA (2100) exactly like
                                   POST /invoices, then stamps + archives
- POST /cfdi/invoices/{id}/payments   Complemento de Pago (REP 2.0) for a
                                   PPD invoice; posts Dr cash / Cr AR
- POST /cfdi/invoices/{id}/credit-notes  egreso CFDI (TipoRelacion 01)
                                   linked to the original; posts like the
                                   existing credit-note flow (Dr revenue /
                                   Cr 2210 customer credit)
- POST /cfdi/documents/{id}/cancel cancellation with motivo 01-04
- GET  /cfdi/documents[/{id}[/xml]]  the archive (XML retained 5 years)
- GET  /cfdi/archive/export        full XML archive export (retention §3.2)

All stamping goes through lib.timbrado.TimbradoProvider (demo by default;
Facturama behind FACTURAMA_* config). CSD custody is an owner/onboarding
concern (playbook §1.7) — no CSD material is accepted or stored here.
"""
import hashlib
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from db import db
from lib import cfdi as F
from lib import ledger as L
from lib import posting as P
from lib import timbrado as T
from lib.auth import get_current_user, require
from lib.base import Scoped, cents_to_dollars, new_id, now_iso, today_str, to_cents

router = APIRouter(prefix="/cfdi", tags=["cfdi"])

SERIE = "A"


def docs_coll(user):
    return Scoped(db.cfdi_documents, user["tenant_id"])


def inv_coll(user):
    return Scoped(db.invoices, user["tenant_id"])


# ---------------- MX gating ----------------
async def _tenant(user) -> dict:
    return await db.tenants.find_one({"id": user["tenant_id"]}, {"_id": 0}) or {}


def _is_mx(tenant: dict) -> bool:
    settings = tenant.get("settings") or {}
    return (settings.get("market") == "MX" or tenant.get("market") == "MX"
            or bool(tenant.get("fiscal_profile")))


async def _require_mx(user) -> dict:
    tenant = await _tenant(user)
    if not _is_mx(tenant):
        raise HTTPException(status_code=404,
                            detail="CFDI is only available to MX-market workspaces")
    return tenant


def _emisor(tenant: dict) -> dict:
    profile = tenant.get("fiscal_profile") or {}
    errs = F.validate_emisor_profile(profile)
    if errs:
        raise HTTPException(status_code=422, detail=(
            "Emisor fiscal profile incomplete: " + "; ".join(errs)))
    return {
        "rfc": profile["rfc"].strip().upper(),
        "razon_social": profile["razon_social"],
        "regimen_fiscal": str(profile["regimen_fiscal"]),
        "cp_fiscal": str(profile["cp_fiscal"]),
    }


def _provider():  # -> lib.timbrado.TimbradoProvider (Protocol, type-check only)
    try:
        return T.get_provider()
    except T.TimbradoError as e:
        raise HTTPException(status_code=503, detail=str(e))


async def _stamp(provider, doc) -> T.StampResult:
    try:
        return await provider.stamp(doc)
    except T.TimbradoError as e:
        raise HTTPException(status_code=502, detail=f"Timbrado failed: {e}")


def _fecha_now() -> str:
    return now_iso()[:19]


def _archive_doc(user, *, kind, invoice_id, result: T.StampResult, doc,
                 subtotal_cents, iva_cents, total_cents, related=None):
    return {
        "id": new_id(), "kind": kind, "invoice_id": invoice_id,
        "uuid": result.uuid, "provider": result.provider,
        "provider_id": result.provider_id, "status": "stamped",
        "serie": doc.get("serie"), "folio": doc.get("folio"),
        "emisor": doc["emisor"], "receptor": doc["receptor"],
        "metodo_pago": doc.get("metodo_pago"), "forma_pago": doc.get("forma_pago"),
        "moneda": doc.get("moneda", "MXN"),
        "subtotal_cents": subtotal_cents, "iva_cents": iva_cents,
        "total_cents": total_cents,
        "xml": result.xml,
        "xml_sha256": hashlib.sha256(result.xml.encode()).hexdigest(),
        "related": related or {},
        "retention_until": F.retention_until(today_str()),
        "stamped_at": result.stamped_at or now_iso(),
        "created_at": now_iso(), "cancel": None,
    }


# ---------------- Fiscal profile (emisor) ----------------
class FiscalProfileBody(BaseModel):
    rfc: str
    razon_social: str
    regimen_fiscal: str
    cp_fiscal: str


@router.get("/fiscal-profile")
async def get_fiscal_profile(user: dict = Depends(get_current_user)):
    tenant = await _tenant(user)
    return {"profile": tenant.get("fiscal_profile"),
            "market": (tenant.get("settings") or {}).get("market")}


@router.put("/fiscal-profile")
async def put_fiscal_profile(body: FiscalProfileBody,
                             user: dict = Depends(require("ops:manage"))):
    profile = {"rfc": body.rfc.strip().upper(), "razon_social": body.razon_social,
               "regimen_fiscal": body.regimen_fiscal, "cp_fiscal": body.cp_fiscal}
    errs = F.validate_emisor_profile(profile)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    # Saving a Mexican fiscal profile IS the MX-market opt-in: it flips the
    # workspace market so the gated CFDI surface becomes visible.
    await db.tenants.update_one(
        {"id": user["tenant_id"]},
        {"$set": {"fiscal_profile": profile, "settings.market": "MX"}})
    return {"profile": profile, "market": "MX"}


# ---------------- Ingreso (invoice) ----------------
class CfdiLine(BaseModel):
    kind: str = "service"
    name: str
    revenue_code: str = "4020"
    amount: float                       # base amount, IVA excluded (as /invoices)
    clave_prod_serv: str = F.DEFAULT_CLAVE_PROD_SERV
    clave_unidad: str = F.DEFAULT_CLAVE_UNIDAD


class ReceptorBody(BaseModel):
    rfc: str
    razon_social: str
    cp_fiscal: str
    regimen_fiscal: str
    uso_cfdi: str = "G03"


class IssueCfdiBody(BaseModel):
    client_name: str
    customer_id: Optional[str] = None
    lines: List[CfdiLine]
    metodo_pago: str = "PUE"
    forma_pago: str = "03"
    receptor: Optional[ReceptorBody] = None
    currency: str = "MXN"
    tipo_cambio: Optional[float] = None
    due_date: Optional[str] = None
    terms: Optional[str] = "Net 30"


async def _resolve_receptor(user, body) -> dict:
    if body.receptor is not None:
        rec = body.receptor.model_dump()
    elif body.customer_id:
        cust = await Scoped(db.customers, user["tenant_id"]).find_one(
            {"id": body.customer_id})
        rec = (cust or {}).get("fiscal")
        if not rec:
            raise HTTPException(status_code=422, detail=(
                "Customer has no fiscal (receptor) data; add it on the "
                "customer record or pass receptor in the request"))
    else:
        raise HTTPException(status_code=422, detail=(
            "Receptor fiscal data is required to stamp a CFDI"))
    rec = {**rec, "rfc": str(rec["rfc"]).strip().upper()}
    errs = F.validate_receptor(rec)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    return rec


@router.post("/invoices")
async def issue_cfdi_invoice(body: IssueCfdiBody,
                             user: dict = Depends(require("ops:manage"))):
    tenant = await _require_mx(user)
    emisor = _emisor(tenant)
    errs = F.validate_forma_metodo(body.forma_pago, body.metodo_pago)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    currency = (body.currency or "MXN").upper()
    if currency not in ("MXN", "USD"):
        raise HTTPException(status_code=422, detail="CFDI currency must be MXN or USD")
    if currency == "USD" and not body.tipo_cambio:
        raise HTTPException(status_code=422,
                            detail="tipo_cambio is required for a USD CFDI")
    receptor = await _resolve_receptor(user, body)

    # Amounts: lines are IVA-excluded bases; IVA is definitional 16%.
    rev_lines, conceptos = [], []
    subtotal = 0
    for ln in body.lines:
        amt = to_cents(ln.amount)
        subtotal += amt
        rev_lines.append({"revenue_code": ln.revenue_code, "amount_cents": amt})
        conceptos.append({
            "clave_prod_serv": ln.clave_prod_serv, "clave_unidad": ln.clave_unidad,
            "unidad": "Unidad de servicio", "descripcion": ln.name, "cantidad": 1,
            "valor_unitario_cents": amt, "importe_cents": amt,
            "objeto_imp": "02", "iva_cents": 0})
    iva_total = int(round(subtotal * F.IVA_RATE))
    # Per-line IVA reconciled to the invoice total (last line takes rounding).
    remaining = iva_total
    for i, c in enumerate(conceptos):
        if i == len(conceptos) - 1:
            c["iva_cents"] = remaining
        else:
            c["iva_cents"] = int(round(c["importe_cents"] * F.IVA_RATE))
            remaining -= c["iva_cents"]
    gross = subtotal + iva_total

    # Period must be open before anything is stamped (mirrors ledger rules).
    period = await L.ensure_period(db, user["tenant_id"], today_str())
    if period["status"] == "locked":
        raise HTTPException(status_code=423,
                            detail=f"Period {period['key']} is locked")

    folio = await docs_coll(user).next_seq(db.counters, "cfdi:I")
    doc = {
        "tipo": "I", "serie": SERIE, "folio": folio, "fecha": _fecha_now(),
        "lugar_expedicion": emisor["cp_fiscal"],
        "metodo_pago": body.metodo_pago, "forma_pago": body.forma_pago,
        "moneda": currency, "tipo_cambio": body.tipo_cambio,
        "subtotal_cents": subtotal, "total_cents": gross,
        "emisor": emisor, "receptor": receptor, "conceptos": conceptos,
    }
    provider = _provider()
    result = await _stamp(provider, doc)

    inv_id = new_id()
    try:
        entry = await L.post_journal_entry(db, user["tenant_id"], user,
            P.build_invoice_issue(
                revenue_lines=rev_lines, tax_cents=iva_total, cogs_cents=0,
                gross_cents=gross, entry_date=today_str(), source_ref=inv_id,
                memo=f"CFDI Invoice — {body.client_name}", currency=currency),
            status="posted")
        inv = {
            "id": inv_id, "client_name": body.client_name,
            "customer_id": body.customer_id,
            "lines": [l.model_dump() for l in body.lines],
            "subtotal_cents": subtotal, "tax_cents": iva_total,
            "total_cents": gross, "currency": currency,
            "tax_jurisdiction_id": None, "status": "issued", "paid_cents": 0,
            "payments": [], "due_date": body.due_date, "terms": body.terms,
            "issue_entry": entry["number"], "issue_entry_id": entry["id"],
            "created_at": now_iso(), "issue_date": today_str(),
            "cfdi_uuid": result.uuid,
            "fiscal": {"metodo_pago": body.metodo_pago,
                       "forma_pago": body.forma_pago, "receptor": receptor},
        }
        await inv_coll(user).insert_one(inv)
        archive = _archive_doc(user, kind="I", invoice_id=inv_id, result=result,
                               doc=doc, subtotal_cents=subtotal,
                               iva_cents=iva_total, total_cents=gross)
        await docs_coll(user).insert_one(archive)
    except HTTPException:
        # Stamped but unposted: cancel the orphan at the PAC (motivo 02).
        try:
            await provider.cancel(result.uuid, "02", provider_id=result.provider_id)
        except Exception:
            pass
        raise
    inv["paid_cents"] = 0
    inv["balance_cents"] = gross
    inv["balance"] = cents_to_dollars(gross)
    inv["total"] = cents_to_dollars(gross)
    return {"invoice": inv, "cfdi": {"id": archive["id"], "uuid": result.uuid,
                                     "status": "stamped",
                                     "provider": result.provider}}


# ---------------- Complemento de Pago (PPD payments) ----------------
class CfdiPaymentBody(BaseModel):
    amount: Optional[float] = None      # None = full remaining balance
    forma_pago: str = "03"
    pay_date: Optional[str] = None
    payment_method: str = "bank"        # books cash account selector


@router.post("/invoices/{invoice_id}/payments")
async def cfdi_payment(invoice_id: str, body: CfdiPaymentBody,
                       user: dict = Depends(require("ops:manage"))):
    tenant = await _require_mx(user)
    emisor = _emisor(tenant)
    if str(body.forma_pago) not in F.FORMA_PAGO:
        raise HTTPException(status_code=422, detail="forma_pago: not a SAT catalogue code")
    inv = await inv_coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    orig = await docs_coll(user).find_one(
        {"invoice_id": invoice_id, "kind": "I", "status": "stamped"})
    if not orig:
        raise HTTPException(status_code=409,
                            detail="Invoice has no stamped CFDI ingreso")
    if (inv.get("fiscal") or {}).get("metodo_pago") != "PPD":
        raise HTTPException(status_code=409, detail=(
            "Complemento de Pago applies only to PPD invoices; "
            "PUE invoices settle at issue"))
    balance = inv["total_cents"] - inv.get("paid_cents", 0)
    if balance <= 0:
        raise HTTPException(status_code=409, detail="Invoice already fully paid")
    amount = to_cents(body.amount) if body.amount is not None else balance
    if amount <= 0 or amount > balance:
        raise HTTPException(status_code=422, detail=(
            "Payment amount must be positive and no more than the balance "
            "(a fiscal DoctoRelacionado cannot be overpaid)"))
    pay_date = body.pay_date or today_str()
    receptor = dict((inv.get("fiscal") or {}).get("receptor") or {})
    receptor["uso_cfdi"] = "CP01"
    errs = F.validate_receptor(receptor, for_pago=True)
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))

    prior = await docs_coll(user).find_list(
        {"invoice_id": invoice_id, "kind": "P", "status": "stamped"})
    parcialidad = len(prior) + 1
    folio = await docs_coll(user).next_seq(db.counters, "cfdi:P")
    doc = {
        "tipo": "P", "serie": SERIE, "folio": folio, "fecha": _fecha_now(),
        "lugar_expedicion": emisor["cp_fiscal"], "moneda": "MXN",
        "subtotal_cents": 0, "total_cents": 0,
        "emisor": emisor, "receptor": receptor,
        "conceptos": [{
            "clave_prod_serv": F.PAGO_CLAVE_PROD_SERV,
            "clave_unidad": F.PAGO_CLAVE_UNIDAD, "descripcion": "Pago",
            "cantidad": 1, "valor_unitario_cents": 0, "importe_cents": 0,
            "objeto_imp": "01", "iva_cents": 0}],
        "pago": {
            "fecha_pago": f"{pay_date}T12:00:00", "forma_pago": body.forma_pago,
            "moneda": inv.get("currency", "MXN"), "monto_cents": amount,
            "doctos": [{
                "uuid": orig["uuid"], "serie": orig.get("serie"),
                "folio": orig.get("folio"), "moneda": inv.get("currency", "MXN"),
                "num_parcialidad": parcialidad, "saldo_ant_cents": balance,
                "pagado_cents": amount,
                "insoluto_cents": balance - amount}],
        },
    }
    provider = _provider()
    result = await _stamp(provider, doc)
    cash = P._cash_account(body.payment_method)
    try:
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": pay_date, "source": "invoice_paid",
            "source_ref": invoice_id,
            "memo": f"CFDI payment — {inv['client_name']}",
            "lines": [P.dr(cash, amount, "Invoice payment (complemento)"),
                      P.cr("1200", amount, "AR cleared")],
            "currency": inv.get("currency", "MXN"),
        }, status="posted")
        new_paid = inv.get("paid_cents", 0) + amount
        status = "paid" if new_paid >= inv["total_cents"] else "partial"
        pmt = {"date": pay_date, "amount_cents": amount, "cash_cents": amount,
               "credit_cents": 0, "method": body.payment_method,
               "pay_currency": inv.get("currency", "MXN"),
               "entry": entry["number"], "entry_id": entry["id"],
               "cfdi_uuid": result.uuid}
        await inv_coll(user).update_one({"id": invoice_id}, {
            "$set": {"status": status, "paid_cents": new_paid},
            "$push": {"payments": pmt}})
        archive = _archive_doc(user, kind="P", invoice_id=invoice_id,
                               result=result, doc=doc, subtotal_cents=0,
                               iva_cents=0, total_cents=amount,
                               related={"original_uuid": orig["uuid"],
                                        "num_parcialidad": parcialidad})
        archive["journal_entry_id"] = entry["id"]
        await docs_coll(user).insert_one(archive)
    except HTTPException:
        try:
            await provider.cancel(result.uuid, "02", provider_id=result.provider_id)
        except Exception:
            pass
        raise
    return {"status": status, "paid_cents": new_paid,
            "balance_cents": inv["total_cents"] - new_paid,
            "entry_number": entry["number"],
            "cfdi": {"id": archive["id"], "uuid": result.uuid,
                     "num_parcialidad": parcialidad}}


# ---------------- Egreso (credit note) ----------------
class CfdiCreditNoteBody(BaseModel):
    amount: Optional[float] = None      # IVA-included; None = all remaining
    revenue_code: str = "4020"
    memo: str = ""


def _split_iva(total_cents: int):
    base = int(round(total_cents * 100 / 116))
    return base, total_cents - base


@router.post("/invoices/{invoice_id}/credit-notes")
async def cfdi_credit_note(invoice_id: str, body: CfdiCreditNoteBody,
                           user: dict = Depends(require("ops:manage"))):
    tenant = await _require_mx(user)
    emisor = _emisor(tenant)
    inv = await inv_coll(user).find_one({"id": invoice_id})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    orig = await docs_coll(user).find_one(
        {"invoice_id": invoice_id, "kind": "I", "status": "stamped"})
    if not orig:
        raise HTTPException(status_code=409,
                            detail="Invoice has no stamped CFDI ingreso")
    prior = await docs_coll(user).find_list(
        {"invoice_id": invoice_id, "kind": "E", "status": "stamped"})
    credited = sum(d["total_cents"] for d in prior)
    remaining = inv["total_cents"] - credited
    amount = to_cents(body.amount) if body.amount is not None else remaining
    if amount <= 0 or amount > remaining:
        raise HTTPException(status_code=422, detail=(
            "Credit-note amount must be positive and no more than the "
            "invoice total minus prior credit notes"))
    base, iva = _split_iva(amount)
    receptor = dict((inv.get("fiscal") or {}).get("receptor") or {})
    folio = await docs_coll(user).next_seq(db.counters, "cfdi:E")
    doc = {
        "tipo": "E", "serie": SERIE, "folio": folio, "fecha": _fecha_now(),
        "lugar_expedicion": emisor["cp_fiscal"],
        "metodo_pago": orig.get("metodo_pago") or "PUE",
        "forma_pago": orig.get("forma_pago") or "03",
        "moneda": inv.get("currency", "MXN"),
        "subtotal_cents": base, "total_cents": amount,
        "emisor": emisor, "receptor": receptor,
        "conceptos": [{
            "clave_prod_serv": F.DEFAULT_CLAVE_PROD_SERV,
            "clave_unidad": F.DEFAULT_CLAVE_UNIDAD,
            "unidad": "Unidad de servicio",
            "descripcion": body.memo or f"Nota de crédito — {inv['client_name']}",
            "cantidad": 1, "valor_unitario_cents": base,
            "importe_cents": base, "objeto_imp": "02", "iva_cents": iva}],
        "relacionados": {"tipo_relacion": "01", "uuids": [orig["uuid"]]},
    }
    provider = _provider()
    result = await _stamp(provider, doc)
    cid = new_id()
    try:
        entry = await L.post_journal_entry(db, user["tenant_id"], user, {
            "entry_date": today_str(), "source": "credit_note", "source_ref": cid,
            "memo": f"CFDI credit note — {inv['client_name']}: {body.memo}".strip(),
            "lines": [P.dr(body.revenue_code, base, "Revenue reduced (egreso)"),
                      P.dr("2100", iva, "IVA backed out (egreso)"),
                      P.cr("2210", amount, "Customer credit issued")],
            "currency": inv.get("currency", "MXN"),
        }, status="posted")
        credit = {"id": cid, "client_name": inv["client_name"],
                  "customer_id": inv.get("customer_id"),
                  "amount_cents": amount, "remaining_cents": amount,
                  "source": "cfdi_credit_note", "memo": body.memo,
                  "entry_number": entry["number"], "entry_id": entry["id"],
                  "cfdi_uuid": result.uuid, "created_at": now_iso()}
        await Scoped(db.customer_credits, user["tenant_id"]).insert_one(credit)
        archive = _archive_doc(user, kind="E", invoice_id=invoice_id,
                               result=result, doc=doc, subtotal_cents=base,
                               iva_cents=iva, total_cents=amount,
                               related={"original_uuid": orig["uuid"],
                                        "tipo_relacion": "01"})
        archive["journal_entry_id"] = entry["id"]
        archive["credit_id"] = cid
        await docs_coll(user).insert_one(archive)
    except HTTPException:
        try:
            await provider.cancel(result.uuid, "02", provider_id=result.provider_id)
        except Exception:
            pass
        raise
    credit.pop("_id", None)
    return {"credit": credit,
            "cfdi": {"id": archive["id"], "uuid": result.uuid,
                     "status": "stamped"}}


# ---------------- Cancellation ----------------
class CancelBody(BaseModel):
    motivo: str
    substitution_doc_id: Optional[str] = None


@router.post("/documents/{doc_id}/cancel")
async def cancel_document(doc_id: str, body: CancelBody,
                          user: dict = Depends(require("ops:manage"))):
    await _require_mx(user)
    doc = await docs_coll(user).find_one({"id": doc_id})
    if not doc:
        raise HTTPException(status_code=404, detail="CFDI document not found")
    if doc["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="Document already cancelled")
    if body.motivo not in F.MOTIVOS_CANCELACION:
        raise HTTPException(status_code=422,
                            detail="motivo must be a SAT code (01, 02, 03, 04)")
    substitution_uuid = None
    if body.motivo == "01":
        if not body.substitution_doc_id:
            raise HTTPException(status_code=422, detail=(
                "motivo 01 requires substitution_doc_id (the replacement CFDI)"))
        sub = await docs_coll(user).find_one({"id": body.substitution_doc_id})
        if not sub or sub["status"] != "stamped" or sub["id"] == doc["id"]:
            raise HTTPException(status_code=422, detail=(
                "substitution_doc_id must reference another stamped CFDI"))
        substitution_uuid = sub["uuid"]

    reversal_note = None
    if doc["kind"] == "I":
        children = await docs_coll(user).find_list(
            {"invoice_id": doc["invoice_id"], "status": "stamped"})
        if any(c["kind"] in ("P", "E") for c in children):
            raise HTTPException(status_code=409, detail=(
                "Cancel the related Complemento de Pago / credit notes first"))
        inv = await inv_coll(user).find_one({"id": doc["invoice_id"]})
        if inv and inv.get("paid_cents", 0) > 0:
            raise HTTPException(status_code=409, detail=(
                "Invoice has payments; use a credit note instead of cancelling"))

    provider = _provider()
    try:
        res = await provider.cancel(doc["uuid"], body.motivo,
                                    substitution_uuid=substitution_uuid,
                                    provider_id=doc.get("provider_id"))
    except T.TimbradoError as e:
        raise HTTPException(status_code=502, detail=f"Cancellation failed: {e}")
    cancel_info = {"motivo": body.motivo, "substitution_uuid": substitution_uuid,
                   "acuse": res.acuse, "at": now_iso(), "status": res.status}
    await docs_coll(user).update_one({"id": doc_id}, {
        "$set": {"status": "cancelled", "cancel": cancel_info}})

    # Books follow the fiscal act: reverse the document's journal entry.
    if doc["kind"] == "I":
        inv = await inv_coll(user).find_one({"id": doc["invoice_id"]})
        if inv and inv.get("issue_entry_id"):
            try:
                await L.reverse_entry(db, user["tenant_id"], user,
                                      inv["issue_entry_id"])
                await inv_coll(user).update_one({"id": inv["id"]},
                                                {"$set": {"status": "void"}})
            except HTTPException as e:
                reversal_note = f"journal reversal skipped: {e.detail}"
    elif doc.get("journal_entry_id"):
        try:
            await L.reverse_entry(db, user["tenant_id"], user,
                                  doc["journal_entry_id"])
            if doc["kind"] == "P":
                inv = await inv_coll(user).find_one({"id": doc["invoice_id"]})
                if inv:
                    new_paid = max(0, inv.get("paid_cents", 0) - doc["total_cents"])
                    st = "paid" if new_paid >= inv["total_cents"] else (
                        "partial" if new_paid > 0 else "issued")
                    await inv_coll(user).update_one({"id": inv["id"]}, {
                        "$set": {"paid_cents": new_paid, "status": st}})
            if doc["kind"] == "E" and doc.get("credit_id"):
                await Scoped(db.customer_credits, user["tenant_id"]).update_one(
                    {"id": doc["credit_id"]}, {"$set": {"remaining_cents": 0,
                                                        "status": "void"}})
        except HTTPException as e:
            reversal_note = f"journal reversal skipped: {e.detail}"
    return {"id": doc_id, "uuid": doc["uuid"], "status": "cancelled",
            "cancel": cancel_info, "journal_reversal": reversal_note or "posted"}


# ---------------- Archive / reads ----------------
def _public_doc(doc):
    d = {k: v for k, v in doc.items() if k not in ("_id", "xml")}
    d["has_xml"] = bool(doc.get("xml"))
    return d


@router.get("/documents")
async def list_documents(user: dict = Depends(get_current_user)):
    await _require_mx(user)
    rows = await docs_coll(user).find_list(sort=[("created_at", -1)], limit=500)
    return [_public_doc(d) for d in rows]


@router.get("/documents/{doc_id}")
async def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    await _require_mx(user)
    doc = await docs_coll(user).find_one({"id": doc_id})
    if not doc:
        raise HTTPException(status_code=404, detail="CFDI document not found")
    return _public_doc(doc)


@router.get("/documents/{doc_id}/xml")
async def get_document_xml(doc_id: str, user: dict = Depends(get_current_user)):
    await _require_mx(user)
    doc = await docs_coll(user).find_one({"id": doc_id})
    if not doc or not doc.get("xml"):
        raise HTTPException(status_code=404, detail="No archived XML for this document")
    return Response(content=doc["xml"], media_type="application/xml")


@router.get("/documents/{doc_id}/status")
async def get_document_status(doc_id: str, user: dict = Depends(get_current_user)):
    await _require_mx(user)
    doc = await docs_coll(user).find_one({"id": doc_id})
    if not doc:
        raise HTTPException(status_code=404, detail="CFDI document not found")
    provider_status = "unavailable"
    try:
        provider_status = await _provider().status(
            doc["uuid"], provider_id=doc.get("provider_id"))
    except Exception:
        pass
    return {"uuid": doc["uuid"], "local_status": doc["status"],
            "provider_status": provider_status, "provider": doc["provider"]}


@router.get("/archive/export")
async def export_archive(user: dict = Depends(get_current_user)):
    await _require_mx(user)
    rows = await docs_coll(user).find_list(sort=[("created_at", 1)], limit=5000)
    return {
        "tenant_id": user["tenant_id"], "exported_at": now_iso(),
        "retention_years": 5,
        "documents": [{
            "uuid": d["uuid"], "kind": d["kind"], "status": d["status"],
            "invoice_id": d["invoice_id"], "stamped_at": d["stamped_at"],
            "retention_until": d["retention_until"],
            "xml_sha256": d["xml_sha256"], "xml": d["xml"],
        } for d in rows],
    }
