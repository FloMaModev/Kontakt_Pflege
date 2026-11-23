# E-Rechnungs-Check Kontakte – Odoo 18 (Docker)

Die Seite liest **direkt und nur lesend aus der Odoo-18-Datenbank (PostgreSQL)**.
Keine Hop-Pipeline und keine `odoo18_kontakte_data.js` mehr: Beim Öffnen der Seite
und bei Klick auf **„↻ Neu laden“** kommen die Daten live aus Odoo.

```
Browser ──► Docker-Dienst „erechnung-check“ (Port 8080) ──► PostgreSQL von Odoo 18
            /            die Seite                     nur SELECT, eigener Nur-Lese-Benutzer
            /api/kontakte  Kontakte + Prüfergebnis (JSON)
            /health        Verbindungstest
```

## Einrichten

**1. Nur-Lese-Benutzer in der Odoo-Datenbank anlegen** (einmalig). Vorher das Passwort in
`sql/readonly_user.sql` ändern:

```bash
docker exec -i <odoo-db-container> psql -U odoo -d <odoo-datenbank> < sql/readonly_user.sql
```

**2. Einstellungen**

```bash
cp .env.example .env
```

Danach in `.env` mindestens `PGHOST`, `PGDATABASE`, `PGPASSWORD`, `ODOO_URL` und
`APP_USER`/`APP_PASSWORD` eintragen.

Läuft Odoo selbst in Docker Compose, in `docker-compose.yml` den Block `networks`
einkommentieren und den Netzwerknamen eintragen (`docker network ls`).
`PGHOST` ist dann der Dienstname der Datenbank (meist `db`).

**3. Starten**

```bash
docker compose up -d --build
```

Danach die Seite unter `http://<server>:8080` öffnen. Verbindungstest: `http://<server>:8080/health`
muss `"nur_lesend": true` zeigen.

## Was ist neu gegenüber der Version mit Hop-Pipeline?

* **Live-Daten:** kein Export und keine Datei. Oben rechts stehen „● live“, der Datenstand und der
  Knopf **„↻ Neu laden“** (liest sofort neu aus Odoo). Ohne Klick wird ein Ergebnis höchstens
  `CACHE_SECONDS` lang wiederverwendet.
* **Lesen ist dreifach abgesichert:** Datenbankbenutzer nur mit `SELECT`, Sitzung mit
  `default_transaction_read_only`, jede Abfrage als `READ ONLY`-Transaktion. Zusätzlich ist das
  Dateisystem des Containers schreibgeschützt, und der Dienst läuft ohne Root-Rechte.
* **Passwortschutz** über `APP_USER`/`APP_PASSWORD`. Die Seite zeigt Kundendaten, daher bitte setzen.
* **Ladeanzeige und klare Fehlermeldung**, wenn die Datenbank nicht erreichbar ist.
* Alle Texte, die auf „Hop-Pipeline neu laufen lassen“ verwiesen, zeigen jetzt auf „↻ Neu laden“.
* Getestet mit 30.000 Kontakten: Abfrage ≈ 3 s, Seite ≈ 2 s.

## Woher kommen die Werte? (Odoo 18)

| Seite | Odoo-Tabelle / Feld |
|---|---|
| Kontakt, Adresse, E-Mail, USt-IdNr. | `res_partner` |
| Format der E-Rechnung | `invoice_edi_format_store` (firmenabhängig). Leer = „XML-Format“, dann wird Odoos Vorschlag nachgebildet (DE: ZUGFeRD, bei Leitweg-ID: XRechnung) |
| Rechnungsversand, Barcode, Peppol-Status | `invoice_sending_method`, `barcode`, `peppol_verification_state` (firmenabhängig, gilt für `ODOO_COMPANY_ID`) |
| Peppol-ID | `peppol_eas`, `peppol_endpoint` |
| Käuferreferenz BT-10 | `ref` des Unternehmens (wie Odoo es in die XRechnung schreibt) |
| Rechnungen, E-Rechnungs-XML, Peppol-Versand | `account_move`, `ir_attachment` (`ubl_cii_xml_file`), `peppol_move_state` |
| Letzte Änderungen | `mail_tracking_value` (Verlauf im Chatter) |
| Vier-Augen-Kontrolle | Verlauf und letzte Speicherung ab `KONTROLLE_AB` sowie Notizen `#geprüft` / `#falsch: Grund` |
| Externe ID (für Import) | `ir_model_data` |

Format, Käuferreferenz und Peppol-ID liest Odoo immer vom übergeordneten **Unternehmen**.
Deshalb gelten diese Werte auch bei Ansprechpartnern und Rechnungsadressen.

**Was wird geprüft?** Alle aktiven Kontakte außer den eigenen Firmen, internen Benutzern und
Mitarbeitern (`PRUEF_UMFANG=alle`). Ansprechpartner und Adressen werden nur mit ihren eigenen
Angaben geprüft (Adresse, E-Mail). Format, Käuferreferenz, Peppol-ID und USt-IdNr. nimmt Odoo vom
Unternehmen, deshalb stehen diese Fehler nur beim Unternehmen. Solange das Unternehmen nicht bereit
ist, gilt das auch für seine Ansprechpartner („Unternehmen: E-Rechnung noch nicht bereit“). Diese
Kontakte erscheinen aber nicht in der Fehlerliste, weil dort nichts zu tun ist.

**Vier-Augen-Kontrolle:** Massenänderungen zählen nicht, also wenn eine Person in derselben Minute
mehr als `MASSEN_GRENZE` Kontakte geändert hat (Import, Migration auf Odoo 18, Serveraktion).

Geprüft wird wie in Odoo: USt-IdNr. mit `python-stdnum` (gleiche Bibliothek wie Odoo), GLN mit
GS1-Prüfziffer, Leitweg-ID mit Prüfziffer nach ISO 7064 MOD 97-10.

## Einstellungen (.env)

| Variable | Bedeutung |
|---|---|
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE` | Verbindung zur Odoo-Datenbank |
| `ODOO_URL` | Adresse für die Links „In Odoo öffnen“ |
| `ODOO_COMPANY_ID` | Firma für firmenabhängige Felder (leer = erste deutsche Firma) |
| `KONTROLLE_AB` | Vier-Augen-Kontrolle für Änderungen ab diesem Datum |
| `INCLUDE_INACTIVE` | archivierte Kontakte mitladen |
| `PRUEF_UMFANG` | `alle` (Standard) oder `rechnungsempfaenger` |
| `MASSEN_GRENZE` | ab wie vielen Kontakten pro Person und Minute eine Änderung als Massenänderung gilt (Standard 30) |
| `CACHE_SECONDS` | wie lange ein Ergebnis wiederverwendet wird |
| `APP_USER`, `APP_PASSWORD` | Passwortschutz der Seite |
| `APP_PORT` | Port auf dem Server |

## Wie geht es weiter?

„Erledigt“-Markierungen und „Kollegen fragen“ bleiben wie bisher im Browser des Mitarbeiters
gespeichert. Sollen sie für alle Kollegen gemeinsam gelten, kann der Dienst sie in einer eigenen
kleinen Datenbank im Container speichern. Die Odoo-Datenbank bliebe dabei weiter nur lesend.
