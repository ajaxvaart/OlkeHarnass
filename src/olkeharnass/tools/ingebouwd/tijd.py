from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from ..registry import ToolContext, tool


class HuidigeTijdArgs(BaseModel):
    tijdzone: str = Field(
        default="Europe/Amsterdam",
        description="IANA-tijdzone, bijvoorbeeld Europe/Amsterdam",
    )


@tool("huidige_tijd", "Geeft de huidige datum, tijd en weekdag in een tijdzone.")
async def huidige_tijd(args: HuidigeTijdArgs, ctx: ToolContext) -> dict:
    try:
        tz = ZoneInfo(args.tijdzone)
    except ZoneInfoNotFoundError:
        return {"fout": f"onbekende tijdzone {args.tijdzone!r}"}
    nu = datetime.now(tz)
    weekdagen = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    return {
        "iso": nu.isoformat(timespec="seconds"),
        "datum": nu.strftime("%Y-%m-%d"),
        "tijd": nu.strftime("%H:%M"),
        "weekdag": weekdagen[nu.weekday()],
        "tijdzone": args.tijdzone,
    }
