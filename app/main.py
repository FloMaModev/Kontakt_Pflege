"""Webdienst: liefert die Seite und die Daten live aus der Odoo-Datenbank (nur lesend)."""
from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config, odoo_db

log = logging.getLogger("erechnung")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="E-Rechnungs-Check Odoo 18", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2000, compresslevel=6)

_cache: dict = {"t": 0.0, "data": None}
_lock = threading.Lock()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optionaler Passwortschutz (APP_USER / APP_PASSWORD). /health bleibt offen."""
    if config.APP_USER and config.APP_PASSWORD and request.url.path != "/health":
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


def get_data(refresh: bool = False) -> dict:
    with _lock:  # nur eine Abfrage gleichzeitig; parallele Aufrufe bekommen dasselbe Ergebnis
        if not refresh and _cache["data"] and time.monotonic() - _cache["t"] < config.CACHE_SECONDS:
            return _cache["data"]
        t0 = time.monotonic()
        with odoo_db.connect() as conn:
            data = odoo_db.load(conn)
        _cache.update(t=time.monotonic(), data=data)
        log.info("Daten aus Odoo gelesen: %d Kontakte in %.1fs", len(data["rows"]), time.monotonic() - t0)
        return data


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/kontakte")
def kontakte(refresh: int = 0):
    try:
        data = get_data(bool(refresh))
    except Exception as e:  # Fehler verständlich an die Seite melden
        log.exception("Datenbankabfrage fehlgeschlagen")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=503)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    try:
        with odoo_db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok, current_setting('transaction_read_only') AS ro")
            r = cur.fetchone()
        return {"status": "ok", "datenbank": config.PGDATABASE, "nur_lesend": r["ro"] == "on"}
    except Exception as e:
        return JSONResponse({"status": "fehler", "fehler": str(e)}, status_code=503)
