"""Kostenbudget per app, bijgehouden in een SQLite-bestand.

Na elke modelaanroep worden de invoer- en uitvoertokens en de kosten in euro
geboekt, per app per dag. De kosten volgen uit de prijs per model in de
config, zodat het niet afhangt van de prijstabel van LiteLLM. Lokale modellen
worden ook geboekt (handig als statistiek), maar kosten niets.

Het budget wordt gecontroleerd voor elke aanroep van een model dat geld kost,
ook binnen de tool-lus. Een verzoek kan het budget dus met hooguit een aanroep
overschrijden. Lokale modellen blijven werken als het budget op is.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import AppConfig


@dataclass(frozen=True)
class Periode:
    tokens_invoer: int
    tokens_uitvoer: int
    kosten: float  # euro


@dataclass(frozen=True)
class Gebruik:
    dag: Periode
    maand: Periode


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
                " app TEXT NOT NULL, dag TEXT NOT NULL,"
                " tokens_invoer INTEGER NOT NULL, tokens_uitvoer INTEGER NOT NULL,"
                " kosten REAL NOT NULL,"
                " PRIMARY KEY (app, dag))"
            )

    @staticmethod
    def _vandaag() -> date:
        return datetime.now().date()  # lokale tijd van de laptop

    def voeg_toe(
        self, app: str, tokens_invoer: int, tokens_uitvoer: int, kosten: float,
        dag: date | None = None,
    ) -> None:
        if tokens_invoer <= 0 and tokens_uitvoer <= 0 and kosten <= 0:
            return
        dag = dag or self._vandaag()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO gebruik (app, dag, tokens_invoer, tokens_uitvoer, kosten)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (app, dag) DO UPDATE SET"
                "  tokens_invoer = tokens_invoer + excluded.tokens_invoer,"
                "  tokens_uitvoer = tokens_uitvoer + excluded.tokens_uitvoer,"
                "  kosten = kosten + excluded.kosten",
                (app, dag.isoformat(), tokens_invoer, tokens_uitvoer, kosten),
            )

    def _som(self, app: str, waar: str, waarde: str) -> Periode:
        rij = self._db.execute(
            "SELECT COALESCE(SUM(tokens_invoer), 0), COALESCE(SUM(tokens_uitvoer), 0),"
            f" COALESCE(SUM(kosten), 0.0) FROM gebruik WHERE app = ? AND {waar}",
            (app, waarde),
        ).fetchone()
        return Periode(tokens_invoer=rij[0], tokens_uitvoer=rij[1], kosten=rij[2])

    def gebruik(self, app: str, dag: date | None = None) -> Gebruik:
        dag = dag or self._vandaag()
        with self._lock:
            return Gebruik(
                dag=self._som(app, "dag = ?", dag.isoformat()),
                maand=self._som(app, "dag LIKE ?", dag.strftime("%Y-%m-") + "%"),
            )

    def sluit(self) -> None:
        self._db.close()


def overschreden(app: AppConfig, gebruik: Gebruik) -> str | None:
    """Geeft een foutmelding als een limiet bereikt is, anders None."""
    b = app.budget
    if b.euro_per_dag is not None and gebruik.dag.kosten >= b.euro_per_dag:
        return f"dagbudget bereikt (EUR {gebruik.dag.kosten:.2f} van {b.euro_per_dag:.2f})"
    if b.euro_per_maand is not None and gebruik.maand.kosten >= b.euro_per_maand:
        return f"maandbudget bereikt (EUR {gebruik.maand.kosten:.2f} van {b.euro_per_maand:.2f})"
    return None
