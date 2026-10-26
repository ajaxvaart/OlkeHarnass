"""Voorstellen van de assistent, bewaard in SQLite.

Een voorstel doorloopt: open -> bezig -> uitgevoerd | mislukt, of open -> geweigerd.
Een open voorstel na zijn vervaltijd telt als verlopen en kan niet meer uitgevoerd
worden. De overgang open -> bezig is een enkele UPDATE met een voorwaarde op de
status: van twee gelijktijdige klikken krijgt er maar een het voorstel. Blijft een
voorstel op bezig staan (de harnass viel weg tijdens het uitvoeren), dan wordt het
nooit opnieuw uitgevoerd.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voorstellen (
    id          TEXT PRIMARY KEY,
    actie       TEXT NOT NULL,
    args        TEXT NOT NULL,
    titel       TEXT NOT NULL,
    cmd         TEXT NOT NULL,
    reden       TEXT NOT NULL,
    risico      TEXT NOT NULL,
    status      TEXT NOT NULL,
    aangemaakt  REAL NOT NULL,
    verloopt    REAL NOT NULL,
    besloten    REAL,
    door        TEXT,
    uitkomst    TEXT
)
"""


@dataclass(frozen=True)
class Voorstel:
    id: str
    actie: str
    args: dict[str, Any]
    titel: str
    cmd: str
    reden: str
    risico: str
    status: str
    verloopt: float

    def voor_bord(self) -> dict[str, Any]:
        """De vorm die het bord verwacht."""
        return {"id": self.id, "title": self.titel, "cmd": self.cmd,
                "why": self.reden, "risk": self.risico}


class VoorstelStore:
    def __init__(self, pad: str, geldig_seconden: float) -> None:
        if pad != ":memory:":
            Path(pad).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._geldig = geldig_seconden
        with self._lock:
            self._db.execute(_SCHEMA)

    def _rij(self, r: tuple) -> Voorstel:
        return Voorstel(r[0], r[1], json.loads(r[2]), r[3], r[4], r[5], r[6], r[7], r[8])

    _KOLOMMEN = "id, actie, args, titel, cmd, reden, risico, status, verloopt"

    def maak(self, actie: str, args: dict[str, Any], titel: str, cmd: str, reden: str,
             risico: str) -> Voorstel:
        nu = time.time()
        v = Voorstel(secrets.token_urlsafe(12), actie, args, titel, cmd, reden, risico,
                     "open", nu + self._geldig)
        with self._lock:
            self._db.execute(
                "INSERT INTO voorstellen (id, actie, args, titel, cmd, reden, risico, status,"
                " aangemaakt, verloopt) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (v.id, actie, json.dumps(args, ensure_ascii=False), titel, cmd, reden, risico,
                 nu, v.verloopt),
            )
        return v

    def haal(self, id_: str) -> Voorstel | None:
        with self._lock:
            r = self._db.execute(f"SELECT {self._KOLOMMEN} FROM voorstellen WHERE id = ?",
                                 (id_,)).fetchone()
        return self._rij(r) if r else None

    def open_voorstellen(self) -> list[Voorstel]:
        with self._lock:
            rijen = self._db.execute(
                f"SELECT {self._KOLOMMEN} FROM voorstellen WHERE status = 'open' AND verloopt > ?"
                " ORDER BY aangemaakt", (time.time(),)).fetchall()
        return [self._rij(r) for r in rijen]

    def claim(self, id_: str, door: str) -> bool:
        """open -> bezig, alleen als het voorstel open en niet verlopen is."""
        with self._lock:
            c = self._db.execute(
                "UPDATE voorstellen SET status = 'bezig', besloten = ?, door = ?"
                " WHERE id = ? AND status = 'open' AND verloopt > ?",
                (time.time(), door, id_, time.time()))
        return c.rowcount == 1

    def afronden(self, id_: str, gelukt: bool, uitkomst: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE voorstellen SET status = ?, uitkomst = ? WHERE id = ? AND status = 'bezig'",
                ("uitgevoerd" if gelukt else "mislukt", uitkomst, id_))

    def weiger(self, id_: str, door: str) -> bool:
        with self._lock:
            c = self._db.execute(
                "UPDATE voorstellen SET status = 'geweigerd', besloten = ?, door = ?"
                " WHERE id = ? AND status = 'open'", (time.time(), door, id_))
        return c.rowcount == 1
