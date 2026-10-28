"""HTTP-kant van de assistent voor het bord: /api/assistent/send, /decide en /pending.

Het bord praat hier via Caddy mee (zelfde origin, geen key in de browser). Wie
er vraagt, volgt uit Cloudflare Access (zie `toegang.py`). Chatten en weigeren
mag op het thuisnetwerk als `lan_mag_chatten` aan staat; toestaan alleen met een
geldig Access-token van een e-mailadres op de lijst.

POST-verzoeken moeten de kop `X-Bord-Assistent: 1` hebben. Een andere website
kan die kop niet meesturen zonder CORS-toestemming, en die geven we niet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..lus import VerzoekFout, voer_chat_uit_stapsgewijs
from ..tools.acties import Actie, acties as alle_acties
from ..tools.registry import ToolContext
from .gesprekken import GesprekStore
from .meldingen import Logboek, Ntfy
from .toegang import CloudflareToegang
from .voorstellen import VoorstelStore

log = logging.getLogger("olkeharnass.assistent")

MAX_TEKST = 2000
UITVOER_TIMEOUT = 60.0
KOP_BORD = "x-bord-assistent"

SYSTEEMPROMPT = """Je bent de assistent van Olke voor zijn thuisserver apulinux \
(Debian, Docker-mediastack: Sonarr, Radarr, Prowlarr, Bazarr, SABnzbd, Seerr, Jellyfin). \
Het is nu {nu}.

