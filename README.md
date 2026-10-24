# OlkeHarnass

Lokale, gedeelde AI-laag voor eigen apps (PA app, dashboard, later meer). Apps praten
via een OpenAI-compatibele API met de harnass; de harnass controleert de app-key,
stuurt het verzoek via LiteLLM naar Ollama (lokaal) of een cloudprovider, en voert
gedeelde tools zelf uit.

Luistert alleen op `127.0.0.1`. Apps die van buiten bereikbaar zijn, praten via hun
eigen backend met de harnass.

## Structuur

```
src/olkeharnass/
  main.py        HTTP-endpoints (FastAPI), app-factory maak_app()
  config.py      YAML-config: server, models, apps, tool_lus
  auth.py        API-keys per app (alleen sha256-hash in config), CLI olkeharnass-key
  budget.py      kosten en tokens per app in SQLite, limieten in euro per dag en maand
  lus.py         tool-lus model -> tool -> model, doorgeefmodus
  llm.py         aansluiting op LiteLLM (vervangbaar in tests)
  tools/
    registry.py  @tool-decorator, ToolRegistry, ToolContext
    ingebouwd/   voorbeeldtools: huidige_tijd, rekenen
config/olkeharnass.example.yaml
deploy/olkeharnass.service   systemd-unit
tests/
```

## Installeren (Linux)

```bash
sudo mkdir -p /opt/olkeharnass && sudo chown $USER /opt/olkeharnass
git clone https://github.com/ajaxvaart/olkeharnass /opt/olkeharnass && cd /opt/olkeharnass
python3 -m venv .venv && .venv/bin/pip install -e .
cp config/olkeharnass.example.yaml config/olkeharnass.yaml
.venv/bin/olkeharnass-key nieuw pa          # zet de hash in config/olkeharnass.yaml
echo 'ANTHROPIC_API_KEY=...' > .env && chmod 600 .env
.venv/bin/olkeharnass                       # handmatig starten, of:
sudo cp deploy/olkeharnass.service /etc/systemd/system/   # pas User= aan
sudo systemctl daemon-reload && sudo systemctl enable --now olkeharnass
```

Ollama apart installeren (https://ollama.com) en een klein model ophalen, bijvoorbeeld
`ollama pull qwen3:4b`. Zet de Ollama-modellen op de SSD.

## Gebruik vanuit een app

Elke OpenAI-client werkt. Python:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8400/v1", api_key="hk_...")
r = client.chat.completions.create(
    model="lokaal-klein",
    messages=[{"role": "user", "content": "Hoe laat is het?"}],
)
print(r.choices[0].message.content)
```

- `model` is een naam uit `models` in de config, niet de LiteLLM-naam.
- De harnass biedt automatisch de tools aan die de app mag gebruiken en voert ze zelf
  uit. Beperken per verzoek: extra veld `harnass_tools: ["rekenen"]` (in de OpenAI
  Python-client via `extra_body`), of `[]` voor geen tools.
- Stuurt de app zelf `tools` mee, dan geldt doorgeefmodus: de app krijgt de tool-calls
  terug en voert ze zelf uit. Harnass-tools staan dan uit.
- Het antwoord heeft een extra veld `harnass` met het aantal rondes en de uitgevoerde
  tool-calls.

Streaming werkt met `stream: true` (server-sent events in OpenAI-formaat), ook met de
tool-lus: de app krijgt alleen tekst te zien, tool-rondes gebeuren tussendoor. Tekst
die het model voor een tool-call schrijft komt ook door, gescheiden door een witregel.
De laatste chunk bevat `usage` en het veld `harnass`. Een fout voordat er tekst is
(onbekend model, budget op, provider onbereikbaar) is een gewone HTTP-fout; een fout
halverwege komt als `{"error": ...}`-event, gevolgd door `[DONE]`.

Endpoints: `POST /v1/chat/completions`, `GET /v1/models`, `GET /v1/tools`,
`GET /v1/budget`, `GET /health`.

## Budget per app

Elk cloudmodel krijgt in de config zijn prijs per miljoen tokens, precies zoals op de
prijspagina van de provider. Na elke aanroep leest de harnass het verbruik dat de
provider teruggeeft (gewone invoer, cache lezen, cache schrijven en uitvoer apart),
rekent de kosten uit en boekt ze per app per dag:

```yaml
valuta:
  usd_naar_eur: 0.92          # koers; providers rekenen in dollars af

models:
  cloud-sterk:
    litellm_model: anthropic/claude-sonnet-5-5
    api_key_env: ANTHROPIC_API_KEY
    prijs: {valuta: USD, invoer: 3.00, cache_lezen: 0.30, cache_schrijven: 3.75, uitvoer: 15.00}

apps:
  pa:
    budget:
      euro_per_dag: 1.00
      euro_per_maand: 10.00
```

- Een cloudmodel zonder prijs wordt bij het starten geweigerd, net als een prijs in
  dollars zonder koers, zodat het budget nooit stilletjes op nul rekent. Ollama-modellen
  hebben geen prijs nodig en kosten niets.
- Zonder `cache_lezen`/`cache_schrijven` rekent de harnass cache-tokens tegen de
  invoerprijs. Vul ze in voor providers met prompt-caching.
- Het verbruik (tokens per soort en euro's) staat in SQLite (`opslag.database`,
  standaard `data/olkeharnass.db`). Lokale modellen worden ook geteld, als statistiek.
- De controle gebeurt voor elke aanroep van een model dat geld kost, ook binnen de
  tool-lus. Is het budget op, dan krijgt de app een 429 met `type: budget_exceeded`.
  Een verzoek kan het budget met hooguit een aanroep overschrijden. Lokale modellen
  blijven werken.
- Een dag loopt volgens de klok van de laptop; de maand is de kalendermaand.
- Wat er nog kan afwijken van de rekening: de wisselkoers van je bank, toeslagen voor
  zeer lange prompts (sommige modellen boven 200k tokens) en losse kosten zoals
  webzoeken. Houd prijzen en koers in de config bij.
- `GET /v1/budget` laat de app zijn eigen kosten, tokens en limieten zien.

## Een tool toevoegen

1. Maak `src/olkeharnass/tools/ingebouwd/<naam>.py`:

   ```python
   from pydantic import BaseModel, Field
   from ..registry import ToolContext, tool

   class Args(BaseModel):
       stad: str = Field(description="Naam van de stad")

   @tool("weer", "Geeft het weerbericht voor een stad.")
   async def weer(args: Args, ctx: ToolContext) -> dict:
       return {"stad": args.stad, "verwachting": "..."}
   ```

2. Importeer de module in `src/olkeharnass/tools/__init__.py`.
3. Zet de toolnaam bij de apps die hem mogen gebruiken (`tools:` in de config).
4. Schrijf een test.

Argumenten worden met pydantic gevalideerd; fouten en exceptions gaan als JSON
`{"fout": ...}` terug naar het model, zodat het zich kan herstellen.

## Testen

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

De tests hebben geen netwerk, Ollama of cloud-keys nodig. `test_litellm_echt.py`
gebruikt de echte LiteLLM-bibliotheek (met `mock_response`) en vangt een kapotte
LiteLLM-release; draai de tests na elke versie-update.

## Wat nog niet kan

- Harnass-tools en eigen app-tools in een verzoek combineren.
- Embeddings-endpoint.
- Per-app opslag of gedeelde info tussen apps.
- MCP-servers als toolbron.
