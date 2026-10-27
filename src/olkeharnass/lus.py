"""De tool-lus: model -> tool -> model, tot het model een eindantwoord geeft.

Twee modi per verzoek:
- Harnass-tools (standaard): de harnass biedt de tools aan die de app mag
  gebruiken, voert tool-calls zelf uit en geeft de app alleen het eindantwoord.
- Doorgeven: stuurt de app zelf `tools` mee, dan worden die ongewijzigd
  doorgegeven en krijgt de app de tool-calls terug om zelf uit te voeren.
  Harnass-tools staan dan uit; combineren kan nog niet.

Beide modi werken met en zonder streaming. De lus is een async generator die
gebeurtenissen oplevert: ("delta", chunk) voor tekst die naar de app gestreamd
mag worden en als laatste ("klaar", LusResultaat). Zonder streaming komen er
geen delta's en wordt alleen het resultaat gebruikt.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .budget import Gebruiksregister, Verbruik, bereken_kosten, overschreden
from .config import AppConfig, ModelConfig, Settings
from .llm import LLMFunctie, LLMStroomFunctie
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
    doorgeven: bool = False


Gebeurtenis = tuple[str, Any]


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


def bouw_antwoord(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Zet gestreamde chunks om in een gewoon chat.completion-antwoord."""
    tekst: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason = None
    usage = None
    for c in chunks:
        if c.get("usage"):
            usage = c["usage"]
        for keuze in c.get("choices") or []:
            finish_reason = keuze.get("finish_reason") or finish_reason
            delta = keuze.get("delta") or {}
            if delta.get("content"):
                tekst.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                doel = tool_calls.setdefault(
                    tc.get("index", 0),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    doel["id"] = tc["id"]
                functie = tc.get("function") or {}
                if functie.get("name"):
                    doel["function"]["name"] += functie["name"]
                if functie.get("arguments"):
                    doel["function"]["arguments"] += functie["arguments"]
    eerste = chunks[0] if chunks else {}
    return {
        "id": eerste.get("id"),
        "object": "chat.completion",
        "created": eerste.get("created"),
        "model": eerste.get("model"),
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {
                "role": "assistant",
                "content": "".join(tekst) or None,
                "tool_calls": [tool_calls[i] for i in sorted(tool_calls)] or None,
            },
        }],
        "usage": usage,
    }


class _Aanroeper:
    """Roept het model aan, met budgetcontrole vooraf en boeking achteraf.

    Met streaming worden de chunks doorgegeven als ("delta", chunk); welke chunks
    door mogen bepaalt `filter`. Als laatste komt altijd ("antwoord", dict).
    """

    def __init__(
        self, llm: LLMFunctie, llm_stroom: LLMStroomFunctie | None,
        register: Gebruiksregister | None,
        app_naam: str, app: AppConfig, model: ModelConfig, settings: Settings,
    ) -> None:
        self.llm, self.llm_stroom, self.register = llm, llm_stroom, register
        self.app_naam, self.app, self.model, self.settings = app_naam, app, model, settings

    def _controleer(self) -> None:
        if self.register is not None and self.model.kost_geld:
            fout = overschreden(self.app, self.register.gebruik(self.app_naam))
            if fout:
                log.warning("app=%s %s", self.app_naam, fout)
                raise VerzoekFout(429, fout, "budget_exceeded")

    def _boek(self, antwoord: dict[str, Any]) -> None:
        if self.register is None:
            return
        if not antwoord.get("usage"):
            log.warning("app=%s geen usage van het model; niets geboekt", self.app_naam)
        verbruik = Verbruik.uit_usage(antwoord.get("usage"))
        kosten = bereken_kosten(self.model.prijs, verbruik, self.settings)
        self.register.voeg_toe(self.app_naam, verbruik, kosten)

    async def roep(self, filter, **kwargs: Any) -> AsyncIterator[Gebeurtenis]:
        self._controleer()
        if self.llm_stroom is None:
            antwoord = await self.llm(**kwargs)
        else:
            chunks = []
            async for chunk in self.llm_stroom(**kwargs):
                chunks.append(chunk)
                door = filter(chunk)
                if door is not None:
                    yield ("delta", door)
            antwoord = bouw_antwoord(chunks)
        self._boek(antwoord)
        yield ("antwoord", antwoord)


def _alles_behalve_usage(chunk: dict[str, Any]) -> dict[str, Any] | None:
    # Doorgeefmodus: alles door, de harnass stuurt zelf een usage-chunk aan het eind.
    if not chunk.get("choices"):
        return None
    return {**chunk, "usage": None}


def _alleen_tekst(chunk: dict[str, Any]) -> dict[str, Any] | None:
    # Harnass-modus: alleen tekst; tool-calls voert de harnass zelf uit.
    for keuze in chunk.get("choices") or []:
        inhoud = (keuze.get("delta") or {}).get("content")
        if inhoud:
            return {**chunk, "usage": None, "choices": [
                {"index": 0, "finish_reason": None, "delta": {"content": inhoud}}
            ]}
    return None


