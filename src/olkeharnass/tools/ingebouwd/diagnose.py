"""Onderzoek van een dienst als tool, en acties die daarbij horen.

`onderzoek` geeft het model in een ronde een kort feitenblad met bekende problemen
en passende acties, in plaats van vier rondes losse tools.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...onderzoek import onderzoek as doe_onderzoek, voor_model
from ..acties import actie
from ..registry import ToolContext, tool
from .server import _dienst, _post

Diensten = Literal["sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd", "seerr", "jellyfin",
                   "immich", "vaultwarden", "cloudflared", "server"]


class OnderzoekArgs(BaseModel):
    dienst: Diensten = Field(description="Welke dienst; 'server' voor de schijven")


@tool("onderzoek", "Onderzoekt een dienst in een keer: containers, foutregels, meldingen van de app "
      "en bekende problemen met de acties die ze oplossen. Gebruik dit eerst bij een probleem.")
async def onderzoek(args: OnderzoekArgs, ctx: ToolContext) -> str:
    return voor_model(await doe_onderzoek(args.dienst, ctx))


class OndertitelArgs(BaseModel):
    soort: Literal["series", "films"]
    reden: str = Field(description="Kort waarom")


_TAAK = {"series": "wanted_search_missing_subtitles_series", "films": "wanted_search_missing_subtitles_movies"}


@actie("bazarr_zoek_ondertitels", "Laat Bazarr nu zoeken naar alle ontbrekende ondertitels.", risico="laag",
       titel=lambda a: f"Ondertitels zoeken ({a.soort})", cmd=lambda a: f"bazarr: taak {_TAAK[a.soort]}")
async def bazarr_zoek_ondertitels(args: OndertitelArgs, ctx: ToolContext) -> str:
    url, key = _dienst(ctx, "bazarr")
    await _post(ctx, f"{url}/api/system/tasks", headers={"X-API-KEY": key or ""},
                data={"taskid": _TAAK[args.soort]})
    return "Bazarr zoekt nu naar ontbrekende ondertitels; dat kan enkele minuten duren"
