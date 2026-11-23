"""Prüfregeln: USt-IdNr., GLN, Leitweg-ID, E-Mail, Namensvergleich.

Die Texte der Meldungen müssen exakt mit den Schlüsseln in der Seite (ERKL, FEHLER, HINW, DUPW)
übereinstimmen, sonst fehlen dort Erklärung und Gewichtung.
"""
from __future__ import annotations

import re
import unicodedata

from stdnum import util as stdnum_util
from stdnum.eu import vat as eu_vat
from stdnum.exceptions import InvalidChecksum, ValidationError

EU_CC = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT",
         "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}
VAT_PREFIX_TO_CC = {"EL": "GR"}

# ----------------------------------------------------------------- Meldungen
F_STR, F_PLZ, F_ORT, F_LAND = "Straße fehlt", "PLZ fehlt", "Ort fehlt", "Land fehlt"
F_MAIL_FEHLT = "E-Mail fehlt"
F_MAIL_UNG = "E-Mail ungültig"
F_MAIL_INV = "E-Mail enthält unsichtbare Zeichen (Leerzeichen/Zeilenumbruch)"
F_MAIL_MEHR = "Mehrere E-Mail-Adressen in einem Feld"
F_VAT_EU = "USt-IdNr. fehlt (EU-Ausland)"
F_VAT_PZ = "USt-IdNr. Prüfziffer falsch"
F_VAT_FMT = "USt-IdNr. Format ungültig"
F_KEIN_FMT = "Kein E-Rechnungsformat"
F_BT10 = "XRechnung: Käuferreferenz BT-10 (Feld Referenz am Unternehmen) fehlt"
F_LW_FEHLT = "B2G: Leitweg-ID fehlt (Peppol-Endpoint)"
F_LW_FMT = "B2G: Leitweg-ID hat falsches Format"
F_LW_PZ = "B2G: Leitweg-ID Prüfziffer falsch"
F_LW_ART = "Peppol-Art falsch: Leitweg-ID unter anderer Art eingetragen"
F_B2G_FMT = "B2G: Format muss XRechnung sein"
F_PEP_FEHLT = "Peppol: EAS/Endpoint fehlt"
F_PEP_VERIF = "Peppol: Empfänger nicht verifiziert"
F_PEP_9958 = "Peppol: EAS 9958 ist veraltet"
F_PEP_9930 = "Peppol 9930: Endpoint ist keine gültige DE-USt-IdNr."
F_GLN_13 = "GLN: muss 13 Ziffern haben"
F_GLN_PZ = "GLN: Prüfziffer falsch"
F_GLNN_PEP = "GLN im Namen: als Peppol-ID „EAN-Standortcode“ eintragen"
F_GLNN_PZ = "GLN im Namen: Prüfziffer falsch"
F_GLN_ZF = "Handelskunde mit GLN: Format muss „Deutschland (ZUGFeRD)“ sein"

H_GLNN_BC = "GLN im Namen: auch im Feld Barcode eintragen"
H_VAT = "USt-IdNr. fehlt (empfohlen)"
H_VORSCHLAG = "Format nur Odoo-Vorschlag (nicht bewusst gesetzt)"
H_SEND = "Versandart nicht gesetzt (Standard)"
H_BC_PZ = "GLN im Barcode: Prüfziffer falsch"
H_BC_DIFF = "GLN in Barcode und Peppol-Endpoint unterschiedlich"
H_LW_REF = "Leitweg-ID im Feld Referenz: Behörde? Peppol-Art „Leitweg-ID“ prüfen"
H_PRIVAT = "Privatperson: E-Rechnung nicht erforderlich"

D_VAT, D_IBAN, D_PEPPOL, D_REF = "gleiche USt-IdNr.", "gleiche IBAN", "gleiche Peppol-ID", "gleiche Referenz/Kundennr."
D_NAMEPLZ, D_MAIL, D_NAME, D_TEL = "gleicher Name + PLZ", "gleiche E-Mail", "gleicher Name", "gleiche Telefonnr."
DUP_STARK = {D_VAT, D_IBAN, D_PEPPOL, D_NAMEPLZ}
DUP_MITTEL = {D_REF, D_MAIL}

# ----------------------------------------------------------------- Hilfen
INVISIBLE_RE = re.compile(r"[\s ​‌‍⁠﻿]")
ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿]")
MAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\"]+@[^@\s,;<>()\[\]\"]+\.[A-Za-z]{2,}$")
LW_RE = re.compile(r"^[0-9]{2,12}(-[A-Za-z0-9]{1,30})?-[0-9]{2}$")
GLN_IN_NAME_RE = re.compile(r"(?:GLN|ILN)\s*(?:Nr\.?|Nummer)?\s*[:#.\-]?\s*((?:\d[\s.]?){12,14}\d)", re.I)


