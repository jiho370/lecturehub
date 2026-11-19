"""Shared browser discovery and artifact URL allowlist, independent of optional features."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

URL_RE = re.compile(r"https://claude\.ai/(artifact|code/artifact)/[A-Za-z0-9_-]+")
MAX_URL = 200


def is_allowed_url(url) -> bool:
    return isinstance(url, str) and len(url) <= MAX_URL and URL_RE.fullmatch(url) is not None


class BrowserFinder:
    def __init__(self, *, env=None, which=None, isfile=None):
        self.env = os.environ if env is None else env
        self.which = which or shutil.which
        self.isfile = isfile or os.path.isfile

    def find_browser(self, preference="edge-app"):
        if preference == "default":
            return "default", None
        order = ("chrome", "edge") if preference == "chrome-app" else ("edge", "chrome")
        for kind in order:
            rel, names = (("Microsoft", "Edge", "Application", "msedge.exe"), ("msedge", "microsoft-edge")) \
                if kind == "edge" else (("Google", "Chrome", "Application", "chrome.exe"), ("chrome", "google-chrome"))
            for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
                base = self.env.get(variable)
                if base:
                    candidate = str(Path(base).joinpath(*rel))
                    if self.isfile(candidate):
                        return kind, candidate
            for name in names:
                found = self.which(name)
                if found:
                    return kind, found
        return "default", None
