"""Onderzoek van een dienst: verzamelen (nagebootste API's), herkennen en het rapport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import NepLLM, tekst_antwoord, tool_antwoord
from fastapi.testclient import TestClient

from olkeharnass.config import DienstConfig, ServerToolsConfig
from olkeharnass.main import maak_app
from olkeharnass.onderzoek import als_tekst, onderzoek, voor_model
from olkeharnass.onderzoek.catalogus import herken
from olkeharnass.onderzoek.verzamel import Feiten, foutregels
from olkeharnass.tools import ToolContext
from olkeharnass.assistent.toegang import CloudflareToegang
from test_assistent import BORD, NepSleutels, access, maak_settings

DIENSTEN = {
    "bazarr": {"url": "http://bazarr.test", "key_env": "BAZARR_KEY"},
    "prowlarr": {"url": "http://prowlarr.test", "key_env": "PROWLARR_KEY"},
    "sabnzbd": {"url": "http://sab.test", "key_env": "SAB_KEY"},
    "vaultwarden": {"url": "http://vw.test"},
}


def container(staat="running", health="healthy", **extra):
    return {"State": {"Status": staat, "Health": {"Status": health} if health else None,
                      "ExitCode": extra.get("exit", 0), "OOMKilled": extra.get("oom", False),
                      "StartedAt": "2026-10-28T10:00:00Z"}, "RestartCount": extra.get("herstarts", 0)}


class Server:
    """Docker-proxy en de app-API's, met per test aan te passen antwoorden."""

    def __init__(self, **over) -> None:
        self.containers = {"bazarr": container(), "prowlarr": container(), "sabnzbd": container(),
                           "vaultwarden": container()}
        self.containers.update(over.pop("containers", {}))
        self.antwoorden = {
            "/api/system/health": {"data": []},
            "/api/badges": {"episodes": 3, "movies": 0, "providers": 0},
            "/api/providers": {"data": [{"name": "opensubtitlescom", "status": "Good", "retry": "-"}]},
            "/api/episodes/wanted": {"data": [
                {"seriesTitle": "Bureau Groningen", "episode_number": "1x1",
                 "missing_subtitles": [{"code2": "en"}]},
                {"seriesTitle": "WAR (2026)", "episode_number": "1x4", "missing_subtitles": [{"code2": "nl"}]}],
                "total": 3},
            "/api/v1/health": [{"source": "UpdateCheck", "type": "warning", "message": "New update is available"}],
            "/api/v1/indexer": [{"id": 2, "name": "altHUB", "enable": False},
                                {"id": 1, "name": "NZBgeek", "enable": True}],
            "/api/v1/indexerstatus": [],
            "/alive": None,
        }
        self.antwoorden.update(over)
        self.verzoeken: list[httpx.Request] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.verzoeken.append(r)
        pad = r.url.path
        if r.url.host == "ntfy.test":
            return httpx.Response(200, json={})
        if pad.startswith("/containers/") and pad.endswith("/json"):
            naam = pad.split("/")[2]
            return httpx.Response(200, json=self.containers[naam]) if naam in self.containers else httpx.Response(404)
        if pad.endswith("/logs"):
            regel = b"2026-10-28T10:00:01Z ERROR provider timeout apikey=geheim\n"
            return httpx.Response(200, content=bytes([2, 0, 0, 0]) + len(regel).to_bytes(4, "big") + regel)
        if r.url.host == "sab.test":
            if r.url.params.get("mode") == "queue":
                return httpx.Response(200, json={"queue": {"paused": False, "diskspace1": "500", "slots": []}})
            return httpx.Response(200, json={"history": {"slots": [
                {"nzo_id": "SAB1", "name": "Serie.S01E01", "status": "Failed", "fail_message": "Missende blokken"}]}})
        if pad == "/api/system/tasks" and r.method == "POST":
            return httpx.Response(204)
        if pad in self.antwoorden:
            a = self.antwoorden[pad]
            return httpx.Response(a if isinstance(a, int) else 200, json=None if isinstance(a, int) else a)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for k in ("BAZARR_KEY", "PROWLARR_KEY", "SAB_KEY"):
        monkeypatch.setenv(k, "k")


def ctx(server: Server) -> ToolContext:
    cfg = ServerToolsConfig(containers_logs=["bazarr"], containers_herstarten=["bazarr", "vaultwarden"],
                            diensten=DIENSTEN)
    return ToolContext(app="t", diensten=cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(server)))


def rapport(dienst: str, server: Server):
    return asyncio.run(onderzoek(dienst, ctx(server)))


def test_bazarr_ondertitels_en_foutregels():
    r = rapport("bazarr", Server())
    assert r.feiten.app["zonder_ondertitels"] == {"afleveringen": 3, "films": 0}
    assert "Bureau Groningen 1x1 (en)" in r.feiten.app["voorbeelden"]
    assert r.feiten.foutregels == ["ERROR provider timeout apikey=***"]
    [t] = [t for t in r.treffers if t.code == "ondertitels_series"]
    assert t.acties == [("bazarr_zoek_ondertitels", {"soort": "series"})]
    tekst = als_tekst(r)
    assert "3 afleveringen missen ondertitels" in tekst and "geheim" not in tekst


def test_prowlarr_update_is_info_en_handmatig_uit_is_geen_storing():
    r = rapport("prowlarr", Server())
    assert [t.code for t in r.treffers] == ["update"]
    assert als_tekst(r).startswith("Geen problemen gevonden bij Prowlarr.")


