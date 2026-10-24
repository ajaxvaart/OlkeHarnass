from __future__ import annotations

import json

from conftest import DASH_KEY, auth, tekst_antwoord, tool_antwoord

VRAAG = [{"role": "user", "content": "hoi"}]


def test_health_zonder_key(maak_client):
    client, _ = maak_client([])
    assert client.get("/health").json()["status"] == "ok"


def test_zonder_key_geweigerd(maak_client):
    client, _ = maak_client([])
    r = client.post("/v1/chat/completions", json={"model": "lokaal-klein", "messages": VRAAG})
    assert r.status_code == 401


def test_verkeerde_key_geweigerd(maak_client):
    client, _ = maak_client([])
    r = client.get("/v1/models", headers=auth("hk_fout"))
    assert r.status_code == 401


def test_models_per_app(maak_client):
    client, _ = maak_client([])
    pa = [m["id"] for m in client.get("/v1/models", headers=auth()).json()["data"]]
    dash = [m["id"] for m in client.get("/v1/models", headers=auth(DASH_KEY)).json()["data"]]
    assert sorted(pa) == ["cloud-sterk", "lokaal-klein"]
    assert dash == ["lokaal-klein"]


def test_tools_per_app(maak_client):
    client, _ = maak_client([])
    dash = client.get("/v1/tools", headers=auth(DASH_KEY)).json()["data"]
    assert [t["function"]["name"] for t in dash] == ["huidige_tijd"]


def test_model_niet_toegestaan(maak_client):
    client, llm = maak_client([])
    r = client.post("/v1/chat/completions", headers=auth(DASH_KEY),
                    json={"model": "cloud-sterk", "messages": VRAAG})
    assert r.status_code == 403
    assert llm.aanroepen == []


def test_onbekend_model(maak_client):
    client, _ = maak_client([])
    r = client.post("/v1/chat/completions", headers=auth(),
                    json={"model": "bestaat-niet", "messages": VRAAG})
    assert r.status_code == 404
    assert "error" in r.json()


def test_eenvoudig_antwoord_en_litellm_params(maak_client):
    client, llm = maak_client([tekst_antwoord("hallo")])
    r = client.post("/v1/chat/completions", headers=auth(),
                    json={"model": "lokaal-klein", "messages": VRAAG, "temperature": 0.2,
                          "onzin_param": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "hallo"
    assert body["model"] == "lokaal-klein"
    assert body["harnass"]["rondes"] == 0
    call = llm.aanroepen[0]
    assert call["model"] == "ollama_chat/qwen3:4b"
    assert call["api_base"] == "http://127.0.0.1:11434"
    assert call["temperature"] == 0.2
    assert "onzin_param" not in call
    assert {t["function"]["name"] for t in call["tools"]} == {"huidige_tijd", "rekenen"}


def test_tool_lus_voert_tool_uit(maak_client):
    client, llm = maak_client([
        tool_antwoord("rekenen", {"expressie": "6 * 7"}),
        tekst_antwoord("Het is 42."),
    ])
    r = client.post("/v1/chat/completions", headers=auth(),
                    json={"model": "cloud-sterk", "messages": VRAAG})
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Het is 42."
    assert body["harnass"]["rondes"] == 1
    assert body["harnass"]["tool_calls"][0]["tool"] == "rekenen"
    assert body["usage"]["total_tokens"] == 28 + 15
    # Tweede aanroep bevat het tool-resultaat
    tweede = llm.aanroepen[1]["messages"]
    assert tweede[-2]["role"] == "assistant" and tweede[-2]["tool_calls"][0]["id"] == "call_1"
    assert tweede[-1]["role"] == "tool"
    assert json.loads(tweede[-1]["content"])["uitkomst"] == 42


def test_tool_van_andere_app_wordt_niet_uitgevoerd(maak_client):
    # Dashboard mag `rekenen` niet; als het model hem toch aanroept, krijgt het een fout terug.
    client, llm = maak_client([
        tool_antwoord("rekenen", {"expressie": "1+1"}),
        tekst_antwoord("sorry"),
    ])
    r = client.post("/v1/chat/completions", headers=auth(DASH_KEY),
                    json={"model": "lokaal-klein", "messages": VRAAG})
    assert r.status_code == 200
    assert "onbekende tool" in llm.aanroepen[1]["messages"][-1]["content"]


def test_max_rondes(maak_client):
    # Dashboard heeft max_tool_rondes 1: na een ronde volgt een call zonder tools.
    client, llm = maak_client([
        tool_antwoord("huidige_tijd", {}),
        tool_antwoord("huidige_tijd", {}, "call_2"),
    ])
    r = client.post("/v1/chat/completions", headers=auth(DASH_KEY),
                    json={"model": "lokaal-klein", "messages": VRAAG})
    assert r.status_code == 200
    assert len(llm.aanroepen) == 2
    assert "tools" not in llm.aanroepen[1]
    assert r.json()["choices"][0]["message"]["tool_calls"] is None


def test_harnass_tools_beperken(maak_client):
    client, llm = maak_client([tekst_antwoord("ok")])
    client.post("/v1/chat/completions", headers=auth(),
                json={"model": "lokaal-klein", "messages": VRAAG, "harnass_tools": []})
    assert "tools" not in llm.aanroepen[0]


def test_harnass_tools_niet_toegestaan(maak_client):
    client, _ = maak_client([])
    r = client.post("/v1/chat/completions", headers=auth(DASH_KEY),
                    json={"model": "lokaal-klein", "messages": VRAAG,
                          "harnass_tools": ["rekenen"]})
    assert r.status_code == 403


def test_doorgeefmodus_geeft_tool_calls_terug(maak_client):
    app_tool = {"type": "function", "function": {"name": "eigen_tool", "parameters": {}}}
    client, llm = maak_client([tool_antwoord("eigen_tool", {})])
    r = client.post("/v1/chat/completions", headers=auth(),
                    json={"model": "lokaal-klein", "messages": VRAAG, "tools": [app_tool]})
    body = r.json()
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "eigen_tool"
    assert llm.aanroepen[0]["tools"] == [app_tool]
    assert len(llm.aanroepen) == 1


def test_providerfout_wordt_nette_fout(settings):
    from fastapi.testclient import TestClient
    from olkeharnass.main import maak_app

    async def kapot(**kwargs):
        raise ConnectionError("ollama draait niet")

    client = TestClient(maak_app(settings, llm=kapot))
    r = client.post("/v1/chat/completions", headers=auth(),
                    json={"model": "lokaal-klein", "messages": VRAAG})
    assert r.status_code == 502
    assert "ollama draait niet" in r.json()["error"]["message"]
