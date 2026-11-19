"""Loopback-only HTTP server so the app's <audio> element can seek in recordings.

Only files explicitly registered by the app are served, under random tokens,
read-only, with HTTP Range support (needed to jump to a transcript time).
"""
from __future__ import annotations

import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
_TYPES = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav"}


class MediaServer:
    def __init__(self):
        self._files: dict[str, Path] = {}
        self._by_path: dict[Path, str] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silent under pythonw
                pass

            def do_HEAD(self):
                self._serve(body=False)

            def do_GET(self):
                self._serve(body=True)

            def _serve(self, body: bool):
                match = re.fullmatch(r"/m/([A-Za-z0-9_-]+)", self.path)
                path = server._files.get(match.group(1)) if match else None
                if path is None or not path.is_file():
                    self.send_error(404)
                    return
                size = path.stat().st_size
                start, end = 0, size - 1
                ranged = _RANGE.match(self.headers.get("Range", ""))
                if ranged and (ranged.group(1) or ranged.group(2)):
                    if ranged.group(1):
                        start = int(ranged.group(1))
                        end = int(ranged.group(2)) if ranged.group(2) else end
                    else:
                        start = max(0, size - int(ranged.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                kind = _TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_header("Content-Type", kind)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if not body:
                    return
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    try:
                        while remaining > 0:
                            chunk = stream.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (ConnectionError, OSError):
                        pass  # the player seeked away and closed this request

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url_for(self, path: str | Path) -> str:
        resolved = Path(path).resolve()
        token = self._by_path.get(resolved)
        if token is None:
            token = secrets.token_urlsafe(12)
            self._files[token], self._by_path[resolved] = resolved, token
        return f"http://127.0.0.1:{self.port}/m/{token}"

    def close(self) -> None:
        self.httpd.shutdown()
