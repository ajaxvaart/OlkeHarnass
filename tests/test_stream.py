from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import DASH_KEY, NepLLM, auth, naar_chunks, tekst_antwoord, tool_antwoord
from olkeharnass.budget import Gebruiksregister
from olkeharnass.lus import bouw_antwoord
from olkeharnass.main import maak_app

VRAAG = [{"role": "user", "content": "hoi"}]


def _events(r) -> list:
    """Leest de SSE-stroom; geeft dicts en de string '[DONE]'."""
    uit = []
    for regel in r.text.splitlines():
        if regel.startswith("data: "):
            data = regel[len("data: "):]
            uit.append(data if data == "[DONE]" else json.loads(data))
    return uit


def _tekst(events) -> str:
    return "".join(
        (c["choices"][0]["delta"].get("content") or "")
        for c in events if isinstance(c, dict) and c.get("choices")
    )


def _stream(client, key=None, **extra):
    body = {"model": "lokaal-klein", "messages": VRAAG, "stream": True, **extra}
    return client.post("/v1/chat/completions", headers=auth(key) if key else auth(), json=body)


def test_bouw_antwoord_uit_chunks():
    origineel = tool_antwoord("rekenen", {"expressie": "6 * 7"})
    gebouwd = bouw_antwoord(naar_chunks(origineel))
    assert gebouwd["choices"][0]["message"]["tool_calls"] == \
        origineel["choices"][0]["message"]["tool_calls"]
    assert gebouwd["usage"] == origineel["usage"]
    assert bouw_antwoord(naar_chunks(tekst_antwoord("hallo wereld")))["choices"][0][
        "message"]["content"] == "hallo wereld"


def test_stream_tekst(maak_client):
    client, _ = maak_client([tekst_antwoord("hallo wereld")])
    r = _stream(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r)
    assert ev[-1] == "[DONE]"
    assert _tekst(ev) == "hallo wereld"
    ids = {c["id"] for c in ev if isinstance(c, dict)}
    assert len(ids) == 1
    assert all(c["model"] == "lokaal-klein" for c in ev if isinstance(c, dict))
    slot = ev[-2]
    assert slot["usage"]["total_tokens"] == 15
    assert slot["harnass"]["rondes"] == 0
    assert ev[-3]["choices"][0]["finish_reason"] == "stop"


def test_stream_met_tool_lus(maak_client):
    client, llm = maak_client([
        tool_antwoord("rekenen", {"expressie": "6 * 7"}),
        tekst_antwoord("Het is 42."),
    ])
    ev = _events(_stream(client))
    assert _tekst(ev) == "Het is 42."
    # Geen tool-call-delta's naar de app: die voert de harnass zelf uit.
    assert not any(c["choices"][0]["delta"].get("tool_calls")
                   for c in ev if isinstance(c, dict) and c.get("choices"))
    assert ev[-2]["harnass"]["tool_calls"][0]["tool"] == "rekenen"
    assert ev[-2]["usage"]["total_tokens"] == 28 + 15
    # Het tool-resultaat ging terug naar het model
    assert json.loads(llm.aanroepen[1]["messages"][-1]["content"])["uitkomst"] == 42


def test_stream_tekst_uit_tool_ronde_wordt_gescheiden(maak_client):
    met_tekst = tool_antwoord("rekenen", {"expressie": "1+1"})
    met_tekst["choices"][0]["message"]["content"] = "Even rekenen."
    client, _ = maak_client([met_tekst, tekst_antwoord("Het is 2.")])
    assert _tekst(_events(_stream(client))) == "Even rekenen.\n\nHet is 2."


def test_stream_doorgeefmodus(maak_client):
    app_tool = {"type": "function", "function": {"name": "eigen_tool", "parameters": {}}}
    client, _ = maak_client([tool_antwoord("eigen_tool", {"a": 1})])
    ev = _events(_stream(client, tools=[app_tool]))
    gebouwd = bouw_antwoord([c for c in ev if isinstance(c, dict)])
    tc = gebouwd["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "eigen_tool"
    assert json.loads(tc["function"]["arguments"]) == {"a": 1}
    assert gebouwd["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_fout_voor_start_is_gewone_http_fout(maak_client):
    client, _ = maak_client([])
    r = _stream(client, DASH_KEY, model="cloud-sterk")
    assert r.status_code == 403
    assert "error" in r.json()


def test_stream_providerfout_voor_start(settings):
    async def kapot(**kwargs):
        raise ConnectionError("ollama draait niet")
        yield  # pragma: no cover

    client = TestClient(maak_app(settings, llm=NepLLM([]), llm_stroom=kapot,
                                 register=Gebruiksregister(":memory:")))
    r = _stream(client)
    assert r.status_code == 502


def test_stream_fout_halverwege_wordt_error_event(settings):
    async def breekt(**kwargs):
        for c in naar_chunks(tekst_antwoord("hallo wereld"))[:3]:
            yield c
        raise ConnectionError("verbinding weg")

    client = TestClient(maak_app(settings, llm=NepLLM([]), llm_stroom=breekt,
                                 register=Gebruiksregister(":memory:")))
    r = _stream(client)
    assert r.status_code == 200
    ev = _events(r)
    assert ev[-1] == "[DONE]"
    assert "verbinding weg" in ev[-2]["error"]["message"]


def test_stream_boekt_budget(settings):
    llm = NepLLM([tekst_antwoord("a")])
    register = Gebruiksregister(":memory:")
    client = TestClient(maak_app(settings, llm=llm, llm_stroom=llm.stroom, register=register))
    _stream(client, model="cloud-sterk")
    # cloud-sterk: 3 euro / 15 euro per miljoen; 10 in, 5 uit
    assert register.gebruik("pa").dag.kosten == pytest.approx((10 * 3 + 5 * 15) / 1e6)
