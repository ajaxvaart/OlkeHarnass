"""API-keys per app.

In de config staat alleen de sha256-hash van een key, zodat het bestand
geen geheimen bevat. Een nieuwe key maak je met `olkeharnass-key nieuw <app>`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys

from fastapi import HTTPException, Request

from .config import AppConfig, Settings

KEY_PREFIX = "hk_"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def nieuwe_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def zoek_app(settings: Settings, key: str) -> tuple[str, AppConfig] | None:
    h = hash_key(key)
    for naam, app in settings.apps.items():
        if hmac.compare_digest(h, app.key_sha256):
            return naam, app
    return None


def app_uit_verzoek(request: Request) -> tuple[str, AppConfig]:
    """FastAPI-dependency: leest `Authorization: Bearer <key>` en geeft de app terug."""
    header = request.headers.get("authorization", "")
    schema, _, key = header.partition(" ")
    if schema.lower() != "bearer" or not key:
        raise HTTPException(status_code=401, detail="ontbrekende of ongeldige Authorization-header")
    gevonden = zoek_app(request.app.state.settings, key.strip())
    if gevonden is None:
        raise HTTPException(status_code=401, detail="onbekende API-key")
    return gevonden


def cli() -> None:
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "nieuw":
        print("gebruik: olkeharnass-key nieuw <appnaam>", file=sys.stderr)
        raise SystemExit(2)
    key = nieuwe_key()
    print(f"API-key voor {args[1]} (geef aan de app, bewaar niet in de harnass-config):")
    print(f"  {key}")
    print("Zet dit in config/olkeharnass.yaml onder apps:")
    print(f"  {args[1]}:")
    print(f"    key_sha256: {hash_key(key)}")
