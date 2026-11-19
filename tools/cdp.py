"""Minimal Chrome DevTools Protocol client for verification screenshots (dev tool).

Works with WebView2 (app started with LECTUREHUB_UI_CDP_PORT) and headless Edge.
Requires websocket-client.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from itertools import count
from pathlib import Path

import websocket


def targets(port: int, timeout: float = 30) -> list[dict]:
    end = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                pages = [t for t in json.load(response) if t.get("type") == "page"]
                if pages:
                    return pages
        except OSError:
            pass
        if time.monotonic() > end:
            raise TimeoutError(f"CDP port {port} not ready")
        time.sleep(0.3)


class Page:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=60)
        self.ids = count(1)

    @classmethod
    def find(cls, port: int, contains: str, timeout: float = 30) -> Page:
        end = time.monotonic() + timeout
        while True:
            for target in targets(port, timeout):
                if contains in target.get("url", ""):
                    return cls(target["webSocketDebuggerUrl"])
            if time.monotonic() > end:
                raise TimeoutError(f"no page with {contains!r}")
            time.sleep(0.3)

    def send(self, method: str, **params):
        msg_id = next(self.ids)
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") == msg_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def eval(self, expression: str, *, wait: bool = True):
        result = self.send("Runtime.evaluate", expression=expression, awaitPromise=wait, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 30, interval: float = 0.2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = self.eval(expression)
            if value:
                return value
            time.sleep(interval)
        raise TimeoutError(expression)

    def viewport(self, width: int, height: int) -> None:
        self.send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1,
                  mobile=False)

    def scheme(self, dark: bool) -> None:
        self.send("Emulation.setEmulatedMedia", features=[
            {"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])

    def screenshot(self, path: Path) -> Path:
        data = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    def close(self) -> None:
        self.ws.close()
