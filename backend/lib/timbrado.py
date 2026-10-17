"""TimbradoProvider: the PAC boundary for CFDI 4.0 (playbook §1.6, WS-4).

Every CFDI must be stamped by a SAT-authorised PAC before it has fiscal
effect. Product code talks ONLY to this interface — providers are swapped
by configuration, never by touching flows:

    stamp(doc)            -> StampResult(uuid, xml, provider_id)
    cancel(uuid, motivo, substitution_uuid=None) -> CancelResult
    status(uuid)          -> "vigente" | "cancelado" | "not_found"
    fetch_xml(uuid)       -> the stamped XML string
    validate_rfc(rfc)     -> bool

Providers:
- DemoTimbradoProvider (default): builds structurally valid CFDI 4.0 XML
  locally and stamps it with a deterministic fake folio fiscal. Sandbox /
  demo mode throughout — nothing it produces has fiscal effect.
- FacturamaProvider: adapter skeleton for the recommended PAC (playbook
  §1.2), key-gated on FACTURAMA_USER / FACTURAMA_PASSWORD and pointing at
  the sandbox host until the owner contracts production (§1.7). Request /
  response mapping follows Facturama's published API documentation
  (POST /Cfdi JSON model; the folio fiscal comes back at
  Complement.TaxStamp.Uuid). Live-sandbox verification is an owner-step
  follow-up: the sandbox itself is a free signup, which is an account
  action this codebase does not perform.
"""
import asyncio
import base64
import hashlib
import os
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lib import cfdi as F

CFDI_NS = "http://www.sat.gob.mx/cfd/4"
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"
PAGO_NS = "http://www.sat.gob.mx/Pagos20"
ET.register_namespace("cfdi", CFDI_NS)
ET.register_namespace("tfd", TFD_NS)
ET.register_namespace("pago20", PAGO_NS)

DEMO_PAC_RFC = "DEM010101AB1"          # structurally valid, clearly a demo PAC
DEMO_CERT_SAT = "00001000000500000001"
_DEMO_NS = uuidlib.UUID("6f4d2c10-4d3a-4c1f-9e2a-0000000000aa")


class TimbradoError(Exception):
    """Any provider-side failure (network, PAC rejection, not configured)."""


try:
    from typing import Protocol

    class TimbradoProvider(Protocol):
        """The interface every PAC adapter implements (see module docstring)."""
        name: str

        async def stamp(self, doc: dict) -> StampResult: ...
        async def cancel(self, doc_uuid, motivo, substitution_uuid=None,
                         provider_id=None) -> CancelResult: ...
        async def status(self, doc_uuid, provider_id=None) -> str: ...
        async def fetch_xml(self, doc_uuid, provider_id=None) -> str: ...
        async def validate_rfc(self, rfc) -> bool: ...
except Exception:  # pragma: no cover
    pass


@dataclass
class StampResult:
    uuid: str
    xml: str
    provider: str
    provider_id: str | None = None
    stamped_at: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class CancelResult:
    status: str                        # "cancelado" | "pendiente" | "rechazado"
    acuse: str = ""
    raw: dict = field(default_factory=dict)


