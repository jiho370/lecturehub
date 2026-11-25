"""AI app link (D-072): a read-only local MCP server over stdio, started by an AI app as
``<worker> -m lecturehub.mcp`` (``__main__.py``; ``lecturehub mcp serve`` is an alias). No network, no writes,
no new dependency; what it may show is decided in ``access`` alone (master switch + per-course consent
``ai_app_link``). Keep this file empty of imports: it is loaded first on the lean start."""
