"""Tools voor de thuisserver: uitlezen en (via voorstellen) vaste acties.

Alles loopt over HTTP naar localhost: Docker via een socket-proxy, het bord
(status.json), Prometheus en de API's van de apps. De API-keys van de apps
komen uit omgevingsvariabelen, nooit uit de config of het gesprek.

Uitvoer naar het model is kort gehouden (een klein lokaal model heeft weinig
context) en geheimen in logregels worden gemaskeerd.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..acties import actie
from ..registry import ToolContext, tool

MAX_TEKENS = 2000  # num_ctx is 4096 tokens; tools en prompt nemen er al ~2000


class ServerFout(Exception):
    pass


# --- hulpfuncties ---

_GEHEIM = re.compile(
    r"(?i)((?:api[_-]?key|apikey|token|password|passwd|secret|authorization)[\"'\s]*[:=]\s*[\"']?)"
    r"([^\s\"'&,;]+)"
)
_LANGE_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def maskeer(tekst: str) -> str:
    tekst = _GEHEIM.sub(lambda m: m.group(1) + "***", tekst)
    return _LANGE_HEX.sub("***", tekst)


def kort(tekst: str, max_tekens: int = MAX_TEKENS) -> str:
    if len(tekst) <= max_tekens:
        return tekst
    return tekst[: max_tekens - 40] + f"\n[... ingekort, {len(tekst)} tekens totaal]"


def _cfg(ctx: ToolContext):
    if ctx.diensten is None or ctx.http is None:
        raise ServerFout("server-tools zijn niet ingesteld (server_tools in de config)")
    return ctx.diensten


async def _get(ctx: ToolContext, url: str, **kwargs: Any) -> Any:
    cfg = _cfg(ctx)
    try:
        r = await ctx.http.get(url, timeout=cfg.timeout, **kwargs)
    except Exception as e:
        raise ServerFout(f"{url} niet bereikbaar: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise ServerFout(f"{url} gaf HTTP {r.status_code}")
    return r


async def _post(ctx: ToolContext, url: str, **kwargs: Any) -> Any:
    cfg = _cfg(ctx)
    try:
        r = await ctx.http.post(url, timeout=cfg.timeout, **kwargs)
    except Exception as e:
        raise ServerFout(f"{url} niet bereikbaar: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise ServerFout(f"{url} gaf HTTP {r.status_code}")
    return r


def _dienst(ctx: ToolContext, naam: str):
    d = _cfg(ctx).diensten.get(naam)
    if d is None:
        raise ServerFout(f"{naam} is niet ingesteld")
    key = d.key()
    if d.key_env and not key:
        raise ServerFout(f"API-key voor {naam} ontbreekt (omgevingsvariabele {d.key_env})")
    return d.url.rstrip("/"), key


def ontvlecht_docker_log(data: bytes) -> str:
    """Docker stuurt logs zonder TTY in blokjes met een kop van 8 bytes."""
    delen, i = [], 0
    while i + 8 <= len(data):
        soort, grootte = data[i], int.from_bytes(data[i + 4 : i + 8], "big")
        if soort not in (0, 1, 2) or data[i + 1 : i + 4] != b"\0\0\0":
            return data.decode("utf-8", "replace")  # container met TTY: gewone tekst
        delen.append(data[i + 8 : i + 8 + grootte])
        i += 8 + grootte
    return b"".join(delen).decode("utf-8", "replace")


async def _veilig(fn, *args) -> Any:
    try:
        return await fn(*args)
    except ServerFout as e:
        return {"fout": str(e)}


# --- uitlezen ---

class GeenArgs(BaseModel):
    pass


@tool("bord_status", "Overzicht van de server uit het bord: lampen die niet groen zijn, "
      "CPU, geheugen, temperatuur, schijfruimte en recente fouten. Begin hier.")
async def bord_status(args: GeenArgs, ctx: ToolContext) -> Any:
    async def doe():
        s = (await _get(ctx, _cfg(ctx).bord_status_url)).json()
        niet_ok = [
            {k: c.get(k) for k in ("name", "state", "detail")}
            for c in s.get("checks", []) if c.get("state") != "ok"
        ]
        return {
            "bijgewerkt": s.get("generated"),
            "kerncijfers": s.get("kpis"),
            "niet_groen": niet_ok or "alles groen",
            "schijven": s.get("disks"),
            "fouten": (s.get("errors") or [])[:8],
        }
    uit = await _veilig(doe)
    return kort(maskeer(json.dumps(uit, ensure_ascii=False, default=str)))


@tool("containers", "Lijst van alle Docker-containers met status en health.")
async def containers(args: GeenArgs, ctx: ToolContext) -> Any:
    async def doe():
        lijst = (await _get(ctx, f"{_cfg(ctx).docker_url}/containers/json", params={"all": "1"})).json()
        return [
            {"naam": (c.get("Names") or ["?"])[0].lstrip("/"), "staat": c.get("State"),
             "status": c.get("Status")}
            for c in lijst
        ]
    return await _veilig(doe)


class LogsArgs(BaseModel):
    naam: str = Field(description="Naam van de container, bijv. sonarr")
    regels: int = Field(default=60, ge=1, le=300, description="Aantal laatste regels")
    alleen_problemen: bool = Field(default=True, description="Alleen regels met error, warn, fail of exception")


_PROBLEEM = re.compile(r"(?i)\b(error|err|warn|warning|fail|failed|exception|fatal|critical)\b")


@tool("container_logs", "Laatste logregels van een container, met geheimen gemaskeerd.")
async def container_logs(args: LogsArgs, ctx: ToolContext) -> Any:
    async def doe():
        cfg = _cfg(ctx)
        if args.naam not in cfg.containers_logs:
            raise ServerFout(f"logs van {args.naam!r} mogen niet gelezen worden; "
                             f"wel van: {', '.join(cfg.containers_logs)}")
        r = await _get(ctx, f"{cfg.docker_url}/containers/{args.naam}/logs",
                       params={"stdout": "1", "stderr": "1", "tail": str(args.regels),
                               "timestamps": "1"})
        regels = ontvlecht_docker_log(r.content).splitlines()
        if args.alleen_problemen:
            regels = [x for x in regels if _PROBLEEM.search(x)]
        if not regels:
            return f"geen {'probleemregels' if args.alleen_problemen else 'regels'} in de laatste {args.regels}"
        return kort(maskeer("\n".join(regels)))
    return await _veilig(doe)


class PromArgs(BaseModel):
    query: str = Field(description="PromQL, bijv. node_load5 of 100 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes * 100")


@tool("prometheus", "Vraagt een actuele waarde op bij Prometheus (30 dagen cijfers van de server).")
async def prometheus(args: PromArgs, ctx: ToolContext) -> Any:
    async def doe():
        d = (await _get(ctx, f"{_cfg(ctx).prometheus_url}/api/v1/query",
                        params={"query": args.query})).json()
        if d.get("status") != "success":
            raise ServerFout(f"Prometheus: {d.get('error', 'onbekende fout')}")
        uit = [{"labels": r.get("metric"), "waarde": (r.get("value") or [None, None])[1]}
               for r in (d.get("data", {}).get("result") or [])[:20]]
        return uit or "geen resultaat"
    uit = await _veilig(doe)
    return uit if isinstance(uit, dict) else kort(json.dumps(uit, ensure_ascii=False))


class ArrArgs(BaseModel):
    app: Literal["sonarr", "radarr", "prowlarr"]


@tool("arr_status", "Meldingen (health) en downloadwachtrij van Sonarr, Radarr of Prowlarr.")
async def arr_status(args: ArrArgs, ctx: ToolContext) -> Any:
    async def doe():
        url, key = _dienst(ctx, args.app)
        versie = "v1" if args.app == "prowlarr" else "v3"
        kop = {"X-Api-Key": key or ""}
        health = (await _get(ctx, f"{url}/api/{versie}/health", headers=kop)).json()
        uit: dict[str, Any] = {
            "meldingen": [{"soort": h.get("type"), "bericht": h.get("message")} for h in health]
            or "geen",
        }
        if args.app != "prowlarr":
            q = (await _get(ctx, f"{url}/api/v3/queue", headers=kop,
                            params={"pageSize": "15"})).json()
            uit["wachtrij"] = [
                {"titel": r.get("title"), "status": r.get("status"),
                 "staat": r.get("trackedDownloadState"),
                 "probleem": r.get("errorMessage") or [m.get("title") for m in r.get("statusMessages") or []] or None}
                for r in q.get("records", [])
            ] or "leeg"
        return uit
    uit = await _veilig(doe)
    return kort(maskeer(json.dumps(uit, ensure_ascii=False, default=str)))


@tool("sabnzbd_status", "Wachtrij en laatste 10 downloads van SABnzbd, inclusief mislukte.")
async def sabnzbd_status(args: GeenArgs, ctx: ToolContext) -> Any:
    async def doe():
        url, key = _dienst(ctx, "sabnzbd")
        basis = {"output": "json", "apikey": key or ""}
        q = (await _get(ctx, f"{url}/api", params={**basis, "mode": "queue"})).json().get("queue", {})
        h = (await _get(ctx, f"{url}/api", params={**basis, "mode": "history", "limit": "10"})).json().get("history", {})
        return {
            "gepauzeerd": q.get("paused"),
            "snelheid": q.get("speed"),
            "vrij": q.get("diskspace1"),
            "wachtrij": [{"id": s.get("nzo_id"), "naam": s.get("filename"), "status": s.get("status"),
                          "procent": s.get("percentage")} for s in q.get("slots", [])[:10]] or "leeg",
            "geschiedenis": [{"id": s.get("nzo_id"), "naam": s.get("name"), "status": s.get("status"),
                              "fout": s.get("fail_message") or None} for s in h.get("slots", [])],
        }
    uit = await _veilig(doe)
    return kort(maskeer(json.dumps(uit, ensure_ascii=False, default=str)))


# --- acties (alleen als voorstel; uitvoeren na toestemming) ---

class HerstartArgs(BaseModel):
    naam: str = Field(description="Naam van de container")
    reden: str = Field(description="Kort waarom herstarten nodig is")


def _mag_herstarten(args: HerstartArgs, ctx: ToolContext) -> str | None:
    if ctx.diensten is None:
        return "server-tools zijn niet ingesteld"
    if args.naam not in ctx.diensten.containers_herstarten:
        return (f"{args.naam!r} mag niet herstart worden; wel: "
                f"{', '.join(ctx.diensten.containers_herstarten)}")
    return None


@actie("herstart_container", "Herstart een container van de mediastack.", risico="middel",
       titel=lambda a: f"{a.naam} herstarten", cmd=lambda a: f"docker restart {a.naam}",
       controle=_mag_herstarten)
async def herstart_container(args: HerstartArgs, ctx: ToolContext) -> str:
    fout = _mag_herstarten(args, ctx)
    if fout:
        raise ServerFout(fout)
    await _post(ctx, f"{ctx.diensten.docker_url}/containers/{args.naam}/restart", params={"t": "30"})
    return f"{args.naam} is herstart"


class ZoekArgs(BaseModel):
    app: Literal["sonarr", "radarr"]
    reden: str = Field(description="Kort waarom")


@actie("zoek_ontbrekend", "Laat Sonarr of Radarr zoeken naar alle ontbrekende afleveringen of films.",
       risico="laag",
       titel=lambda a: f"{a.app}: zoeken naar ontbrekende {'afleveringen' if a.app == 'sonarr' else 'films'}",
       cmd=lambda a: f"{a.app} API: {'MissingEpisodeSearch' if a.app == 'sonarr' else 'MissingMoviesSearch'}")
async def zoek_ontbrekend(args: ZoekArgs, ctx: ToolContext) -> str:
    url, key = _dienst(ctx, args.app)
    commando = "MissingEpisodeSearch" if args.app == "sonarr" else "MissingMoviesSearch"
    await _post(ctx, f"{url}/api/v3/command", headers={"X-Api-Key": key or ""}, json={"name": commando})
    return f"{args.app} zoekt nu naar ontbrekende items"


class RedenArgs(BaseModel):
    reden: str = Field(description="Kort waarom")


@actie("test_indexers", "Laat Prowlarr alle indexers testen.", risico="laag",
       titel=lambda a: "Prowlarr: alle indexers testen", cmd=lambda a: "prowlarr API: indexer/testall")
async def test_indexers(args: RedenArgs, ctx: ToolContext) -> str:
    url, key = _dienst(ctx, "prowlarr")
    r = await _post(ctx, f"{url}/api/v1/indexer/testall", headers={"X-Api-Key": key or ""})
    try:
        uitslag = r.json()
        fout = [x for x in uitslag if not x.get("isValid", True)]
        return f"{len(uitslag)} indexers getest, {len(fout)} met fouten"
    except ValueError:
        return "indexers getest"


@actie("sabnzbd_pauzeer", "Pauzeer alle downloads in SABnzbd.", risico="laag",
       titel=lambda a: "SABnzbd pauzeren", cmd=lambda a: "sabnzbd API: mode=pause")
async def sabnzbd_pauzeer(args: RedenArgs, ctx: ToolContext) -> str:
    url, key = _dienst(ctx, "sabnzbd")
    await _get(ctx, f"{url}/api", params={"mode": "pause", "output": "json", "apikey": key or ""})
    return "SABnzbd is gepauzeerd"


@actie("sabnzbd_hervat", "Hervat de downloads in SABnzbd.", risico="laag",
       titel=lambda a: "SABnzbd hervatten", cmd=lambda a: "sabnzbd API: mode=resume")
async def sabnzbd_hervat(args: RedenArgs, ctx: ToolContext) -> str:
    url, key = _dienst(ctx, "sabnzbd")
    await _get(ctx, f"{url}/api", params={"mode": "resume", "output": "json", "apikey": key or ""})
    return "SABnzbd downloadt weer"


class OpnieuwArgs(BaseModel):
    id: str = Field(description="nzo_id van de mislukte download uit sabnzbd_status")
    reden: str = Field(description="Kort waarom")


@actie("sabnzbd_opnieuw", "Probeer een mislukte download in SABnzbd opnieuw.", risico="laag",
       titel=lambda a: f"SABnzbd: download {a.id} opnieuw proberen",
       cmd=lambda a: f"sabnzbd API: mode=retry value={a.id}")
async def sabnzbd_opnieuw(args: OpnieuwArgs, ctx: ToolContext) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", args.id):
        raise ServerFout("ongeldig download-id")
    url, key = _dienst(ctx, "sabnzbd")
    await _get(ctx, f"{url}/api", params={"mode": "retry", "value": args.id, "output": "json",
                                          "apikey": key or ""})
    return f"download {args.id} staat weer in de wachtrij"


# --- systeem: vaste systemctl-commando's, rechten via polkit (zie deploy/polkit) ---
#
# Geen sudo: de dienst draait met NoNewPrivileges. systemctl vraagt via D-Bus aan
# systemd/logind of het mag, en de polkit-regel staat de gebruiker olkeharnass
# alleen deze commando's toe. De argumenten liggen hier vast; het model kiest niets.

REBOOT_VERTRAGING = "+1"  # minuten: tijd voor logboek en ntfy, en om af te breken
REBOOT_NODIG = "/run/reboot-required"


async def voer_commando_uit(*argv: str, timeout: float = 30.0) -> tuple[int, str]:
    """Draait een vast commando zonder shell. Apart, zodat tests het kunnen vervangen."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        uit, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ServerFout(f"{argv[0]} duurde langer dan {int(timeout)} s")
    return proc.returncode or 0, uit.decode("utf-8", "replace").strip()


