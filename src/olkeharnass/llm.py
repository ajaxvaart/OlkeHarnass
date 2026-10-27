"""Aansluiting op LiteLLM. Alles wat met providers praat loopt via `LLMFunctie`
en `LLMStroomFunctie`, zodat tests een nep-model kunnen invullen zonder netwerk."""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Awaitable, Callable

LLMFunctie = Callable[..., Awaitable[dict[str, Any]]]
LLMStroomFunctie = Callable[..., AsyncIterator[dict[str, Any]]]


def _litellm():
    # Pas hier importeren: litellm laden kost een paar seconden en flink wat geheugen.
    # Zonder LITELLM_MODE leest litellm bij het importeren een .env in de werkmap. Die
    # is van root (systemd leest hem) en de keys staan al in de omgeving: niet doen.
    os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
    import litellm

    litellm.drop_params = True  # parameters die een provider niet kent stilletjes weglaten
    return litellm


async def litellm_completion(**kwargs: Any) -> dict[str, Any]:
    antwoord = await _litellm().acompletion(**kwargs)
    return antwoord.model_dump()


async def litellm_stroom(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    # include_usage: de laatste chunk bevat het verbruik, nodig voor het budget.
    stroom = await _litellm().acompletion(
        stream=True, stream_options={"include_usage": True}, **kwargs
    )
    async for chunk in stroom:
        yield chunk.model_dump()
