"""De tool-lus: model -> tool -> model, tot het model een eindantwoord geeft.

Twee modi per verzoek:
- Harnass-tools (standaard): de harnass biedt de tools aan die de app mag
  gebruiken, voert tool-calls zelf uit en geeft de app alleen het eindantwoord.
- Doorgeven: stuurt de app zelf `tools` mee, dan worden die ongewijzigd
  doorgegeven en krijgt de app de tool-calls terug om zelf uit te voeren.
  Harnass-tools staan dan uit; combineren kan nog niet.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .budget import Gebruiksregister, Verbruik, bereken_kosten, overschreden
from .config import AppConfig, ModelConfig, Settings
from .llm import LLMFunctie
from .tools import ToolContext, ToolRegistry

log = logging.getLogger("olkeharnass.lus")

# Parameters die de app mag meegeven en die naar het model doorgaan.
DOORGEEF_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "response_format",
    "presence_penalty",
    "frequency_penalty",
    "user",
)


class VerzoekFout(Exception):
    def __init__(self, status: int, bericht: str, soort: str = "invalid_request_error") -> None:
        super().__init__(bericht)
        self.status = status
        self.bericht = bericht
        self.soort = soort


@dataclass
class LusResultaat:
    antwoord: dict[str, Any]
    rondes: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _actieve_tools(verzoek: dict[str, Any], app: AppConfig, registry: ToolRegistry) -> list[str]:
    toegestaan = [n for n in registry.namen() if app.mag_tool(n)]
    gevraagd = verzoek.get("harnass_tools")
    if gevraagd is None:
        return toegestaan
    if not isinstance(gevraagd, list):
        raise VerzoekFout(400, "harnass_tools moet een lijst met toolnamen zijn")
    niet_toegestaan = [n for n in gevraagd if n not in toegestaan]
    if niet_toegestaan:
        raise VerzoekFout(403, f"tools niet beschikbaar voor deze app: {niet_toegestaan}")
    return list(gevraagd)


class _BudgetLLM:
    """Wikkelt de LLM-functie: controleert het budget voor en boekt na elke aanroep."""

    def __init__(
        self, llm: LLMFunctie, register: Gebruiksregister | None,
        app_naam: str, app: AppConfig, model: ModelConfig, settings: Settings,
    ) -> None:
        self.llm, self.register, self.app_naam = llm, register, app_naam
        self.app, self.model, self.settings = app, model, settings

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if self.register is not None and self.model.kost_geld:
            fout = overschreden(self.app, self.register.gebruik(self.app_naam))
            if fout:
                log.warning("app=%s %s", self.app_naam, fout)
                raise VerzoekFout(429, fout, "budget_exceeded")
        antwoord = await self.llm(**kwargs)
        if self.register is not None:
            verbruik = Verbruik.uit_usage(antwoord.get("usage"))
            kosten = bereken_kosten(self.model.prijs, verbruik, self.settings)
            self.register.voeg_toe(self.app_naam, verbruik, kosten)
        return antwoord


def _tel_usage(totaal: dict[str, int], antwoord: dict[str, Any]) -> None:
    for k, v in (antwoord.get("usage") or {}).items():
        if isinstance(v, int):
            totaal[k] = totaal.get(k, 0) + v


async def voer_chat_uit(
    verzoek: dict[str, Any],
    app_naam: str,
    app: AppConfig,
    settings: Settings,
    registry: ToolRegistry,
    llm: LLMFunctie,
    register: Gebruiksregister | None = None,
) -> LusResultaat:
    model_naam = verzoek.get("model")
    if not isinstance(model_naam, str) or not model_naam:
        raise VerzoekFout(400, "veld `model` ontbreekt")
    model = settings.models.get(model_naam)
    if model is None:
        raise VerzoekFout(404, f"onbekend model {model_naam!r}")
    if not app.mag_model(model_naam):
        raise VerzoekFout(403, f"app {app_naam!r} mag model {model_naam!r} niet gebruiken")
    if verzoek.get("stream"):
        raise VerzoekFout(400, "streaming wordt nog niet ondersteund")
    berichten = verzoek.get("messages")
    if not isinstance(berichten, list) or not berichten:
        raise VerzoekFout(400, "veld `messages` ontbreekt of is leeg")

    llm = _BudgetLLM(llm, register, app_naam, app, model, settings)
    kwargs = model.litellm_kwargs()
    kwargs.update({k: verzoek[k] for k in DOORGEEF_PARAMS if k in verzoek})
    berichten = list(berichten)

    app_tools = verzoek.get("tools")
    if app_tools:
        # Doorgeefmodus: een enkele call, tool-calls gaan terug naar de app.
        kwargs["tools"] = app_tools
        if "tool_choice" in verzoek:
            kwargs["tool_choice"] = verzoek["tool_choice"]
        antwoord = await llm(messages=berichten, **kwargs)
        antwoord["model"] = model_naam
        return LusResultaat(antwoord=antwoord)

    tool_namen = _actieve_tools(verzoek, app, registry)
    schemas = [registry.get(n).openai_schema() for n in tool_namen]  # type: ignore[union-attr]
    max_rondes = app.max_tool_rondes or settings.tool_lus.max_rondes
    ctx = ToolContext(app=app_naam)
    usage: dict[str, int] = {}
    uitgevoerd: list[dict[str, Any]] = []

    for ronde in range(max_rondes + 1):
        laatste = ronde == max_rondes
        call_kwargs = dict(kwargs)
        if schemas and not laatste:
            call_kwargs["tools"] = schemas
        antwoord = await llm(messages=berichten, **call_kwargs)
        _tel_usage(usage, antwoord)

        bericht = antwoord["choices"][0]["message"]
        tool_calls = bericht.get("tool_calls") or []
        if not tool_calls or laatste:
            # Bij de laatste ronde zijn geen tools meer aangeboden; negeer eventuele calls.
            if laatste and tool_calls:
                log.warning("app=%s max_rondes (%d) bereikt", app_naam, max_rondes)
                bericht["tool_calls"] = None
            antwoord["model"] = model_naam
            if usage:
                antwoord["usage"] = usage
            return LusResultaat(antwoord=antwoord, rondes=ronde, tool_calls=uitgevoerd)

        berichten.append(
            {
                "role": "assistant",
                "content": bericht.get("content"),
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"].get("arguments") or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            naam = tc["function"]["name"]
            ruwe_args = tc["function"].get("arguments") or "{}"
            t = registry.get(naam) if naam in tool_namen else None
            start = time.monotonic()
            if t is None:
                uitkomst = f'{{"fout": "onbekende tool {naam}"}}'
            else:
                uitkomst = await t.voer_uit(ruwe_args, ctx)
            duur_ms = int((time.monotonic() - start) * 1000)
            log.info("app=%s tool=%s ronde=%d duur_ms=%d", app_naam, naam, ronde + 1, duur_ms)
            uitgevoerd.append({"tool": naam, "arguments": ruwe_args, "ronde": ronde + 1})
            berichten.append({"role": "tool", "tool_call_id": tc["id"], "content": uitkomst})

    raise AssertionError("onbereikbaar")  # de lus retourneert altijd in de laatste ronde