async def _systemctl(*args: str) -> str:
    code, uit = await voer_commando_uit("systemctl", "--no-ask-password", *args)
    if code != 0:
        raise ServerFout(f"systemctl {' '.join(args)} mislukte ({code}): {kort(uit, 300)}")
    return uit


@tool("updates", "Welke systeemupdates (apt) klaarstaan en of een herstart van de server nodig is.")
async def updates(args: GeenArgs, ctx: ToolContext) -> Any:
    async def doe():
        code, uit = await voer_commando_uit("apt", "list", "--upgradable", timeout=60)
        if code != 0:
            raise ServerFout(f"apt list mislukte ({code})")
        regels = [r for r in uit.splitlines() if "/" in r and "[" in r]
        return {
            "aantal": len(regels),
            "pakketten": [r.split("/")[0] for r in regels][:40],
            "herstart_nodig": os.path.exists(REBOOT_NODIG),
            "let_op": "lijst is zo vers als de laatste apt update op de server",
        }
    return await _veilig(doe)


@actie("herstart_ollama", "Herstart Ollama (het lokale model). Het gesprek stopt even.",
       risico="middel", titel=lambda a: "Ollama herstarten",
       cmd=lambda a: "systemctl restart ollama")
async def herstart_ollama(args: RedenArgs, ctx: ToolContext) -> str:
    await _systemctl("restart", "ollama.service")
    return "Ollama is herstart"


@actie("herstart_server", "Herstart de hele server. Alle apps zijn een paar minuten weg.",
       risico="hoog", titel=lambda a: "Server herstarten",
       cmd=lambda a: f"shutdown -r {REBOOT_VERTRAGING}")
async def herstart_server(args: RedenArgs, ctx: ToolContext) -> str:
    await voer_shutdown()
    return ("Server herstart over 1 minuut. Afbreken kan in de terminal met "
            "sudo shutdown -c")


async def voer_shutdown() -> None:
    # shutdown plant via logind; dezelfde polkit-actie als reboot.
    code, uit = await voer_commando_uit("shutdown", "-r", REBOOT_VERTRAGING,
                                        "OlkeHarnass: herstart na toestemming van Olke")
    if code != 0:
        raise ServerFout(f"shutdown mislukte ({code}): {kort(uit, 300)}")
