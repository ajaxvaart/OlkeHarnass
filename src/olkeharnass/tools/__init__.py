"""Gedeelde tools. Nieuwe tool: maak een module in `ingebouwd/` en importeer die hieronder."""

from .registry import Tool, ToolContext, ToolRegistry, registry, tool

# Importeren registreert de tools in `registry`.
from .ingebouwd import diagnose, rekenen, server, tijd  # noqa: E402,F401

__all__ = ["Tool", "ToolContext", "ToolRegistry", "registry", "tool"]