- Antwoord in het Nederlands, kort en concreet.
- Zoek het eerst uit met je tools; begin meestal met bord_status. Verzin niets: \
weet je iets niet, zeg dat dan.
- Zelf verander je niets. Een actie-tool zet alleen een voorstel klaar dat Olke \
in het bord moet goedkeuren. Zeg dan dat het voorstel klaarstaat, nooit dat het al gedaan is.
- Stel een actie alleen voor als die het probleem waarschijnlijk oplost, met een duidelijke reden. \
Alleen een actie-tool maakt een voorstel. Is er geen tool voor, of weigert de tool, \
zeg dan dat je het niet kunt voorstellen.
- Een bericht dat begint met [Besluit van Olke] zegt wat er met een voorstel gebeurd is.
- Je hebt geen shell en geen sudo. Kan iets alleen zo, leg dan uit wat Olke zelf \
in de terminal kan doen."""


# Wat het bord live laat zien per tool. Alleen naam en een kort argument, nooit de uitvoer.
# Per tool: (met argument, zonder als het argument ontbreekt).
STAP_LABELS = {
    "bord_status": ("Bord-status lezen", ""),
    "containers": ("Containers bekijken", ""),
    "container_logs": ("Logs van {naam} lezen", "Logs lezen"),
    "prometheus": ("Prometheus: {query}", "Prometheus bevragen"),
    "arr_status": ("{app} bekijken", "Sonarr/Radarr/Prowlarr bekijken"),
    "sabnzbd_status": ("SABnzbd bekijken", ""),
    "updates": ("Updates opzoeken", ""),
}
MAX_STAP = 80


def stap_label(naam: str, ruwe_args: str) -> str:
    """Korte Nederlandse omschrijving van een toolaanroep, gemaskeerd."""
    from ..tools.ingebouwd.server import maskeer
    try:
        args = json.loads(ruwe_args or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if naam in alle_acties:
        tekst = f"Voorstel klaarzetten: {naam}"
    else:
        sjabloon, kaal = STAP_LABELS.get(naam, (naam, ""))
        try:
            tekst = sjabloon.format(**{k: str(v) for k, v in args.items()})
        except (KeyError, IndexError, ValueError):
            tekst = kaal or naam
    tekst = maskeer(" ".join(tekst.split()))
    return tekst if len(tekst) <= MAX_STAP else tekst[:MAX_STAP - 1] + "…"

VERVOLG = ("Kijk met je tools of het probleem nu is opgelost en meld dat kort. "
           "Is het niet opgelost, zeg dan wat je ziet; stel niet meteen iets nieuws voor.")


def _tijd(t: float) -> str:
    vandaag = time.strftime("%d-%m") == time.strftime("%d-%m", time.localtime(t))
    return time.strftime("%H:%M" if vandaag else "%d-%m %H:%M", time.localtime(t))


def _fout(status: int, bericht: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": bericht,
                                                               "type": "assistent_error"}})


class Assistent:
    def __init__(self, api: Any, toegang: CloudflareToegang | None = None,
                 acties: dict[str, Actie] | None = None) -> None:
        self.api = api
        self.settings = api.state.settings
        self.cfg = self.settings.assistent
        self.app = self.settings.apps[self.cfg.app]
        self.store = VoorstelStore(self.settings.opslag.database,
                                   self.cfg.voorstel_geldig_minuten * 60)
        self.logboek = Logboek(self.cfg.logboek)
        self.ntfy = Ntfy(self.cfg.ntfy, api.state.http)
        if toegang is None and self.cfg.cloudflare is not None:
            toegang = CloudflareToegang(self.cfg.cloudflare)
        self.toegang = toegang
        self.acties = alle_acties if acties is None else acties
        self.gesprekken = GesprekStore(self.settings.opslag.database, self.cfg.gesprek_bewaar_dagen)
        # Een klein model op een kleine GPU: een vraag tegelijk.
        self.bezig = asyncio.Lock()

    # --- wie vraagt er ---

    async def email(self, request: Request) -> str | None:
        return await self.toegang.wie(request.headers) if self.toegang else None

    async def wie(self, request: Request) -> str | None:
        """E-mail via Access, "lan" als het thuisnetwerk mag chatten, anders None."""
        email = await self.email(request)
        if email:
            return email
        return "lan" if self.cfg.lan_mag_chatten else None

    def _ctx(self, stel_voor: Any = None) -> ToolContext:
        return ToolContext(app=self.cfg.app, diensten=self.settings.server_tools,
                           http=self.api.state.http, stel_voor=stel_voor)

    # --- chatten ---

    async def send(self, tekst: str, gesprek_id: str | None, wie: str) -> dict[str, Any]:
        """Zonder live meekijken: alleen het eindresultaat."""
        async for e in self.send_stapsgewijs(tekst, gesprek_id, wie, live=False):
            if e["type"] == "klaar":
                return {k: v for k, v in e.items() if k != "type"}
        raise AssertionError("assistent eindigde zonder resultaat")

    def _geschiedenis(self, gesprek: str) -> list[dict[str, str]]:
        """Wat het model van eerdere beurten terugziet: vragen, antwoorden en besluiten."""
        uit: list[dict[str, str]] = []
        for b in self.gesprekken.berichten(gesprek):
            if b["soort"] == "vraag":
                uit.append({"role": "user", "content": b["tekst"]})
            elif b["soort"] == "antwoord":
                uit.append({"role": "assistant", "content": b["tekst"]})
            elif b["soort"] == "voorstel" and uit and uit[-1]["role"] == "assistant":
                uit[-1] = {"role": "assistant",
                           "content": uit[-1]["content"] + f"\n[Voorstel klaargezet: {b['tekst']}]"}
            elif b["soort"] == "uitkomst":
                uit.append({"role": "user", "content": f"[Besluit van Olke] {b['tekst']}"})
        return uit[-self.cfg.max_berichten:] if self.cfg.max_berichten else []

    def gesprek_voor_bord(self, gesprek: str) -> list[dict[str, Any]]:
        uit: list[dict[str, Any]] = []
        for b in self.gesprekken.berichten(gesprek):
            soort = b["soort"]
            if soort == "vraag":
                uit.append({"who": "me", "text": b["tekst"]})
            elif soort == "antwoord":
                uit.append({"who": "bot", "text": b["tekst"]})
            elif soort == "stappen":
                uit.append({"who": "steps", "steps": (b["extra"] or {}).get("stappen", []),
                            "s": (b["extra"] or {}).get("s", 0)})
            elif soort == "voorstel":
                uit.append({"who": "sys", "text": f"Voorstel klaargezet: {b['tekst']}."})
            else:
                uit.append({"who": "sys", "text": b["tekst"]})
        return uit

    def besloten_voor_bord(self) -> list[dict[str, Any]]:
        woorden = {"uitgevoerd": ("ok", "Toegestaan en uitgevoerd"), "mislukt": ("fail", "Mislukt"),
                   "geweigerd": ("denied", "Geweigerd")}
        return [{"time": _tijd(b["besloten"] or 0), "title": b["titel"],
                 "result": woorden[b["status"]][0], "note": woorden[b["status"]][1],
                 "output": b["uitkomst"] or ""} for b in self.store.besloten()]

    async def send_stapsgewijs(self, tekst: str, gesprek_id: str | None, wie: str,
                               live: bool = True, vervolg: bool = False
                               ) -> AsyncIterator[dict[str, Any]]:
        """Levert events voor het bord: "stap", "stap_klaar", "tekst" en als laatste "klaar".

        Met `live` wordt het model gestreamd, zodat de tekst al verschijnt terwijl
        hij geschreven wordt. Wachten op de beurt telt mee voor de time-out. Met
        `vervolg` is `tekst` een opdracht van de harnass zelf (na een besluit) en
        wordt hij niet als vraag van Olke bewaard.
        """
        if not self.gesprekken.bestaat(gesprek_id):
            gesprek_id = self.gesprekken.nieuw()
        assert gesprek_id is not None
        geschiedenis = self._geschiedenis(gesprek_id)
        if not vervolg:
            self.gesprekken.voeg_toe(gesprek_id, "vraag", tekst)

        nieuw: list[Any] = []

        async def stel_voor(a: Actie, args: Any) -> dict[str, Any]:
            v = self.store.maak(a.naam, args.model_dump(), a.titel(args), a.cmd(args),
                                args.reden, a.risico)
            nieuw.append(v)
            self.logboek.schrijf("voorgesteld", id=v.id, actie=a.naam, args=v.args,
                                 risico=a.risico, titel=v.titel, wie=wie)
            return {"id": v.id, "title": v.titel}

        nu = time.strftime("%d-%m-%Y %H:%M")
        berichten = [{"role": "system", "content": SYSTEEMPROMPT.format(nu=nu)}, *geschiedenis]
        berichten.append({"role": "user", "content": tekst})
        verzoek = {"model": self.cfg.model, "messages": berichten, "temperature": 0.3}
        st = self.api.state
        eind = time.monotonic() + self.cfg.antwoord_timeout

        def rest() -> float:
            return max(0.0, eind - time.monotonic())

        antwoord = ""
        stappen: list[dict[str, Any]] = []
        begin = time.monotonic()
        try:
            await asyncio.wait_for(self.bezig.acquire(), rest())
            try:
                gen = voer_chat_uit_stapsgewijs(
                    verzoek, self.cfg.app, self.app, self.settings, st.registry, st.llm,
                    st.register, llm_stroom=st.llm_stroom if live else None,
                    tool_ctx=self._ctx(stel_voor))
                try:
                    while True:
                        try:
                            soort, data = await asyncio.wait_for(gen.__anext__(), rest())
                        except StopAsyncIteration:
                            break
                        if soort == "tool" and data["fase"] == "start":
                            stappen.append({"tekst": stap_label(data["naam"], data["arguments"])})
                            yield {"type": "stap", "tekst": stappen[-1]["tekst"]}
                        elif soort == "tool":
                            if stappen:
                                stappen[-1].update(ok=data["ok"], ms=data["duur_ms"])
                            yield {"type": "stap_klaar", "ok": data["ok"], "ms": data["duur_ms"]}
                        elif soort == "delta":
                            stuk = data["choices"][0]["delta"].get("content")
                            if stuk:
                                yield {"type": "tekst", "delta": stuk}
                        elif soort == "klaar":
                            antwoord = (data.antwoord["choices"][0]["message"].get("content")
                                        or "").strip()
                finally:
                    await gen.aclose()
            finally:
                self.bezig.release()
            antwoord = antwoord or "(geen antwoord van het model)"
        except asyncio.TimeoutError:
            antwoord = ("Dat duurde te lang; ik ben gestopt. Probeer een kortere of "
                        "preciezere vraag.")
        except VerzoekFout as e:
            antwoord = f"De harnass weigerde het verzoek: {e.bericht}"
        except Exception as e:
            log.exception("assistent: fout bij het model")
            antwoord = f"Het model gaf een fout ({type(e).__name__}). Draait Ollama?"

        if stappen:
            self.gesprekken.voeg_toe(gesprek_id, "stappen", "", {
                "stappen": stappen, "s": round(time.monotonic() - begin)})
        self.gesprekken.voeg_toe(gesprek_id, "antwoord", antwoord)
        for v in nieuw:
            self.gesprekken.voeg_toe(gesprek_id, "voorstel", v.titel, voorstel=v.id)
        yield {"type": "klaar", "text": antwoord, "proposals": [v.voor_bord() for v in nieuw],
               "conversation": gesprek_id}

    # --- besluiten ---

    async def _meld(self, gebeurtenis: str, v: Any, wie: str, uitkomst: str = "",
                    prioriteit: int = 3) -> None:
        self.logboek.schrijf(gebeurtenis, id=v.id, actie=v.actie, args=v.args,
                             risico=v.risico, titel=v.titel, wie=wie, uitkomst=uitkomst)
        woorden = {"uitgevoerd": "Uitgevoerd", "mislukt": "Mislukt", "geweigerd": "Geweigerd",
                   "geen_toegang": "Toestaan geweigerd (geen Access)"}
        await self.ntfy.stuur(f"Assistent: {woorden.get(gebeurtenis, gebeurtenis)}",
                              f"{v.titel}\n{v.cmd}" + (f"\n{uitkomst}" if uitkomst else ""),
                              prioriteit=prioriteit, tags="robot")

    async def decide(self, request: Request, id_: str, toegestaan: bool) -> JSONResponse:
        v = self.store.haal(id_)
        if v is None:
            return JSONResponse(status_code=404, content={"ok": False, "output": "Onbekend voorstel"})

        if not toegestaan:
            wie = await self.wie(request)
            if wie is None:
                return JSONResponse(status_code=403, content={"ok": False, "output": "Geen toegang"})
            if not self.store.weiger(id_, wie):
                return JSONResponse(status_code=409, content={
                    "ok": False, "output": "Dit voorstel was al afgehandeld"})
            await self._meld("geweigerd", v, wie)
            self._in_gesprek(v, f"Geweigerd: {v.titel}.", "denied")
            return JSONResponse({"ok": True, "output": "Geweigerd, niet uitgevoerd"})

        email = await self.email(request)
        if email is None:
            await self._meld("geen_toegang", v, "lan", prioriteit=4)
            return JSONResponse(status_code=403, content={
                "ok": False,
                "output": "Toestaan kan alleen via het bord achter Cloudflare Access. "
                          "Het voorstel blijft open."})

        a = self.acties.get(v.actie)
        if not self.store.claim(id_, email):
            return JSONResponse(status_code=409, content={
                "ok": False, "output": "Dit voorstel is al afgehandeld of verlopen"})
        try:
            if a is None or not self.app.mag_tool(v.actie):
                raise RuntimeError(f"actie {v.actie!r} staat niet (meer) op de lijst")
            args = a.args_model.model_validate(v.args)
            ctx = self._ctx()
            fout = a.controle(args, ctx) if a.controle else None
            if fout:
                raise RuntimeError(fout)
            uitkomst = str(await asyncio.wait_for(a.uitvoer(args, ctx), UITVOER_TIMEOUT))
            gelukt = True
        except Exception as e:
            uitkomst = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            gelukt = False
        self.store.afronden(id_, gelukt, uitkomst)
        await self._meld("uitgevoerd" if gelukt else "mislukt", v, email, uitkomst,
                         prioriteit=3 if gelukt else 4)
        gesprek = self._in_gesprek(
            v, f"{'Toegestaan en uitgevoerd' if gelukt else 'Mislukt'}: {v.titel}. {uitkomst}".strip(),
            "ok" if gelukt else "fail")
        # Met followUp vraagt het bord daarna een vervolgbeurt: kijken of het geholpen heeft.
        return JSONResponse({"ok": gelukt, "output": uitkomst, "conversation": gesprek,
                             "followUp": gesprek is not None})

    def _in_gesprek(self, v: Any, tekst: str, resultaat: str) -> str | None:
        """Zet een besluit in het gesprek waarin het voorstel gedaan is."""
        gesprek = self.gesprekken.gesprek_van_voorstel(v.id)
        if gesprek:
            self.gesprekken.voeg_toe(gesprek, "uitkomst", tekst,
                                     {"resultaat": resultaat, "vervolgd": int(resultaat == "denied")},
                                     voorstel=v.id)
        return gesprek


def maak_router(assistent: Assistent) -> APIRouter:
    router = APIRouter(prefix="/api/assistent")

    def _bord_kop(request: Request) -> bool:
        return request.headers.get(KOP_BORD) == "1"

    async def _json(request: Request) -> dict[str, Any] | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @router.post("/send")
    async def send(request: Request) -> Any:
        if not _bord_kop(request):
            return _fout(403, "kop X-Bord-Assistent ontbreekt")
        wie = await assistent.wie(request)
        if wie is None:
            return _fout(403, "de assistent is alleen bereikbaar via Cloudflare Access")
        body = await _json(request) or {}
        vervolg = body.get("followUp")
        if vervolg is not None:
            # Na een besluit: de harnass bepaalt de opdracht zelf, niet de browser.
            if not isinstance(vervolg, str):
                return _fout(400, "veld `followUp` moet een voorstel-id zijn")
            gesprek = assistent.gesprekken.gesprek_van_voorstel(vervolg)
            if gesprek is None or not assistent.gesprekken.claim_vervolg(vervolg):
                return _fout(409, "geen vervolg mogelijk voor dit voorstel")
            tekst = VERVOLG
        else:
            tekst = body.get("text")
            if not isinstance(tekst, str) or not tekst.strip():
                return _fout(400, "veld `text` ontbreekt")
            if len(tekst) > MAX_TEKST:
                return _fout(400, f"bericht te lang (max {MAX_TEKST} tekens)")
            tekst = tekst.strip()
            gesprek = body.get("conversation")
            gesprek = gesprek if isinstance(gesprek, str) else None
        if body.get("stream") is not True:
            async for e in assistent.send_stapsgewijs(tekst, gesprek, wie, live=False,
                                                      vervolg=vervolg is not None):
                if e["type"] == "klaar":
                    return {k: v for k, v in e.items() if k != "type"}

        async def events() -> AsyncIterator[str]:
            async for e in assistent.send_stapsgewijs(tekst, gesprek, wie,
                                                      vervolg=vervolg is not None):
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

        # no-transform/X-Accel-Buffering: proxy's (Caddy, Cloudflare) niet laten bufferen.
        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @router.post("/decide")
    async def decide(request: Request) -> Any:
        if not _bord_kop(request):
            return _fout(403, "kop X-Bord-Assistent ontbreekt")
        body = await _json(request)
        if not body or not isinstance(body.get("id"), str) or not isinstance(body.get("approved"), bool):
            return _fout(400, "verwacht {id: string, approved: boolean}")
        return await assistent.decide(request, body["id"], body["approved"])

    @router.get("/conversation")
    async def conversation(request: Request, id: str | None = None) -> Any:
        if await assistent.wie(request) is None:
            return _fout(403, "de assistent is alleen bereikbaar via Cloudflare Access")
        g = id if assistent.gesprekken.bestaat(id) else assistent.gesprekken.laatste()
        return {"conversation": g, "messages": assistent.gesprek_voor_bord(g) if g else []}

    @router.post("/new")
    async def nieuw(request: Request) -> Any:
        if not _bord_kop(request):
            return _fout(403, "kop X-Bord-Assistent ontbreekt")
        if await assistent.wie(request) is None:
            return _fout(403, "de assistent is alleen bereikbaar via Cloudflare Access")
        return {"conversation": assistent.gesprekken.nieuw()}

    @router.get("/done")
    async def done(request: Request) -> Any:
        if await assistent.wie(request) is None:
            return _fout(403, "de assistent is alleen bereikbaar via Cloudflare Access")
        return assistent.besloten_voor_bord()

    @router.get("/pending")
    async def pending(request: Request) -> Any:
        if await assistent.wie(request) is None:
            return _fout(403, "de assistent is alleen bereikbaar via Cloudflare Access")
        return [v.voor_bord() for v in assistent.store.open_voorstellen()]

    return router