def test_prowlarr_storing_wordt_herkend():
    r = rapport("prowlarr", Server(**{"/api/v1/indexerstatus": [
        {"indexerId": 1, "disabledTill": "2999-01-01T00:00:00Z", "initialFailure": "2026-10-28T09:00:00Z"}]}))
    [t] = [t for t in r.treffers if t.code == "indexer_uitgeschakeld"]
    assert "NZBgeek" in t.wat and t.acties == [("test_indexers", {})]


def test_gestopte_container_door_geheugen():
    r = rapport("vaultwarden", Server(containers={"vaultwarden": container("exited", None, exit=137, oom=True)}))
    t = r.treffers[0]
    assert t.code == "container_gestopt" and "geheugen" in t.wat
    assert t.acties == [("herstart_container", {"naam": "vaultwarden"})]


def test_niet_ingestelde_api_en_kapotte_api_stoppen_niets():
    r = rapport("bazarr", Server(**{"/api/badges": 500}))
    assert any("bazarr-API" in x for x in r.feiten.leesfouten)
    assert r.feiten.containers[0]["staat"] == "running"
    r = rapport("seerr", Server())
    assert "niet ingesteld" in r.feiten.leesfouten[0]


def test_sabnzbd_mislukt():
    r = rapport("sabnzbd", Server())
    [t] = r.treffers
    assert t.acties == [("sabnzbd_opnieuw", {"id": "SAB1"})] and "Missende blokken" in t.wat


def test_herstart_lus_en_foutregels_ontdubbeld():
    f = Feiten("x", containers=[{"naam": "x", "staat": "running", "health": None, "herstarts": 5}])
    assert [t.code for t in herken(f)] == ["herstart_lus"]
    log = "\n".join(f"2026-10-28T10:00:0{i}Z ERROR job {i} failed" for i in range(9)) + "\nINFO ok\n"
    assert foutregels(log) == ["ERROR job # failed".replace("#", "8")]


def test_voor_model_is_kort():
    tekst = voor_model(rapport("bazarr", Server()))
    assert len(tekst) <= 1800 and json.loads(tekst)["bevindingen"][0]["acties"] == ["bazarr_zoek_ondertitels"]


# --- via de assistent ---

@pytest.fixture
def bord(tmp_path):
    def _maak(server: Server, antwoorden=()):
        s = maak_settings(tmp_path)
        s.server_tools.diensten = {k: DienstConfig(**v) for k, v in DIENSTEN.items()}
        s.server_tools.containers_logs = ["bazarr"]
        s.server_tools.containers_herstarten = ["bazarr", "vaultwarden"]
        s.apps["bord"].tools += ["onderzoek", "bazarr_zoek_ondertitels", "sabnzbd_opnieuw"]
        llm = NepLLM(list(antwoorden))
        app = maak_app(s, llm=llm, llm_stroom=llm.stroom, http_transport=httpx.MockTransport(server),
                       toegang=CloudflareToegang(s.assistent.cloudflare, sleutels=NepSleutels()))
        return TestClient(app), llm
    return _maak


def test_knop_onderzoek_maakt_rapport_en_voorstel_zonder_model(bord):
    server = Server()
    client, llm = bord(server)
    r = client.post("/api/assistent/onderzoek", headers=BORD, json={"service": "bazarr"})
    assert r.status_code == 200, r.text
    uit = r.json()
    assert uit["text"].startswith("Bij Bazarr zie ik 1 probleem:")
    assert "Laatste foutregels" in uit["text"]
    [p] = uit["proposals"]
    assert p["title"] == "Ondertitels zoeken (series)" and p["risk"] == "laag"
    assert llm.aanroepen == []
    # Nog een keer: geen dubbel voorstel.
    assert client.post("/api/assistent/onderzoek", headers=BORD, json={"service": "bazarr"}).json()["proposals"] == []
    gesprek = client.get("/api/assistent/conversation").json()["messages"]
    assert gesprek[0] == {"who": "me", "text": "Onderzoek Bazarr"}
    # Toestaan start de Bazarr-taak.
    client.post("/api/assistent/decide", headers=access(), json={"id": p["id"], "approved": True})
    [taak] = [x for x in server.verzoeken if x.url.path == "/api/system/tasks"]
    assert b"wanted_search_missing_subtitles_series" in taak.content


def test_onderzoek_stream_en_onbekende_dienst(bord):
    client, _ = bord(Server())
    tekst = client.post("/api/assistent/onderzoek", headers=BORD, json={"service": "vaultwarden", "stream": True}).text
    events = [json.loads(x[6:]) for x in tekst.splitlines() if x.startswith("data: ")]
    assert [e["type"] for e in events] == ["stap", "stap_klaar", "stap", "stap_klaar", "klaar"]
    assert events[0]["tekst"] == "Feiten verzamelen: Vaultwarden"
    assert client.post("/api/assistent/onderzoek", headers=BORD, json={"service": "rm -rf"}).status_code == 400


def test_model_gebruikt_onderzoek_als_tool(bord):
    client, llm = bord(Server(), [tool_antwoord("onderzoek", {"dienst": "bazarr"}),
                                  tekst_antwoord("Er missen ondertitels.")])
    client.post("/api/assistent/send", headers=BORD, json={"text": "waarom geen ondertitels?"})
    assert "ondertitels_series" not in llm.aanroepen[1]["messages"][-1]["content"]
    assert "missen ondertitels" in llm.aanroepen[1]["messages"][-1]["content"]
