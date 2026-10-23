"""Rekenmachine-tool. Kleine modellen rekenen slecht; dit doet het exact.

Gebruikt een eigen evaluator over de Python-AST in plaats van eval(), zodat
alleen getallen en rekenkundige operatoren mogelijk zijn.
"""

from __future__ import annotations

import ast
import math
import operator

from pydantic import BaseModel, Field

from ..registry import ToolContext, tool

_BINAIR = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNAIR = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIES = {"sqrt": math.sqrt, "round": round, "abs": abs}
_MAX_EXPONENT = 1000
_MAX_LENGTE = 200


def bereken(expressie: str) -> float | int:
    if len(expressie) > _MAX_LENGTE:
        raise ValueError(f"expressie langer dan {_MAX_LENGTE} tekens")
    boom = ast.parse(expressie, mode="eval")
    return _eval(boom.body)


def _eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINAIR:
        links, rechts = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(rechts) > _MAX_EXPONENT:
            raise ValueError("exponent te groot")
        return _BINAIR[type(node.op)](links, rechts)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNAIR:
        return _UNAIR[type(node.op)](_eval(node.operand))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIES
        and not node.keywords
    ):
        return _FUNCTIES[node.func.id](*(_eval(a) for a in node.args))
    raise ValueError(f"niet toegestaan in expressie: {ast.dump(node)[:60]}")


class RekenenArgs(BaseModel):
    expressie: str = Field(
        description="Rekenkundige expressie, bijv. '(12.5 * 4) / 3' of 'sqrt(2) * 10'. "
        "Toegestaan: + - * / // % **, haakjes, sqrt, round, abs."
    )


@tool("rekenen", "Rekent een rekenkundige expressie exact uit.")
async def rekenen(args: RekenenArgs, ctx: ToolContext) -> dict:
    try:
        return {"expressie": args.expressie, "uitkomst": bereken(args.expressie)}
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as e:
        return {"fout": f"kan niet berekenen: {e}"}
