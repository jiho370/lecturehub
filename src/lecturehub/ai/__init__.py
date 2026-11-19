"""AI engines for explicit user requests. Codex is the default; no silent fallback."""
from .engines import Engine, get_engine, selected, set_choice, status

__all__ = ["Engine", "get_engine", "selected", "set_choice", "status"]
