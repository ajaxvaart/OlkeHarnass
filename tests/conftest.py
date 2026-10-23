from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from olkeharnass.auth import hash_key
from olkeharnass.config import Settings
from olkeharnass.main import maak_app

PA_KEY = "hk_test_pa"
DASH_KEY = "hk_test_dashboard"


def tekst_antwoord(tekst: str) -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "model": "nep",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": tekst}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def tool_antwoord(naam: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "model": "nep",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": naam, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }


class NepLLM:
    """Geeft vooraf ingestelde antwoorden terug en onthoudt elke aanroep."""

    def __init__(self, antwoorden: list[dict[str, Any]]) -> None:
        self.antwoorden = list(antwoorden)
        self.aanroepen: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.aanroepen.append(json.loads(json.dumps(kwargs)))  # kopie, lijsten muteren later
        if not self.antwoorden:
            raise AssertionError("NepLLM heeft geen antwoorden meer")
        return self.antwoorden.pop(0)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({
        "models": {
            "lokaal-klein": {"litellm_model": "ollama_chat/qwen3:4b",
                             "api_base": "http://127.0.0.1:11434"},
            "cloud-sterk": {"litellm_model": "anthropic/claude-sonnet-5-5"},
        },
        "apps": {
            "pa": {"key_sha256": hash_key(PA_KEY), "models": ["*"],
                   "tools": ["huidige_tijd", "rekenen"]},
            "dashboard": {"key_sha256": hash_key(DASH_KEY), "models": ["lokaal-klein"],
                          "tools": ["huidige_tijd"], "max_tool_rondes": 1},
        },
    })


@pytest.fixture
def maak_client(settings):
    def _maak(antwoorden: list[dict[str, Any]]) -> tuple[TestClient, NepLLM]:
        llm = NepLLM(antwoorden)
        return TestClient(maak_app(settings, llm=llm)), llm
    return _maak


def auth(key: str = PA_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
