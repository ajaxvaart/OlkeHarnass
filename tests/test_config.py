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
    s = Settings.model_validate({"models": {"c": {"litellm_model": "anthropic/x",
                                                  "api_key_env": "TEST_KEY"}}, "apps": {}})
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(RuntimeError):
        s.models["c"].litellm_kwargs()
    monkeypatch.setenv("TEST_KEY", "geheim")
    assert s.models["c"].litellm_kwargs()["api_key"] == "geheim"
