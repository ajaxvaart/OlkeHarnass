"""Onderzoek van een dienst: feiten verzamelen en bekende problemen herkennen, zonder model.

Zie het plan "assistent die storingen oplost": fase 1 (verzamelen) en 2 (herkennen)
zijn gewone code, zodat ook een klein lokaal model met een kort, betrouwbaar
feitenblad werkt in plaats van zelf tools te moeten kiezen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..tools.ingebouwd.server import kort
from ..tools.registry import ToolContext
from .catalogus import Treffer, herken
from .verzamel import DIENSTEN, Feiten, verzamel

NAMEN = {"sonarr": "Sonarr", "radarr": "Radarr", "prowlarr": "Prowlarr", "bazarr": "Bazarr",
         "sabnzbd": "SABnzbd", "seerr": "Seerr", "jellyfin": "Jellyfin", "immich": "Immich",
         "vaultwarden": "Vaultwarden", "cloudflared": "de tunnel (cloudflared)", "server": "de schijven"}


@dataclass
class Rapport:
    feiten: Feiten
    treffers: list[Treffer]


async def onderzoek(dienst: str, ctx: ToolContext) -> Rapport:
    feiten = await verzamel(dienst, ctx)
    return Rapport(feiten, herken(feiten))


def voor_model(r: Rapport, max_tekens: int = 1800) -> str:
    """Compact feitenblad met bevindingen, voor het lokale model."""
    d = r.feiten.als_dict()
    d["bevindingen"] = [{"ernst": t.ernst, "wat": t.wat,
                         **({"acties": [a for a, _ in t.acties]} if t.acties else {}),
                         **({"handwerk": t.handwerk} if t.handwerk else {})} for t in r.treffers]
    return kort(json.dumps(d, ensure_ascii=False, default=str), max_tekens)


def _containers_kort(f: Feiten) -> str:
    if not f.containers:
        return ""
    goed = [c for c in f.containers if c.get("staat") == "running" and c.get("health") != "unhealthy"]
    if len(goed) == len(f.containers):
        return f"Alle containers draaien ({len(goed)})."
    return ""


def als_tekst(r: Rapport) -> str:
    """Het rapport voor Olke in het gesprek."""
    naam = NAMEN.get(r.feiten.dienst, r.feiten.dienst)
    regels = []
    problemen = [t for t in r.treffers if t.ernst != "info"]
    if not problemen:
        regels.append(f"Geen storing gevonden bij {naam}." + (" Wel ter info:" if r.treffers else ""))
    else:
        regels.append(f"Bij {naam} zie ik {len(problemen)} {'probleem' if len(problemen) == 1 else 'problemen'}:")
    for t in r.treffers:
        regels.append(f"- {t.wat}" + (f" Zelf doen: {t.handwerk}" if t.handwerk and not t.acties else ""))
    if r.feiten.foutregels:
        regels.append("Laatste foutregels:")
        regels += [f"  {x}" for x in r.feiten.foutregels[-3:]]
    if r.feiten.leesfouten:
        regels.append("Kon niet lezen: " + "; ".join(r.feiten.leesfouten) + ".")
    c = _containers_kort(r.feiten)
    if c:
        regels.append(c)
    return "\n".join(regels)


__all__ = ["DIENSTEN", "NAMEN", "Rapport", "Treffer", "als_tekst", "onderzoek", "voor_model"]
