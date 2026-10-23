from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from olkeharnass.tools import ToolContext, ToolRegistry, registry, tool
from olkeharnass.tools.ingebouwd.rekenen import bereken

CTX = ToolContext(app="test")


class Args(BaseModel):
    x: int


@pytest.mark.parametrize("expr,verwacht", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("2 ** 10", 1024), ("7 // 2", 3),
    ("-5 + 2", -3), ("sqrt(16)", 4.0), ("round(2.567, 2)", 2.57),
])
def test_bereken(expr, verwacht):
    assert bereken(expr) == verwacht


@pytest.mark.parametrize("expr", [
    "__import__('os').system('ls')", "open('x')", "a + 1", "9 ** 9 ** 9", "[1,2]", "x" * 300,
])
def test_bereken_weigert(expr):
    with pytest.raises((ValueError, SyntaxError)):
        bereken(expr)


def test_rekenen_tool_geeft_fout_als_json():
    uit = json.loads(asyncio.run(registry.get("rekenen").voer_uit('{"expressie": "1/0"}', CTX)))
    assert "fout" in uit


def test_ongeldige_args_gaan_terug_naar_model():
    uit = json.loads(asyncio.run(registry.get("rekenen").voer_uit('{"verkeerd": 1}', CTX)))
    assert "ongeldige argumenten" in uit["fout"]


def test_huidige_tijd():
    uit = json.loads(asyncio.run(registry.get("huidige_tijd").voer_uit("{}", CTX)))
    assert uit["tijdzone"] == "Europe/Amsterdam"
    assert uit["weekdag"] in {"maandag", "dinsdag", "woensdag", "donderdag",
                              "vrijdag", "zaterdag", "zondag"}


def test_schema_uit_pydantic():
    schema = registry.get("rekenen").openai_schema()
    assert schema["function"]["name"] == "rekenen"
    assert schema["function"]["parameters"]["required"] == ["expressie"]


def test_eigen_tool_registreren_en_crash_afvangen():
    reg = ToolRegistry()

    @tool("kapot", "gaat altijd mis", registry_=reg)
    async def kapot(args: Args, ctx: ToolContext) -> str:
        raise RuntimeError("boem")

    assert reg.namen() == ["kapot"]
    uit = json.loads(asyncio.run(reg.get("kapot").voer_uit('{"x": 1}', CTX)))
    assert "boem" in uit["fout"]
    with pytest.raises(ValueError):
        tool("kapot", "dubbel", registry_=reg)(kapot)
