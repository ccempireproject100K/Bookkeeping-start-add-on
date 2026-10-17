"""WS-4 CFDI (Mexico): RFC validation, CFDI 4.0 XML structure, the demo
timbrado provider end-to-end (ingreso PUE/PPD -> Complemento de Pago ->
egreso -> cancelación), journal balance on every flow, MX-market gating
isolation, and the Facturama adapter mapping against documented fixtures.

Run from the backend directory with:
    python -m pytest tests/test_cfdi_ws4.py

No PAC account, MongoDB or network needed: the db is faked in-memory
(following tests/test_stripe_integration.py conventions), the default
provider is the local demo PAC, and the Facturama adapter's HTTP layer is
stubbed with the fixtures recorded from Facturama's published docs
(tests/fixtures/facturama_create_*.json).
"""
import asyncio
import base64
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_cfdi")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from lib import cfdi as F  # noqa: E402
from lib import timbrado as T  # noqa: E402
from routers import cfdi as CF  # noqa: E402
from routers import customers as CUST  # noqa: E402

CFD = "{http://www.sat.gob.mx/cfd/4}"
TFD = "{http://www.sat.gob.mx/TimbreFiscalDigital}"
PAGO = "{http://www.sat.gob.mx/Pagos20}"
FIXTURES = Path(__file__).parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


# ---------------- fake in-memory db ----------------
def _matches(doc, q):
    for k, v in (q or {}).items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


