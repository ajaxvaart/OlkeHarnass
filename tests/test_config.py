from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from olkeharnass.config import Settings, laad_settings

H = "a" * 64


def test_voorbeeldconfig_is_geldig():
    s = laad_settings(Path(__file__).parent.parent / "config" / "olkeharnass.example.yaml")
    assert s.server.host == "127.0.0.1"
    assert "pa" in s.apps


def test_niet_lokale_host_geweigerd():
    with pytest.raises(ValidationError):
        Settings.model_validate({"server": {"host": "0.0.0.0"}, "models": {}, "apps": {}})


def test_onbekend_model_in_app_geweigerd():
    with pytest.raises(ValidationError):
        Settings.model_validate({"models": {}, "apps": {"x": {"key_sha256": H, "models": ["m"]}}})


def test_dubbele_key_geweigerd():
    with pytest.raises(ValidationError):
        Settings.model_validate({"models": {}, "apps": {
            "a": {"key_sha256": H, "models": []}, "b": {"key_sha256": H, "models": []}}})


def test_api_key_uit_omgeving(monkeypatch):
    s = Settings.model_validate({"models": {"c": {"litellm_model": "anthropic/x", "prijs": {"valuta": "EUR", "invoer": 1, "uitvoer": 1},
                                                  "api_key_env": "TEST_KEY"}}, "apps": {}})
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(RuntimeError):
        s.models["c"].litellm_kwargs()
    monkeypatch.setenv("TEST_KEY", "geheim")
    assert s.models["c"].litellm_kwargs()["api_key"] == "geheim"


def test_cloudmodel_zonder_prijs_geweigerd():
    with pytest.raises(ValidationError, match="geen prijs"):
        Settings.model_validate({"models": {"c": {"litellm_model": "anthropic/x"}}, "apps": {}})


def test_lokaal_model_zonder_prijs_mag():
    s = Settings.model_validate({"models": {"l": {"litellm_model": "ollama_chat/q"}}, "apps": {}})
    assert s.models["l"].prijs is None
    assert not s.models["l"].kost_geld


def test_usd_prijs_zonder_koers_geweigerd():
    with pytest.raises(ValidationError, match="usd_naar_eur"):
        Settings.model_validate({"models": {"c": {"litellm_model": "anthropic/x",
                                                  "prijs": {"invoer": 3, "uitvoer": 15}}},
                                 "apps": {}})
