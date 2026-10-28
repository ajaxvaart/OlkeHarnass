"""Gesprekken van de assistent, bewaard in SQLite, zodat ze herladen en herstarten overleven.

Een gesprek is een reeks berichten. Soorten:
- vraag:    wat Olke typte
- antwoord: wat het model antwoordde
- stappen:  de live stappen van een beurt (labels, geen tooluitvoer)
- voorstel: een klaargezet voorstel (kolom `voorstel` = id in VoorstelStore)
- uitkomst: een besluit over een voorstel (toegestaan en uitgevoerd, mislukt, geweigerd)

Het model krijgt alleen vraag, antwoord en uitkomst terug als geschiedenis; het bord
krijgt alles. Gesprekken ouder dan `bewaar_dagen` worden bij het starten opgeruimd.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS gesprekken (
        id          TEXT PRIMARY KEY,
        aangemaakt  REAL NOT NULL,
        bijgewerkt  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS berichten (
        nr       INTEGER PRIMARY KEY AUTOINCREMENT,
        gesprek  TEXT NOT NULL,
        tijd     REAL NOT NULL,
        soort    TEXT NOT NULL,
        tekst    TEXT NOT NULL,
        extra    TEXT,
        voorstel TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS berichten_gesprek ON berichten (gesprek, nr)",
    "CREATE INDEX IF NOT EXISTS berichten_voorstel ON berichten (voorstel)",
]

SOORTEN = ("vraag", "antwoord", "stappen", "voorstel", "uitkomst")


class GesprekStore:
    def __init__(self, pad: str, bewaar_dagen: int = 30) -> None:
        if pad != ":memory:":
            Path(pad).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            for s in _SCHEMA:
                self._db.execute(s)
        self.ruim_op(bewaar_dagen)

    def ruim_op(self, bewaar_dagen: int) -> None:
        grens = time.time() - bewaar_dagen * 86400
        with self._lock:
            self._db.execute("DELETE FROM berichten WHERE gesprek IN "
                             "(SELECT id FROM gesprekken WHERE bijgewerkt < ?)", (grens,))
            self._db.execute("DELETE FROM gesprekken WHERE bijgewerkt < ?", (grens,))

    def nieuw(self) -> str:
        id_, nu = secrets.token_urlsafe(9), time.time()
        with self._lock:
            self._db.execute("INSERT INTO gesprekken VALUES (?, ?, ?)", (id_, nu, nu))
        return id_

    def bestaat(self, id_: str | None) -> bool:
        if not id_:
            return False
        with self._lock:
            return self._db.execute("SELECT 1 FROM gesprekken WHERE id = ?", (id_,)).fetchone() is not None

    def laatste(self) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT id FROM gesprekken ORDER BY bijgewerkt DESC LIMIT 1").fetchone()
        return r[0] if r else None

    def voeg_toe(self, gesprek: str, soort: str, tekst: str, extra: dict[str, Any] | None = None,
                 voorstel: str | None = None) -> None:
        if soort not in SOORTEN:
            raise ValueError(f"onbekende soort {soort!r}")
        nu = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO berichten (gesprek, tijd, soort, tekst, extra, voorstel)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (gesprek, nu, soort, tekst,
                 json.dumps(extra, ensure_ascii=False) if extra is not None else None, voorstel))
            self._db.execute("UPDATE gesprekken SET bijgewerkt = ? WHERE id = ?", (nu, gesprek))

    def berichten(self, gesprek: str) -> list[dict[str, Any]]:
        with self._lock:
            rijen = self._db.execute(
                "SELECT tijd, soort, tekst, extra, voorstel FROM berichten WHERE gesprek = ?"
                " ORDER BY nr", (gesprek,)).fetchall()
        return [{"tijd": r[0], "soort": r[1], "tekst": r[2],
                 "extra": json.loads(r[3]) if r[3] else None, "voorstel": r[4]} for r in rijen]

    def gesprek_van_voorstel(self, voorstel: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT gesprek FROM berichten WHERE voorstel = ? AND soort = 'voorstel'",
                                 (voorstel,)).fetchone()
        return r[0] if r else None

    def claim_vervolg(self, voorstel: str) -> bool:
        """Een vervolgbeurt na een uitkomst mag precies een keer."""
        with self._lock:
            c = self._db.execute(
                "UPDATE berichten SET extra = json_set(coalesce(extra, '{}'), '$.vervolgd', 1)"
                " WHERE voorstel = ? AND soort = 'uitkomst'"
                " AND coalesce(json_extract(extra, '$.vervolgd'), 0) = 0", (voorstel,))
        return c.rowcount == 1
