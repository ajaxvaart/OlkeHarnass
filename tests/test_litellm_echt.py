"""Draait de echte LiteLLM-bibliotheek (zonder netwerk, via mock_response).

Bedoeld om een kapotte LiteLLM-release te vangen voordat hij op de laptop
staat: draai de tests na elke versie-update.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import PA_KEY, auth
from olkeharnass.auth import hash_key
from olkeharnass.config import Settings
from olkeharnass.main import maak_app


def _client() -> TestClient:
    settings = Settings.model_validate({
        "opslag": {"database": ":memory:"},
        "models": {
            "nep": {"litellm_model": "openai/gpt-nep", "prijs": {"valuta": "EUR", "invoer": 1, "uitvoer": 1},
                    "extra": {
                "api_key": "x", "mock_response": "hallo vanuit litellm"}},
            "dood": {"litellm_model": "ollama_chat/qwen3:4b",
                     "api_base": "http://127.0.0.1:9", "timeout": 5},
        },
        # tools aan: met tooldefinities faalde LiteLLM 1.106.2 (ontbrekende module mcp)
        "apps": {"pa": {"key_sha256": hash_key(PA_KEY), "models": ["*"], "tools": ["*"]}},
    })
    return TestClient(maak_app(settings))


def test_litellm_completion_werkt():
    r = _client().post("/v1/chat/completions", headers=auth(),
                       json={"model": "nep", "messages": [{"role": "user", "content": "hoi"}]})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hallo vanuit litellm"


def test_onbereikbare_ollama_geeft_502():
    r = _client().post("/v1/chat/completions", headers=auth(),
                       json={"model": "dood", "messages": [{"role": "user", "content": "hoi"}]})
    assert r.status_code == 502
    assert "\n" not in r.json()["error"]["message"]


def test_litellm_stream_werkt():
    r = _client().post("/v1/chat/completions", headers=auth(),
                       json={"model": "nep", "stream": True,
                             "messages": [{"role": "user", "content": "hoi"}]})
    assert r.status_code == 200, r.text
    regels = [json.loads(x[6:]) for x in r.text.splitlines()
              if x.startswith("data: ") and x != "data: [DONE]"]
    tekst = "".join(c["choices"][0]["delta"].get("content") or ""
                    for c in regels if c.get("choices"))
    assert tekst == "hallo vanuit litellm"
    assert regels[-1]["usage"]["total_tokens"] > 0  # include_usage werkt