def _apply(doc, update):
    for op, changes in (update or {}).items():
        if op == "$set":
            for k, v in changes.items():
                target = doc
                parts = k.split(".")
                for p in parts[:-1]:
                    target = target.setdefault(p, {})
                target[parts[-1]] = v
        elif op == "$push":
            for k, v in changes.items():
                doc.setdefault(k, []).append(v)
        elif op == "$inc":
            for k, v in changes.items():
                doc[k] = doc.get(k, 0) + v


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self.docs.sort(key=lambda d: d.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, n):
        if n:
            self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in self.docs]


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def find_one(self, q=None, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return dict(d)
        return None

    def find(self, q=None, projection=None):
        return _Cursor([dict(d) for d in self.docs if _matches(d, q)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        for d in docs:
            self.docs.append(dict(d))

    async def update_one(self, q, update):
        n = 0
        for d in self.docs:
            if _matches(d, q):
                _apply(d, update)
                n += 1
        return type("R", (), {"matched_count": n})()

    async def find_one_and_update(self, q, update, upsert=False,
                                  return_document=False, **kw):
        for d in self.docs:
            if _matches(d, q):
                before = dict(d)
                _apply(d, update)
                return dict(d) if return_document else before
        if upsert:
            d = dict(q or {})
            _apply(d, update)
            self.docs.append(d)
            return dict(d) if return_document else None
        return None

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs if _matches(d, q))


class _FakeDB:
    def __getattr__(self, name):
        coll = _FakeCollection()
        object.__setattr__(self, name, coll)
        return coll


MX_USER = {"id": "u-mx", "tenant_id": "tmx", "email": "owner@mx.test",
           "name": "MX Owner", "role": "owner"}
US_USER = {"id": "u-us", "tenant_id": "tus", "email": "owner@us.test",
           "name": "US Owner", "role": "owner"}
EMISOR = {"rfc": "EKU9003173C9", "razon_social": "ESCUELA KEMPER URGATE SA DE CV",
          "regimen_fiscal": "601", "cp_fiscal": "78000"}
RECEPTOR = {"rfc": "CUPO800101AB1", "razon_social": "JUAN CUEVAS PEREZ",
            "cp_fiscal": "06000", "regimen_fiscal": "626", "uso_cfdi": "G03"}
ACCOUNTS = ["1010", "1020", "1050", "1200", "2100", "2210", "4000", "4020"]


def _fx(monkeypatch):
    fdb = _FakeDB()
    monkeypatch.setattr(CF, "db", fdb)
    monkeypatch.setattr(CUST, "db", fdb)
    monkeypatch.delenv("TIMBRADO_PROVIDER", raising=False)
    fdb.tenants.docs.append({"id": "tmx", "name": "Taller MX",
                             "settings": {"base_currency": "MXN"}})
    fdb.tenants.docs.append({"id": "tus", "name": "US Shop",
                             "settings": {"base_currency": "USD"}})
    for tid in ("tmx", "tus"):
        for code in ACCOUNTS:
            fdb.accounts.docs.append({"code": code, "tenant_id": tid,
                                      "archived": False})
    return fdb


def _balances(fdb, tenant_id):
    bal = {}
    for ln in fdb.journal_lines.docs:
        if ln["tenant_id"] == tenant_id and ln["status"] == "posted":
            b = bal.setdefault(ln["account_code"], {"debit": 0, "credit": 0})
            b["debit"] += ln["debit"]
            b["credit"] += ln["credit"]
    return bal


def _assert_all_entries_balanced(fdb):
    by_entry = {}
    for ln in fdb.journal_lines.docs:
        e = by_entry.setdefault(ln["entry_id"], [0, 0])
        e[0] += ln["debit"]
        e[1] += ln["credit"]
    for entry_id, (d, c) in by_entry.items():
        assert d == c, f"entry {entry_id} unbalanced: {d} != {c}"


def _setup_mx(monkeypatch):
    fdb = _fx(monkeypatch)
    run(CF.put_fiscal_profile(CF.FiscalProfileBody(**EMISOR), MX_USER))
    return fdb


def _issue(monkeypatch, metodo="PUE", amount=1000.0, receptor=RECEPTOR):
    body = CF.IssueCfdiBody(
        client_name="Cliente Uno", lines=[CF.CfdiLine(name="Servicio", amount=amount)],
        metodo_pago=metodo, forma_pago="03",
        receptor=CF.ReceptorBody(**receptor))
    return run(CF.issue_cfdi_invoice(body, MX_USER))


# ---------------- RFC validation ----------------
@pytest.mark.parametrize("rfc", [
    "EKU9003173C9",        # persona moral (SAT's published example issuer)
    "CUPO800101AB1",       # persona física
    "PLOM850212XY3",       # persona física
    "TME140515GAA",        # check digit 'A' case
    "XAXX010101000",       # generic — público en general
    "XEXX010101000",       # generic — extranjero
    " eku9003173c9 ",      # normalised input
])
def test_rfc_valid_vectors(rfc):
    assert F.validate_rfc(rfc) is True


@pytest.mark.parametrize("rfc", [
    "EKU9003173C0",        # wrong check digit
    "CUPO800101AB2",       # wrong check digit
    "ABC123",              # too short
    "EKU9003173C99",       # too long
    "1234567890123",       # no letters
    "",
    None,
])
def test_rfc_invalid_vectors(rfc):
    assert F.validate_rfc(rfc) is False


def test_rfc_bad_embedded_date_rejected_even_with_right_digit():
    base = "CUPO801301AB"  # month 13
    assert F.validate_rfc(base + F.rfc_check_digit(base)) is False


# ---------------- Catalogue / profile validation ----------------
def test_emisor_profile_validation():
    assert F.validate_emisor_profile(EMISOR) == []
    bad = dict(EMISOR, regimen_fiscal="999")
    assert any("regimen" in e for e in F.validate_emisor_profile(bad))
    resico = dict(EMISOR, regimen_fiscal="626")   # RESICO física is valid
    assert F.validate_emisor_profile(resico) == []
    assert any("cp_fiscal" in e for e in
               F.validate_emisor_profile(dict(EMISOR, cp_fiscal="7800")))


def test_receptor_and_pago_rules():
    assert F.validate_receptor(RECEPTOR) == []
    errs = F.validate_receptor(RECEPTOR, for_pago=True)
    assert any("CP01" in e for e in errs)         # pago complement needs CP01
    assert F.validate_receptor(dict(RECEPTOR, uso_cfdi="CP01"), for_pago=True) == []
    assert F.validate_forma_metodo("03", "PUE") == []
    assert F.validate_forma_metodo("99", "PPD") == []
    assert F.validate_forma_metodo("99", "PUE") != []   # 99 only with PPD
    assert F.validate_forma_metodo("77", "PUE") != []


def test_retention_and_razon_social():
    assert F.retention_until("2026-10-16") == "2031-10-16"
    assert F.retention_until("2024-02-29") == "2029-02-28"
    assert F.razon_social_matches("  ESCUELA   KEMPER ", "escuela kemper")
    assert not F.razon_social_matches("ACME SA", "ACME SA DE CV")


# ---------------- CFDI XML structure ----------------
def _doc(tipo="I", **over):
    doc = {
        "tipo": tipo, "serie": "A", "folio": 1, "fecha": "2026-10-16T12:00:00",
        "lugar_expedicion": "78000", "moneda": "MXN",
        "subtotal_cents": 10000, "total_cents": 11600,
        "emisor": EMISOR, "receptor": RECEPTOR,
        "conceptos": [{"clave_prod_serv": "01010101", "clave_unidad": "E48",
                       "unidad": "Unidad de servicio", "descripcion": "Servicio",
                       "cantidad": 1, "valor_unitario_cents": 10000,
                       "importe_cents": 10000, "objeto_imp": "02",
                       "iva_cents": 1600}],
    }
    if tipo in ("I", "E"):
        doc.update({"metodo_pago": "PUE", "forma_pago": "03"})
    doc.update(over)
    return doc


def test_xml_ingreso_structure():
    res = run(T.DemoTimbradoProvider().stamp(_doc()))
    root = ET.fromstring(res.xml)
    assert root.tag == CFD + "Comprobante"
    assert root.get("Version") == "4.0"
    assert root.get("TipoDeComprobante") == "I"
    assert root.get("Total") == "116.00"
    emisor = root.find(CFD + "Emisor")
    assert emisor.get("Rfc") == "EKU9003173C9"
    assert emisor.get("RegimenFiscal") == "601"
    receptor = root.find(CFD + "Receptor")
    assert receptor.get("DomicilioFiscalReceptor") == "06000"
    assert receptor.get("UsoCFDI") == "G03"
    conceptos = root.find(CFD + "Conceptos")
    assert len(conceptos.findall(CFD + "Concepto")) == 1
    impuestos = root.find(CFD + "Impuestos")
    assert impuestos.get("TotalImpuestosTrasladados") == "16.00"
    tfd = root.find(f"{CFD}Complemento/{TFD}TimbreFiscalDigital")
    assert tfd is not None
    assert tfd.get("UUID") == res.uuid
    assert tfd.get("RfcProvCertif") == T.DEMO_PAC_RFC


def test_xml_pago_complement_structure():
    doc = _doc("P", subtotal_cents=0, total_cents=0, pago={
        "fecha_pago": "2026-10-20T12:00:00", "forma_pago": "03",
        "moneda": "MXN", "monto_cents": 20000,
        "doctos": [{"uuid": "12345678-1234-1234-1234-1234567890ab",
                    "serie": "A", "folio": 7, "moneda": "MXN",
                    "num_parcialidad": 1, "saldo_ant_cents": 58000,
                    "pagado_cents": 20000, "insoluto_cents": 38000}]})
    doc["conceptos"] = [{"clave_prod_serv": "84111506", "clave_unidad": "ACT",
                         "descripcion": "Pago", "cantidad": 1,
                         "valor_unitario_cents": 0, "importe_cents": 0,
                         "objeto_imp": "01", "iva_cents": 0}]
    doc["receptor"] = dict(RECEPTOR, uso_cfdi="CP01")
    res = run(T.DemoTimbradoProvider().stamp(doc))
    root = ET.fromstring(res.xml)
    assert root.get("TipoDeComprobante") == "P"
    assert root.get("Total") == "0.00"
    pagos = root.find(f"{CFD}Complemento/{PAGO}Pagos")
    assert pagos is not None and pagos.get("Version") == "2.0"
    totales = pagos.find(PAGO + "Totales")
    assert totales.get("MontoTotalPagos") == "200.00"
    pago = pagos.find(PAGO + "Pago")
    docto = pago.find(PAGO + "DoctoRelacionado")
    assert docto.get("IdDocumento") == "12345678-1234-1234-1234-1234567890ab"
    assert docto.get("ImpPagado") == "200.00"
    assert docto.get("ImpSaldoInsoluto") == "380.00"
    assert docto.get("NumParcialidad") == "1"


def test_xml_egreso_relacionados():
    doc = _doc("E", relacionados={"tipo_relacion": "01",
                                  "uuids": ["aaaaaaaa-1111-2222-3333-444444444444"]})
    res = run(T.DemoTimbradoProvider().stamp(doc))
    root = ET.fromstring(res.xml)
    rel = root.find(CFD + "CfdiRelacionados")
    assert rel.get("TipoRelacion") == "01"
    assert rel.find(CFD + "CfdiRelacionado").get("UUID").startswith("aaaaaaaa")


# ---------------- Demo provider behaviour ----------------
def test_demo_provider_deterministic_and_cancel_rules():
    p = T.DemoTimbradoProvider()
    r1 = run(p.stamp(_doc()))
    r2 = run(T.DemoTimbradoProvider().stamp(_doc()))
    assert r1.uuid == r2.uuid                    # deterministic fake folio
    assert run(p.status(r1.uuid)) == "vigente"
    with pytest.raises(T.TimbradoError):
        run(p.cancel(r1.uuid, "01"))              # motivo 01 needs substitute
    with pytest.raises(T.TimbradoError):
        run(p.cancel(r1.uuid, "07"))              # not a SAT motivo
    out = run(p.cancel(r1.uuid, "01", substitution_uuid=r2.uuid))
    assert out.status == "cancelado"
    assert run(p.status(r1.uuid)) == "cancelado"
    assert run(p.status("no-such-uuid")) == "not_found"


# ---------------- Facturama adapter (offline, fixtures) ----------------
class _FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text or (json.dumps(data) if data is not None else "")

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def test_facturama_key_gate():
    p = T.FacturamaProvider(user="", password="")
    assert p.configured is False
    with pytest.raises(T.TimbradoError):
        run(p.stamp(_doc()))


def test_facturama_mapping_with_documented_fixtures(monkeypatch):
    req_fx = json.loads((FIXTURES / "facturama_create_request.json").read_text())
    res_fx = json.loads((FIXTURES / "facturama_create_response.json").read_text())
    calls = []

    def fake_request(method, url, json=None, auth=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "body": json, "auth": auth})
        if method == "POST":
            return _FakeResp(200, res_fx)
        xml = "<cfdi:Comprobante/>"
        return _FakeResp(200, {"Content": base64.b64encode(xml.encode()).decode(),
                               "ContentType": "xml"})

    monkeypatch.setattr("requests.request", fake_request)
    p = T.FacturamaProvider(user="demo-user", password="demo-password")
    assert p.base == "https://apisandbox.facturama.mx"
    res = run(p.stamp(_doc()))
    post = [c for c in calls if c["method"] == "POST"][0]
    assert post["url"].endswith("/Cfdi")
    assert post["auth"] == ("demo-user", "demo-password")
    body = post["body"]
    for key in ("CfdiType", "PaymentForm", "PaymentMethod", "ExpeditionPlace",
                "Receiver", "Items"):
        assert key in body and key in req_fx       # documented shape parity
    assert body["Receiver"]["Rfc"] == "CUPO800101AB1"
    assert body["Items"][0]["Taxes"][0]["Name"] == "IVA"
    assert res.uuid == res_fx["Complement"]["TaxStamp"]["Uuid"]
    assert res.provider_id == res_fx["Id"]
    assert res.xml == "<cfdi:Comprobante/>"        # base64 Content decoded


# ---------------- Router end-to-end ----------------
def test_issue_pue_posts_and_archives(monkeypatch):
    fdb = _setup_mx(monkeypatch)
    out = _issue(monkeypatch)
    inv = out["invoice"]
    assert inv["total_cents"] == 116000            # 1000.00 + 16% IVA
    assert inv["cfdi_uuid"] == out["cfdi"]["uuid"]
    bal = _balances(fdb, "tmx")
    assert bal["1200"]["debit"] - bal["1200"]["credit"] == 116000   # Dr AR
    assert bal["4020"]["credit"] == 100000                          # Cr revenue
    assert bal["2100"]["credit"] == 16000                           # Cr IVA
    docs = [d for d in fdb.cfdi_documents.docs if d["kind"] == "I"]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["status"] == "stamped" and doc["uuid"] == out["cfdi"]["uuid"]
    assert doc["retention_until"].startswith("2031")                # 5-year rule
    assert "TimbreFiscalDigital" in doc["xml"]
    assert doc["xml_sha256"]
    _assert_all_entries_balanced(fdb)


def test_ppd_payment_complemento_flow(monkeypatch):
    fdb = _setup_mx(monkeypatch)
    out = _issue(monkeypatch, metodo="PPD", amount=500.0)
    inv_id = out["invoice"]["id"]
    assert out["invoice"]["total_cents"] == 58000

    pay1 = run(CF.cfdi_payment(inv_id, CF.CfdiPaymentBody(amount=200.0), MX_USER))
    assert pay1["status"] == "partial" and pay1["paid_cents"] == 20000
    assert pay1["cfdi"]["num_parcialidad"] == 1
    pay2 = run(CF.cfdi_payment(inv_id, CF.CfdiPaymentBody(), MX_USER))
    assert pay2["status"] == "paid" and pay2["balance_cents"] == 0
    assert pay2["cfdi"]["num_parcialidad"] == 2

    pagos = [d for d in fdb.cfdi_documents.docs if d["kind"] == "P"]
    assert len(pagos) == 2
    root = ET.fromstring(pagos[0]["xml"])
    docto = root.find(f"{CFD}Complemento/{PAGO}Pagos/{PAGO}Pago/{PAGO}DoctoRelacionado")
    assert docto.get("IdDocumento") == out["cfdi"]["uuid"]
    assert docto.get("ImpSaldoAnt") == "580.00"
    assert docto.get("ImpPagado") == "200.00"
    bal = _balances(fdb, "tmx")
    assert bal["1200"]["debit"] - bal["1200"]["credit"] == 0        # AR cleared
    assert bal["1020"]["debit"] == 58000                            # cash in bank
    _assert_all_entries_balanced(fdb)


def test_pue_invoice_rejects_complemento(monkeypatch):
    _setup_mx(monkeypatch)
    out = _issue(monkeypatch, metodo="PUE")
    with pytest.raises(HTTPException) as e:
        run(CF.cfdi_payment(out["invoice"]["id"],
                            CF.CfdiPaymentBody(amount=10.0), MX_USER))
    assert e.value.status_code == 409


def test_credit_note_egreso_flow(monkeypatch):
    fdb = _setup_mx(monkeypatch)
    out = _issue(monkeypatch, metodo="PUE", amount=1000.0)
    inv_id = out["invoice"]["id"]
    res = run(CF.cfdi_credit_note(
        inv_id, CF.CfdiCreditNoteBody(amount=116.0, memo="Devolución"), MX_USER))
    assert res["credit"]["remaining_cents"] == 11600
    assert res["credit"]["source"] == "cfdi_credit_note"
    egresos = [d for d in fdb.cfdi_documents.docs if d["kind"] == "E"]
    assert len(egresos) == 1
    assert egresos[0]["related"]["original_uuid"] == out["cfdi"]["uuid"]
    root = ET.fromstring(egresos[0]["xml"])
    assert root.get("TipoDeComprobante") == "E"
    bal = _balances(fdb, "tmx")
    assert bal["4020"]["debit"] == 10000            # revenue reduced (net of IVA)
    assert bal["2100"]["debit"] == 1600             # IVA backed out
    assert bal["2210"]["credit"] == 11600           # customer credit issued
    with pytest.raises(HTTPException) as e:          # cannot over-credit
        run(CF.cfdi_credit_note(
            inv_id, CF.CfdiCreditNoteBody(amount=99999.0), MX_USER))
    assert e.value.status_code == 422
    _assert_all_entries_balanced(fdb)


def test_cancel_ingreso_reverses_journal(monkeypatch):
    fdb = _setup_mx(monkeypatch)
    out = _issue(monkeypatch, metodo="PUE", amount=300.0)
    doc_id = out["cfdi"]["id"]
    with pytest.raises(HTTPException) as e:          # motivo 01 needs substitute
        run(CF.cancel_document(doc_id, CF.CancelBody(motivo="01"), MX_USER))
    assert e.value.status_code == 422
    res = run(CF.cancel_document(doc_id, CF.CancelBody(motivo="02"), MX_USER))
    assert res["status"] == "cancelled"
    inv = [i for i in fdb.invoices.docs if i["id"] == out["invoice"]["id"]][0]
    assert inv["status"] == "void"
    bal = _balances(fdb, "tmx")
    assert bal["1200"]["debit"] - bal["1200"]["credit"] == 0
    assert bal["4020"]["credit"] - bal["4020"]["debit"] == 0
    with pytest.raises(HTTPException) as e:          # double cancel
        run(CF.cancel_document(doc_id, CF.CancelBody(motivo="02"), MX_USER))
    assert e.value.status_code == 409
    _assert_all_entries_balanced(fdb)


def test_cancel_motivo01_with_substitute(monkeypatch):
    _setup_mx(monkeypatch)
    first = _issue(monkeypatch, amount=100.0)
    second = _issue(monkeypatch, amount=100.0)
    res = run(CF.cancel_document(
        first["cfdi"]["id"],
        CF.CancelBody(motivo="01", substitution_doc_id=second["cfdi"]["id"]),
        MX_USER))
    assert res["status"] == "cancelled"
    assert res["cancel"]["substitution_uuid"] == second["cfdi"]["uuid"]


def test_cancel_blocked_by_children(monkeypatch):
    _setup_mx(monkeypatch)
    out = _issue(monkeypatch, metodo="PPD", amount=500.0)
    run(CF.cfdi_payment(out["invoice"]["id"],
                        CF.CfdiPaymentBody(amount=100.0), MX_USER))
    with pytest.raises(HTTPException) as e:
        run(CF.cancel_document(out["cfdi"]["id"],
                               CF.CancelBody(motivo="02"), MX_USER))
    assert e.value.status_code == 409


def test_archive_export_and_status(monkeypatch):
    _setup_mx(monkeypatch)
    out = _issue(monkeypatch, amount=100.0)
    export = run(CF.export_archive(MX_USER))
    assert export["retention_years"] == 5
    assert len(export["documents"]) == 1
    assert export["documents"][0]["uuid"] == out["cfdi"]["uuid"]
    assert "TimbreFiscalDigital" in export["documents"][0]["xml"]
    status = run(CF.get_document_status(out["cfdi"]["id"], MX_USER))
    assert status["local_status"] == "stamped"
    xml = run(CF.get_document_xml(out["cfdi"]["id"], MX_USER))
    assert "Comprobante" in xml.body.decode()


# ---------------- MX gating isolation ----------------
def test_non_mx_tenant_sees_nothing(monkeypatch):
    _fx(monkeypatch)
    calls = [
        lambda: CF.list_documents(US_USER),
        lambda: CF.export_archive(US_USER),
        lambda: CF.issue_cfdi_invoice(CF.IssueCfdiBody(
            client_name="X", lines=[CF.CfdiLine(name="S", amount=1.0)],
            receptor=CF.ReceptorBody(**RECEPTOR)), US_USER),
    ]
    for make in calls:
        with pytest.raises(HTTPException) as e:
            run(make())
        assert e.value.status_code == 404
    profile = run(CF.get_fiscal_profile(US_USER))
    assert profile["profile"] is None


def test_mx_tenant_without_profile_cannot_stamp(monkeypatch):
    fdb = _fx(monkeypatch)
    fdb.tenants.docs[0]["settings"]["market"] = "MX"   # market on, no profile
    with pytest.raises(HTTPException) as e:
        _issue(monkeypatch)
    assert e.value.status_code == 422


def test_customer_fiscal_block_validation(monkeypatch):
    fdb = _fx(monkeypatch)
    ok = run(CUST.create_customer(CUST.CustomerBody(
        name="Cliente Fiscal", fiscal=CUST.CustomerFiscal(**RECEPTOR)), US_USER))
    assert ok["fiscal"]["rfc"] == "CUPO800101AB1"
    plain = run(CUST.create_customer(CUST.CustomerBody(name="Plain"), US_USER))
    assert plain["fiscal"] is None                   # non-MX flow untouched
    with pytest.raises(HTTPException) as e:
        run(CUST.create_customer(CUST.CustomerBody(
            name="Bad", fiscal=CUST.CustomerFiscal(
                **dict(RECEPTOR, rfc="BAD"))), US_USER))
    assert e.value.status_code == 422
    assert len([c for c in fdb.customers.docs if c["tenant_id"] == "tus"]) == 2
