"""HTTP-laag: OpenAI-compatibele endpoints."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .auth import app_uit_verzoek
from .budget import Gebruiksregister
from .config import AppConfig, Settings, laad_settings
from .llm import LLMFunctie, LLMStroomFunctie, litellm_completion, litellm_stroom
from .lus import LusResultaat, VerzoekFout, voer_chat_uit, voer_chat_uit_stapsgewijs
from .tools import ToolRegistry
from .tools import registry as standaard_registry

log = logging.getLogger("olkeharnass")


def _fout(status: int, bericht: str, soort: str = "invalid_request_error") -> JSONResponse:
    # Zelfde vorm als OpenAI-fouten, zodat bestaande clients ze netjes tonen.
    return JSONResponse(status_code=status, content={"error": {"message": bericht, "type": soort}})


def _providerfout(e: Exception) -> tuple[int, str]:
    # 4xx van de provider (bijv. 429 rate limit, 401 foute cloud-key) doorgeven,
    # al het andere is een 502: de harnass zelf werkt, de upstream niet.
    status = getattr(e, "status_code", None)
    status = status if isinstance(status, int) and 400 <= status < 500 else 502
    regel = (str(e).strip().splitlines() or [""])[0][:300]
    return status, f"fout bij model: {type(e).__name__}: {regel}"


def _sse(data: dict[str, Any] | str) -> str:
    tekst = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"data: {tekst}\n\n"


def _log_resultaat(app_naam: str, model: Any, res: LusResultaat, start: float) -> None:
    usage = res.antwoord.get("usage") or {}
    log.info(
        "app=%s model=%s rondes=%d tokens_in=%s tokens_uit=%s duur_ms=%d",
        app_naam, model, res.rondes, usage.get("prompt_tokens"),
        usage.get("completion_tokens"), int((time.monotonic() - start) * 1000),
    )


def maak_app(
    settings: Settings | None = None,
    llm: LLMFunctie = litellm_completion,
    llm_stroom: LLMStroomFunctie = litellm_stroom,
    registry: ToolRegistry = standaard_registry,
    register: Gebruiksregister | None = None,
) -> FastAPI:
    api = FastAPI(title="OlkeHarnass", version=__version__)
    api.state.settings = settings or laad_settings()
    api.state.register = register or Gebruiksregister(api.state.settings.opslag.database)
    api.state.llm = llm
    api.state.llm_stroom = llm_stroom
    api.state.registry = registry

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "versie": __version__}

    @api.get("/v1/models")
    async def modellen(app_info: tuple[str, AppConfig] = Depends(app_uit_verzoek)) -> dict:
        _, app = app_info
        namen = [n for n in api.state.settings.models if app.mag_model(n)]
        return {
            "object": "list",
            "data": [{"id": n, "object": "model", "owned_by": "olkeharnass"} for n in namen],
        }

    @api.get("/v1/tools")
    async def tools(app_info: tuple[str, AppConfig] = Depends(app_uit_verzoek)) -> dict:
        """Geen OpenAI-endpoint: laat zien welke harnass-tools deze app mag gebruiken."""
        _, app = app_info
        reg: ToolRegistry = api.state.registry
        return {"data": [reg.get(n).openai_schema() for n in reg.namen() if app.mag_tool(n)]}

    @api.get("/v1/budget")
    async def budget(app_info: tuple[str, AppConfig] = Depends(app_uit_verzoek)) -> dict:
        """Geen OpenAI-endpoint: verbruik en limieten van deze app."""
        app_naam, app = app_info
        g = api.state.register.gebruik(app_naam)

        def periode(p, limiet):
            return {
                "kosten_euro": round(p.kosten, 4),
                "limiet_euro": limiet,
                "tokens_invoer": p.tokens_invoer,
                "tokens_cache_lezen": p.tokens_cache_lezen,
                "tokens_cache_schrijven": p.tokens_cache_schrijven,
                "tokens_uitvoer": p.tokens_uitvoer,
            }

        return {
            "app": app_naam,
            "dag": periode(g.dag, app.budget.euro_per_dag),
            "maand": periode(g.maand, app.budget.euro_per_maand),
        }

    @api.post("/v1/chat/completions")
    async def chat(
        request: Request, app_info: tuple[str, AppConfig] = Depends(app_uit_verzoek)
    ) -> Any:
        app_naam, app = app_info
        try:
            verzoek = await request.json()
        except ValueError:
            return _fout(400, "body is geen geldige JSON")
        if not isinstance(verzoek, dict):
            return _fout(400, "body moet een JSON-object zijn")

        start = time.monotonic()
        args = (verzoek, app_naam, app, api.state.settings, api.state.registry,
                api.state.llm, api.state.register)
        if verzoek.get("stream"):
            return await _stream(args, app_naam, verzoek.get("model"), start)
        try:
            res = await voer_chat_uit(*args)
        except VerzoekFout as e:
            return _fout(e.status, e.bericht, e.soort)
        except Exception as e:  # fout bij de provider of in LiteLLM
            log.exception("app=%s model=%s providerfout", app_naam, verzoek.get("model"))
            status, bericht = _providerfout(e)
            return _fout(status, bericht, "upstream_error")
        _log_resultaat(app_naam, verzoek.get("model"), res, start)
        # Extra veld; OpenAI-clients negeren onbekende velden.
        res.antwoord["harnass"] = {"rondes": res.rondes, "tool_calls": res.tool_calls}
        return res.antwoord

    async def _stream(args: tuple, app_naam: str, model: Any, start: float) -> Any:
        """Server-sent events in OpenAI-formaat.

        Fouten voor de eerste tekst (onbekend model, budget op, provider onbereikbaar)
        worden een gewone HTTP-fout. Fouten daarna komen als `error`-event in de stroom.
        """
        gen = voer_chat_uit_stapsgewijs(*args, llm_stroom=api.state.llm_stroom)
        stroom_id = f"chatcmpl-{uuid.uuid4().hex}"
        aangemaakt = int(time.time())

        def chunk(**velden: Any) -> dict[str, Any]:
            return {"id": stroom_id, "object": "chat.completion.chunk",
                    "created": aangemaakt, "model": model, **velden}

        try:
            eerste = await gen.__anext__()
        except VerzoekFout as e:
            return _fout(e.status, e.bericht, e.soort)
        except Exception as e:
            log.exception("app=%s model=%s providerfout", app_naam, model)
            status, bericht = _providerfout(e)
            return _fout(status, bericht, "upstream_error")

        async def events() -> AsyncIterator[str]:
            yield _sse(chunk(choices=[{"index": 0, "finish_reason": None,
                                       "delta": {"role": "assistant", "content": ""}}]))
            gebeurtenis = eerste
            try:
                while True:
                    soort, data = gebeurtenis
                    if soort == "delta":
                        yield _sse({**data, "id": stroom_id, "created": aangemaakt,
                                    "model": model})
                    else:
                        res: LusResultaat = data
                        _log_resultaat(app_naam, model, res, start)
                        if not res.doorgeven:
                            finish = res.antwoord["choices"][0].get("finish_reason")
                            yield _sse(chunk(choices=[{
                                "index": 0, "delta": {},
                                "finish_reason": "stop" if finish == "tool_calls" else finish,
                            }]))
                        yield _sse(chunk(
                            choices=[], usage=res.antwoord.get("usage"),
                            harnass={"rondes": res.rondes, "tool_calls": res.tool_calls},
                        ))
                        break
                    gebeurtenis = await gen.__anext__()
            except VerzoekFout as e:
                yield _sse({"error": {"message": e.bericht, "type": e.soort}})
            except Exception as e:
                log.exception("app=%s model=%s providerfout tijdens stream", app_naam, model)
                yield _sse({"error": {"message": _providerfout(e)[1], "type": "upstream_error"}})
            yield _sse("[DONE]")

        return StreamingResponse(events(), media_type="text/event-stream")

    return api


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = laad_settings()
    uvicorn.run(maak_app(settings), host=settings.server.host, port=settings.server.port)


if __name__ == "__main__":
    run()