def _tel_usage(totaal: dict[str, int], antwoord: dict[str, Any]) -> None:
    for k, v in (antwoord.get("usage") or {}).items():
        if isinstance(v, int):
            totaal[k] = totaal.get(k, 0) + v


async def voer_chat_uit_stapsgewijs(
    verzoek: dict[str, Any],
    app_naam: str,
    app: AppConfig,
    settings: Settings,
    registry: ToolRegistry,
    llm: LLMFunctie,
    register: Gebruiksregister | None = None,
    llm_stroom: LLMStroomFunctie | None = None,
    tool_ctx: ToolContext | None = None,
) -> AsyncIterator[Gebeurtenis]:
    """De lus als generator. Met `llm_stroom` wordt er gestreamd."""
    model_naam = verzoek.get("model")
    if not isinstance(model_naam, str) or not model_naam:
        raise VerzoekFout(400, "veld `model` ontbreekt")
    model = settings.models.get(model_naam)
    if model is None:
        raise VerzoekFout(404, f"onbekend model {model_naam!r}")
    if not app.mag_model(model_naam):
        raise VerzoekFout(403, f"app {app_naam!r} mag model {model_naam!r} niet gebruiken")
    berichten = verzoek.get("messages")
    if not isinstance(berichten, list) or not berichten:
        raise VerzoekFout(400, "veld `messages` ontbreekt of is leeg")

    aanroeper = _Aanroeper(llm, llm_stroom, register, app_naam, app, model, settings)
    kwargs = model.litellm_kwargs()
    kwargs.update({k: verzoek[k] for k in DOORGEEF_PARAMS if k in verzoek})
    berichten = list(berichten)

    app_tools = verzoek.get("tools")
    if app_tools:
        # Doorgeefmodus: een enkele call, tool-calls gaan terug naar de app.
        kwargs["tools"] = app_tools
        if "tool_choice" in verzoek:
            kwargs["tool_choice"] = verzoek["tool_choice"]
        async for soort, data in aanroeper.roep(_alles_behalve_usage, messages=berichten, **kwargs):
            if soort == "delta":
                yield ("delta", data)
            else:
                data["model"] = model_naam
                yield ("klaar", LusResultaat(antwoord=data, doorgeven=True))
        return

    tool_namen = _actieve_tools(verzoek, app, registry)
    schemas = [registry.get(n).openai_schema() for n in tool_namen]  # type: ignore[union-attr]
    max_rondes = app.max_tool_rondes or settings.tool_lus.max_rondes
    ctx = tool_ctx or ToolContext(app=app_naam)
    usage: dict[str, int] = {}
    uitgevoerd: list[dict[str, Any]] = []
    tekst_gestreamd = False

    for ronde in range(max_rondes + 1):
        laatste = ronde == max_rondes
        call_kwargs = dict(kwargs)
        if schemas and not laatste:
            call_kwargs["tools"] = schemas
        antwoord: dict[str, Any] = {}
        eerste_delta = True
        async for soort, data in aanroeper.roep(_alleen_tekst, messages=berichten, **call_kwargs):
            if soort == "delta":
                if eerste_delta and tekst_gestreamd:
                    # Tekst uit een eerdere ronde loopt anders aan elkaar vast.
                    data["choices"][0]["delta"]["content"] = "\n\n" + data["choices"][0]["delta"]["content"]
                eerste_delta, tekst_gestreamd = False, True
                yield ("delta", data)
            else:
                antwoord = data
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
            yield ("klaar", LusResultaat(antwoord=antwoord, rondes=ronde, tool_calls=uitgevoerd))
            return

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
            # Voor wie live wil meekijken (de assistent in het bord); de HTTP-stream negeert dit.
            yield ("tool", {"fase": "start", "naam": naam, "arguments": ruwe_args})
            start = time.monotonic()
            if t is None:
                uitkomst = f'{{"fout": "onbekende tool {naam}"}}'
            else:
                uitkomst = await t.voer_uit(ruwe_args, ctx)
            duur_ms = int((time.monotonic() - start) * 1000)
            log.info("app=%s tool=%s ronde=%d duur_ms=%d", app_naam, naam, ronde + 1, duur_ms)
            yield ("tool", {"fase": "klaar", "naam": naam, "duur_ms": duur_ms,
                            "ok": not uitkomst.startswith('{"fout"')})
            uitgevoerd.append({"tool": naam, "arguments": ruwe_args, "ronde": ronde + 1})
            berichten.append({"role": "tool", "tool_call_id": tc["id"], "content": uitkomst})


async def voer_chat_uit(*args: Any, **kwargs: Any) -> LusResultaat:
    """Zonder streaming: draait de lus en geeft het eindresultaat."""
    async for soort, data in voer_chat_uit_stapsgewijs(*args, **kwargs):
        if soort == "klaar":
            return data
    raise AssertionError("lus eindigde zonder resultaat")
