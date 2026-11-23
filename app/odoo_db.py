"""Liest Kontakte, Rechnungen und Verlauf direkt aus der Odoo-18-Datenbank (nur lesend)
und berechnet daraus alle Spalten, die die Seite braucht.

Lesend abgesichert dreifach:
  1. eigener PostgreSQL-Benutzer nur mit SELECT-Rechten (sql/readonly_user.sql),
  2. Verbindung mit default_transaction_read_only=on,
  3. jede Transaktion wird als READ ONLY gestartet und am Ende zurückgerollt.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from . import checks as C
from . import config

log = logging.getLogger("erechnung.db")

PEPPOL_DEFAULT_COUNTRIES = {"AT", "BE", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "IE", "IS",
                            "LT", "LU", "LV", "MT", "NL", "NO", "SE", "SI"}
# Odoo 18, account_edi_ubl_cii._get_ubl_cii_formats_info(): Länder je Format (+ sequence; Standard 100)
UBL_FORMATS = {"ubl_bis3": (PEPPOL_DEFAULT_COUNTRIES, 200), "xrechnung": ({"DE"}, 200), "ubl_a_nz": ({"NZ", "AU"}, 100),
               "nlcius": ({"NL"}, 100), "ubl_sg": ({"SG"}, 100), "facturx": ({"FR"}, 100), "zugferd": ({"DE"}, 100)}


# =================================================================== Verbindung
def connect() -> psycopg.Connection:
    conn = psycopg.connect(
        host=config.PGHOST, port=config.PGPORT, dbname=config.PGDATABASE,
        user=config.PGUSER, password=config.PGPASSWORD, sslmode=config.PGSSLMODE,
        options=f"-c default_transaction_read_only=on -c statement_timeout={config.STATEMENT_TIMEOUT_MS}",
        application_name="erechnung-check (read-only)", connect_timeout=10, row_factory=dict_row,
    )
    conn.read_only = True
    return conn


class Schema:
    """Welche Tabellen/Spalten gibt es? (Module wie hr oder account_peppol sind optional.)"""

    def __init__(self, cur):
        cur.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                       WHERE table_schema = current_schema()""")
        self.cols: dict[str, dict[str, str]] = defaultdict(dict)
        for r in cur.fetchall():
            self.cols[r["table_name"]][r["column_name"]] = r["data_type"]

    def has(self, table, col=None):
        return table in self.cols and (col is None or col in self.cols[table])

    def typ(self, table, col):
        return self.cols.get(table, {}).get(col)


def _tr(expr: str) -> str:
    """Übersetzbares Feld (jsonb) als Text, deutsch bevorzugt."""
    return f"COALESCE({expr}->>'de_DE', {expr}->>'en_US', ({expr})::text)"


# =================================================================== Formatierung
_TZ = ZoneInfo(config.TZ)


def fdt(v) -> str:
    """UTC-Zeitstempel aus Odoo -> lokale Zeit 'YYYY-MM-DD HH:MM'."""
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc).astimezone(_TZ).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _clean_line(v) -> str:
    return C.s(v).replace("\r", "").replace("\n", "⏎").replace(" | ", " ¦ ")


def _clean_list(v) -> str:
    return C.s(v).replace(";", ",").replace("\n", " ").strip()


