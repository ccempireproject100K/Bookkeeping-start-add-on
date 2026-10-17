"""CFDI 4.0 fiscal data: catalogues, RFC validation, profile validation.

Implements the fiscal field spec from the Mexico entry playbook (§3.2):
emisor/receptor capture validated against the Constancia de Situación
Fiscal, RESICO regimes 625/626 included, uso CFDI / forma de pago /
cancellation-motivo catalogues, and 5-year XML retention bookkeeping.

Pure functions and data — no db, no providers (see lib/timbrado.py).
MX-market gating lives in routers/cfdi.py; nothing here is imported by
non-MX code paths, so non-MX tenants are untouched.
"""
import re
from datetime import date

# ---------------- RFC validation ----------------
# Persona física: 4 letters + YYMMDD + 3-char homoclave (13 chars).
# Persona moral:  3 letters + YYMMDD + 3-char homoclave (12 chars).
_RFC_FISICA = re.compile(r"^[A-ZÑ&]{4}\d{6}[A-Z0-9]{3}$")
_RFC_MORAL = re.compile(r"^[A-ZÑ&]{3}\d{6}[A-Z0-9]{3}$")

#: Generic RFCs defined by SAT for público en general / foreigners. Their
#: trailing homoclave does not follow the check-digit algorithm, so every
#: validator special-cases them (SAT practice).
GENERIC_RFC_NACIONAL = "XAXX010101000"
GENERIC_RFC_EXTRANJERO = "XEXX010101000"
GENERIC_RFCS = {GENERIC_RFC_NACIONAL, GENERIC_RFC_EXTRANJERO}


def _char_value(c: str) -> int:
    if "0" <= c <= "9":
        return ord(c) - 48
    if "A" <= c <= "N":
        return ord(c) - 55          # A=10 .. N=23
    if c == "&":
        return 24
    if "O" <= c <= "Z":
        return ord(c) - 54          # O=25 .. Z=36
    if c == " ":
        return 37
    if c == "Ñ":
        return 38
    raise ValueError(f"character {c!r} not in the RFC alphabet")


def rfc_check_digit(base12: str) -> str:
    """SAT dígito verificador over the 12-char weighted string.

    Persona-moral RFCs (11-char base before the digit) are space-padded on
    the left, per SAT's published algorithm.
    """
    s = sum(_char_value(c) * (13 - i) for i, c in enumerate(base12))
    mod = s % 11
    if mod == 0:
        return "0"
    d = 11 - mod
    if d == 10:
        return "A"
    if d == 11:
        return "0"
    return str(d)


def _date_part_ok(yymmdd: str) -> bool:
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return False
    try:
        date(2000 + yy if yy <= 29 else 1900 + yy, mm, dd)
    except ValueError:
        return False
    return True


def validate_rfc(rfc) -> bool:
    """Format + embedded-date + check-digit validation for an RFC.

    Accepts surrounding whitespace / lowercase input (normalised first).
    Generic SAT RFCs validate by definition.
    """
    if not rfc or not isinstance(rfc, str):
        return False
    r = rfc.strip().upper()
    if r in GENERIC_RFCS:
        return True
    if _RFC_FISICA.match(r):
        base, datepart = r[:12], r[4:10]
    elif _RFC_MORAL.match(r):
        base, datepart = " " + r[:11], r[3:9]
    else:
        return False
    if not _date_part_ok(datepart):
        return False
    return rfc_check_digit(base) == r[-1]


# ---------------- Catalogues (playbook §3.2) ----------------
REGIMEN_FISCAL = {
    "601": "General de Ley Personas Morales",
    "603": "Personas Morales con Fines no Lucrativos",
    "605": "Sueldos y Salarios e Ingresos Asimilados a Salarios",
    "606": "Arrendamiento",
    "607": "Régimen de Enajenación o Adquisición de Bienes",
    "608": "Demás ingresos",
    "610": "Residentes en el Extranjero sin Establecimiento Permanente en México",
    "611": "Ingresos por Dividendos (socios y accionistas)",
    "612": "Personas Físicas con Actividades Empresariales y Profesionales",
    "614": "Ingresos por Intereses",
    "615": "Régimen de los Ingresos por Obtención de Préstamos",
    "616": "Sin Obligaciones Fiscales",
    "620": "Sociedades Cooperativas de Producción que optan por Diferir sus Ingresos",
    "621": "Incorporación Fiscal",
    "622": "Actividades Agrícolas, Ganaderas, Silvícolas y Pesqueras",
    "623": "Opcional para Grupos de Sociedades",
    "624": "Coordinados",
    "625": "RESICO — Personas Morales",
    "626": "RESICO — Personas Físicas",
}

#: The uso CFDI values trades actually use (full SAT catalogue is longer;
#: this is the playbook §3.2 working set).
USO_CFDI = {
    "G01": "Adquisición de mercancías",
    "G03": "Gastos en general",
    "I04": "Equipo de computo y accesorios",
    "D01": "Honorarios médicos, dentales y gastos hospitalarios",
    "D03": "Gastos funerales",
    "CP01": "Pagos",
    "S01": "Sin efectos fiscales",
}

