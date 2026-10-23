"""Configuratie van de harnass, geladen uit een YAML-bestand.

Pad: omgevingsvariabele OLKEHARNASS_CONFIG, anders config/olkeharnass.yaml.
Geheimen (cloud-API-keys) staan nooit in het bestand zelf, alleen de naam
van de omgevingsvariabele waar ze in staan.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServerConfig(BaseModel):
    # Alleen localhost. Apps die van buiten bereikbaar zijn praten via hun
    # eigen backend met de harnass, nooit rechtstreeks.
    host: str = "127.0.0.1"
    port: int = 8400

    @field_validator("host")
    @classmethod
    def alleen_lokaal(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"host {v!r} is niet lokaal; de harnass mag alleen op localhost luisteren"
            )
        return v


class ModelConfig(BaseModel):
    """Een model zoals apps het kennen, met de LiteLLM-instellingen erachter."""

    litellm_model: str  # bijv. "ollama_chat/qwen3:4b" of "anthropic/claude-sonnet-5-5"
    api_base: str | None = None
    api_key_env: str | None = None  # naam van de omgevingsvariabele met de key
    timeout: float = 300.0
    # Extra parameters die ongewijzigd naar LiteLLM gaan, bijv. keep_alive voor Ollama.
    extra: dict[str, Any] = Field(default_factory=dict)

    def litellm_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.litellm_model, "timeout": self.timeout}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"omgevingsvariabele {self.api_key_env} is niet gezet")
            kwargs["api_key"] = key
        kwargs.update(self.extra)
        return kwargs


class AppConfig(BaseModel):
    """Een app die de harnass mag gebruiken."""

    key_sha256: str  # hash van de API-key; maak met `olkeharnass-key nieuw`
    models: list[str]  # namen uit `models`, of ["*"] voor alles
    tools: list[str] = Field(default_factory=list)  # namen van harnass-tools, of ["*"]
    max_tool_rondes: int | None = None  # overschrijft tool_lus.max_rondes

    @field_validator("key_sha256")
    @classmethod
    def is_hash(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("key_sha256 moet een sha256-hexhash van 64 tekens zijn")
        return v

    def mag_model(self, naam: str) -> bool:
        return "*" in self.models or naam in self.models

    def mag_tool(self, naam: str) -> bool:
        return "*" in self.tools or naam in self.tools


class ToolLusConfig(BaseModel):
    max_rondes: int = 5  # maximaal aantal model -> tool -> model rondes per verzoek


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    models: dict[str, ModelConfig]
    apps: dict[str, AppConfig]
    tool_lus: ToolLusConfig = Field(default_factory=ToolLusConfig)

    @model_validator(mode="after")
    def controleer_verwijzingen(self) -> "Settings":
        for app_naam, app in self.apps.items():
            for m in app.models:
                if m != "*" and m not in self.models:
                    raise ValueError(f"app {app_naam!r} verwijst naar onbekend model {m!r}")
        hashes = [a.key_sha256 for a in self.apps.values()]
        if len(hashes) != len(set(hashes)):
            raise ValueError("twee apps hebben dezelfde API-key")
        return self


def laad_settings(pad: str | Path | None = None) -> Settings:
    pad = Path(pad or os.environ.get("OLKEHARNASS_CONFIG", "config/olkeharnass.yaml"))
    with pad.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Settings.model_validate(data)
