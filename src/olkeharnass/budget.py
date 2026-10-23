"""Tokenbudget per app, bijgehouden in een SQLite-bestand.

Er wordt per app per dag opgeteld hoeveel tokens (invoer plus uitvoer) de
cloudmodellen hebben verbruikt. Lokale modellen tellen standaard niet mee.

De controle gebeurt voor elke modelaanroep, ook binnen de tool-lus. Een
verzoek kan het budget dus met hooguit een aanroep overschrijden.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import AppConfig


@dataclass(frozen=True)
class Gebruik:
    dag: int
    maand: int


class Gebruiksregister:
    def __init__(self, pad: str | Path) -> None:
        pad = str(pad)
        if pad != ":memory:":
            Path(pad).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(pad, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS gebruik ("
                " app TEXT NOT NULL, dag TEXT NOT NULL, tokens INTEGER NOT NULL,"
                " PRIMARY KEY (app, dag))"
            )

    @staticmethod
    def _vandaag() -> date:
        return datetime.now().date()  # lokale tijd van de laptop

    def voeg_toe(self, app: str, tokens: int, dag: date | None = None) -> None:
        if tokens <= 0:
            return
        dag = dag or self._vandaag()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO gebruik (app, dag, tokens) VALUES (?, ?, ?)"
                " ON CONFLICT (app, dag) DO UPDATE SET tokens = tokens + excluded.tokens",
                (app, dag.isoformat(), tokens),
            )

    def gebruik(self, app: str, dag: date | None = None) -> Gebruik:
        dag = dag or self._vandaag()
        with self._lock:
            vandaag = self._db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM gebruik WHERE app = ? AND dag = ?",
                (app, dag.isoformat()),
            ).fetchone()[0]
            maand = self._db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM gebruik WHERE app = ? AND dag LIKE ?",
                (app, dag.strftime("%Y-%m-") + "%"),
            ).fetchone()[0]
        return Gebruik(dag=vandaag, maand=maand)

    def sluit(self) -> None:
        self._db.close()


def overschreden(app: AppConfig, gebruik: Gebruik) -> str | None:
    """Geeft een foutmelding als een limiet bereikt is, anders None."""
    b = app.budget
    if b.tokens_per_dag is not None and gebruik.dag >= b.tokens_per_dag:
        return f"dagbudget bereikt ({gebruik.dag} van {b.tokens_per_dag} tokens)"
    if b.tokens_per_maand is not None and gebruik.maand >= b.tokens_per_maand:
        return f"maandbudget bereikt ({gebruik.maand} van {b.tokens_per_maand} tokens)"
    return None
