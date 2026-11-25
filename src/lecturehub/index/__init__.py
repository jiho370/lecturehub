"""Existing-semester source indexing, search and status services.

The names below are loaded on first use (PEP 562): importing one small module of this package, such as
``index.lectures`` for the AI app link's lean start (D-072), does not load the indexer and every parser.
"""

_LAZY = {"IndexFailure": "ingest", "IndexReport": "ingest", "index_root": "ingest",
         "source_inventory": "scanner", "verify_sources": "scanner"}

__all__ = ["IndexFailure", "IndexReport", "index_root", "source_inventory", "verify_sources"]


def __getattr__(name: str):
    if name in _LAZY:
        from importlib import import_module

        value = getattr(import_module(f"{__name__}.{_LAZY[name]}"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
