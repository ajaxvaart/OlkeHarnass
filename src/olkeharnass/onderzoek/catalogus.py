"""Fase 2 van een onderzoek: bekende problemen herkennen in het feitenblad, zonder model.

Elke regel kijkt naar de feiten van een dienst en geeft nul of meer treffers: wat
er mis is, waarom, en welke acties uit de vaste lijst het oplossen. Is er geen
actie voor, dan zegt `handwerk` wat Olke zelf moet doen. Een nieuw probleem dat
vaker voorkomt, wordt hier een nieuwe regel (met een test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .verzamel import Feiten

VRIJ_KRAP = 10.0  # procent vrije ruimte op een schijf


@dataclass(frozen=True)
class Treffer:
    code: str
    ernst: str  # fout | waarschuwing | info
    wat: str  # in gewone taal, met de oorzaak als die bekend is
    acties: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (actie, args zonder reden)
    handwerk: str | None = None


Regel = Callable[[Feiten], list[Treffer]]


def containers(f: Feiten) -> list[Treffer]:
    uit = []
    for c in f.containers:
        n = c["naam"]
        if c.get("staat") == "bestaat niet":
            uit.append(Treffer("container_ontbreekt", "fout", f"Container {n} bestaat niet.",
                               handwerk="Controleer de compose van deze dienst en start hem met docker compose up -d."))
        elif c.get("staat") != "running":
            oorzaak = ("het geheugen was vol (OOM)" if c.get("geheugen_vol")
                       else f"exitcode {c.get('exitcode')}" + (f": {c['fout']}" if c.get("fout") else ""))
            uit.append(Treffer("container_gestopt", "fout", f"Container {n} draait niet ({c.get('staat')}, {oorzaak}).",
                               [("herstart_container", {"naam": n})]))
        elif c.get("health") == "unhealthy":
            uit.append(Treffer("container_unhealthy", "fout", f"Container {n} draait, maar is unhealthy.",
                               [("herstart_container", {"naam": n})]))
        if (c.get("herstarts") or 0) >= 3:
            uit.append(Treffer("herstart_lus", "fout",
                               f"Container {n} is al {c['herstarts']} keer vanzelf herstart; er is iets structureel mis.",
                               handwerk="Bekijk de foutregels; nog een herstart helpt waarschijnlijk niet."))
    return uit


# Health-meldingen van Sonarr, Radarr en Prowlarr, op hun `source`.
_BRONNEN: dict[str, tuple[str, str, str | None]] = {
    # bron: (code, uitleg, handwerk)
    "DownloadClientCheck": ("downloadclient_weg", "De downloadclient (SABnzbd) is niet bereikbaar.", None),
    "DownloadClientStatusCheck": ("downloadclient_weg", "De downloadclient (SABnzbd) geeft fouten.", None),
    "IndexerStatusCheck": ("indexer_storing", "Een of meer indexers zijn uitgeschakeld na fouten.", None),
    "IndexerLongTermStatusCheck": ("indexer_storing", "Een of meer indexers werken al 6 uur of langer niet.", None),
    "IndexerRssCheck": ("indexer_storing", "Er zijn geen indexers met RSS beschikbaar.", None),
    "IndexerSearchCheck": ("indexer_storing", "Er zijn geen indexers om mee te zoeken.", None),
    "RootFolderCheck": ("map_ontbreekt", "Een mediamap ontbreekt.", "Controleer of de schijf gekoppeld is (mount) en de map bestaat."),
    "ImportMechanismCheck": ("import", "Importeren is niet goed ingesteld.", "Controleer de paden tussen SABnzbd en Sonarr/Radarr."),
    "UpdateCheck": ("update", "Er is een update beschikbaar.", "Updaten gaat via een nieuwe image; dat doe je zelf."),
    "ApplicationLongTermStatusCheck": ("app_koppeling", "De koppeling van Prowlarr met Sonarr of Radarr werkt niet.", None),
}


def arr_meldingen(f: Feiten) -> list[Treffer]:
    if f.dienst not in ("sonarr", "radarr", "prowlarr"):
        return []
    uit = []
    for m in f.app.get("meldingen") or []:
        if not isinstance(m, dict):
            continue
        code, uitleg, handwerk = _BRONNEN.get(m.get("bron") or "", ("melding", "Melding", None))
        ernst = "info" if code == "update" else ("fout" if m.get("soort") == "error" else "waarschuwing")
        acties: list[tuple[str, dict[str, Any]]] = []
        if code == "downloadclient_weg":
            acties = [("herstart_container", {"naam": "sabnzbd"})]
        elif code in ("indexer_storing", "app_koppeling"):
            acties = [("test_indexers", {})]
        uit.append(Treffer(code, ernst, f"{uitleg} ({m.get('bericht')})", acties,
                           handwerk if not acties else None))
    return uit


def arr_wachtrij(f: Feiten) -> list[Treffer]:
    if f.dienst not in ("sonarr", "radarr"):
        return []
    uit = [Treffer("wachtrij_probleem", "waarschuwing", f"{p['titel']} blijft hangen: {p.get('probleem') or p.get('staat')}.",
                   handwerk="Importeer het handmatig in Sonarr/Radarr, of verwijder het en zoek opnieuw.")
           for p in f.app.get("wachtrij_problemen") or []]
    if f.app.get("ontbrekend"):
        uit.append(Treffer("ontbrekend", "info", f"{f.app['ontbrekend']} items ontbreken nog.",
                           [("zoek_ontbrekend", {"app": f.dienst})]))
    return uit


def prowlarr(f: Feiten) -> list[Treffer]:
    if f.dienst != "prowlarr":
        return []
    return [Treffer("indexer_uitgeschakeld", "fout",
                    f"Indexer {s.get('naam')} is na fouten uitgeschakeld tot {s.get('uit_tot')} (storing sinds {s.get('sinds')}).",
                    [("test_indexers", {})])
            for s in f.app.get("storingen") or []]


def bazarr(f: Feiten) -> list[Treffer]:
    if f.dienst != "bazarr":
        return []
    uit = []
    z = f.app.get("zonder_ondertitels") or {}
    voorbeelden = ", ".join(f.app.get("voorbeelden") or [])
    if z.get("afleveringen"):
        uit.append(Treffer("ondertitels_series", "waarschuwing",
                           f"{z['afleveringen']} afleveringen missen ondertitels" + (f": {voorbeelden}" if voorbeelden else "") + ".",
                           [("bazarr_zoek_ondertitels", {"soort": "series"})]))
    if z.get("films"):
        uit.append(Treffer("ondertitels_films", "waarschuwing", f"{z['films']} films missen ondertitels.",
                           [("bazarr_zoek_ondertitels", {"soort": "films"})]))
    for p in f.app.get("providers") or []:
        uit.append(Treffer("provider", "waarschuwing",
                           f"Ondertitelprovider {p['naam']} doet het niet ({p['status']}, opnieuw: {p.get('opnieuw')}).",
                           handwerk="Wacht tot de opgegeven tijd, of controleer het account bij die provider."))
    for m in f.app.get("meldingen") or []:
        uit.append(Treffer("bazarr_melding", "waarschuwing", f"Bazarr meldt: {m}"))
    return uit


def sabnzbd(f: Feiten) -> list[Treffer]:
    if f.dienst != "sabnzbd":
        return []
    uit = [Treffer("download_mislukt", "waarschuwing", f"Download mislukt: {m['naam']} ({m.get('fout') or 'geen reden'}).",
                   [("sabnzbd_opnieuw", {"id": m["id"]})]) for m in f.app.get("mislukt") or []]
    if f.app.get("gepauzeerd"):
        uit.append(Treffer("gepauzeerd", "waarschuwing", f"SABnzbd staat op pauze (vrij: {f.app.get('vrij_gb')} GB).",
                           [("sabnzbd_hervat", {})]))
    return uit


def seerr(f: Feiten) -> list[Treffer]:
    if f.dienst != "seerr":
        return []
    uit = []
    if f.app.get("herstart_nodig"):
        uit.append(Treffer("herstart_nodig", "waarschuwing", "Seerr vraagt om een herstart na een instelling.",
                           [("herstart_container", {"naam": "seerr"})]))
    if f.app.get("mislukte_verzoeken"):
        uit.append(Treffer("verzoek_mislukt", "waarschuwing", f"{f.app['mislukte_verzoeken']} verzoeken zijn mislukt.",
                           handwerk="Open ze in Seerr en dien ze opnieuw in."))
    for app, n in (f.app.get("koppelfouten") or {}).items():
        uit.append(Treffer("koppeling", "fout", f"Seerr kan {app} niet bereiken ({n} fouten)."))
    return uit


def jellyfin(f: Feiten) -> list[Treffer]:
    if f.dienst != "jellyfin":
        return []
    uit = []
    if f.app.get("herstart_nodig"):
        uit.append(Treffer("herstart_nodig", "waarschuwing", "Jellyfin wacht op een herstart.",
                           [("herstart_container", {"naam": "jellyfin"})]))
    if f.app.get("mislukte_taken"):
        uit.append(Treffer("taak_mislukt", "waarschuwing", "Mislukte taken in Jellyfin: " + ", ".join(f.app["mislukte_taken"]) + "."))
    return uit


def bereikbaar(f: Feiten) -> list[Treffer]:
    if f.dienst == "immich" and f.app.get("ping") not in (None, "pong"):
        return [Treffer("niet_bereikbaar", "fout", f"Immich antwoordt niet ({f.app['ping']}).",
                        [("herstart_container", {"naam": "immich_server"})])]
    if f.dienst == "vaultwarden" and f.app.get("alive") not in (None, 200):
        return [Treffer("niet_bereikbaar", "fout", f"Vaultwarden antwoordt niet ({f.app['alive']}).",
                        [("herstart_container", {"naam": "vaultwarden"})])]
    return []


def schijven(f: Feiten) -> list[Treffer]:
    return [Treffer("schijf_vol", "fout" if s["vrij_procent"] < 5 else "waarschuwing",
                    f"Schijf {s['map']} is bijna vol: {s['vrij_procent']}% vrij.",
                    [("sabnzbd_pauzeer", {})] if s["vrij_procent"] < 5 else [],
                    handwerk="Ruim op of verplaats media; dat doe je zelf.")
            for s in f.app.get("schijven") or [] if s["vrij_procent"] < VRIJ_KRAP]


REGELS: list[Regel] = [containers, arr_meldingen, arr_wachtrij, prowlarr, bazarr, sabnzbd, seerr,
                       jellyfin, bereikbaar, schijven]


def herken(f: Feiten) -> list[Treffer]:
    """Alle treffers, ernstigste eerst. Foutregels zonder treffer worden zelf een treffer."""
    uit = [t for regel in REGELS for t in regel(f)]
    if f.foutregels and not any(t.ernst != "info" for t in uit):
        uit.append(Treffer("foutregels", "waarschuwing",
                           f"Er staan {len(f.foutregels)} soorten foutregels in de logs van {f.dienst}; "
                           "geen bekend probleem herkend."))
    volgorde = {"fout": 0, "waarschuwing": 1, "info": 2}
    return sorted(uit, key=lambda t: volgorde.get(t.ernst, 3))
