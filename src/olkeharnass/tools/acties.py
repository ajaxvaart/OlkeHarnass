"""Acties: tools die iets veranderen op de server.

Een actie wordt nooit direct uitgevoerd. Roept het model een actie-tool aan,
dan zet de harnass een voorstel klaar (via `ToolContext.stel_voor`). Pas als
Olke het voorstel in het bord goedkeurt, voert de server de bijbehorende
`uitvoer`-functie uit, precies een keer.

    class Args(BaseModel):
        naam: str
        reden: str = Field(description="Waarom deze actie nodig is")

    @actie("herstart_container", "Herstart een container.", risico="middel",
           titel=lambda a: f"{a.naam} herstarten", cmd=lambda a: f"docker restart {a.naam}")
    async def herstart(args: Args, ctx: ToolContext) -> str:
        ...  # voert het echt uit; geeft een korte uitkomst terug

Elk argumentenmodel heeft een veld `reden`: die komt als "waarom" in het voorstel.
`controle` (optioneel) wordt al bij het voorstellen gedraaid, zodat het model
meteen hoort dat iets niet mag; `uitvoer` moet zelf ook alles controleren.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel

from .registry import Tool, ToolContext, ToolRegistry, registry

Risico = Literal["laag", "middel", "hoog"]


@dataclass(frozen=True)
class Actie:
    naam: str
    risico: Risico
    args_model: type[BaseModel]
    titel: Callable[[Any], str]
    cmd: Callable[[Any], str]
    uitvoer: Callable[[Any, ToolContext], Awaitable[str]]
    controle: Callable[[Any, ToolContext], str | None] | None = None


acties: dict[str, Actie] = {}


def actie(
    naam: str,
    beschrijving: str,
    *,
    risico: Risico,
    titel: Callable[[Any], str],
    cmd: Callable[[Any], str],
    controle: Callable[[Any, ToolContext], str | None] | None = None,
    registry_: ToolRegistry | None = None,
    acties_: dict[str, Actie] | None = None,
):
    def decorator(fn: Callable[[Any, ToolContext], Awaitable[str]]):
        args_model = typing.get_type_hints(fn).get("args")
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"actie {naam!r}: `args` moet een pydantic-model zijn")
        if "reden" not in args_model.model_fields:
            raise TypeError(f"actie {naam!r}: het argumentenmodel mist het veld `reden`")
        a = Actie(naam, risico, args_model, titel, cmd, fn, controle)

        async def stel_voor(args: Any, ctx: ToolContext) -> dict[str, Any]:
            if ctx.stel_voor is None:
                return {"fout": "acties kunnen alleen via de assistent in het bord worden voorgesteld"}
            if controle is not None:
                fout = controle(args, ctx)
                if fout:
                    return {"fout": fout}
            v = await ctx.stel_voor(a, args)
            return {
                "voorgesteld": True,
                "id": v["id"],
                "titel": v["title"],
                "risico": risico,
                "status": "NOG NIET uitgevoerd; wacht op toestemming van Olke in het bord",
            }

        stel_voor.__annotations__ = {"args": args_model, "ctx": ToolContext, "return": dict}
        (registry_ or registry).registreer(
            Tool(naam, f"{beschrijving} Wordt pas uitgevoerd na toestemming van Olke.",
                 args_model, stel_voor)
        )
        (acties if acties_ is None else acties_)[naam] = a
        return fn

    return decorator