def s(v) -> str:
    return "" if v is None else str(v)


def gln_valid(code: str) -> bool:
    """GS1-Prüfziffer (Modulo 10, Gewichte 3/1 von rechts)."""
    if not re.fullmatch(r"\d{13}", code or ""):
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[12])


def leitweg_checksum_ok(lw: str) -> bool:
    """Leitweg-ID: Prüfziffer nach ISO/IEC 7064 MOD 97-10 (wie IBAN), Buchstaben A=10 … Z=35."""
    lw = lw.strip().upper()
    if not LW_RE.fullmatch(lw):
        return False
    body, pz = lw.rsplit("-", 1)
    digits = "".join(str(int(ch, 36)) for ch in body.replace("-", ""))
    return 98 - (int(digits + "00") % 97) == int(pz)


def normalize_vat(v: str) -> str:
    return re.sub(r"[\s.\-/]", "", s(v)).upper()


def vat_check(v: str) -> str | None:
    """None = gültig, sonst Fehlermeldung. Prüft wie Odoo (python-stdnum)."""
    raw = s(v).strip()
    n = normalize_vat(raw)
    if not n or n == "/":
        return None
    cc = n[:2]
    if not cc.isalpha():
        return F_VAT_FMT
    if cc == "DE" and not re.fullmatch(r"DE\d{9}", n):
        return F_VAT_FMT
    try:
        if VAT_PREFIX_TO_CC.get(cc, cc) in EU_CC or cc == "EL":
            eu_vat.validate(n)
        else:
            mod = stdnum_util.get_cc_module(cc.lower(), "vat")
            if mod is None:
                return None
            mod.validate(n)
    except InvalidChecksum:
        return F_VAT_PZ
    except ValidationError:
        return F_VAT_FMT
    except Exception:  # unbekanntes Land o. ä.: nicht prüfen
        return None
    return None


def vat_valid(v: str) -> bool:
    return bool(normalize_vat(v)) and vat_check(v) is None


def mail_problem(email: str) -> str | None:
    e = s(email)
    if not e.strip():
        return F_MAIL_FEHLT
    if e.count("@") > 1 or re.search(r"[,;]", e) or re.search(r"\S+@\S+\s+\S+@\S+", e):
        return F_MAIL_MEHR
    if INVISIBLE_RE.search(e):
        return F_MAIL_INV
    if not MAIL_RE.fullmatch(e):
        return F_MAIL_UNG
    return None


def mail_clean(email: str) -> str | None:
    """Nur 100 % sichere Bereinigung: unsichtbare Zeichen, Rand-Leerzeichen, Leerzeichen am @."""
    e = s(email)
    if not e:
        return None
    c = ZERO_WIDTH_RE.sub("", e).replace(" ", " ").strip()
    c = re.sub(r"\s*@\s*", "@", c)
    if c == e or re.search(r"\s", c) or c.count("@") != 1 or not MAIL_RE.fullmatch(c):
        return None
    return c


def gln_from_name(name: str) -> str | None:
    m = GLN_IN_NAME_RE.search(s(name))
    if not m:
        return None
    d = re.sub(r"\D", "", m.group(1))
    return d if len(d) == 13 else None


_LEGAL = r"\b(gmbh|mbh|co|kg|kgaa|ag|ug|haftungsbeschraenkt|ohg|gbr|ek|e\s?k|ev|e\s?v|eg|se|ltd|limited|inc|llc|sarl|sas|sa|bv|nv|spa|srl|und|and|the|der|die|das)\b"


def norm_name(name: str) -> str:
    n = s(name).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        n = n.replace(a, b)
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = n.replace("&", " ").replace("+", " ")
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = re.sub(_LEGAL, " ", n)
    return re.sub(r"\s+", "", n)


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", s(p))
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("49"):
        d = d[2:]
    d = d.lstrip("0")
    return d if len(d) >= 6 else ""


COMPANY_HINT_RE = re.compile(
    r"\b(gmbh|mbh|ag|kg|ug|ohg|gbr|e\.?\s?k\.?|e\.?\s?v\.?|eg|ltd|inc|llc|sarl|s\.?a\.?|b\.?v\.?|"
    r"gemeinde|stadt|landkreis|kreis|amt|verwaltung|bundes|landes|ministerium|schule|universit|hochschule|"
    r"klinik|krankenhaus|hotel|gasthof|restaurant|caf[eé]|b[äa]ckerei|metzgerei|markt|edeka|rewe|kaufland|"
    r"genossenschaft|stiftung|verein|verband|praxis|kanzlei|apotheke|betrieb|werk|handel|service|gruppe|holding)\b",
    re.I)
