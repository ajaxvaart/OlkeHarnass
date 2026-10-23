from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from conftest import PA_KEY, NepLLM, auth, tekst_antwoord, tool_antwoord
from olkeharnass.auth import hash_key
from olkeharnass.budget import Gebruiksregister
from olkeharnass.config import Settings
from olkeharnass.main import maak_app

VRAAG = [{"role": "user", "content": "hoi"}]


def _settings(**budget) -> Settings:
    return Settings.model_validate({
        "models": {
            "cloud": {"litellm_model": "anthropic/claude-haiku-5-5"},
            "lokaal": {"litellm_model": "ollama_chat/qwen3:4b"},
            "lokaal-telt": {"litellm_model": "ollama_chat/qwen3:4b", "telt_voor_budget": True},
        },
        "apps": {"pa": {"key_sha256": hash_key(PA_KEY), "models": ["*"], "tools": ["*"],
                        "budget": budget}},
    })


def _client(antwoorden, register=None, **budget):
    register = register or Gebruiksregister(":memory:")
    llm = NepLLM(antwoorden)
    return TestClient(maak_app(_settings(**budget), llm=llm, register=register)), llm, register


def _vraag(client, model="cloud"):
    return client.post("/v1/chat/completions", headers=auth(),
                       json={"model": model, "messages": VRAAG})


def test_register_telt_per_dag_en_maand():
    r = Gebruiksregister(":memory:")
    r.voeg_toe("pa", 100, date(2026, 10, 1))
    r.voeg_toe("pa", 50, date(2026, 10, 23))
    r.voeg_toe("pa", 25, date(2026, 10, 23))
    r.voeg_toe("pa", 999, date(2026, 9, 30))
    r.voeg_toe("ander", 7, date(2026, 10, 23))
    g = r.gebruik("pa", date(2026, 10, 23))
    assert (g.dag, g.maand) == (75, 175)


def test_register_blijft_bewaard(tmp_path):
    pad = tmp_path / "sub" / "db.sqlite"
    r = Gebruiksregister(pad)
    r.voeg_toe("pa", 10)
    r.sluit()
    assert Gebruiksregister(pad).gebruik("pa").dag == 10


def test_cloudgebruik_wordt_geboekt():
    client, _, register = _client([tekst_antwoord("a")])
    assert _vraag(client).status_code == 200
    assert register.gebruik("pa").dag == 15


def test_tool_lus_boekt_elke_ronde():
    client, _, register = _client([tool_antwoord("rekenen", {"expressie": "1+1"}),
                                   tekst_antwoord("2")])
    _vraag(client)
    assert register.gebruik("pa").dag == 28 + 15


def test_lokaal_telt_standaard_niet_mee():
    client, _, register = _client([tekst_antwoord("a"), tekst_antwoord("b")])
    _vraag(client, "lokaal")
    assert register.gebruik("pa").dag == 0
    _vraag(client, "lokaal-telt")
    assert register.gebruik("pa").dag == 15


def test_dagbudget_bereikt_geeft_429_zonder_modelaanroep():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 100)
    client, llm, _ = _client([], register, tokens_per_dag=100)
    r = _vraag(client)
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "budget_exceeded"
    assert llm.aanroepen == []


def test_maandbudget_bereikt():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 500)
    client, _, _ = _client([], register, tokens_per_dag=10_000, tokens_per_maand=500)
    assert "maandbudget" in _vraag(client).json()["error"]["message"]


def test_lokaal_werkt_ook_als_budget_op_is():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 100)
    client, _, _ = _client([tekst_antwoord("a")], register, tokens_per_dag=100)
    assert _vraag(client, "lokaal").status_code == 200


def test_budget_stopt_tool_lus_halverwege():
    # Eerste ronde (28 tokens) brengt het verbruik boven de 20: tweede aanroep wordt geweigerd.
    client, llm, _ = _client([tool_antwoord("rekenen", {"expressie": "1+1"}),
                              tekst_antwoord("2")], tokens_per_dag=20)
    r = _vraag(client)
    assert r.status_code == 429
    assert len(llm.aanroepen) == 1


def test_budget_endpoint():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 42)
    client, _, _ = _client([], register, tokens_per_dag=100)
    body = client.get("/v1/budget", headers=auth()).json()
    assert body["dag"] == {"verbruikt": 42, "limiet": 100}
    assert body["maand"] == {"verbruikt": 42, "limiet": None}