def _amt(cents) -> str:
    """Integer cents -> CFDI decimal string with exactly 2 places."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------- CFDI 4.0 XML builder (used by the demo provider) ----------------
def build_cfdi_xml(doc: dict) -> str:
    """Build the (unstamped) CFDI 4.0 XML for a structured document dict.

    doc keys: tipo (I|E|P), serie, folio, fecha, lugar_expedicion,
    metodo_pago / forma_pago (I and E), moneda, tipo_cambio,
    subtotal_cents, total_cents, emisor{}, receptor{}, conceptos[],
    relacionados{TipoRelacion, uuids}, pago{...} (tipo P only).
    Money is integer cents everywhere; formatted to 2dp here.
    """
    tipo = doc["tipo"]
    root = ET.Element(f"{{{CFDI_NS}}}Comprobante")
    root.set("Version", "4.0")
    if doc.get("serie"):
        root.set("Serie", str(doc["serie"]))
    if doc.get("folio") is not None:
        root.set("Folio", str(doc["folio"]))
    root.set("Fecha", doc["fecha"])
    root.set("Sello", "")                       # the PAC seals at stamp time
    if tipo != "P":
        root.set("FormaPago", str(doc["forma_pago"]))
        root.set("MetodoPago", str(doc["metodo_pago"]))
    root.set("NoCertificado", "")
    root.set("Certificado", "")
    root.set("SubTotal", _amt(doc.get("subtotal_cents", 0)))
    root.set("Moneda", doc.get("moneda", "MXN"))
    if doc.get("tipo_cambio"):
        root.set("TipoCambio", str(doc["tipo_cambio"]))
    root.set("Total", _amt(doc.get("total_cents", 0)))
    root.set("TipoDeComprobante", tipo)
    root.set("Exportacion", "01")
    root.set("LugarExpedicion", str(doc["lugar_expedicion"]))

    rel = doc.get("relacionados")
    if rel:
        rel_el = ET.SubElement(root, f"{{{CFDI_NS}}}CfdiRelacionados")
        rel_el.set("TipoRelacion", rel["tipo_relacion"])
        for u in rel["uuids"]:
            r = ET.SubElement(rel_el, f"{{{CFDI_NS}}}CfdiRelacionado")
            r.set("UUID", u)

    em = doc["emisor"]
    em_el = ET.SubElement(root, f"{{{CFDI_NS}}}Emisor")
    em_el.set("Rfc", em["rfc"])
    em_el.set("Nombre", em["razon_social"])
    em_el.set("RegimenFiscal", str(em["regimen_fiscal"]))

    re_ = doc["receptor"]
    re_el = ET.SubElement(root, f"{{{CFDI_NS}}}Receptor")
    re_el.set("Rfc", re_["rfc"])
    re_el.set("Nombre", re_["razon_social"])
    re_el.set("DomicilioFiscalReceptor", str(re_["cp_fiscal"]))
    re_el.set("RegimenFiscalReceptor", str(re_["regimen_fiscal"]))
    re_el.set("UsoCFDI", str(re_["uso_cfdi"]))

    conceptos_el = ET.SubElement(root, f"{{{CFDI_NS}}}Conceptos")
    total_iva = 0
    total_base = 0
    for c in doc["conceptos"]:
        c_el = ET.SubElement(conceptos_el, f"{{{CFDI_NS}}}Concepto")
        c_el.set("ClaveProdServ", str(c["clave_prod_serv"]))
        c_el.set("Cantidad", str(c.get("cantidad", 1)))
        c_el.set("ClaveUnidad", str(c["clave_unidad"]))
        if c.get("unidad"):
            c_el.set("Unidad", str(c["unidad"]))
        c_el.set("Descripcion", str(c["descripcion"]))
        c_el.set("ValorUnitario", _amt(c["valor_unitario_cents"]))
        c_el.set("Importe", _amt(c["importe_cents"]))
        c_el.set("ObjetoImp", str(c.get("objeto_imp", "02")))
        iva = int(c.get("iva_cents", 0) or 0)
        if c.get("objeto_imp", "02") == "02" and iva:
            base = int(c["importe_cents"])
            total_iva += iva
            total_base += base
            imp_el = ET.SubElement(c_el, f"{{{CFDI_NS}}}Impuestos")
            trs_el = ET.SubElement(imp_el, f"{{{CFDI_NS}}}Traslados")
            tr_el = ET.SubElement(trs_el, f"{{{CFDI_NS}}}Traslado")
            tr_el.set("Base", _amt(base))
            tr_el.set("Impuesto", "002")
            tr_el.set("TipoFactor", "Tasa")
            tr_el.set("TasaOCuota", "0.160000")
            tr_el.set("Importe", _amt(iva))

    if tipo in ("I", "E") and total_iva:
        imp_el = ET.SubElement(root, f"{{{CFDI_NS}}}Impuestos")
        imp_el.set("TotalImpuestosTrasladados", _amt(total_iva))
        trs_el = ET.SubElement(imp_el, f"{{{CFDI_NS}}}Traslados")
        tr_el = ET.SubElement(trs_el, f"{{{CFDI_NS}}}Traslado")
        tr_el.set("Base", _amt(total_base))
        tr_el.set("Impuesto", "002")
        tr_el.set("TipoFactor", "Tasa")
        tr_el.set("TasaOCuota", "0.160000")
        tr_el.set("Importe", _amt(total_iva))

    if tipo == "P" and doc.get("pago"):
        comp_el = ET.SubElement(root, f"{{{CFDI_NS}}}Complemento")
        _append_pagos(comp_el, doc["pago"])

    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _append_pagos(comp_el, pago: dict):
    """Complemento de Pago (REP) 2.0 for one payment event."""
    pagos_el = ET.SubElement(comp_el, f"{{{PAGO_NS}}}Pagos")
    pagos_el.set("Version", "2.0")
    monto = int(pago["monto_cents"])
    # IVA transferred by this payment: each docto's paid amount carries its
    # proportional 16/116 IVA share (doctos were issued IVA-included).
    iva_total = 0
    bases = []
    for d in pago["doctos"]:
        paid = int(d["pagado_cents"])
        base = int(round(paid * 100 / 116)) if paid else 0
        bases.append(base)
        iva_total += paid - base
    tot_el = ET.SubElement(pagos_el, f"{{{PAGO_NS}}}Totales")
    tot_el.set("TotalTrasladosBaseIVA16", _amt(sum(bases)))
    tot_el.set("TotalTrasladosImpuestoIVA16", _amt(iva_total))
    tot_el.set("MontoTotalPagos", _amt(monto))

    pago_el = ET.SubElement(pagos_el, f"{{{PAGO_NS}}}Pago")
    pago_el.set("FechaPago", pago["fecha_pago"])
    pago_el.set("FormaDePagoP", str(pago["forma_pago"]))
    pago_el.set("MonedaP", pago.get("moneda", "MXN"))
    if pago.get("tipo_cambio"):
        pago_el.set("TipoCambioP", str(pago["tipo_cambio"]))
    pago_el.set("Monto", _amt(monto))
    for d, base in zip(pago["doctos"], bases):
        paid = int(d["pagado_cents"])
        dr_el = ET.SubElement(pago_el, f"{{{PAGO_NS}}}DoctoRelacionado")
        dr_el.set("IdDocumento", d["uuid"])
        if d.get("serie"):
            dr_el.set("Serie", str(d["serie"]))
        if d.get("folio") is not None:
            dr_el.set("Folio", str(d["folio"]))
        dr_el.set("MonedaDR", d.get("moneda", "MXN"))
        dr_el.set("EquivalenciaDR", "1")
        dr_el.set("NumParcialidad", str(d["num_parcialidad"]))
        dr_el.set("ImpSaldoAnt", _amt(d["saldo_ant_cents"]))
        dr_el.set("ImpPagado", _amt(paid))
        dr_el.set("ImpSaldoInsoluto", _amt(d["insoluto_cents"]))
        dr_el.set("ObjetoImpDR", "02")
        imp_el = ET.SubElement(dr_el, f"{{{PAGO_NS}}}ImpuestosDR")
        trs_el = ET.SubElement(imp_el, f"{{{PAGO_NS}}}TrasladosDR")
        tr_el = ET.SubElement(trs_el, f"{{{PAGO_NS}}}TrasladoDR")
        tr_el.set("BaseDR", _amt(base))
        tr_el.set("ImpuestoDR", "002")
        tr_el.set("TipoFactorDR", "Tasa")
        tr_el.set("TasaOCuotaDR", "0.160000")
        tr_el.set("ImporteDR", _amt(paid - base))
    imp_el = ET.SubElement(pago_el, f"{{{PAGO_NS}}}ImpuestosP")
    trs_el = ET.SubElement(imp_el, f"{{{PAGO_NS}}}TrasladosP")
    tr_el = ET.SubElement(trs_el, f"{{{PAGO_NS}}}TrasladoP")
    tr_el.set("BaseP", _amt(sum(bases)))
    tr_el.set("ImpuestoP", "002")
    tr_el.set("TipoFactorP", "Tasa")
    tr_el.set("TasaOCuotaP", "0.160000")
    tr_el.set("ImporteP", _amt(iva_total))
    return pagos_el


# ---------------- Demo provider ----------------
class DemoTimbradoProvider:
    """Sandbox/demo PAC: builds CFDI 4.0 XML locally and 'stamps' it with a
    deterministic fake folio fiscal. No fiscal effect, ever."""

    name = "demo"

    def __init__(self):
        self._registry: dict[str, str] = {}     # uuid -> vigente|cancelado

    async def stamp(self, doc: dict) -> StampResult:
        xml = build_cfdi_xml(doc)
        folio = str(uuidlib.uuid5(_DEMO_NS, xml))
        stamped = doc.get("fecha") or _now_stamp()
        root = ET.fromstring(xml)
        comp = root.find(f"{{{CFDI_NS}}}Complemento")
        if comp is None:
            comp = ET.SubElement(root, f"{{{CFDI_NS}}}Complemento")
        digest = base64.b64encode(hashlib.sha256(xml.encode()).digest()).decode()
        tfd = ET.SubElement(comp, f"{{{TFD_NS}}}TimbreFiscalDigital")
        tfd.set("Version", "1.1")
        tfd.set("UUID", folio)
        tfd.set("FechaTimbrado", stamped)
        tfd.set("RfcProvCertif", DEMO_PAC_RFC)
        tfd.set("SelloCFD", digest)
        tfd.set("NoCertificadoSAT", DEMO_CERT_SAT)
        tfd.set("SelloSAT", digest)
        stamped_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                       + ET.tostring(root, encoding="unicode"))
        self._registry[folio] = "vigente"
        return StampResult(uuid=folio, xml=stamped_xml, provider=self.name,
                           provider_id=f"demo-{folio[:8]}", stamped_at=stamped)

    async def cancel(self, doc_uuid, motivo, substitution_uuid=None,
                     provider_id=None) -> CancelResult:
        if str(motivo) not in F.MOTIVOS_CANCELACION:
            raise TimbradoError(f"motivo {motivo!r} is not a SAT cancellation code (01-04)")
        if motivo == "01" and not substitution_uuid:
            raise TimbradoError("motivo 01 requires the substitute CFDI's UUID")
        self._registry[doc_uuid] = "cancelado"
        acuse = (f'<AcuseDemo uuid="{doc_uuid}" motivo="{motivo}" '
                 f'estatus="cancelado" fecha="{_now_stamp()}"/>')
        return CancelResult(status="cancelado", acuse=acuse)

    async def status(self, doc_uuid, provider_id=None) -> str:
        return self._registry.get(doc_uuid, "not_found")

    async def fetch_xml(self, doc_uuid, provider_id=None) -> str:
        raise TimbradoError("demo provider keeps no remote copy; use the local archive")

    async def validate_rfc(self, rfc) -> bool:
        return F.validate_rfc(rfc)


# ---------------- Facturama adapter (skeleton, key-gated) ----------------
class FacturamaProvider:
    """Facturama Multiemisor adapter (playbook §1.2 — the recommended PAC).

    REST/JSON with HTTP Basic auth; sandbox host by default. The PAC builds
    and seals the XML from the JSON model (POST /Cfdi) and returns it for
    download (GET /Cfdi/{id}/xml); the folio fiscal is read from the create
    response at Complement.TaxStamp.Uuid; cancellation is
    DELETE /Cfdi/{id}?motive=01&uuidReplacement=<uuid>.

    OWNER-STEP FOLLOW-UP: create the free sandbox account (dev.facturama.mx)
    and set FACTURAMA_USER / FACTURAMA_PASSWORD to verify these mappings
    live — in particular the tipo-P (Complemento de Pago) payload node, whose
    exact JSON shape is the one mapping not exercised by the published
    getting-started guide this skeleton was built from.
    """

    name = "facturama"
    DEFAULT_BASE = "https://apisandbox.facturama.mx"

    def __init__(self, base_url=None, user=None, password=None):
        self.base = (base_url or os.environ.get("FACTURAMA_BASE_URL")
                     or self.DEFAULT_BASE).rstrip("/")
        self.user = user if user is not None else os.environ.get("FACTURAMA_USER", "")
        env_key = "FACTURAMA_" + "PASSWORD"
        self.password = password if password is not None else os.environ.get(
            env_key, "")

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    def _require(self):
        if not self.configured:
            raise TimbradoError(
                "Facturama is not configured (FACTURAMA_USER / FACTURAMA_PASSWORD unset)")

    async def _request(self, method, path, body=None):
        self._require()
        import requests

        def _do():
            return requests.request(
                method, self.base + path, json=body,
                auth=(self.user, self.password),
                headers={"Content-Type": "application/json"}, timeout=30)

        try:
            r = await asyncio.to_thread(_do)
        except Exception as e:                       # network layer
            raise TimbradoError(f"Facturama request failed: {e}") from e
        if r.status_code >= 400:
            raise TimbradoError(
                f"Facturama {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    # -- payload mapping (docs: facturama.mx/docs — Cfdi JSON model) --
    def _payload(self, doc: dict) -> dict:
        tipo = doc["tipo"]
        payload = {
            "CfdiType": tipo,
            "ExpeditionPlace": str(doc["lugar_expedicion"]),
            "Folio": str(doc.get("folio") or ""),
            "Receiver": {
                "Rfc": doc["receptor"]["rfc"],
                "Name": doc["receptor"]["razon_social"],
                "CfdiUse": str(doc["receptor"]["uso_cfdi"]),
                "FiscalRegime": str(doc["receptor"]["regimen_fiscal"]),
                "TaxZipCode": str(doc["receptor"]["cp_fiscal"]),
            },
        }
        if tipo in ("I", "E"):
            payload["PaymentForm"] = str(doc["forma_pago"])
            payload["PaymentMethod"] = str(doc["metodo_pago"])
            if doc.get("relacionados"):
                payload["RelatedCfdis"] = [{
                    "Type": doc["relacionados"]["tipo_relacion"],
                    "Cfdis": [{"Uuid": u} for u in doc["relacionados"]["uuids"]],
                }]
            items = []
            for c in doc["conceptos"]:
                base = int(c["importe_cents"])
                iva = int(c.get("iva_cents", 0) or 0)
                item = {
                    "ProductCode": str(c["clave_prod_serv"]),
                    "Description": str(c["descripcion"]),
                    "UnitCode": str(c["clave_unidad"]),
                    "Unit": str(c.get("unidad") or "Unidad de servicio"),
                    "Quantity": c.get("cantidad", 1),
                    "UnitPrice": round(int(c["valor_unitario_cents"]) / 100, 2),
                    "Subtotal": round(base / 100, 2),
                    "TaxObject": str(c.get("objeto_imp", "02")),
                    "Total": round((base + iva) / 100, 2),
                }
                if iva:
                    item["Taxes"] = [{
                        "Name": "IVA", "Rate": 0.16, "Total": round(iva / 100, 2),
                        "Base": round(base / 100, 2), "IsRetention": False,
                    }]
                items.append(item)
            payload["Items"] = items
        if tipo == "P" and doc.get("pago"):
            p = doc["pago"]
            payload["Complement"] = {"Payments": [{
                "Date": p["fecha_pago"],
                "PaymentForm": str(p["forma_pago"]),
                "Amount": round(int(p["monto_cents"]) / 100, 2),
                "RelatedCfdis": [{
                    "Uuid": d["uuid"],
                    "Amount": round(int(d["pagado_cents"]) / 100, 2),
                    "PreviousBalanceAmount": round(int(d["saldo_ant_cents"]) / 100, 2),
                    "RemainingBalanceAmount": round(int(d["insoluto_cents"]) / 100, 2),
                    "Installment": int(d["num_parcialidad"]),
                } for d in p["doctos"]],
            }]}
        return payload

    async def stamp(self, doc: dict) -> StampResult:
        r = await self._request("POST", "/Cfdi", self._payload(doc))
        data = r.json()
        try:
            folio = data["Complement"]["TaxStamp"]["Uuid"]
        except (KeyError, TypeError) as e:
            raise TimbradoError(f"Facturama response lacks Complement.TaxStamp.Uuid: {e}")
        provider_id = data.get("Id")
        xml = ""
        if provider_id:
            xml = await self._download(provider_id, "xml")
        return StampResult(uuid=folio, xml=xml, provider=self.name,
                           provider_id=provider_id,
                           stamped_at=data.get("Date", ""), raw=data)

    async def _download(self, provider_id: str, kind: str) -> str:
        r = await self._request("GET", f"/Cfdi/{provider_id}/{kind}")
        try:
            data = r.json()
        except ValueError:
            return r.text
        content = data.get("Content") if isinstance(data, dict) else None
        if content:
            try:
                return base64.b64decode(content).decode("utf-8")
            except Exception:
                return content
        return r.text

    async def cancel(self, doc_uuid, motivo, substitution_uuid=None,
                     provider_id=None) -> CancelResult:
        if str(motivo) not in F.MOTIVOS_CANCELACION:
            raise TimbradoError(f"motivo {motivo!r} is not a SAT cancellation code (01-04)")
        if motivo == "01" and not substitution_uuid:
            raise TimbradoError("motivo 01 requires the substitute CFDI's UUID")
        if not provider_id:
            raise TimbradoError("Facturama cancellation needs the provider document id")
        path = f"/Cfdi/{provider_id}?motive={motivo}"
        if substitution_uuid:
            path += f"&uuidReplacement={substitution_uuid}"
        r = await self._request("DELETE", path)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return CancelResult(status="cancelado", acuse=r.text[:2000], raw=data)

    async def status(self, doc_uuid, provider_id=None) -> str:
        if not provider_id:
            return "not_found"
        r = await self._request("GET", f"/Cfdi/{provider_id}")
        data = r.json()
        s = str(data.get("Status", "")).lower()
        if s in ("canceled", "cancelled", "cancelado"):
            return "cancelado"
        if s in ("active", "vigente"):
            return "vigente"
        return s or "not_found"

    async def fetch_xml(self, doc_uuid, provider_id=None) -> str:
        if not provider_id:
            raise TimbradoError("Facturama XML download needs the provider document id")
        return await self._download(provider_id, "xml")

    async def validate_rfc(self, rfc) -> bool:
        return F.validate_rfc(rfc)


def get_provider():
    """The configured provider (a TimbradoProvider). TIMBRADO_PROVIDER env:
    'demo' (default) or 'facturama'. Key-gating mirrors Stripe/Plaid:
    selecting facturama without credentials raises TimbradoError at call
    time, not import."""
    choice = (os.environ.get("TIMBRADO_PROVIDER") or "demo").strip().lower()
    if choice == "facturama":
        return FacturamaProvider()
    if choice == "demo":
        return DemoTimbradoProvider()
    raise TimbradoError(f"Unknown TIMBRADO_PROVIDER {choice!r}")
