"""Trust the certificates Windows trusts (release review 2026-11-16).

Python's network libraries bring their own list of certificate authorities (certifi). A school or company
network that inspects HTTPS installs its own authority in Windows; browsers then work and this app did not
(every request to Google, Groq or eTL failed with a certificate error). ``truststore`` makes Python ask Windows,
like a browser does.

Called once at the start of the window and of every helper (``applog.setup``, after the log file is attached so
that a failure is written there). If it cannot be done the app goes on with the built-in list, exactly as
before; it never weakens verification (nothing here turns checking off).

What follows the Windows store after this, and what does not. Told on 2026-11-19 by reading the installed code
(truststore 0.10.5, httpx 0.28.1, requests 2.34.2, urllib3 2.8.0, websockets 17.4, grpcio 1.85.1) and by
tests/test_release_safety.py, which checks the context each Python library builds; no handshake was tried:

- follows: whatever builds its TLS context from Python's ``ssl.SSLContext`` after this call, because
  ``inject_into_ssl`` replaces that class (and urllib3's own reference to it). In this app: ``httpx`` (Gemini,
  Groq, class notices; ``ssl.create_default_context(cafile=certifi)``), ``urllib.request`` (eTL, update check,
  connection tests), the ``websockets`` sync client for wss:// (Gemini Live captions;
  ``ssl.create_default_context()``) and ``requests``/``urllib3`` (google-auth fetching the login token for
  Chirp). truststore asks Windows to check the chain first; only when Windows refuses does it try the
  authorities loaded into the context (certifi's list for httpx and requests), so a certificate either trusts
  is accepted.
- does not follow: gRPC. Chirp captions (``google-cloud-speech`` ``SpeechClient``, gRPC transport) make the TLS
  connection in gRPC's C core with the ``roots.pem`` shipped inside ``grpcio`` (``grpc.ssl_channel_credentials()``
  without roots); Python's ``ssl`` is not used for it. On a network that inspects HTTPS, Chirp captions can
  still fail with a certificate error even though the token fetch works.
- not touched: the window itself (WebView2 has its own certificate handling), other programs the app starts, and
  the recorder engine (``python -m lecturehub.recorder.service`` never calls ``applog.setup``).

Cost: ``inject_into_ssl()`` imports ``urllib3`` and ``requests`` to patch them, so every process that starts the
log pays for those imports, also helpers that never use the network. Measured 2026-11-19 on the developer's PC
(12 fresh processes per case, interleaved, below-normal priority): ``applog.setup("worker")`` took a median
1220 ms with this step and 284 ms without; ``import truststore`` + ``inject_into_ssl()`` alone a median 1290 ms.
"""
from __future__ import annotations

_done = False


def use_system_certificates() -> bool:
    """True when Python's ``ssl`` module now verifies against the Windows certificate store (see the module text
    for which connections that covers). A failure is reported through ``applog.swallowed`` and never raised."""
    global _done
    if _done:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()  # the library's own entry point; do not copy its body here
        _done = True
    except Exception:  # noqa: BLE001 - missing package or an old Windows: keep the bundled list
        from . import applog

        applog.swallowed("Windows 인증서 저장소 사용")
    return _done
