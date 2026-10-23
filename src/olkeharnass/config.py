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


class PrijsConfig(BaseModel):
    """Prijs in euro per miljoen tokens. Neem de prijs van de provider over."""

    invoer: float = Field(ge=0)
    uitvoer: float = Field(ge=0)


class ModelConfig(BaseModel):
    """Een model zoals apps het kennen, met de LiteLLM-instellingen erachter."""

    litellm_model: str  # bijv. "ollama_chat/qwen3:4b" of "anthropic/claude-sonnet-5-5"
    api_base: str | None = None
    api_key_env: str | None = None  # naam van de omgevingsvariabele met de key
    timeout: float = 300.0
    # Verplicht voor cloudmodellen, zodat het budget nooit stilletjes op nul rekent.
    # Ollama-modellen zijn gratis en hebben geen prijs nodig.
    prijs: PrijsConfig | None = None
    # Extra parameters die ongewijzigd naar LiteLLM gaan, bijv. keep_alive voor Ollama.
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_lokaal(self) -> bool:
        return self.litellm_model.startswith(("ollama/", "ollama_chat/"))

    @model_validator(mode="after")
    def cloud_heeft_prijs(self) -> "ModelConfig":
        if self.prijs is None and not self.is_lokaal:
            raise ValueError(
                f"model {self.litellm_model!r} heeft geen prijs; "
                "zet prijs.invoer en prijs.uitvoer (euro per miljoen tokens)"
            )
        return self

    @property
    def kost_geld(self) -> bool:
        return self.prijs is not None and (self.prijs.invoer > 0 or self.prijs.uitvoer > 0)

    def kosten(self, tokens_invoer: int, tokens_uitvoer: int) -> float:
        if self.prijs is None:
            return 0.0
        return (tokens_invoer * self.prijs.invoer + tokens_uitvoer * self.prijs.uitvoer) / 1_000_000

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


class BudgetConfig(BaseModel):
    """Limieten in euro. None betekent geen limiet."""

    euro_per_dag: float | None = Field(default=None, ge=0)
    euro_per_maand: float | None = Field(default=None, ge=0)


class AppConfig(BaseModel):
    """Een app die de harnass mag gebruiken."""

    key_sha256: str  # hash van de API-key; maak met `olkeharnass-key nieuw`
    models: list[str]  # namen uit `models`, of ["*"] voor alles
    tools: list[str] = Field(default_factory=list)  # namen van harnass-tools, of ["*"]
    max_tool_rondes: int | None = None  # overschrijft tool_lus.max_rondes
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

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


class OpslagConfig(BaseModel):
    # Relatief pad is ten opzichte van de werkmap van de dienst.
    database: str = "data/olkeharnass.db"


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    opslag: OpslagConfig = Field(default_factory=OpslagConfig)
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
