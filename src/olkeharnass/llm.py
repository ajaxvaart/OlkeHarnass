"""Aansluiting op LiteLLM. Alles wat met providers praat loopt via `LLMFunctie`,
zodat tests een nep-model kunnen invullen zonder netwerk."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

LLMFunctie = Callable[..., Awaitable[dict[str, Any]]]


async def litellm_completion(**kwargs: Any) -> dict[str, Any]:
    # Pas hier importeren: litellm laden kost een paar seconden en flink wat geheugen.
    import litellm

    litellm.drop_params = True  # parameters die een provider niet kent stilletjes weglaten
    antwoord = await litellm.acompletion(**kwargs)
    return antwoord.model_dump()
