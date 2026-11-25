"""``<worker> -m lecturehub.mcp``: the AI app link server (D-072), as an AI app starts it.

A lean start: no typer, no other track's CLI, no HTTP/TLS library (the AI app keeps this process alive all day).
The app log gets its usual lines, without the Windows-certificate step only processes that talk to a service need
(``applog.setup(network=False)``). ``lecturehub mcp serve`` runs the same ``server.serve``.
"""
from __future__ import annotations

import sys


def main() -> int:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    from lecturehub.core import applog

    applog.setup("worker", network=False)
    from .server import serve

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
