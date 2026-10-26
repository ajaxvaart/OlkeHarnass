"""Server-tools: uitlezen via HTTP (nagebootst), maskeren, allowlists."""

from __future__ import annotations

import asyncio
import json

import httpx

from olkeharnass.config import ServerToolsConfig
from olkeharnass.tools import ToolContext, registry
from olkeharnass.tools.ingebouwd.server import kort, maskeer, ontvlecht_docker_log
from test_assistent import NepServer


def ctx(server: NepServer, **cfg) -> ToolContext:
    diensten = ServerToolsConfig(containers_logs=["radarr"], containers_herstarten=["radarr"], **cfg)
    return ToolContext(app="test", diensten=diensten,
                       http=httpx.AsyncClient(transport=httpx.MockTransport(server)))


def draai(naam: str, args: dict, c: ToolContext) -> str:
    return asyncio.run(registry.get(naam).voer_uit(json.dumps(args), c))


def test_maskeer_geheimen():
    assert maskeer("apikey=abc123 ok") == "apikey=*** ok"
    assert maskeer('"token": "xyz"') == '"token": "***"'
    assert maskeer("id 0123456789abcdef0123456789abcdef") == "id ***"


def test_kort_kapt_af():
    uit = kort("x" * 5000, 100)
    assert len(uit) < 200 and "ingekort" in uit


def test_ontvlecht_docker_log():
    regel = b"hallo\n"
    assert ontvlecht_docker_log(bytes([1, 0, 0, 0, 0, 0, 0, 6]) + regel) == "hallo\n"
    assert ontvlecht_docker_log(b"gewone tekst\n") == "gewone tekst\n"


def test_logs_gemaskeerd_en_gefilterd():
    uit = draai("container_logs", {"naam": "radarr"}, ctx(NepServer()))
    assert "ERROR" in uit and "geheim123" not in uit


def test_logs_buiten_lijst_geweigerd():
    server = NepServer()
    uit = draai("container_logs", {"naam": "vaultwarden"}, ctx(server))
    assert "mogen niet gelezen worden" in uit
    assert server.verzoeken == []


def test_bord_status_toont_alleen_niet_groen():
    uit = json.loads(draai("bord_status", {}, ctx(NepServer())))
    assert uit["niet_groen"] == [{"name": "Radarr", "state": "warn", "detail": "traag"}]


def test_containers():
    uit = json.loads(draai("containers", {}, ctx(NepServer())))
    assert uit == [{"naam": "radarr", "staat": "running", "status": "Up 2 hours (healthy)"}]


def test_dienst_zonder_key_geeft_fout(monkeypatch):
    monkeypatch.delenv("SONARR_KEY", raising=False)
    c = ctx(NepServer(), diensten={"sonarr": {"url": "http://127.0.0.1:8989", "key_env": "SONARR_KEY"}})
    uit = json.loads(draai("arr_status", {"app": "sonarr"}, c))
    assert "SONARR_KEY" in uit["fout"]


def test_niet_ingesteld():
    uit = json.loads(draai("containers", {}, ToolContext(app="x")))
    assert "niet ingesteld" in uit["fout"]


def test_actie_zonder_assistent_wordt_niet_voorgesteld():
    server = NepServer()
    uit = json.loads(draai("herstart_container", {"naam": "radarr", "reden": "x"}, ctx(server)))
    assert "alleen via de assistent" in uit["fout"]
    assert server.verzoeken == []


def test_elke_actie_heeft_reden_en_risico():
    from olkeharnass.tools.acties import acties
    assert {"herstart_container", "zoek_ontbrekend", "test_indexers", "sabnzbd_pauzeer",
            "sabnzbd_hervat", "sabnzbd_opnieuw"} <= set(acties)
    for a in acties.values():
        assert "reden" in a.args_model.model_fields
        assert a.risico in ("laag", "middel", "hoog")
