# CLAUDE.md

OlkeHarnass: lokale AI-harnass voor Olke's eigen apps. Lees README.md voor gebruik en structuur.

## Vastgelegde keuzes (niet heroverwegen zonder Olke)

- Python, eigen dunne FastAPI-laag met OpenAI-compatibele HTTP API.
- LiteLLM als SDK-bibliotheek, niet de LiteLLM proxy. Versie vastgezet in pyproject.toml.
- Ollama voor lokale modellen; zwaar werk (tool-gebruik, lang redeneren) naar de cloud.
- De app kiest zelf het model; geen automatische routering.
- De harnass voert gedeelde tools zelf uit (lus model -> tool -> model).
- Per app een API-key (alleen sha256-hash in de config) en een allowlist van modellen en tools.
- Alleen bereikbaar op localhost. `config.py` weigert een andere host; laat dat zo.
- Draait als systemd-dienst op een Medion P6689: i5-8250U, geen GPU, 8 GB RAM (16 GB
  gepland). Lokaal realistisch tot ~4B (8 GB) of ~8B (16 GB). Houd de harnass zelf
  zuinig: hij gebruikt nu ~250 MB RSS, vooral door LiteLLM.

## Werken in deze repo

- Taal van code, comments, logs en foutmeldingen: Nederlands. Geen emoji's.
- Tests: `.venv/bin/pytest`. Geen netwerk nodig; het model wordt vervangen door een
  nep-functie via `maak_app(settings, llm=...)`.
- LiteLLM updaten: verander de versie in pyproject.toml, installeer en draai alle tests.
  `tests/test_litellm_echt.py` gebruikt de echte bibliotheek. 1.106.2 faalde bij
  verzoeken met tools (module `mcp` ontbrak); daarom staat 1.105.3 vast.
- Nieuwe tool: module in `src/olkeharnass/tools/ingebouwd/`, importeren in
  `tools/__init__.py`, toolnaam per app toestaan in de config, test schrijven.
  Tools krijgen een `ToolContext` met de appnaam; gebruik die voor rechten per app.
- Geheimen nooit in `config/olkeharnass.yaml` of in de repo: cloud-keys via omgevingsvariabelen
  (`api_key_env`), in productie uit `/opt/olkeharnass/.env`.
- Budget: `budget.py` boekt per app per dag tokens (invoer, cache lezen, cache
  schrijven, uitvoer) en kosten in euro in SQLite. Het verbruik komt uit het
  `usage`-blok van de provider; de prijs staat per model in de config (per miljoen
  tokens, USD of EUR, met `valuta.usd_naar_eur`), niet uit de prijstabel van LiteLLM.
  Cloudmodellen zonder prijs worden geweigerd.
  Tests gebruiken `opslag.database: ":memory:"`, zodat er geen bestand ontstaat.
- Foutvorm naar apps is die van OpenAI: `{"error": {"message", "type"}}`.

## Open punten

Streaming, combineren van harnass- en app-tools, embeddings,
gedeelde info tussen apps, MCP als toolbron, welke cloudproviders.