FORMA_PAGO = {
    "01": "Efectivo",
    "02": "Cheque nominativo",
    "03": "Transferencia electrónica de fondos",
    "04": "Tarjeta de crédito",
    "28": "Tarjeta de débito",
    "99": "Por definir",
}

METODO_PAGO = {"PUE", "PPD"}

MOTIVOS_CANCELACION = {
    "01": "Comprobante emitido con errores con relación",
    "02": "Comprobante emitido con errores sin relación",
    "03": "No se llevó a cabo la operación",
    "04": "Operación nominativa relacionada en la factura global",
}

#: Defaults from the playbook §3.2 — validated against the SAT catalogue
#: with pilot shops' accountants before GA; per-line overrides exist.
DEFAULT_CLAVE_PROD_SERV = "01010101"   # SAT generic
DEFAULT_CLAVE_UNIDAD = "E48"           # Unidad de servicio
PAGO_CLAVE_PROD_SERV = "84111506"      # the mandatory concept on tipo P
PAGO_CLAVE_UNIDAD = "ACT"              # Actividad

IVA_RATE = 0.16


# ---------------- Razón social exact-match handling ----------------
def normalize_razon_social(name) -> str:
    """Comparison form for razón social: SAT validates the name jointly
    with RFC + CP against the constancia, and strict PAC validation
    rejects 'S.A. de C.V.' omissions and stray whitespace. We store the
    registered string verbatim and compare on this normalised form."""
    if not name:
        return ""
    return " ".join(str(name).split()).upper()


def razon_social_matches(a, b) -> bool:
    return normalize_razon_social(a) == normalize_razon_social(b)


# ---------------- Profile / receptor validation ----------------
_CP_RE = re.compile(r"^\d{5}$")


def _cp_ok(cp) -> bool:
    return bool(cp and _CP_RE.match(str(cp).strip()))


def validate_emisor_profile(profile) -> list:
    """Errors for a shop (emisor) fiscal profile; [] means stampable."""
    p = profile or {}
    errs = []
    if not validate_rfc(p.get("rfc")):
        errs.append("rfc: not a valid RFC (format or check digit)")
    if not normalize_razon_social(p.get("razon_social")):
        errs.append("razon_social: required, exactly as on the Constancia de Situación Fiscal")
    if str(p.get("regimen_fiscal") or "") not in REGIMEN_FISCAL:
        errs.append("regimen_fiscal: must be a SAT catalogue code (601…626)")
    if not _cp_ok(p.get("cp_fiscal")):
        errs.append("cp_fiscal: must be the 5-digit domicilio fiscal ZIP")
    return errs


def validate_receptor(receptor, *, for_pago=False) -> list:
    """Errors for a customer (receptor) fiscal block; [] means stampable."""
    r = receptor or {}
    errs = []
    if not validate_rfc(r.get("rfc")):
        errs.append("receptor.rfc: not a valid RFC (format or check digit)")
    if not normalize_razon_social(r.get("razon_social")):
        errs.append("receptor.razon_social: required, exactly as on their constancia")
    if not _cp_ok(r.get("cp_fiscal")):
        errs.append("receptor.cp_fiscal: domicilio fiscal ZIP is mandatory in CFDI 4.0")
    if str(r.get("regimen_fiscal") or "") not in REGIMEN_FISCAL:
        errs.append("receptor.regimen_fiscal: must be a SAT catalogue code (601…626)")
    uso = str(r.get("uso_cfdi") or "")
    if uso not in USO_CFDI:
        errs.append("receptor.uso_cfdi: must be a catalogue code (G03, S01, CP01, …)")
    if for_pago and uso != "CP01":
        errs.append("receptor.uso_cfdi: a Complemento de Pago requires CP01")
    return errs


def validate_forma_metodo(forma_pago, metodo_pago) -> list:
    errs = []
    if str(forma_pago or "") not in FORMA_PAGO:
        errs.append("forma_pago: must be a SAT catalogue code (01, 02, 03, 04, 28, 99)")
    if str(metodo_pago or "") not in METODO_PAGO:
        errs.append("metodo_pago: must be PUE or PPD")
    if metodo_pago == "PUE" and forma_pago == "99":
        errs.append("forma_pago 99 (Por definir) is only valid with metodo_pago PPD")
    return errs


def retention_until(stamped_date: str) -> str:
    """Mexican law requires 5-year retention of stamped XML (playbook §3.2).
    stamped_date is an ISO date; returns the ISO date 5 years later."""
    y, m, d = (int(x) for x in stamped_date[:10].split("-"))
    try:
        return date(y + 5, m, d).isoformat()
    except ValueError:  # Feb 29 -> Feb 28 five years on
        return date(y + 5, m, 28).isoformat()
