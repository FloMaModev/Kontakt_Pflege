"""Zwischenspeicher für das Abfrageergebnis, damit die Odoo-Datenbank nicht bei jedem Aufruf gelesen wird.

* Das Ergebnis liegt fertig komprimiert (gzip) im Speicher und als Datei auf dem Volume (CACHE_DIR).
  Nach einem Neustart des Containers wird die Datei geladen, ohne die Datenbank zu fragen.
* Aufrufe bekommen immer sofort den gespeicherten Stand. Ist er älter als CACHE_MINUTEN,
  wird einmal im Hintergrund neu gelesen (weitere Aufrufe lösen keine zweite Abfrage aus).
* „Neu laden“ liest sofort neu, aber höchstens alle NEU_LADEN_SPERRE_SEKUNDEN.
* Optional feste Uhrzeiten (AKTUALISIEREN_UM), z. B. morgens vor Arbeitsbeginn.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, odoo_db

log = logging.getLogger("erechnung.cache")


class DatenCache:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.file = self.dir / "kontakte.json.gz"
        self.gz: bytes | None = None
        self.etag = ""
        self.stand = 0.0          # Zeitpunkt der Datenbankabfrage (Unix-Zeit)
        self.dauer = 0.0
        self.anzahl = 0
        self.fehler = ""          # letzter Fehler einer Hintergrund-Aktualisierung
        self._lock = threading.Lock()          # nur eine Datenbankabfrage gleichzeitig
        self._bg: threading.Thread | None = None
        self._laden_aus_datei()

    # ------------------------------------------------------------------ Zustand
    @property
    def alter_s(self) -> float:
        return time.time() - self.stand if self.stand else float("inf")

    @property
    def veraltet(self) -> bool:
        return config.CACHE_MINUTEN > 0 and self.alter_s > config.CACHE_MINUTEN * 60

    @property
    def laeuft(self) -> bool:
        return self._lock.locked()

    def info(self) -> dict:
        return {
            "stand": datetime.fromtimestamp(self.stand, ZoneInfo(config.TZ)).strftime("%d.%m.%Y %H:%M:%S") if self.stand else None,
            "alter_minuten": round(self.alter_s / 60, 1) if self.stand else None,
            "kontakte": self.anzahl,
            "abfragedauer_s": round(self.dauer, 1),
            "groesse_kb": round(len(self.gz) / 1024) if self.gz else 0,
            "aktualisierung_laeuft": self.laeuft,
            "letzter_fehler": self.fehler or None,
            "cache_minuten": config.CACHE_MINUTEN,
        }

    # ------------------------------------------------------------------ Datei
    def _setzen(self, gz: bytes, stand: float, anzahl: int):
        self.gz, self.stand, self.anzahl = gz, stand, anzahl
        self.etag = '"' + hashlib.sha1(gz).hexdigest()[:20] + '"'

    def _laden_aus_datei(self):
        try:
            gz = self.file.read_bytes()
            meta = json.loads(self.file.with_suffix(".meta").read_text("utf-8"))
            self._setzen(gz, float(meta["stand"]), int(meta.get("anzahl", 0)))
            self.dauer = float(meta.get("dauer", 0))
            log.info("Cache aus Datei geladen: %d Kontakte, Stand vor %.0f Min.", self.anzahl, self.alter_s / 60)
        except FileNotFoundError:
            log.info("Noch kein Cache vorhanden (%s)", self.file)
        except Exception as e:
            log.warning("Cache-Datei unbrauchbar, wird neu erstellt: %s", e)

    def _schreiben(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_bytes(self.gz)
            os.replace(tmp, self.file)  # atomar: nie eine halbe Datei
            self.file.with_suffix(".meta").write_text(
                json.dumps({"stand": self.stand, "anzahl": self.anzahl, "dauer": self.dauer}), "utf-8")
        except OSError as e:  # ohne Volume trotzdem weiterarbeiten (dann nur im Speicher)
            log.warning("Cache-Datei kann nicht geschrieben werden (%s), Cache nur im Speicher", e)

    # ------------------------------------------------------------------ Datenbank
    def aktualisieren(self, warten_wenn_belegt: bool = True) -> bool:
        """Liest die Odoo-Datenbank (nur lesend). False, wenn schon eine Abfrage läuft und nicht gewartet wird."""
        if not self._lock.acquire(blocking=warten_wenn_belegt):
            return False
        try:
            if warten_wenn_belegt and self.gz and self.alter_s < 5:
                return True  # während des Wartens hat eine andere Abfrage gerade aktualisiert
            t0 = time.monotonic()
            with odoo_db.connect() as conn:
                data = odoo_db.load(conn)
            gz = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"), 6)
            self.dauer = time.monotonic() - t0
            self._setzen(gz, time.time(), len(data["rows"]))
            self.fehler = ""
            self._schreiben()
            log.info("Odoo-Datenbank gelesen: %d Kontakte in %.1fs, %d KB", self.anzahl, self.dauer, len(gz) // 1024)
            return True
        except Exception as e:
            self.fehler = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._lock.release()

    def im_hintergrund(self):
        """Startet höchstens eine Aktualisierung im Hintergrund."""
        if self.laeuft or (self._bg and self._bg.is_alive()):
            return

        def run():
            try:
                self.aktualisieren(warten_wenn_belegt=False)
            except Exception:
                log.exception("Hintergrund-Aktualisierung fehlgeschlagen, alter Stand bleibt")
        self._bg = threading.Thread(target=run, name="cache-aktualisierung", daemon=True)
        self._bg.start()

    def holen(self, neu: bool = False) -> tuple[bytes, str, bool]:
        """(gzip-Daten, ETag, wird_aktualisiert). Wirft Fehler nur, wenn es gar keinen Stand gibt."""
        if neu and self.alter_s >= config.NEU_LADEN_SPERRE_SEKUNDEN:
            try:
                self.aktualisieren()
            except Exception:
                if not self.gz:
                    raise
                log.exception("Neu laden fehlgeschlagen, alter Stand wird gezeigt")
        elif not self.gz:
            self.aktualisieren()
        elif self.veraltet:
            self.im_hintergrund()
        return self.gz, self.etag, self.laeuft

    # ------------------------------------------------------------------ Zeitplan
    def zeitplan_starten(self):
        zeiten = sorted({z.strip() for z in config.AKTUALISIEREN_UM.split(",") if z.strip()})
        if config.VORLADEN and not self.gz:
            self.im_hintergrund()
        if not zeiten:
            return
        tz = ZoneInfo(config.TZ)

        def loop():
            erledigt = set()
            while True:
                now = datetime.now(tz)
                hm, tag = now.strftime("%H:%M"), now.date()
                if hm in zeiten and (tag, hm) not in erledigt:
                    erledigt.add((tag, hm))
                    log.info("Geplante Aktualisierung um %s", hm)
                    self.im_hintergrund()
                time.sleep(20)
        threading.Thread(target=loop, name="cache-zeitplan", daemon=True).start()
        log.info("Geplante Aktualisierung täglich um: %s", ", ".join(zeiten))
