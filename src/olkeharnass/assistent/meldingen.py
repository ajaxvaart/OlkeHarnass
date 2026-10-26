"""Logboek (jsonl) en ntfy-meldingen voor elk besluit over een actie.

Het logboek is de bron; ntfy is een seintje. Een mislukte melding mag een actie
nooit tegenhouden, een mislukte logregel wel: zonder spoor geen uitvoering.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..config import NtfyConfig

log = logging.getLogger("olkeharnass.assistent")


class Logboek:
    def __init__(self, pad: str) -> None:
        self.pad = Path(pad)
        self.pad.parent.mkdir(parents=True, exist_ok=True)

    def schrijf(self, gebeurtenis: str, **velden: Any) -> None:
        regel = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "gebeurtenis": gebeurtenis, **velden}
        with self.pad.open("a", encoding="utf-8") as f:
            f.write(json.dumps(regel, ensure_ascii=False, default=str) + "\n")


class Ntfy:
    def __init__(self, cfg: NtfyConfig | None, http: Any) -> None:
        self.cfg = cfg
        self.http = http

    async def stuur(self, titel: str, tekst: str, prioriteit: int = 3,
                    tags: str = "") -> None:
        if self.cfg is None:
            return
        koppen = {}
        if self.cfg.token_env and os.environ.get(self.cfg.token_env):
            koppen["Authorization"] = f"Bearer {os.environ[self.cfg.token_env]}"
        # JSON-variant van ntfy: dan mogen titel en tekst gewoon UTF-8 zijn.
        bericht = {"topic": self.cfg.topic, "title": titel, "message": tekst,
                   "priority": prioriteit, "tags": [t for t in tags.split(",") if t]}
        try:
            r = await self.http.post(self.cfg.url.rstrip("/"), json=bericht, headers=koppen,
                                     timeout=10)
            if r.status_code >= 400:
                log.warning("ntfy gaf HTTP %s", r.status_code)
        except Exception as e:
            log.warning("ntfy niet bereikbaar: %s", type(e).__name__)
