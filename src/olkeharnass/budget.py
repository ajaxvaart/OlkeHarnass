"""Kostenbudget per app, bijgehouden in een SQLite-bestand.

Na elke modelaanroep worden de tokens en de kosten in euro geboekt, per app
per dag. De kosten volgen uit het verbruik dat de provider zelf teruggeeft
(gewone invoer, cache lezen, cache schrijven en uitvoer apart) en de prijs per
model in de config, niet uit de prijstabel van LiteLLM. Lokale modellen worden
ook geboekt (handig als statistiek), maar kosten niets.

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
from typing import Any

from .config import AppConfig, PrijsConfig, Settings


@dataclass(frozen=True)
class Verbruik:
    """Tokens van een aanroep. `invoer` is zonder de cache-tokens."""

    invoer: int = 0
    cache_lezen: int = 0
    cache_schrijven: int = 0
    uitvoer: int = 0

    @classmethod
    def uit_usage(cls, usage: dict[str, Any] | None) -> "Verbruik":
        """Leest het `usage`-blok zoals LiteLLM het teruggeeft.

        LiteLLM telt cache-tokens mee in `prompt_tokens`. Anthropic geeft ze apart
        als `cache_read_input_tokens` en `cache_creation_input_tokens`, OpenAI als
        `prompt_tokens_details.cached_tokens`.
        """
        u = usage or {}
        details = u.get("prompt_tokens_details") or {}
        lezen = int(u.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
        schrijven = int(
            u.get("cache_creation_input_tokens") or details.get("cache_creation_tokens") or 0
        )
        totaal_invoer = int(u.get("prompt_tokens") or 0)
        return cls(
            invoer=max(totaal_invoer - lezen - schrijven, 0),
            cache_lezen=lezen,
            cache_schrijven=schrijven,
            uitvoer=int(u.get("completion_tokens") or 0),
        )


def bereken_kosten(prijs: PrijsConfig | None, v: Verbruik, settings: Settings) -> float:
    """Kosten in euro van een aanroep."""
    if prijs is None:
        return 0.0
    lezen = prijs.cache_lezen if prijs.cache_lezen is not None else prijs.invoer
    schrijven = prijs.cache_schrijven if prijs.cache_schrijven is not None else prijs.invoer
    bedrag = (
        v.invoer * prijs.invoer
        + v.cache_lezen * lezen
        + v.cache_schrijven * schrijven
        + v.uitvoer * prijs.uitvoer
    ) / 1_000_000
    if prijs.valuta == "USD":
        bedrag *= settings.valuta.usd_naar_eur or 0.0  # validatie eist een koers
    return bedrag


@dataclass(frozen=True)
class Periode:
    tokens_invoer: int
    tokens_cache_lezen: int
    tokens_cache_schrijven: int
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
                " tokens_invoer INTEGER NOT NULL, tokens_cache_lezen INTEGER NOT NULL,"
                " tokens_cache_schrijven INTEGER NOT NULL, tokens_uitvoer INTEGER NOT NULL,"
                " kosten REAL NOT NULL,"
                " PRIMARY KEY (app, dag))"
            )

    @staticmethod
    def _vandaag() -> date:
        return datetime.now().date()  # lokale tijd van de laptop

    def voeg_toe(
        self, app: str, verbruik: Verbruik, kosten: float, dag: date | None = None
    ) -> None:
        v = verbruik
        if not (v.invoer or v.cache_lezen or v.cache_schrijven or v.uitvoer or kosten):
            return
        dag = dag or self._vandaag()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO gebruik (app, dag, tokens_invoer, tokens_cache_lezen,"
                " tokens_cache_schrijven, tokens_uitvoer, kosten) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (app, dag) DO UPDATE SET"
                "  tokens_invoer = tokens_invoer + excluded.tokens_invoer,"
                "  tokens_cache_lezen = tokens_cache_lezen + excluded.tokens_cache_lezen,"
                "  tokens_cache_schrijven = tokens_cache_schrijven + excluded.tokens_cache_schrijven,"
                "  tokens_uitvoer = tokens_uitvoer + excluded.tokens_uitvoer,"
                "  kosten = kosten + excluded.kosten",
                (app, dag.isoformat(), v.invoer, v.cache_lezen, v.cache_schrijven,
                 v.uitvoer, kosten),
            )

    def _som(self, app: str, waar: str, waarde: str) -> Periode:
        rij = self._db.execute(
            "SELECT COALESCE(SUM(tokens_invoer), 0), COALESCE(SUM(tokens_cache_lezen), 0),"
            " COALESCE(SUM(tokens_cache_schrijven), 0), COALESCE(SUM(tokens_uitvoer), 0),"
            f" COALESCE(SUM(kosten), 0.0) FROM gebruik WHERE app = ? AND {waar}",
            (app, waarde),
        ).fetchone()
        return Periode(*rij)

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
