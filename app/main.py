"""Webdienst: liefert die Seite und die Daten aus der Odoo-Datenbank (nur lesend, mit Cache)."""
from __future__ import annotations

import base64
import gzip
import logging
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config, entscheidungen, odoo_db
from .cache import DatenCache

log = logging.getLogger("erechnung")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="E-Rechnungs-Check Odoo 18", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2000, compresslevel=6)

CACHE = DatenCache(config.CACHE_DIR)


@app.on_event("startup")
def _start():
    CACHE.zeitplan_starten()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optionaler Passwortschutz (APP_USER / APP_PASSWORD). /health bleibt offen."""
    if config.APP_USER and config.APP_PASSWORD and not request.url.path.rstrip("/").endswith("/health"):
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(u, config.APP_USER) and secrets.compare_digest(pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Anmeldung erforderlich", 401, {"WWW-Authenticate": 'Basic realm="E-Rechnungs-Check"'})
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def index():
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


def kontakte(request: Request, refresh: int = 0):
    try:
        gz, etag, laeuft = CACHE.holen(neu=bool(refresh))
    except Exception as e:  # Fehler verständlich an die Seite melden
        log.exception("Datenbankabfrage fehlgeschlagen")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=503)
    headers = {
        "ETag": etag,
        # Browser darf behalten, muss aber jedes Mal kurz nachfragen (unverändert -> 304 ohne Daten)
        "Cache-Control": "private, no-cache",
        "X-Daten-Alter-Minuten": str(round(CACHE.alter_s / 60, 1)),
        "X-Wird-Aktualisiert": "1" if laeuft else "0",
        "Vary": "Accept-Encoding",
    }
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gz, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})
    return Response(gzip.decompress(gz), media_type="application/json", headers=headers)


def health(db: int = 0):
    """Ohne ?db=1 wird die Datenbank NICHT gefragt (Docker-Healthcheck läuft jede Minute)."""
    out = {"status": "ok", "cache": CACHE.info()}
    if not db:
        return out
    try:
        with odoo_db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok, current_setting('transaction_read_only') AS ro")
            r = cur.fetchone()
        out.update(datenbank=config.PGDATABASE, nur_lesend=r["ro"] == "on")
        return out
    except Exception as e:
        return JSONResponse({"status": "fehler", "fehler": str(e), "cache": CACHE.info()}, status_code=503)


@app.get("/{pfad:path}")
def route(pfad: str, request: Request, refresh: int = 0, db: int = 0):
    """Jede Adresse funktioniert: auch /index.html, der alte Dateiname und Unterpfade hinter
    einem Reverse-Proxy (z. B. /erechnung/api/kontakte), ohne extra Einstellung."""
    p = pfad.rstrip("/")
    if p == "api/kontakte" or p.endswith("/api/kontakte"):
        return kontakte(request, refresh)
    if p == "health" or p.endswith("/health"):
        return health(db)
    if p == "api/entscheidungen" or p.endswith("/api/entscheidungen"):
        return JSONResponse(entscheidungen.alle(), headers={"Cache-Control": "no-store"})
    if p == "favicon.ico" or p.endswith("/favicon.ico"):
        return Response(status_code=204)
    return index()


@app.post("/{pfad:path}")
async def route_post(pfad: str, request: Request):
    """„Keine Dublette“ usw. für alle Kollegen speichern (im Volume, nicht in Odoo)."""
    if not pfad.rstrip("/").endswith("api/entscheidungen"):
        return JSONResponse({"error": "unbekannt"}, status_code=404)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return JSONResponse({"error": "JSON erwartet"}, status_code=415)
    try:
        body = await request.json()
        d = entscheidungen.speichern(body.get("aktion", ""), body.get("ids") or [], body.get("wer", ""), body.get("grund", ""))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OSError as e:
        return JSONResponse({"error": f"Speichern nicht möglich (Volume {config.CACHE_DIR}?): {e}"}, status_code=500)
    log.info("Entscheidung: %s %s von %s", body.get("aktion"), body.get("ids"), body.get("wer") or "?")
    return JSONResponse(d)
