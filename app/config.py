"""Einstellungen aus Umgebungsvariablen (siehe .env.example)."""
import os


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "ja", "on")


def _int(name: str, default: int | None = None) -> int | None:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


# --- Datenbank (Standard-Variablen von libpq/psycopg) ---
PGHOST = os.environ.get("PGHOST", "db")
PGPORT = _int("PGPORT", 5432)
PGDATABASE = os.environ.get("PGDATABASE", "odoo")
PGUSER = os.environ.get("PGUSER", "erechnung_ro")
PGPASSWORD = os.environ.get("PGPASSWORD", "")
PGSSLMODE = os.environ.get("PGSSLMODE", "prefer")
STATEMENT_TIMEOUT_MS = _int("STATEMENT_TIMEOUT_MS", 120_000)

# --- Odoo ---
ODOO_URL = os.environ.get("ODOO_URL", "https://hogakaprofi.de").rstrip("/")
# Firma, deren firmenabhängige Werte (Format, Versand, Barcode, Peppol-Status) gelesen werden.
# Leer = erste Firma mit Land DE, sonst die mit der kleinsten ID.
ODOO_COMPANY_ID = _int("ODOO_COMPANY_ID")

# --- Prüfung ---
KONTROLLE_AB = os.environ.get("KONTROLLE_AB", "2025-01-01")  # Vier-Augen-Kontrolle gilt für Änderungen ab diesem Datum
INCLUDE_INACTIVE = _bool("INCLUDE_INACTIVE", False)          # archivierte Kontakte mitladen
TZ = os.environ.get("TZ_ANZEIGE", os.environ.get("TZ", "Europe/Berlin"))

# --- Web ---
CACHE_SECONDS = _int("CACHE_SECONDS", 120)   # so lange wird ein Abfrageergebnis wiederverwendet
APP_USER = os.environ.get("APP_USER", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
