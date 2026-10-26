"""Registratie van gedeelde tools.

Een tool is een async functie met een pydantic-model als argumenten:

    class Args(BaseModel):
        stad: str = Field(description="Naam van de stad")

    @tool("weer", "Geeft het weer in een stad.")
    async def weer(args: Args, ctx: ToolContext) -> str:
        ...

Het JSON-schema voor het model wordt uit het pydantic-model afgeleid. De
harnass valideert de argumenten die het model stuurt voordat de tool draait;
ongeldige argumenten gaan als foutmelding terug naar het model zodat het
zichzelf kan corrigeren.
"""

from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True)
class ToolContext:
    """Wat een tool weet over het verzoek.

    `diensten` en `http` zijn nodig voor de server-tools. `stel_voor` is er alleen
    in de assistent van het bord: daarmee zet een actie-tool een voorstel klaar
    dat Olke moet goedkeuren. Zonder `stel_voor` kan een actie niet voorgesteld worden.
    """

    app: str
    diensten: Any = None  # ServerToolsConfig
    http: Any = None  # httpx.AsyncClient
    stel_voor: Callable[..., Awaitable[dict[str, Any]]] | None = None


ToolFunctie = Callable[[Any, ToolContext], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    naam: str
    beschrijving: str
    args_model: type[BaseModel]
    functie: ToolFunctie

    def openai_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.naam,
                "description": self.beschrijving,
                "parameters": schema,
            },
        }

    async def voer_uit(self, ruwe_args: str, ctx: ToolContext) -> str:
        """Voert de tool uit en geeft altijd een string terug, ook bij fouten."""
        try:
            data = json.loads(ruwe_args or "{}")
            args = self.args_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            return json.dumps({"fout": f"ongeldige argumenten: {e}"}, ensure_ascii=False)
        try:
            resultaat = await self.functie(args, ctx)
        except Exception as e:  # een kapotte tool mag het verzoek niet laten crashen
            return json.dumps({"fout": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
        if isinstance(resultaat, str):
            return resultaat
        return json.dumps(resultaat, ensure_ascii=False, default=str)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def registreer(self, t: Tool) -> None:
        if t.naam in self._tools:
            raise ValueError(f"tool {t.naam!r} is al geregistreerd")
        self._tools[t.naam] = t

    def get(self, naam: str) -> Tool | None:
        return self._tools.get(naam)

    def namen(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, naam: str) -> bool:
        return naam in self._tools


# De standaard-registry waar @tool in registreert.
registry = ToolRegistry()


def tool(naam: str, beschrijving: str, *, registry_: ToolRegistry | None = None):
    def decorator(fn: ToolFunctie) -> ToolFunctie:
        args_model = typing.get_type_hints(fn).get("args")
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"tool {naam!r}: eerste parameter `args` moet een pydantic-model zijn")
        (registry_ or registry).registreer(Tool(naam, beschrijving, args_model, fn))
        return fn

    return decorator
