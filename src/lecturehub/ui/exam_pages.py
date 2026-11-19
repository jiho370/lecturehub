"""과목별 시험 페이지 링크 (사용자 요청 2026-11-07, DECISIONS P-045).

시험 대비는 과목마다 너무 달라서 앱 안의 문제 풀기 화면 대신, 과목별로 따로 만든 시험 페이지
(claude.ai 아티팩트나 학기 폴더 안의 HTML)를 [시험] 화면에서 링크로 연다. 단어 암기장
(memorizer_link.py, P-037)의 여는 방식을 과목 여러 개로 넓힌 것이다.

Rules kept here:
- Links live only in ``_lecturehub\\exam\\pages.json`` under the data root, never in the repository.
- A link is either a claude.ai artifact (same full-match allowlist as the memorizer) or an ``.html``/``.htm``
  file inside the semester root (stored relative to the root, resolved and checked again before opening).
- The browser is started with an argument list (never a shell string): Edge → Chrome → default browser.
- Only ``_lecturehub\\exam\\pages.json`` is written (atomic replace). No DB.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path, PureWindowsPath

from lecturehub.core.textio import EncodingSuspectError, read_text_any

from .browser_links import BrowserFinder, is_allowed_url

FOLDER = ("_lecturehub", "exam")
FILE = "pages.json"
PAGE_SUFFIXES = (".html", ".htm")
MAX_LINKS = 12
MAX_TITLE = 60
MAX_COURSE = 80
_LOCK = threading.Lock()


def _clean_title(text) -> str:
    return " ".join(str(text or "").split())[:MAX_TITLE]


class ExamPages:
    def __init__(self, root, *, env=None, which=None, isfile=None, popen=None, open_default=None, new_id=None):
        self.root = Path(root)
        self._popen = popen or subprocess.Popen
        self._open_default = open_default or webbrowser.open
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:10])
        self._browsers = BrowserFinder(env=env, which=which, isfile=isfile)

    @property
    def path(self) -> Path:
        return self.root.joinpath(*FOLDER, FILE)

    # ---------------------------------------------------------------- reading
    def _local(self, rel) -> Path | None:
        """A stored relative path → the file inside the root, or None when it is not an allowed page."""
        if not isinstance(rel, str) or not rel.strip():
            return None
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root.resolve()) or target.suffix.lower() not in PAGE_SUFFIXES:
            return None
        return target

    def _load(self) -> dict[str, list[dict]]:
        try:
            data = json.loads(read_text_any(self.path))
        except (OSError, ValueError, EncodingSuspectError):
            return {}
        courses = data.get("courses") if isinstance(data, dict) else None
        found: dict[str, list[dict]] = {}
        for course, links in (courses or {}).items() if isinstance(courses, dict) else ():
            if not isinstance(course, str) or not isinstance(links, list):
                continue
            kept = []
            for link in links:
                if not isinstance(link, dict) or not isinstance(link.get("id"), str):
                    continue
                url, rel = link.get("url"), link.get("path")
                if is_allowed_url(url) or (url is None and self._local(rel) is not None):  # hand-edited junk is dropped
                    kept.append({"id": link["id"], "title": _clean_title(link.get("title")) or "시험 페이지",
                                 **({"url": url} if url else {"path": rel})})
            if kept:
                found[course] = kept[:MAX_LINKS]
        return found

    def _view(self, link: dict) -> dict:
        if "url" in link:
            return {**link, "kind": "artifact", "target": link["url"], "missing": False}
        target = self._local(link["path"])
        return {**link, "kind": "file", "target": str(target), "missing": not (target and target.is_file())}

    def pages(self, course: str | None = None) -> dict:
        found = self._load()
        if course is not None:
            return {"ok": True, "course": course, "pages": [self._view(x) for x in found.get(course, [])]}
        return {"ok": True, "courses": {c: [self._view(x) for x in links] for c, links in found.items()}}

    # ---------------------------------------------------------------- writing
    def _write(self, courses: dict[str, list[dict]]) -> None:
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": 1, "courses": courses}, ensure_ascii=False, indent=1)
        with _LOCK:
            fd, tmp = tempfile.mkstemp(prefix=".pages-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def _course(self, course) -> str | None:
        name = str(course or "").strip()
        return name if name and len(name) <= MAX_COURSE else None

    def add(self, course, target, title="") -> dict:
        name = self._course(course)
        if not name:
            return {"ok": False, "error": "과목을 먼저 고르세요"}
        text = str(target or "").strip().strip('"')  # pasted from the address bar or "경로로 복사"
        if is_allowed_url(text):
            link = {"url": text}
            default_title = "시험 페이지"
        else:
            if "://" in text:
                return {"ok": False, "error": "claude.ai 아티팩트 주소(https://claude.ai/artifact/…)나 학기 폴더 안 HTML 파일만 넣을 수 있어요"}
            path = Path(text)
            if not path.is_absolute():
                path = self.root / PureWindowsPath(text)
            try:
                resolved = path.resolve()
            except OSError:
                resolved = None
            if (resolved is None or not resolved.is_relative_to(self.root.resolve())
                    or resolved.suffix.lower() not in PAGE_SUFFIXES or not resolved.is_file()):
                return {"ok": False, "error": "학기 폴더 안에 있는 .html 파일만 넣을 수 있어요"}
            link = {"path": resolved.relative_to(self.root.resolve()).as_posix()}
            default_title = resolved.stem
        courses = self._load()
        links = courses.get(name, [])
        if any(x.get("url", x.get("path")) == link.get("url", link.get("path")) for x in links):
            return {"ok": False, "error": "이미 넣은 시험 페이지예요"}
        if len(links) >= MAX_LINKS:
            return {"ok": False, "error": f"한 과목에 {MAX_LINKS}개까지 넣을 수 있어요"}
        links.append({"id": self._new_id(), "title": _clean_title(title) or default_title, **link})
        courses[name] = links
        try:
            self._write(courses)
        except OSError as exc:
            return {"ok": False, "error": f"저장하지 못했습니다: {exc}"}
        return self.pages(name)

    def remove(self, course, link_id) -> dict:
        name = self._course(course)
        courses = self._load()
        links = courses.get(name or "", [])
        kept = [x for x in links if x["id"] != link_id]
        if len(kept) == len(links):
            return {"ok": False, "error": "그 시험 페이지를 찾지 못했어요"}
        if kept:
            courses[name] = kept
        else:
            courses.pop(name, None)
        try:
            self._write(courses)
        except OSError as exc:
            return {"ok": False, "error": f"저장하지 못했습니다: {exc}"}
        return self.pages(name)

    # ---------------------------------------------------------------- opening
    def open(self, course, link_id) -> dict:
        link = next((x for x in self._load().get(self._course(course) or "", []) if x["id"] == link_id), None)
        if link is None:
            return {"ok": False, "error": "그 시험 페이지를 찾지 못했어요"}
        if "url" in link:
            address = link["url"]
            if not is_allowed_url(address):  # checked again right before launch
                return {"ok": False, "error": "허용되지 않은 주소예요"}
        else:
            target = self._local(link["path"])
            if target is None or not target.is_file():
                return {"ok": False, "error": f"파일이 없어요: {link['path']}"}
            address = target.as_uri()
        kind, exe = self._browsers.find_browser("edge-app")
        try:
            if exe:
                kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "close_fds": True}
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
                self._popen([exe, f"--app={address}"], **kwargs)  # argument list, no shell
            elif not self._open_default(address):
                return {"ok": False, "error": "기본 브라우저를 열지 못했습니다"}
        except OSError as exc:
            return {"ok": False, "error": f"브라우저를 열지 못했습니다: {exc}"}
        return {"ok": True, "browser_found": kind, "title": link["title"]}
