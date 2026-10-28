"""Fase 1 van een onderzoek: de feiten van een dienst verzamelen, zonder model.

Per dienst: de containers (staat, health, exitcode, herstarts), de laatste
foutregels uit de logs en wat de API van de app zelf zegt. Alles via HTTP naar
localhost, met dezelfde hulpfuncties en keys als de server-tools. Wat niet te
lezen is, komt als leesfout in het feitenblad; een onderzoek stopt daar nooit op.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..tools.ingebouwd.server import (ServerFout, _cfg, _dienst, _get, maskeer,
                                      ontvlecht_docker_log)
from ..tools.registry import ToolContext

MAX_FOUTREGELS = 6
MAX_REGEL = 200

_PROBLEEM = re.compile(r"(?i)\b(error|err|fail|failed|exception|fatal|critical|panic|denied|refused)\b")
# Tijdstempels en id's voor in een regel, zodat dezelfde fout maar een keer telt.
_VOORVOEGSEL = re.compile(r"^\S*\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d\S*\s*")
_GETAL = re.compile(r"\d+")


@dataclass
class Feiten:
    dienst: str
    containers: list[dict[str, Any]] = field(default_factory=list)
    app: dict[str, Any] = field(default_factory=dict)
    foutregels: list[str] = field(default_factory=list)
    leesfouten: list[str] = field(default_factory=list)

    def als_dict(self) -> dict[str, Any]:
        uit: dict[str, Any] = {"dienst": self.dienst, "containers": self.containers}
        if self.app:
            uit["app"] = self.app
        if self.foutregels:
            uit["foutregels"] = self.foutregels
        if self.leesfouten:
            uit["kon_niet_lezen"] = self.leesfouten
        return uit


Lezer = Callable[[ToolContext, Feiten], Awaitable[None]]


@dataclass(frozen=True)
class Dienst:
    naam: str
    containers: tuple[str, ...]
    lezer: Lezer | None = None


# --- containers en logs ---

async def lees_container(ctx: ToolContext, naam: str) -> dict[str, Any]:
    r = await ctx.http.get(f"{_cfg(ctx).docker_url}/containers/{naam}/json", timeout=_cfg(ctx).timeout)
    if r.status_code == 404:
        return {"naam": naam, "staat": "bestaat niet"}
    if r.status_code >= 400:
        raise ServerFout(f"Docker gaf HTTP {r.status_code} voor {naam}")
    d = r.json()
    st = d.get("State") or {}
    uit = {"naam": naam, "staat": st.get("Status"), "health": (st.get("Health") or {}).get("Status"),
           "herstarts": d.get("RestartCount", 0), "sinds": (st.get("StartedAt") or "")[:16]}
    if st.get("Status") != "running":
        uit.update(exitcode=st.get("ExitCode"), geheugen_vol=bool(st.get("OOMKilled")),
                   fout=st.get("Error") or None)
    return uit


def foutregels(log: str) -> list[str]:
    """De laatste unieke probleemregels, kort en gemaskeerd."""
    gezien: dict[str, str] = {}
    for regel in log.splitlines():
        if not _PROBLEEM.search(regel):
            continue
        schoon = _VOORVOEGSEL.sub("", regel.strip())
        sleutel = _GETAL.sub("#", schoon)
        gezien.pop(sleutel, None)  # opnieuw toevoegen: volgorde = laatst gezien
        gezien[sleutel] = schoon[:MAX_REGEL]
    return [maskeer(r) for r in list(gezien.values())[-MAX_FOUTREGELS:]]


async def lees_logs(ctx: ToolContext, naam: str) -> list[str]:
    r = await _get(ctx, f"{_cfg(ctx).docker_url}/containers/{naam}/logs",
                   params={"stdout": "1", "stderr": "1", "tail": "300"})
    return foutregels(ontvlecht_docker_log(r.content))


# --- per app ---

def _kop(naam: str, key: str | None) -> dict[str, str]:
    if naam == "jellyfin":
        return {"Authorization": f'MediaBrowser Token="{key or ""}"'}
    return {"X-Api-Key": key or ""}


async def _json(ctx: ToolContext, dienst: str, pad: str, **params: str) -> Any:
    url, key = _dienst(ctx, dienst)
    return (await _get(ctx, f"{url}{pad}", headers=_kop(dienst, key), params=params or None)).json()


def _health(lijst: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"bron": h.get("source"), "soort": h.get("type"), "bericht": h.get("message")}
            for h in lijst]


async def lees_arr(ctx: ToolContext, f: Feiten) -> None:
    f.app["meldingen"] = _health(await _json(ctx, f.dienst, "/api/v3/health"))
    q = await _json(ctx, f.dienst, "/api/v3/queue", pageSize="50")
    f.app["wachtrij"] = len(q.get("records") or [])
    f.app["wachtrij_problemen"] = [
        {"titel": r.get("title"), "staat": r.get("trackedDownloadState"),
         "probleem": r.get("errorMessage") or "; ".join(
             m for s in r.get("statusMessages") or [] for m in s.get("messages") or [s.get("title")] if m)}
        for r in q.get("records") or []
        if r.get("trackedDownloadStatus") in ("warning", "error") or r.get("errorMessage")
    ][:5]
    m = await _json(ctx, f.dienst, "/api/v3/wanted/missing", pageSize="1")
    f.app["ontbrekend"] = m.get("totalRecords", 0)


async def lees_prowlarr(ctx: ToolContext, f: Feiten) -> None:
    f.app["meldingen"] = _health(await _json(ctx, "prowlarr", "/api/v1/health"))
    indexers = await _json(ctx, "prowlarr", "/api/v1/indexer")
    namen = {i.get("id"): i.get("name") for i in indexers}
    f.app["indexers"] = [{"id": i.get("id"), "naam": i.get("name"), "aan": i.get("enable")}
                         for i in indexers]
    nu = datetime.now().astimezone()
    storingen = []
    for s in await _json(ctx, "prowlarr", "/api/v1/indexerstatus"):
        tot = s.get("disabledTill")
        try:
            uit = datetime.fromisoformat(tot.replace("Z", "+00:00")) if tot else None
        except ValueError:
            uit = None
        if uit and uit > nu:
            storingen.append({"id": s.get("indexerId"), "naam": namen.get(s.get("indexerId")),
                              "uit_tot": tot[:16], "sinds": (s.get("initialFailure") or "")[:16]})
    f.app["storingen"] = storingen


async def lees_bazarr(ctx: ToolContext, f: Feiten) -> None:
    f.app["meldingen"] = [str(h.get("message") or h) if isinstance(h, dict) else str(h)
                          for h in (await _json(ctx, "bazarr", "/api/system/health")).get("data") or []]
    badges = await _json(ctx, "bazarr", "/api/badges")
    f.app["zonder_ondertitels"] = {"afleveringen": badges.get("episodes", 0),
                                   "films": badges.get("movies", 0)}
    f.app["providers"] = [{"naam": p.get("name"), "status": p.get("status"), "opnieuw": p.get("retry")}
                          for p in (await _json(ctx, "bazarr", "/api/providers")).get("data") or []
                          if p.get("status") != "Good"]
    if badges.get("episodes"):
        w = await _json(ctx, "bazarr", "/api/episodes/wanted", start="0", length="5")
        f.app["voorbeelden"] = [
            f"{e.get('seriesTitle')} {e.get('episode_number')} ("
            + ", ".join(t.get("code2") or t.get("name") or "?" for t in e.get("missing_subtitles") or []) + ")"
            for e in w.get("data") or []]
    if badges.get("movies"):
        w = await _json(ctx, "bazarr", "/api/movies/wanted", start="0", length="5")
        f.app.setdefault("voorbeelden", []).extend(m.get("title") for m in w.get("data") or [])


async def lees_sabnzbd(ctx: ToolContext, f: Feiten) -> None:
    url, key = _dienst(ctx, "sabnzbd")
    basis = {"output": "json", "apikey": key or ""}
    q = (await _get(ctx, f"{url}/api", params={**basis, "mode": "queue"})).json().get("queue", {})
    h = (await _get(ctx, f"{url}/api", params={**basis, "mode": "history", "limit": "20"})).json().get("history", {})
    f.app["gepauzeerd"] = bool(q.get("paused"))
    f.app["vrij_gb"] = q.get("diskspace1")
    f.app["wachtrij"] = len(q.get("slots") or [])
    f.app["mislukt"] = [{"id": s.get("nzo_id"), "naam": s.get("name"), "fout": s.get("fail_message")}
                        for s in h.get("slots") or [] if s.get("status") == "Failed"][:5]


async def lees_seerr(ctx: ToolContext, f: Feiten) -> None:
    st = await _json(ctx, "seerr", "/api/v1/status")
    f.app["herstart_nodig"] = bool(st.get("restartRequired"))
    r = await _json(ctx, "seerr", "/api/v1/request", take="5", filter="failed")
    f.app["mislukte_verzoeken"] = (r.get("pageInfo") or {}).get("results", 0)
    f.app["koppelfouten"] = {k: len(v) for k, v in (r.get("serviceErrors") or {}).items() if v}


async def lees_jellyfin(ctx: ToolContext, f: Feiten) -> None:
    info = await _json(ctx, "jellyfin", "/System/Info")
    f.app["herstart_nodig"] = bool(info.get("HasPendingRestart"))
    f.app["sluit_af"] = bool(info.get("IsShuttingDown"))
    taken = await _json(ctx, "jellyfin", "/ScheduledTasks", isHidden="false")
    f.app["mislukte_taken"] = [t.get("Name") for t in taken
                               if (t.get("LastExecutionResult") or {}).get("Status") == "Failed"]


async def lees_immich(ctx: ToolContext, f: Feiten) -> None:
    url, _ = _dienst(ctx, "immich")
    try:
        r = await ctx.http.get(f"{url}/api/server/ping", timeout=_cfg(ctx).timeout)
        f.app["ping"] = (r.json() or {}).get("res") if r.status_code == 200 else f"HTTP {r.status_code}"
    except Exception as e:
        f.app["ping"] = f"geen antwoord ({type(e).__name__})"


async def lees_vaultwarden(ctx: ToolContext, f: Feiten) -> None:
    url, _ = _dienst(ctx, "vaultwarden")
    try:
        r = await ctx.http.get(f"{url}/alive", timeout=_cfg(ctx).timeout)
        f.app["alive"] = r.status_code
    except Exception as e:
        f.app["alive"] = f"geen antwoord ({type(e).__name__})"


async def lees_schijven(ctx: ToolContext, f: Feiten) -> None:
    q = ('node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
         ' / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"} * 100')
    d = (await _get(ctx, f"{_cfg(ctx).prometheus_url}/api/v1/query", params={"query": q})).json()
    f.app["schijven"] = sorted(
        ({"map": r["metric"].get("mountpoint"), "vrij_procent": round(float(r["value"][1]), 1)}
         for r in (d.get("data") or {}).get("result") or []),
        key=lambda s: s["vrij_procent"])[:5]


DIENSTEN: dict[str, Dienst] = {d.naam: d for d in [
    Dienst("sonarr", ("sonarr",), lees_arr),
    Dienst("radarr", ("radarr",), lees_arr),
    Dienst("prowlarr", ("prowlarr",), lees_prowlarr),
    Dienst("bazarr", ("bazarr",), lees_bazarr),
    Dienst("sabnzbd", ("sabnzbd",), lees_sabnzbd),
    Dienst("seerr", ("seerr",), lees_seerr),
    Dienst("jellyfin", ("jellyfin",), lees_jellyfin),
    Dienst("immich", ("immich_server", "immich_machine_learning", "immich_postgres", "immich_redis"),
           lees_immich),
    Dienst("vaultwarden", ("vaultwarden",), lees_vaultwarden),
    Dienst("cloudflared", ("cloudflared",)),
    Dienst("server", (), lees_schijven),
]}


async def verzamel(dienst: str, ctx: ToolContext) -> Feiten:
    d = DIENSTEN[dienst]
    f = Feiten(dienst)
    cfg = _cfg(ctx)

    async def veilig(wat: str, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except ServerFout as e:
            f.leesfouten.append(f"{wat}: {e}")
        except Exception as e:  # een kapotte API mag het onderzoek niet stoppen
            f.leesfouten.append(f"{wat}: {type(e).__name__}")
        return None

    containers = await asyncio.gather(*(veilig(c, lees_container(ctx, c)) for c in d.containers))
    f.containers = [c for c in containers if c]
    taken = []
    if d.lezer is not None and (d.naam in cfg.diensten or d.naam == "server"):
        taken.append(veilig(f"{d.naam}-API", d.lezer(ctx, f)))
    elif d.lezer is not None:
        f.leesfouten.append(f"{d.naam}-API: niet ingesteld in server_tools.diensten")
    if d.containers and d.containers[0] in cfg.containers_logs:
        async def logs() -> None:
            f.foutregels = await lees_logs(ctx, d.containers[0])
        taken.append(veilig("logs", logs()))
    await asyncio.gather(*taken)
    f.app = {k: v for k, v in f.app.items() if v not in (None, [], {}, "")}
    return f