def html_to_text(h) -> str:
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", C.s(h), flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# =================================================================== Laden
def load(conn) -> dict:
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        S = Schema(cur)
        cid = _company_id(cur)
        data = _query_all(cur, S, cid)
    conn.rollback()
    rows = _build_rows(data, cid)
    cols = list(COLS)
    meta = {
        "generated": datetime.now(_TZ).strftime("%d.%m.%Y %H:%M"),
        "database": f"{config.PGDATABASE} · Firma: {data['company_name']}",
        "odoo_url": config.ODOO_URL,
        "quelle": "Odoo-Datenbank (live, nur lesend)",
        "kontrolle_ab": config.KONTROLLE_AB,
        "dauer_s": round(time.monotonic() - t0, 2),
    }
    return {"meta": meta, "cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


def _company_id(cur) -> int:
    if config.ODOO_COMPANY_ID:
        return config.ODOO_COMPANY_ID
    cur.execute("""SELECT c.id FROM res_company c JOIN res_partner p ON p.id = c.partner_id
                   LEFT JOIN res_country co ON co.id = p.country_id
                   ORDER BY (co.code = 'DE') DESC NULLS LAST, c.id LIMIT 1""")
    return cur.fetchone()["id"]


def _query_all(cur, S: Schema, cid: int) -> dict:
    ck = f"'{int(cid)}'"

    def col(c, alias="p", table="res_partner"):
        t = S.typ(table, c)
        if t is None:
            return "NULL"
        if t == "jsonb":  # firmenabhängiges Feld (Odoo 18: jsonb {firma: wert})
            return f"{alias}.{c}->>{ck}"
        return f"{alias}.{c}"

    def m2o_name(c, table, alias):
        if not S.has("res_partner", c) or not S.has(table):
            return "NULL", ""
        name = _tr(f"{alias}.name") if S.typ(table, "name") == "jsonb" else f"{alias}.name"
        idexpr = col(c)
        if S.typ("res_partner", c) == "jsonb":
            idexpr = f"NULLIF({idexpr}, '')::int"
        return name, f" LEFT JOIN {table} {alias} ON {alias}.id = {idexpr}"

    acc_code = "acc.code_store->>" + ck if S.typ("account_account", "code_store") == "jsonb" else "acc.code"
    pt_name, pt_join = m2o_name("property_payment_term_id", "account_payment_term", "pt")
    spt_name, spt_join = m2o_name("property_supplier_payment_term_id", "account_payment_term", "spt")
    fp_name, fp_join = m2o_name("property_account_position_id", "account_fiscal_position", "fp")
    recv_join = pay_join = ""
    recv = pay = "NULL"
    if S.has("account_account") and S.has("res_partner", "property_account_receivable_id"):
        recv = acc_code
        recv_join = f" LEFT JOIN account_account acc ON acc.id = NULLIF({col('property_account_receivable_id')}, '')::int"
        pay = acc_code.replace("acc.", "acc2.")
        pay_join = f" LEFT JOIN account_account acc2 ON acc2.id = NULLIF({col('property_account_payable_id')}, '')::int"
    title = _tr("ti.name") if S.typ("res_partner_title", "name") == "jsonb" else "ti.name"
    title_join = " LEFT JOIN res_partner_title ti ON ti.id = p.title" if S.has("res_partner", "title") else ""
    if not title_join:
        title = "NULL"

    where = "TRUE" if config.INCLUDE_INACTIVE else "p.active"
    cur.execute(f"""
        SELECT p.id, p.name, p.complete_name, p.parent_id, p.commercial_partner_id, p.type, p.is_company, p.active,
               p.street, p.street2, p.zip, p.city, st.name AS state_name, co.code AS country_code,
               {_tr('co.name')} AS country_name, p.email, p.phone, {col('mobile')} AS mobile, p.website, p.lang,
               p.vat, p.ref, {col('company_registry')} AS company_registry, p.function, {title} AS title,
               {col('barcode')} AS barcode, {col('invoice_edi_format_store')} AS fmt_store,
               {col('invoice_sending_method')} AS send_method, {col('peppol_eas')} AS peppol_eas,
               {col('peppol_endpoint')} AS peppol_endpoint, {col('peppol_verification_state')} AS peppol_state,
               {col('customer_rank')} AS customer_rank, {col('supplier_rank')} AS supplier_rank,
               p.user_id, p.create_date, p.create_uid, p.write_date, p.write_uid, p.comment,
               {pt_name} AS pay_term, {spt_name} AS sup_pay_term, {fp_name} AS fiscal_pos,
               {recv} AS acc_receivable, {pay} AS acc_payable
          FROM res_partner p
          LEFT JOIN res_country co ON co.id = p.country_id
          LEFT JOIN res_country_state st ON st.id = p.state_id
          {title_join}{pt_join}{spt_join}{fp_join}{recv_join}{pay_join}
         WHERE {where}
         ORDER BY p.id""")
    partners = cur.fetchall()
    d: dict = {"partners": partners}

    cur.execute("SELECT c.id, c.name, c.partner_id FROM res_company c ORDER BY c.id")
    comps = cur.fetchall()
    d["company_partner_ids"] = {c["partner_id"] for c in comps}
    d["company_name"] = next((c["name"] for c in comps if c["id"] == cid), str(cid))

    cur.execute("""SELECT u.id, u.partner_id, u.share, u.login, p.name FROM res_users u
                   JOIN res_partner p ON p.id = u.partner_id""")
    users = cur.fetchall()
    d["users"] = {u["id"]: (u["name"] or u["login"]) for u in users}
    d["internal_partner_ids"] = {u["partner_id"] for u in users if not u["share"]}
    d["user_partner_ids"] = {u["partner_id"] for u in users}
    if S.has("hr_employee", "work_contact_id"):
        cur.execute("SELECT work_contact_id FROM hr_employee WHERE work_contact_id IS NOT NULL")
        d["internal_partner_ids"] |= {r["work_contact_id"] for r in cur.fetchall()}

    cur.execute("""SELECT DISTINCT ON (res_id) res_id, module || '.' || name AS xid FROM ir_model_data
                   WHERE model = 'res.partner' ORDER BY res_id, (module = '__export__') DESC, id""")
    d["xids"] = {r["res_id"]: r["xid"] for r in cur.fetchall()}

    d["banks"] = defaultdict(list)
    if S.has("res_partner_bank"):
        act = "WHERE active" if S.has("res_partner_bank", "active") else ""
        san = "sanitized_acc_number" if S.has("res_partner_bank", "sanitized_acc_number") else "acc_number"
        cur.execute(f"SELECT partner_id, acc_number, {san} AS san FROM res_partner_bank {act} ORDER BY id")
        for r in cur.fetchall():
            d["banks"][r["partner_id"]].append((r["acc_number"], (r["san"] or "").upper()))

    d["tags"] = defaultdict(list)
    if S.has("res_partner_res_partner_category_rel"):
        nm = _tr("c.name") if S.typ("res_partner_category", "name") == "jsonb" else "c.name"
        cur.execute(f"""SELECT r.partner_id, {nm} AS name FROM res_partner_res_partner_category_rel r
                        JOIN res_partner_category c ON c.id = r.category_id ORDER BY 2""")
        for r in cur.fetchall():
            d["tags"][r["partner_id"]].append(r["name"])

    # --- Rechnungen ---
    d["inv"], d["last_inv"], d["xml12"] = {}, {}, {}
    if S.has("account_move"):
        cur.execute("""SELECT partner_id, count(*) AS n, max(invoice_date) AS last FROM account_move
                       WHERE move_type IN ('out_invoice', 'out_refund') AND state = 'posted' GROUP BY partner_id""")
        d["inv"] = {r["partner_id"]: r for r in cur.fetchall()}
        pms = "m.peppol_move_state" if S.has("account_move", "peppol_move_state") else "NULL"
        xml_cond = """a.res_model = 'account.move' AND a.res_id = m.id
                      AND (a.res_field = 'ubl_cii_xml_file' OR (a.res_field IS NULL AND a.name ILIKE '%%.xml'))"""
        cur.execute(f"""
            SELECT DISTINCT ON (m.commercial_partner_id) m.commercial_partner_id AS cp, m.id, m.name,
                   m.invoice_date, {pms} AS peppol, x.name AS xml_name, x.create_date AS xml_date
              FROM account_move m
              LEFT JOIN LATERAL (SELECT a.name, a.create_date FROM ir_attachment a WHERE {xml_cond}
                                 ORDER BY a.create_date DESC LIMIT 1) x ON TRUE
             WHERE m.move_type = 'out_invoice' AND m.state = 'posted'
             ORDER BY m.commercial_partner_id, m.invoice_date DESC NULLS LAST, m.id DESC""")
        d["last_inv"] = {r["cp"]: r for r in cur.fetchall()}
        cur.execute(f"""
            SELECT m.commercial_partner_id AS cp, count(*) AS n FROM account_move m
             WHERE m.move_type = 'out_invoice' AND m.state = 'posted'
               AND m.invoice_date >= (CURRENT_DATE - INTERVAL '12 months')
               AND EXISTS (SELECT 1 FROM ir_attachment a WHERE {xml_cond})
             GROUP BY 1""")
        d["xml12"] = {r["cp"]: r["n"] for r in cur.fetchall()}

    # --- Verlauf (mail.tracking.value) ---
    d["tracking"] = defaultdict(list)
    if S.has("mail_tracking_value"):
        def val(side):
            parts = [f"v.{side}_value_{t}" + ("::text" if t != "char" and t != "text" else "")
                     for t in ("char", "text", "datetime", "float", "integer")
                     if S.has("mail_tracking_value", f"{side}_value_{t}")]
            return "COALESCE(" + ", ".join(parts) + ")" if parts else "NULL"
        flabel = _tr("f.field_description") if S.typ("ir_model_fields", "field_description") == "jsonb" else "f.field_description"
        finfo = "v.field_info->>'desc'" if S.has("mail_tracking_value", "field_info") else "NULL"
        cur.execute(f"""
            SELECT m.res_id, m.date, m.create_uid, m.author_id, au.name AS author_name, f.name AS fname,
                   COALESCE({flabel}, {finfo}, f.name) AS flabel, {val('old')} AS old, {val('new')} AS new
              FROM mail_tracking_value v
              JOIN mail_message m ON m.id = v.mail_message_id
              LEFT JOIN ir_model_fields f ON f.id = v.field_id
              LEFT JOIN res_partner au ON au.id = m.author_id
             WHERE m.model = 'res.partner'
             ORDER BY m.res_id, m.date, v.id""")
        for r in cur.fetchall():
            d["tracking"][r["res_id"]].append(r)

    # --- Prüfnotizen (#geprüft / #falsch) ---
    d["notes"] = defaultdict(list)
    cur.execute("""
        SELECT m.res_id, m.date, m.create_uid, au.name AS author_name, m.body
          FROM mail_message m LEFT JOIN res_partner au ON au.id = m.author_id
         WHERE m.model = 'res.partner' AND m.message_type = 'comment' AND m.date >= %(ab)s
           AND (m.body ILIKE '%%#gepr%%' OR m.body ILIKE '%%#falsch%%')
         ORDER BY m.res_id, m.date""", {"ab": config.KONTROLLE_AB})
    for r in cur.fetchall():
        d["notes"][r["res_id"]].append(r)
    return d


# =================================================================== Spalten
COLS = [
    "odoo_id", "externe_id", "name", "ist_unternehmen", "adresstyp", "zugehoeriges_unternehmen", "rechnungs_unternehmen",
    "kundenart", "referenz", "anrede", "stelle", "rechnungs_unternehmen_id", "ist_rechnungsunternehmen",
    "strasse", "strasse2", "plz", "stadt", "bundesland", "land", "land_code",
    "email", "telefon", "mobil", "webseite", "sprache",
    "e_rechnungsformat", "e_rechnungsformat_herkunft", "rechnungsversand", "kaeuferreferenz_bt10",
    "peppol_eas", "peppol_endpoint", "peppol_status", "leitweg_id_peppol", "gln", "gln_quelle", "gln_im_namen", "barcode",
    "e_rechnung_bereit", "e_rechnung_fehler", "e_rechnung_hinweise", "rechnungsrelevant",
    "ust_idnr", "handelsregister", "zahlungsbedingung_kunde", "zahlungsbedingung_lieferant", "steuerzuordnung",
    "debitorenkonto", "kreditorenkonto", "bankkonten_iban",
    "rechnungen_anzahl", "rechnungen_letzte", "erech_letzte_rechnung", "erech_letzte_datum", "erech_letzte_mit_xml",
    "erech_xml_datei", "erech_xml_erstellt", "erech_peppol", "erech_anzahl_xml",
    "kontrolle_status", "kontrolle_geaendert_am", "kontrolle_aenderer", "kontrolle_pruefer", "kontrolle_am",
    "kontrolle_notiz", "kontrolle_aenderungen",
    "dublette_verdacht", "dublette_gruende", "dublette_mit", "dublette_gruppe", "dublette_json", "hat_benutzer", "intern",
    "vertriebsmitarbeiter", "kundenrang", "lieferantenrang", "schlagwoerter", "notizen", "aktiv",
    "angelegt_am", "angelegt_von", "geaendert_am", "geaendert_von", "anzahl_aenderungen", "letzte_aenderungen",
    "korr_email", "korr_vat", "korr_peppol_endpoint", "korr_peppol_eas", "korr_gln_peppol",
]


def _suggested_format(country_code: str, peppol_eas: str) -> str:
    """Nachbau von res.partner._get_suggested_ubl_cii_edi_format() aus Odoo 18."""
    fm = [k for k, (cc, _) in UBL_FORMATS.items() if country_code in cc]
    if not fm:
        return ""
    if len(fm) == 1:
        return fm[0]
    if peppol_eas == "0204":
        return "xrechnung"
    return min(fm, key=lambda k: UBL_FORMATS[k][1])


def _deduce_cc(p) -> str:
    vat = C.s(p["vat"]).strip()
    if vat[:2].isalpha():
        return {"EL": "GR"}.get(vat[:2].upper(), vat[:2].upper())
    return p["country_code"] or ""


def _kundenart(cp) -> str:
    eas = C.s(cp["peppol_eas"])
    ep = C.s(cp["peppol_endpoint"]).strip()
    if eas == "0204" or (ep and C.LW_RE.fullmatch(ep) and C.leitweg_checksum_ok(ep)):
        return "B2G Behörde"
    has_vat = bool(C.normalize_vat(cp["vat"]).strip("/"))
    if not cp["is_company"] and not has_vat and not cp["company_registry"] \
            and not C.COMPANY_HINT_RE.search(C.s(cp["name"])) and not C.gln_from_name(cp["name"]):
        return "B2C Privatperson"
    cc = cp["country_code"] or ""
    if not cc:
        return "B2B (Land fehlt)"
    if cc == "DE":
        return "B2B Deutschland"
    if cc in C.EU_CC:
        return "B2B EU-Ausland"
    return "B2B Drittland"


def _build_rows(d: dict, cid: int) -> list[dict]:
    P = {p["id"]: p for p in d["partners"]}
    users = d["users"]
    uname = lambda uid: users.get(uid, f"Benutzer {uid}") if uid else ""
    excluded_roots = d["company_partner_ids"]
    internal = d["internal_partner_ids"]
    children = defaultdict(list)
    for p in d["partners"]:
        if p["parent_id"]:
            children[p["parent_id"]].append(p["id"])

    def is_internal(p):
        return (p["id"] in internal or p["id"] in excluded_roots or p["commercial_partner_id"] in excluded_roots)

    # ---- Relevanz: welche Kontakte werden geprüft? (PRUEF_UMFANG) ----
    rel = {}
    alle = config.PRUEF_UMFANG != "rechnungsempfaenger"
    for p in d["partners"]:
        if is_internal(p) or not p["active"]:
            rel[p["id"]] = False
            continue
        if alle:
            rel[p["id"]] = True
            continue
        has_inv = p["id"] in d["inv"]
        if p["parent_id"]:
            rel[p["id"]] = p["type"] == "invoice" or has_inv
        else:
            supplier_only = (p["supplier_rank"] or 0) > 0 and not (p["customer_rank"] or 0) and not has_inv \
                and not any(c in d["inv"] for c in children[p["id"]])
            rel[p["id"]] = not supplier_only
    # Unternehmen ist relevant, sobald ein Unterkontakt relevant ist
    for pid, ok in list(rel.items()):
        if ok:
            cp = P[pid]["commercial_partner_id"]
            if cp in P and not is_internal(P[cp]):
                rel[cp] = True

    dup = _duplicates(d, P, is_internal)
    mass = _mass_changes(d)
    rows = []
    kab = config.KONTROLLE_AB
    for p in d["partners"]:
        cp = P.get(p["commercial_partner_id"]) or p
        is_cp = cp["id"] == p["id"]
        r: dict = {}
        r["odoo_id"] = p["id"]
        r["externe_id"] = d["xids"].get(p["id"], "")
        r["name"] = p["name"] or p["complete_name"] or ""
        r["ist_unternehmen"] = "ja" if p["is_company"] else "nein"
        r["adresstyp"] = p["type"] or ""
        r["zugehoeriges_unternehmen"] = (P[p["parent_id"]]["name"] if p["parent_id"] in P else "") or ""
        r["rechnungs_unternehmen"] = cp["name"] or ""
        r["rechnungs_unternehmen_id"] = cp["id"]
        r["ist_rechnungsunternehmen"] = "ja" if is_cp else "nein"
        r["hat_benutzer"] = "ja" if p["id"] in d["user_partner_ids"] else ""
        r["intern"] = "ja" if is_internal(p) else ""
        r["kundenart"] = _kundenart(cp)
        r["referenz"] = p["ref"] or ""
        r["anrede"] = p["title"] or ""
        r["stelle"] = p["function"] or ""
        r["strasse"], r["strasse2"], r["plz"], r["stadt"] = p["street"] or "", p["street2"] or "", p["zip"] or "", p["city"] or ""
        r["bundesland"], r["land"], r["land_code"] = p["state_name"] or "", p["country_name"] or "", p["country_code"] or ""
        r["email"], r["telefon"], r["mobil"] = p["email"] or "", p["phone"] or "", p["mobile"] or ""
        r["webseite"], r["sprache"] = p["website"] or "", p["lang"] or ""

        # ---- E-Rechnung: Werte liest Odoo immer vom Unternehmen (commercial partner) ----
        store = C.s(cp["fmt_store"])
        eas, ep = C.s(cp["peppol_eas"]), C.s(cp["peppol_endpoint"]).strip()
        if store == "none":
            fmt, herkunft = "", "bewusst: kein Format"
        elif store:
            fmt, herkunft = store, "bewusst gewählt"
        else:
            fmt = _suggested_format(_deduce_cc(cp), eas)
            herkunft = "Odoo-Vorschlag (in Odoo steht „XML-Format“)" if fmt else ""
        r["e_rechnungsformat"], r["e_rechnungsformat_herkunft"] = fmt, herkunft
        send = C.s(cp["send_method"])
        r["rechnungsversand"] = send or "(Standard)"
        r["kaeuferreferenz_bt10"] = cp["ref"] or ""
        r["peppol_eas"], r["peppol_endpoint"], r["peppol_status"] = eas, ep, C.s(cp["peppol_state"])
        r["leitweg_id_peppol"] = ep if eas == "0204" else ""
        barcode = C.s(p["barcode"]).strip() or C.s(cp["barcode"]).strip()
        r["barcode"] = barcode
        gln_name = C.gln_from_name(cp["name"]) or C.gln_from_name(p["name"])
        r["gln_im_namen"] = gln_name or ""
        if eas == "0088" and re.fullmatch(r"\d{13}", ep):
            r["gln"], r["gln_quelle"] = ep, "Peppol-ID (EAN-Standortcode)"
        elif re.fullmatch(r"\d{13}", barcode) and C.gln_valid(barcode):
            r["gln"], r["gln_quelle"] = barcode, "Barcode"
        elif gln_name and C.gln_valid(gln_name):
            r["gln"], r["gln_quelle"] = gln_name, "Name"
        else:
            r["gln"], r["gln_quelle"] = "", ""

        r["ust_idnr"] = p["vat"] or cp["vat"] or ""
        r["handelsregister"] = cp["company_registry"] or ""
        r["zahlungsbedingung_kunde"] = cp["pay_term"] or ""
        r["zahlungsbedingung_lieferant"] = cp["sup_pay_term"] or ""
        r["steuerzuordnung"] = cp["fiscal_pos"] or ""
        r["debitorenkonto"], r["kreditorenkonto"] = cp["acc_receivable"] or "", cp["acc_payable"] or ""
        r["bankkonten_iban"] = "; ".join(a for a, _ in d["banks"].get(p["id"], []))

        inv = d["inv"].get(p["id"])
        r["rechnungen_anzahl"] = inv["n"] if inv else 0
        r["rechnungen_letzte"] = fdt(inv["last"]) if inv else ""
        li = d["last_inv"].get(cp["id"])
        r["erech_letzte_rechnung"] = li["name"] if li else ""
        r["erech_letzte_datum"] = fdt(li["invoice_date"]) if li else ""
        r["erech_letzte_mit_xml"] = ("ja" if li["xml_name"] else "nein") if li else ""
        r["erech_xml_datei"] = (li["xml_name"] or "") if li else ""
        r["erech_xml_erstellt"] = fdt(li["xml_date"]) if li and li["xml_date"] else ""
        r["erech_peppol"] = (li["peppol"] or "") if li else ""
        r["erech_anzahl_xml"] = d["xml12"].get(cp["id"], 0)

        r["vertriebsmitarbeiter"] = uname(cp["user_id"] or p["user_id"])
        r["kundenrang"], r["lieferantenrang"] = p["customer_rank"] or 0, p["supplier_rank"] or 0
        r["schlagwoerter"] = ", ".join(d["tags"].get(p["id"], []))
        r["notizen"] = html_to_text(p["comment"])[:2000]
        r["aktiv"] = "ja" if p["active"] else "nein"
        r["angelegt_am"], r["angelegt_von"] = fdt(p["create_date"]), uname(p["create_uid"])
        r["geaendert_am"], r["geaendert_von"] = fdt(p["write_date"]), uname(p["write_uid"])

        tr = d["tracking"].get(p["id"], [])
        r["anzahl_aenderungen"] = len(tr)
        r["letzte_aenderungen"] = "\n".join(_change_line(t, uname) for t in reversed(tr[-5:]))

        r.update(_kontrolle(p, tr, d["notes"].get(p["id"], []), uname, kab, mass))
        r["rechnungsrelevant"] = "ja" if rel.get(p["id"]) else "nein"
        r.update(_pruefen(p, cp, is_cp, r, send, eas, ep, fmt, herkunft, gln_name, barcode))
        r.update(dup.get(p["id"], {}))
        rows.append(r)
    return rows


def _change_line(t, uname) -> str:
    who = uname(t["create_uid"]) if t["create_uid"] and t["create_uid"] != 1 else (t["author_name"] or "Odoo")
    return " | ".join([fdt(t["date"]), _clean_line(who), _clean_line(t["flabel"]), _clean_line(t["old"]), _clean_line(t["new"])])


# =================================================================== Vier-Augen-Kontrolle
def _minute(when):
    return when.replace(second=0, microsecond=0)


def _mass_changes(d) -> set:
    """(Benutzer, Minute) mit mehr als MASSEN_GRENZE geänderten Kontakten = Import/Migration/Serveraktion."""
    cnt = defaultdict(set)
    for p in d["partners"]:
        for when, uid in ((p["create_date"], p["create_uid"]), (p["write_date"], p["write_uid"])):
            if when and uid:
                cnt[(uid, _minute(when))].add(p["id"])
    for pid, tl in d["tracking"].items():
        for t in tl:
            if t["date"] and t["create_uid"]:
                cnt[(t["create_uid"], _minute(t["date"]))].add(pid)
    return {k for k, ids in cnt.items() if len(ids) > config.MASSEN_GRENZE}


def _kontrolle(p, tracking, notes, uname, kab, mass=frozenset()) -> dict:
    """Änderungen (Verlauf + letzte Speicherung) gegen Prüfnotizen #geprüft / #falsch abgleichen.
    Nicht gezählt: Änderungen durch Odoo selbst (Benutzer 1) und Massenänderungen."""
    ab = datetime.fromisoformat(kab)

    def human(when, uid):
        return when and when >= ab and uid and uid != 1 and (uid, _minute(when)) not in mass
    events = []  # (zeit, art, wer, daten)
    for t in tracking:
        if human(t["date"], t["create_uid"]):
            events.append((t["date"], 0, uname(t["create_uid"]), t))
    # Felder ohne Verlauf (Format, Versand, Referenz, Peppol …) zeigen sich nur in write_date
    # (auf Sekunden gekürzt wie mail_message.date, sonst wirkt eine Prüfnotiz aus derselben Sekunde "zu früh")
    for when, uid in ((p["create_date"], p["create_uid"]), (p["write_date"], p["write_uid"])):
        if human(when, uid):
            events.append((when.replace(microsecond=0), 0, uname(uid), None))
    if not events:
        return {}
    for n in notes:
        txt = html_to_text(n["body"])
        m = re.search(r"#(gepr(?:ü|ue|u)ft|falsch)\b[:\s]*(.*)", txt, re.I | re.S)
        if not m:
            continue
        kind = "falsch" if m.group(1).lower() == "falsch" else "ok"
        who = uname(n["create_uid"]) if n["create_uid"] and n["create_uid"] != 1 else (n["author_name"] or "")
        events.append((n["date"], 1, who, {"kind": kind, "text": txt.strip()[:500]}))
    events.sort(key=lambda e: (e[0], e[1]))

    open_auth: list[str] = []
    open_changes: list = []
    closed_auth: list[str] = []
    closed_changes: list = []
    status, pruefer, am, notiz, last_change = "", "", None, "", None
    for when, kind, who, data in events:
        if kind == 0:
            if who not in open_auth:
                open_auth.append(who)
            if data is not None:
                open_changes.append(data)
            last_change = when
            status = "zu prüfen"
        else:
            if not open_auth:
                continue
            pruefer, am, notiz = who, when, data["text"]
            if who in open_auth:
                status = "selbst geprüft"
                continue
            status = "falsch" if data["kind"] == "falsch" else "geprüft"
            closed_auth, closed_changes = open_auth, open_changes
            open_auth, open_changes = [], []
    if status in ("zu prüfen", "selbst geprüft"):
        auth, chg = open_auth, open_changes
        if status == "zu prüfen":
            pruefer, am, notiz = "", None, ""
    else:
        auth, chg = closed_auth, closed_changes
    return {
        "kontrolle_status": status,
        "kontrolle_geaendert_am": fdt(last_change),
        "kontrolle_aenderer": "; ".join(_clean_list(a) for a in auth),
        "kontrolle_pruefer": pruefer,
        "kontrolle_am": fdt(am),
        "kontrolle_notiz": notiz,
        "kontrolle_aenderungen": "\n".join(_change_line(t, uname) for t in chg[-20:]),
    }


# =================================================================== Prüfung
def _pruefen(p, cp, is_cp, r, send, eas, ep, fmt, herkunft, gln_name, barcode) -> dict:
    out = {"korr_email": "", "korr_vat": "", "korr_peppol_endpoint": "", "korr_peppol_eas": "", "korr_gln_peppol": ""}
    # sichere Korrekturen (für den Import), unabhängig von der Relevanz
    ce = C.mail_clean(p["email"])
    if ce:
        out["korr_email"] = ce
    if is_cp:
        vat = C.s(p["vat"])
        nv = C.normalize_vat(vat)
        if vat and nv != vat and nv[:2].isalpha() and C.vat_valid(nv):
            out["korr_vat"] = nv
        if eas == "0088" and ep:
            digits = re.sub(r"[\s.\-]", "", ep)
            if digits != ep and re.fullmatch(r"\d{13}", digits) and C.gln_valid(digits):
                out["korr_peppol_endpoint"] = digits
        if eas == "9930":
            cv = C.normalize_vat(p["vat"])
            if cv.startswith("DE") and C.vat_valid(cv) and ep != cv and (not ep or C.normalize_vat(ep) == cv):
                out["korr_peppol_endpoint"] = cv
        if eas != "0204" and ep and C.LW_RE.fullmatch(ep) and C.leitweg_checksum_ok(ep):
            out["korr_peppol_eas"] = "0204"
        if gln_name and C.gln_valid(gln_name) and not (eas == "0088" and ep == gln_name):
            auto_vat = eas == "9930" and C.normalize_vat(ep) == C.normalize_vat(p["vat"]) and send != "peppol"
            bc_ok = not barcode or barcode == gln_name
            if (not ep or auto_vat) and bc_ok:
                out["korr_gln_peppol"] = gln_name

    if r["rechnungsrelevant"] != "ja":
        out.update(e_rechnung_bereit="nicht geprüft", e_rechnung_fehler="", e_rechnung_hinweise="")
        return out

    err, hint = [], []
    if r["kundenart"].startswith("B2C"):
        hint.append(C.H_PRIVAT)
        out.update(e_rechnung_bereit="privat", e_rechnung_fehler="", e_rechnung_hinweise="; ".join(hint))
        return out

    b2g = eas == "0204"
    cc = p["country_code"] or cp["country_code"] or ""
    # Adresse (Odoo übernimmt für Ansprechpartner die Adresse des Unternehmens)
    if not C.s(p["street"]).strip():
        err.append(C.F_STR)
    if not C.s(p["zip"]).strip():
        err.append(C.F_PLZ)
    if not C.s(p["city"]).strip():
        err.append(C.F_ORT)
    if not cc:
        err.append(C.F_LAND)
    # E-Mail (bei Peppol-Versand nicht nötig)
    mp = C.mail_problem(p["email"])
    if not is_cp:
        # Ansprechpartner/Lieferadresse: E-Mail nur Pflicht, wenn hierhin Rechnungen gehen.
        # Eine eingetragene, aber kaputte Adresse wird immer gemeldet.
        if mp and (mp != C.F_MAIL_FEHLT or p["type"] == "invoice" or r["rechnungen_anzahl"]) \
                and not (send == "peppol" and mp == C.F_MAIL_FEHLT):
            err.append(mp)
        # Format, Referenz, Peppol-ID, USt-IdNr. nimmt Odoo vom Unternehmen -> dort geprüft, nicht hier.
        # Ob das Unternehmen bereit ist, ergänzt die Seite selbst ("Unternehmen: E-Rechnung noch nicht bereit").
        out["e_rechnung_fehler"] = "; ".join(dict.fromkeys(err))
        out["e_rechnung_hinweise"] = ""
        out["e_rechnung_bereit"] = "nein" if err else "ja"
        return out
    if mp and not (send == "peppol" and mp == C.F_MAIL_FEHLT):
        err.append(mp)
    # USt-IdNr.
    vat = C.s(cp["vat"]).strip()
    if vat and vat != "/":
        vp = C.vat_check(vat)
        if not vp and vat != C.normalize_vat(vat):
            vp = C.F_VAT_FMT  # gültige Nummer, aber mit Leerzeichen/Punkten/Kleinbuchstaben (Import-Korrektur möglich)
        if vp:
            err.append(vp)
    elif cc in C.EU_CC and cc != "DE":
        err.append(C.F_VAT_EU)
    elif cc == "DE" and not r["kundenart"].startswith("B2G"):
        hint.append(C.H_VAT)
    # Format
    if cc == "DE" or b2g:
        if not fmt:
            err.append(C.F_KEIN_FMT)
        if b2g and fmt and fmt != "xrechnung":
            err.append(C.F_B2G_FMT)
        if fmt and "Vorschlag" in herkunft:
            hint.append(C.H_VORSCHLAG)
    if fmt == "xrechnung" and not b2g and not C.s(cp["ref"]).strip():
        err.append(C.F_BT10)
    # Leitweg-ID / Behörde
    if b2g:
        if not ep:
            err.append(C.F_LW_FEHLT)
        elif not C.LW_RE.fullmatch(ep):
            err.append(C.F_LW_FMT)
        elif not C.leitweg_checksum_ok(ep):
            err.append(C.F_LW_PZ)
    elif ep and C.LW_RE.fullmatch(ep) and C.leitweg_checksum_ok(ep):
        err.append(C.F_LW_ART)
    ref = C.s(cp["ref"]).strip()
    if not b2g and ref and C.LW_RE.fullmatch(ref) and C.leitweg_checksum_ok(ref):
        hint.append(C.H_LW_REF)
    # Peppol
    if eas == "9958":
        err.append(C.F_PEP_9958)
    if send == "peppol":
        if not eas or not ep:
            err.append(C.F_PEP_FEHLT)
        elif C.s(cp["peppol_state"]) != "valid":
            err.append(C.F_PEP_VERIF)
    if eas == "9930" and ep and not (C.normalize_vat(ep).startswith("DE") and C.vat_valid(ep)) \
            and C.F_LW_ART not in err:
        err.append(C.F_PEP_9930)
    if eas == "0088" and ep:
        if not re.fullmatch(r"\d{13}", ep):
            err.append(C.F_GLN_13)
        elif not C.gln_valid(ep):
            err.append(C.F_GLN_PZ)
    # GLN im Namen (Handelskunden wie REWE/EDEKA)
    if gln_name:
        if not C.gln_valid(gln_name):
            err.append(C.F_GLNN_PZ)
        else:
            if not (eas == "0088" and ep == gln_name):
                err.append(C.F_GLNN_PEP)
            if fmt and fmt != "zugferd" and "Vorschlag" not in herkunft:
                err.append(C.F_GLN_ZF)
            if barcode != gln_name:
                hint.append(C.H_GLNN_BC)
    # Barcode
    if re.fullmatch(r"\d{13}", barcode):
        if not C.gln_valid(barcode):
            hint.append(C.H_BC_PZ)
        elif eas == "0088" and re.fullmatch(r"\d{13}", ep) and ep != barcode:
            hint.append(C.H_BC_DIFF)
    if not send:
        hint.append(C.H_SEND)

    out["e_rechnung_fehler"] = "; ".join(dict.fromkeys(err))
    out["e_rechnung_hinweise"] = "; ".join(dict.fromkeys(hint))
    out["e_rechnung_bereit"] = "nein" if err else "ja"
    return out


# =================================================================== Dubletten
MAX_GRUPPE = 8          # größere Gruppen entstehen nur durch Ketten (A~B~C~…) und sind unbrauchbar
MAX_SCHLUESSEL = 25     # Werte, die bei mehr Kontakten vorkommen (Zentral-E-Mail, Sammel-IBAN), ignorieren


def _duplicates(d, P, is_internal) -> dict:
    """Doppelt angelegte Kontakte finden.

    Verglichen werden alle aktiven Kontakte, auch Ansprechpartner. Eigene Firma, Benutzer und
    Mitarbeiter dienen nur als Vergleich („gleiche Person wie Mitarbeiter …“), bekommen selbst aber
    keine Aufgabe. Geschwister unter demselben Unternehmen zählen nur bei gleichem Namen
    (gemeinsame Telefonnummer/E-Mail der Firma ist normal), Unternehmen und eigene Unterkontakte
    nie (das kann Odoo ohnehin nicht zusammenführen).
    """
    act = [p for p in d["partners"] if p["active"]]
    internal = {p["id"] for p in act if is_internal(p)}
    keys = defaultdict(set)  # (grund, wert) -> {partner_id}
    tokens = {}
    for p in act:
        pid = p["id"]
        is_cp = p["commercial_partner_id"] == pid
        if is_cp:  # USt-IdNr./Peppol-ID übernehmen Unterkontakte vom Unternehmen
            v = C.normalize_vat(p["vat"])
            if len(v) >= 6:
                keys[(C.D_VAT, v)].add(pid)
            ep = C.s(p["peppol_endpoint"]).strip().upper()
            if p["peppol_eas"] and ep and not (p["peppol_eas"] == "9930" and C.normalize_vat(ep) == v):
                keys[(C.D_PEPPOL, f"{p['peppol_eas']}:{ep}")].add(pid)
        for _, san in d["banks"].get(pid, []):
            if len(san) >= 10:
                keys[(C.D_IBAN, san)].add(pid)
        ref = re.sub(r"\s", "", C.s(p["ref"])).upper()
        if len(ref) >= 3:
            keys[(C.D_REF, ref)].add(pid)
        tk = C.name_tokens(p["name"])
        if len(tk.replace(" ", "")) >= 3:
            tokens[pid] = tk
            z = re.sub(r"\s", "", C.s(p["zip"]))
            if z:
                keys[(C.D_NAMEPLZ, tk + "|" + z)].add(pid)
            keys[(C.D_NAME, tk)].add(pid)
        em = C.s(p["email"]).strip().lower()
        if "@" in em:
            keys[(C.D_MAIL, em)].add(pid)
        for ph in {C.norm_phone(p["phone"]), C.norm_phone(p["mobile"])} - {""}:
            keys[(C.D_TEL, ph)].add(pid)

    edges = defaultdict(set)  # (a, b) mit a < b -> {gründe}
    for (why, _), ids in keys.items():
        if 2 <= len(ids) <= MAX_SCHLUESSEL:
            ids = sorted(ids)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    edges[(a, b)].add(why)

    # ähnliche Namen am selben Ort (Tippfehler, „GmbH & Co. KG“ fehlt, Bindestriche …)
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        process = None
        log.warning("rapidfuzz fehlt: ähnliche Namen werden nicht gesucht")
    if process:
        by_zip = defaultdict(list)
        for p in act:
            z = re.sub(r"\s", "", C.s(p["zip"]))
            if z and len(tokens.get(p["id"], "")) >= 6:
                by_zip[z].append(p["id"])
        for ids in by_zip.values():
            if not 2 <= len(ids) <= 600:
                continue
            names = [tokens[i] for i in ids]
            for i, name in enumerate(names[:-1]):
                for _, _, j in process.extract(name, names[i + 1:], scorer=fuzz.token_sort_ratio,
                                               score_cutoff=90, limit=None):
                    j += i + 1
                    if names[j] != name and re.findall(r"\d+", name) == re.findall(r"\d+", names[j]):
                        a, b = sorted((ids[i], ids[j]))
                        edges[(a, b)].add(C.D_AEHNLICH)

    # „Name (Kopie)“ zu einem Kontakt mit sonst gleichem Namen
    for (a, b), w in edges.items():
        if C.D_NAME in w and (C.COPY_RE.search(C.s(P[a]["name"])) or C.COPY_RE.search(C.s(P[b]["name"]))):
            w.add(C.D_KOPIE)
            w.discard(C.D_NAME)

    # unpassende Paare entfernen
    for (a, b) in list(edges):
        pa, pb = P[a], P[b]
        w = edges[(a, b)]
        if a in internal and b in internal:
            del edges[(a, b)]
        elif pa["commercial_partner_id"] == pb["commercial_partner_id"]:
            if a == pa["commercial_partner_id"] or b == pb["commercial_partner_id"] \
                    or pa["parent_id"] == b or pb["parent_id"] == a:
                del edges[(a, b)]  # Unternehmen und eigener Unterkontakt
            else:
                w &= C.DUP_NUR_NAME   # Geschwister: nur gleicher Name zählt
                if not w:
                    del edges[(a, b)]
        elif C.D_NAMEPLZ in w:
            w.discard(C.D_NAME)
            w.discard(C.D_AEHNLICH)

    score = {e: sum(C.DUP_GEWICHT[x] for x in w) for e, w in edges.items()}

    # Gruppen: stärkste Paare zuerst verbinden, aber keine Ketten über schwache Gründe
    parent, size = {}, defaultdict(lambda: 1)

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), sc in sorted(score.items(), key=lambda kv: -kv[1]):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if (sc >= 2 and size[ra] + size[rb] <= MAX_GRUPPE) or (size[ra] == 1 and size[rb] == 1):
            parent[ra] = rb
            size[rb] += size[ra]
    groups = defaultdict(set)
    for a, b in edges:
        groups[find(a)].add(a)
        groups[find(b)].add(b)
    gno = {}
    for i, root in enumerate(sorted(groups, key=lambda g: min(groups[g])), start=1):
        for a in groups[root]:
            gno[a] = i

    nb = defaultdict(list)  # partner -> [(anderer, gründe, score)]
    for (a, b), w in edges.items():
        nb[a].append((b, w, score[(a, b)]))
        nb[b].append((a, w, score[(a, b)]))

    def level(sc):
        return "stark" if sc >= 4 else "mittel" if sc >= 2 else "schwach"

    out = {}
    for a, lst in nb.items():
        if a in internal:
            continue  # eigene Firma/Mitarbeiter: keine eigene Aufgabe
        lst.sort(key=lambda x: (-x[2], x[0]))
        allw = set().union(*(w for _, w, _ in lst))
        ordered = lambda ws: [x for x in C.DUP_REIHENFOLGE if x in ws]
        out[a] = {
            "dublette_verdacht": level(lst[0][2]),
            "dublette_gruende": "; ".join(ordered(allw)),
            "dublette_mit": "; ".join(f"{_clean_list(P[b]['name'])} (#{b}): {', '.join(ordered(w))}" for b, w, _ in lst[:10]),
            "dublette_gruppe": gno.get(a, 0),
            "dublette_json": json.dumps([{"id": b, "gruende": ordered(w), "score": sc, "intern": b in internal}
                                         for b, w, sc in lst[:8]], ensure_ascii=False),
        }
    return out
