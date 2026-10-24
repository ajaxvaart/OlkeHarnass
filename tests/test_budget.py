from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import PA_KEY, NepLLM, auth, tekst_antwoord, tool_antwoord
from olkeharnass.auth import hash_key
from olkeharnass.budget import Gebruiksregister, Verbruik, bereken_kosten
from olkeharnass.config import PrijsConfig, Settings
from olkeharnass.main import maak_app

VRAAG = [{"role": "user", "content": "hoi"}]

# Prijs zo gekozen dat het rekenen makkelijk is: 0,001 euro per invoertoken,
# 0,002 per uitvoertoken. tekst_antwoord (10 in, 5 uit) kost dan 0,02;
# tool_antwoord (20 in, 8 uit) kost 0,036.
TEKST = 0.02
TOOL = 0.036


def _settings(**budget) -> Settings:
    return Settings.model_validate({
        "valuta": {"usd_naar_eur": 0.5},
        "models": {
            "cloud": {"litellm_model": "anthropic/claude-haiku-5-5",
                      "prijs": {"valuta": "EUR", "invoer": 1000, "uitvoer": 2000}},
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


def _v(invoer=0, uitvoer=0):
    return Verbruik(invoer=invoer, uitvoer=uitvoer)


# --- verbruik uitlezen en kosten berekenen ---

def test_verbruik_anthropic_met_cache():
    # LiteLLM telt cache-tokens mee in prompt_tokens en geeft ze apart terug.
    v = Verbruik.uit_usage({"prompt_tokens": 1500, "completion_tokens": 200,
                            "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 400})
    assert v == Verbruik(invoer=100, cache_lezen=1000, cache_schrijven=400, uitvoer=200)


def test_verbruik_openai_met_cache():
    v = Verbruik.uit_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                            "prompt_tokens_details": {"cached_tokens": 800}})
    assert v == Verbruik(invoer=200, cache_lezen=800, cache_schrijven=0, uitvoer=50)


def test_verbruik_zonder_usage():
    assert Verbruik.uit_usage(None) == Verbruik()


def test_kosten_met_cacheprijzen_en_dollarkoers():
    s = _settings()  # usd_naar_eur 0,5
    prijs = PrijsConfig(valuta="USD", invoer=3, cache_lezen=0.3, cache_schrijven=3.75,
                        uitvoer=15)
    v = Verbruik(invoer=1_000_000, cache_lezen=1_000_000, cache_schrijven=1_000_000,
                 uitvoer=1_000_000)
    # (3 + 0,30 + 3,75 + 15) dollar * 0,5 = 11,025 euro
    assert bereken_kosten(prijs, v, s) == pytest.approx(11.025)


def test_kosten_zonder_cacheprijs_rekent_invoerprijs():
    prijs = PrijsConfig(valuta="EUR", invoer=2, uitvoer=10)
    v = Verbruik(cache_lezen=1_000_000, cache_schrijven=1_000_000)
    assert bereken_kosten(prijs, v, _settings()) == pytest.approx(4.0)


def test_lokaal_kost_niets():
    assert bereken_kosten(None, _v(1000, 1000), _settings()) == 0.0


# --- register ---

def test_register_telt_per_dag_en_maand():
    r = Gebruiksregister(":memory:")
    r.voeg_toe("pa", _v(100, 10), 1.0, date(2026, 10, 1))
    r.voeg_toe("pa", _v(50, 5), 0.5, date(2026, 10, 23))
    r.voeg_toe("pa", Verbruik(invoer=25, cache_lezen=7, uitvoer=5), 0.25, date(2026, 10, 23))
    r.voeg_toe("pa", _v(999, 9), 9.0, date(2026, 9, 30))
    r.voeg_toe("ander", _v(7, 7), 7.0, date(2026, 10, 23))
    g = r.gebruik("pa", date(2026, 10, 23))
    assert (g.dag.tokens_invoer, g.dag.tokens_cache_lezen, g.dag.tokens_uitvoer) == (75, 7, 10)
    assert g.dag.kosten == pytest.approx(0.75)
    assert g.maand.kosten == pytest.approx(1.75)
    assert g.maand.tokens_invoer == 175


def test_register_blijft_bewaard(tmp_path):
    pad = tmp_path / "sub" / "db.sqlite"
    r = Gebruiksregister(pad)
    r.voeg_toe("pa", _v(10, 2), 0.1)
    r.sluit()
    assert Gebruiksregister(pad).gebruik("pa").dag.kosten == pytest.approx(0.1)


# --- via de API ---

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


def test_lokaal_tokens_worden_geteld_zonder_kosten():
    client, _, register = _client([tekst_antwoord("a")])
    _vraag(client, "lokaal")
    g = register.gebruik("pa").dag
    assert g.kosten == 0
    assert g.tokens_invoer == 10


def test_dagbudget_bereikt_geeft_429_zonder_modelaanroep():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", _v(1), 1.0)
    client, llm, _ = _client([], register, euro_per_dag=1.0)
    r = _vraag(client)
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "budget_exceeded"
    assert llm.aanroepen == []


def test_maandbudget_bereikt():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", _v(1), 5.0)
    client, _, _ = _client([], register, euro_per_dag=100, euro_per_maand=5)
    assert "maandbudget" in _vraag(client).json()["error"]["message"]


def test_lokaal_werkt_ook_als_budget_op_is():
    register = Gebruiksregister(":memory:")
    register.voeg_toe("pa", _v(1), 1.0)
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
    register.voeg_toe("pa", Verbruik(invoer=1000, cache_lezen=300, uitvoer=200), 0.42)
    client, _, _ = _client([], register, euro_per_dag=1.5)
    body = client.get("/v1/budget", headers=auth()).json()
    assert body["dag"] == {"kosten_euro": 0.42, "limiet_euro": 1.5, "tokens_invoer": 1000,
                           "tokens_cache_lezen": 300, "tokens_cache_schrijven": 0,
                           "tokens_uitvoer": 200}
    assert body["maand"]["limiet_euro"] is None
