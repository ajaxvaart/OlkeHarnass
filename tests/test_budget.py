from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import PA_KEY, NepLLM, auth, tekst_antwoord, tool_antwoord
from olkeharnass.auth import hash_key
from olkeharnass.budget import Gebruiksregister
from olkeharnass.config import Settings
from olkeharnass.main import maak_app

VRAAG = [{"role": "user", "content": "hoi"}]

# Prijs zo gekozen dat het rekenen makkelijk is: 0,001 euro per invoertoken,
# 0,002 per uitvoertoken. tekst_antwoord (10 in, 5 uit) kost dan 0,02;
# tool_antwoord (20 in, 8 uit) kost 0,036.
TEKST = 0.02
TOOL = 0.036


def _settings(**budget) -> Settings:
    return Settings.model_validate({
        "models": {
            "cloud": {"litellm_model": "anthropic/claude-haiku-5-5",
                      "prijs": {"invoer": 1000, "uitvoer": 2000}},
            "lokaal": {"litellm_model": "ollama_chat/qwen3:4b"},
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
    r.voeg_toe("pa", 100, 10, 1.0, date(2026, 10, 1))
    r.voeg_toe("pa", 50, 5, 0.5, date(2026, 10, 23))
    r.voeg_toe("pa", 25, 5, 0.25, date(2026, 10, 23))
    r.voeg_toe("pa", 999, 9, 9.0, date(2026, 9, 30))
    r.voeg_toe("ander", 7, 7, 7.0, date(2026, 10, 23))
    g = r.gebruik("pa", date(2026, 10, 23))
    assert (g.dag.tokens_invoer, g.dag.tokens_uitvoer) == (75, 10)
    assert g.dag.kosten == pytest.approx(0.75)
    assert g.maand.kosten == pytest.approx(1.75)
    assert g.maand.tokens_invoer == 175


def test_register_blijft_bewaard(tmp_path):
    pad = tmp_path / "sub" / "db.sqlite"
    r = Gebruiksregister(pad)
    r.voeg_toe("pa", 10, 2, 0.1)
    r.sluit()
    assert Gebruiksregister(pad).gebruik("pa").dag.kosten == pytest.approx(0.1)


def test_kosten_worden_geboekt():
    client, _, register = _client([tekst_antwoord("a")])
    assert _vraag(client).status_code == 200
    g = register.gebruik("pa").dag
    assert g.kosten == pytest.approx(TEKST)
    assert (g.tokens_invoer, g.tokens_uitvoer) == (10, 5)


def test_tool_lus_boekt_elke_ronde():
    client, _, register = _client([tool_antwoord("rekenen", {"expressie": "1+1"}),
                                   tekst_antwoord("2")])
    _vraag(client)
    assert register.gebruik("pa").dag.kosten == pytest.approx(TOOL + TEKST)


def test_lokaal_kost_niets_maar_tokens_worden_geteld():
    client, _, register = _client([tekst_antwoord("a")])
    _vraag(client, "lokaal")
    g = register.gebruik("pa").dag
    assert g.kosten == 0
    assert g.tokens_invoer == 10


def test_dagbudget_bereikt_geeft_429_zonder_modelaanroep():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 0, 0, 1.0)
    client, llm, _ = _client([], register, euro_per_dag=1.0)
    r = _vraag(client)
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "budget_exceeded"
    assert llm.aanroepen == []


def test_maandbudget_bereikt():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 0, 0, 5.0)
    client, _, _ = _client([], register, euro_per_dag=100, euro_per_maand=5)
    assert "maandbudget" in _vraag(client).json()["error"]["message"]


def test_lokaal_werkt_ook_als_budget_op_is():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 0, 0, 1.0)
    client, _, _ = _client([tekst_antwoord("a")], register, euro_per_dag=1.0)
    assert _vraag(client, "lokaal").status_code == 200


def test_budget_stopt_tool_lus_halverwege():
    # De eerste ronde kost 0,036 en brengt het verbruik boven 0,03: de tweede wordt geweigerd.
    client, llm, _ = _client([tool_antwoord("rekenen", {"expressie": "1+1"}),
                              tekst_antwoord("2")], euro_per_dag=0.03)
    r = _vraag(client)
    assert r.status_code == 429
    assert len(llm.aanroepen) == 1


def test_budget_endpoint():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", 1000, 200, 0.42)
    client, _, _ = _client([], register, euro_per_dag=1.5)
    body = client.get("/v1/budget", headers=auth()).json()
    assert body["dag"] == {"kosten_euro": 0.42, "limiet_euro": 1.5,
                           "tokens_invoer": 1000, "tokens_uitvoer": 200}
    assert body["maand"]["limiet_euro"] is None
