"""Wie vraagt er: Olke via Cloudflare Access, of iemand op het thuisnetwerk.

Cloudflare Access zet bij elk verzoek dat door de tunnel komt de kop
`Cf-Access-Jwt-Assertion`: een token dat Cloudflare ondertekent. We controleren
de handtekening met de publieke sleutels van het Access-team, de audience (AUD)
van de Access-applicatie, de uitgever en de vervaltijd, en of het e-mailadres
op de lijst staat. Op het thuisnetwerk is er geen token; daar mag toestaan niet.
Een nagemaakte kop helpt niet: zonder de sleutel van Cloudflare klopt de
handtekening niet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import jwt

from ..config import CloudflareConfig

log = logging.getLogger("olkeharnass.assistent")

KOP = "cf-access-jwt-assertion"


class CloudflareToegang:
    def __init__(self, cfg: CloudflareConfig, sleutels: Any = None) -> None:
        self.cfg = cfg
        self.uitgever = f"https://{cfg.team_domein}"
        # PyJWKClient haalt de sleutels op en bewaart ze; tests geven een eigen bron mee.
        self._sleutels = sleutels or jwt.PyJWKClient(
            f"{self.uitgever}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=10
        )
        self._emails = {e.strip().lower() for e in cfg.emails}

    def _controleer(self, token: str) -> str | None:
        try:
            sleutel = self._sleutels.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, sleutel, algorithms=["RS256"], audience=self.cfg.aud,
                issuer=self.uitgever, options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception as e:  # ongeldig token, verlopen, of sleutels niet op te halen
            log.warning("Access-token geweigerd: %s: %s", type(e).__name__, e)
            return None
        email = str(claims.get("email") or "").lower()
        if email not in self._emails:
            log.warning("Access-token van %r staat niet op de lijst", email)
            return None
        return email

    async def wie(self, koppen: Any) -> str | None:
        """Het e-mailadres uit een geldig Access-token, anders None."""
        token = koppen.get(KOP)
        if not token:
            return None
        # Sleutels ophalen gebeurt met urllib (blokkerend), dus in een thread.
        return await asyncio.to_thread(self._controleer, token)
