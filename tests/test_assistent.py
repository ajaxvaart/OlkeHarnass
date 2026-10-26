"""De assistent van het bord: voorstellen, toestemming via Cloudflare Access, precies een keer uitvoeren."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from conftest import NepLLM, tekst_antwoord, tool_antwoord
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from olkeharnass.assistent.toegang import CloudflareToegang
from olkeharnass.assistent.voorstellen import VoorstelStore
from olkeharnass.auth import hash_key
from olkeharnass.config import CloudflareConfig, Settings
from olkeharnass.main import maak_app

TEAM = "olke.cloudflareaccess.com"
AUD = "aud-van-het-bord"
OLKE = "olke@example.org"
BORD = {"X-Bord-Assistent": "1"}

SLEUTEL = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ANDERE_SLEUTEL = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class NepSleutels:
    """Staat in voor PyJWKClient: geeft altijd de publieke sleutel van SLEUTEL."""

    class _S:
        key = SLEUTEL.public_key()

    def get_signing_key_from_jwt(self, token: str):
        return self._S()


def token(email: str = OLKE, aud: str = AUD, sleutel=SLEUTEL, verloopt: float = 600) -> str:
    nu = int(time.time())
    return jwt.encode({"aud": aud, "iss": f"https://{TEAM}", "email": email, "iat": nu,
                       "exp": nu + int(verloopt)}, sleutel, algorithm="RS256")


def access(t: str | None = None) -> dict[str, str]:
    return {**BORD, "Cf-Access-Jwt-Assertion": t or token()}


class NepServer:
    """Docker-proxy, status.json, Sonarr, SABnzbd en ntfy in een."""

    def __init__(self) -> None:
        self.verzoeken: list[httpx.Request] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.verzoeken.append(r)
        pad = r.url.path
        if r.url.host == "ntfy.test":
            return httpx.Response(200, json={"id": "x"})
        if pad == "/containers/json":
            return httpx.Response(200, json=[
                {"Names": ["/radarr"], "State": "running", "Status": "Up 2 hours (healthy)"}])
        if pad.endswith("/restart") and r.method == "POST":
            return httpx.Response(204)
        if pad == "/containers/radarr/logs":
            regel = b"2026-10-26 ERROR apikey=geheim123 verbinding mislukt\n"
            kop = bytes([2, 0, 0, 0]) + len(regel).to_bytes(4, "big")
            return httpx.Response(200, content=kop + regel)
        if pad == "/status.json":
            return httpx.Response(200, json={"generated": "nu", "checks": [
                {"name": "Radarr", "state": "warn", "detail": "traag"},
                {"name": "Sonarr", "state": "ok"}]})
        return httpx.Response(404)

    def aanroepen(self, pad_eind: str, methode: str = "POST") -> int:
        return sum(1 for r in self.verzoeken if r.url.path.endswith(pad_eind) and r.method == methode)

    def ntfy(self) -> list[dict]:
        return [json.loads(r.content) for r in self.verzoeken if r.url.host == "ntfy.test"]


def maak_settings(tmp_path, **assistent) -> Settings:
    return Settings.model_validate({
        "opslag": {"database": ":memory:"},
        "models": {"lokaal-assistent": {"litellm_model": "ollama_chat/qwen3:4b-instruct-2507-q4_K_M",
                                        "api_base": "http://127.0.0.1:11434"}},
        "apps": {"bord": {"key_sha256": hash_key("hk_test_bord"), "models": ["lokaal-assistent"],
                          "tools": ["bord_status", "containers", "container_logs",
                                    "herstart_container", "sabnzbd_pauzeer"]}},
        "server_tools": {"bord_status_url": "http://127.0.0.1:8099/status.json",
                         "containers_logs": ["radarr"], "containers_herstarten": ["radarr"]},
        "assistent": {"app": "bord", "model": "lokaal-assistent",
                      "cloudflare": {"team_domein": TEAM, "aud": AUD, "emails": [OLKE]},
                      "ntfy": {"url": "http://ntfy.test", "topic": "t"},
                      "logboek": str(tmp_path / "acties.jsonl"), "lan_mag_chatten": True,
                      **assistent},
    })


@pytest.fixture
def opzet(tmp_path):
    def _maak(antwoorden: list, **assistent):
        s = maak_settings(tmp_path, **assistent)
        server = NepServer()
        llm = NepLLM(antwoorden)
        toegang = CloudflareToegang(s.assistent.cloudflare, sleutels=NepSleutels())
        app = maak_app(s, llm=llm, http_transport=httpx.MockTransport(server), toegang=toegang)
        return TestClient(app), llm, server, tmp_path / "acties.jsonl"
    return _maak


def logregels(pad) -> list[dict]:
    return [json.loads(x) for x in pad.read_text().splitlines()] if pad.exists() else []


def stel_herstart_voor(client) -> dict:
    r = client.post("/api/assistent/send", headers=BORD, json={"text": "radarr is traag"})
    assert r.status_code == 200, r.text
    return r.json()


HERSTART = [tool_antwoord("herstart_container", {"naam": "radarr", "reden": "reageert traag"}),
            tekst_antwoord("Ik heb een herstart van radarr klaargezet.")]


def test_send_zet_voorstel_klaar_zonder_uit_te_voeren(opzet):
    client, llm, server, log = opzet(HERSTART)
    uit = stel_herstart_voor(client)
    assert uit["text"] == "Ik heb een herstart van radarr klaargezet."
    [p] = uit["proposals"]
    assert p["title"] == "radarr herstarten" and p["cmd"] == "docker restart radarr"
    assert p["risk"] == "middel" and p["why"] == "reageert traag"
    assert server.aanroepen("/restart") == 0
    # Het model hoort dat het nog niet is uitgevoerd.
    toolbericht = llm.aanroepen[1]["messages"][-1]
    assert "NOG NIET uitgevoerd" in toolbericht["content"]
    assert client.get("/api/assistent/pending").json() == [p]
    assert [r["gebeurtenis"] for r in logregels(log)] == ["voorgesteld"]


def test_systeemprompt_en_alleen_toegestane_tools(opzet):
    client, llm, _, _ = opzet([tekst_antwoord("Alles groen.")])
    client.post("/api/assistent/send", headers=BORD, json={"text": "hoe gaat het?"})
    eerste = llm.aanroepen[0]
    assert eerste["messages"][0]["role"] == "system"
    namen = {t["function"]["name"] for t in eerste["tools"]}
    assert namen == {"bord_status", "containers", "container_logs", "herstart_container",
                     "sabnzbd_pauzeer"}


def test_gesprek_onthoudt_vorige_berichten(opzet):
    client, llm, _, _ = opzet([tekst_antwoord("Eerste."), tekst_antwoord("Tweede.")])
    g = client.post("/api/assistent/send", headers=BORD, json={"text": "een"}).json()["conversation"]
    client.post("/api/assistent/send", headers=BORD, json={"text": "twee", "conversation": g})
    inhoud = [m["content"] for m in llm.aanroepen[1]["messages"][1:]]
    assert inhoud == ["een", "Eerste.", "twee"]


def test_container_buiten_lijst_wordt_geen_voorstel(opzet):
    client, llm, _, _ = opzet([
        tool_antwoord("herstart_container", {"naam": "vaultwarden", "reden": "test"}),
        tekst_antwoord("Dat mag ik niet.")])
    uit = stel_herstart_voor(client)
    assert uit["proposals"] == []
    assert "mag niet herstart worden" in llm.aanroepen[1]["messages"][-1]["content"]


def test_toestaan_zonder_access_wordt_geweigerd(opzet):
    client, _, server, log = opzet(HERSTART)
    p = stel_herstart_voor(client)["proposals"][0]
    r = client.post("/api/assistent/decide", headers=BORD, json={"id": p["id"], "approved": True})
    assert r.status_code == 403 and r.json()["ok"] is False
    assert server.aanroepen("/restart") == 0
    assert client.get("/api/assistent/pending").json() == [p]  # blijft open
    assert logregels(log)[-1]["gebeurtenis"] == "geen_toegang"
    assert len(server.ntfy()) == 1


@pytest.mark.parametrize("slecht", [
    token(aud="ander-aud"), token(email="iemand@anders.nl"), token(sleutel=ANDERE_SLEUTEL),
    token(verloopt=-60), "geen-token",
])
def test_ongeldig_access_token_mag_niet_toestaan(opzet, slecht):
    client, _, server, _ = opzet(HERSTART)
    p = stel_herstart_voor(client)["proposals"][0]
    r = client.post("/api/assistent/decide", headers=access(slecht),
                    json={"id": p["id"], "approved": True})
    assert r.status_code == 403
    assert server.aanroepen("/restart") == 0


def test_toestaan_voert_precies_een_keer_uit(opzet):
    client, _, server, log = opzet(HERSTART)
    p = stel_herstart_voor(client)["proposals"][0]
    r = client.post("/api/assistent/decide", headers=access(), json={"id": p["id"], "approved": True})
    assert r.json() == {"ok": True, "output": "radarr is herstart"}
    assert server.aanroepen("/containers/radarr/restart") == 1
    nog_eens = client.post("/api/assistent/decide", headers=access(),
                           json={"id": p["id"], "approved": True})
    assert nog_eens.status_code == 409
    assert server.aanroepen("/containers/radarr/restart") == 1
    assert client.get("/api/assistent/pending").json() == []
    laatste = logregels(log)[-1]
    assert laatste["gebeurtenis"] == "uitgevoerd" and laatste["wie"] == OLKE
    assert server.ntfy()[-1]["title"] == "Assistent: Uitgevoerd"


def test_weigeren_voert_niets_uit_en_meldt(opzet):
    client, _, server, log = opzet(HERSTART)
    p = stel_herstart_voor(client)["proposals"][0]
    r = client.post("/api/assistent/decide", headers=BORD, json={"id": p["id"], "approved": False})
    assert r.json()["ok"] is True
    na = client.post("/api/assistent/decide", headers=access(), json={"id": p["id"], "approved": True})
    assert na.status_code == 409
    assert server.aanroepen("/restart") == 0
    assert [x["gebeurtenis"] for x in logregels(log)] == ["voorgesteld", "geweigerd"]
    assert server.ntfy()[0]["title"] == "Assistent: Geweigerd"


def test_mislukte_uitvoering_wordt_gemeld(opzet):
    client, _, server, log = opzet(HERSTART)
    p = stel_herstart_voor(client)["proposals"][0]

    def kapot(r: httpx.Request) -> httpx.Response:
        server.verzoeken.append(r)
        return httpx.Response(500) if r.url.path.endswith("/restart") else httpx.Response(200, json={})

    client.app.state.http._transport = httpx.MockTransport(kapot)
    r = client.post("/api/assistent/decide", headers=access(), json={"id": p["id"], "approved": True})
    assert r.json()["ok"] is False and "HTTP 500" in r.json()["output"]
    assert logregels(log)[-1]["gebeurtenis"] == "mislukt"
    # Mislukt is afgehandeld: niet nog eens proberen.
    assert client.post("/api/assistent/decide", headers=access(),
                       json={"id": p["id"], "approved": True}).status_code == 409


def test_onbekend_voorstel(opzet):
    client, _, _, _ = opzet([])
    r = client.post("/api/assistent/decide", headers=access(), json={"id": "bestaat-niet", "approved": True})
    assert r.status_code == 404


def test_zonder_bordkop_geweigerd(opzet):
    client, _, _, _ = opzet([])
    assert client.post("/api/assistent/send", json={"text": "hoi"}).status_code == 403
    assert client.post("/api/assistent/decide", json={"id": "x", "approved": True}).status_code == 403


def test_lan_mag_niet_chatten_als_uitgezet(opzet):
    client, _, _, _ = opzet([tekst_antwoord("hoi")], lan_mag_chatten=False)
    assert client.post("/api/assistent/send", headers=BORD, json={"text": "hoi"}).status_code == 403
    assert client.get("/api/assistent/pending").status_code == 403
    r = client.post("/api/assistent/send", headers=access(), json={"text": "hoi"})
    assert r.status_code == 200 and r.json()["text"] == "hoi"


def test_modelfout_geeft_nette_tekst(opzet):
    client, _, _, _ = opzet([])  # NepLLM zonder antwoorden gooit een fout
    r = client.post("/api/assistent/send", headers=BORD, json={"text": "hoi"})
    assert r.status_code == 200 and "fout" in r.json()["text"]


def test_verlopen_voorstel_kan_niet_meer():
    store = VoorstelStore(":memory:", geldig_seconden=-1)
    v = store.maak("herstart_container", {"naam": "radarr", "reden": "x"}, "t", "c", "x", "middel")
    assert store.open_voorstellen() == []
    assert store.claim(v.id, OLKE) is False


def test_claim_lukt_maar_een_keer():
    store = VoorstelStore(":memory:", geldig_seconden=60)
    v = store.maak("a", {"reden": "x"}, "t", "c", "x", "laag")
    assert store.claim(v.id, OLKE) is True
    assert store.claim(v.id, OLKE) is False
    assert store.weiger(v.id, OLKE) is False


def test_cloudflare_controle_los():
    t = CloudflareToegang(CloudflareConfig(team_domein=TEAM, aud=AUD, emails=["Olke@Example.org"]),
                          sleutels=NepSleutels())
    assert t._controleer(token()) == OLKE
    assert t._controleer(token(email="x@y.z")) is None


def test_config_assistent_moet_bestaande_app_hebben(tmp_path):
    with pytest.raises(ValueError, match="assistent.app"):
        maak_settings(tmp_path, app="bestaat-niet")
