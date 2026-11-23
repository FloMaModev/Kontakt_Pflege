"""Gemeinsame Entscheidungen der Mitarbeiter, z. B. „keine Dublette“.

Gespeichert als kleine JSON-Datei im Volume (CACHE_DIR), NICHT in Odoo: die Odoo-Datenbank bleibt
nur lesend. Gilt sofort für alle Kollegen.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

_lock = threading.Lock()
_datei = Path(config.CACHE_DIR) / "entscheidungen.json"


def _lesen() -> dict:
    try:
        d = json.loads(_datei.read_text("utf-8"))
    except FileNotFoundError:
        d = {}
    d.setdefault("keine_dublette", {})
    return d


def alle() -> dict:
    with _lock:
        return _lesen()


def _paar(a: int, b: int) -> str:
    a, b = sorted((int(a), int(b)))
    return f"{a}-{b}"


def speichern(aktion: str, ids: list, wer: str = "", grund: str = "") -> dict:
    ids = list(dict.fromkeys(int(i) for i in ids))  # Reihenfolge behalten: erster = „dieser Kontakt“
    if len(ids) < 2 or len(ids) > 20:
        raise ValueError("2 bis 20 Kontakt-IDs angeben")
    wer, grund = str(wer)[:80], str(grund)[:300]
    with _lock:
        d = _lesen()
        kd = d["keine_dublette"]
        paare = [_paar(ids[0], b) for b in ids[1:]]  # erster Kontakt gegen jeden weiteren
        if aktion == "keine_dublette":
            now = datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d %H:%M")
            for p in paare:
                kd[p] = {"wer": wer, "wann": now, "grund": grund}
        elif aktion == "zuruecknehmen":
            for p in paare:
                kd.pop(p, None)
        else:
            raise ValueError(f"unbekannte Aktion: {aktion}")
        _datei.parent.mkdir(parents=True, exist_ok=True)
        tmp = _datei.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, _datei)
        return d
