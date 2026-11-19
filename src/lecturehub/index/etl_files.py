"""eTL announcements and course files next to what is already on this PC (user request 2026-10-11).

"eTL에 들어가서 공지사항에 뭐가 올랐는지, 파일은 내 폴더 확인해서 안 받아져 있으면 받아 줘. 앱에도 반영."
Read-only GETs with the user's own token (``etl_sync``). Two files are written under ``_lecturehub\\etl``:
``announcements.json`` and ``files.json``. Nothing is downloaded by a sync: ``compare`` only says, for every
eTL file, what this PC has —

- ``have``     the course folder holds it (same size; the name may differ),
- ``local``    it sits in Downloads or 카카오톡 받은 파일 (same size, same name) → it is moved, not fetched again,
- ``changed``  the course folder holds a file of that name with another size → the user decides. Which side
               changed is read from three times (``_which``): when my file was made on this PC (= downloaded),
               when it was last written, and when eTL's file was put up. A file this module placed itself is
               judged from what was written down then (eTL's size and date, the bytes' hash) instead,
- ``missing``  nowhere → fetched when the user asks.

``take`` puts one file into the course folder (D-067: new files only, never over an existing one, size checked).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

from . import etl_sync, organize

BIG = 300 * 1024 * 1024  # larger files are shown with their size and never fetched without a second yes
_COPY = re.compile(r"\s*\(\d+\)$")  # "name (1).pdf": the browser's mark for a second download


def _move_no_clobber(source: Path, target: Path) -> None:
    """Move without ever replacing a file that appears at ``target`` meanwhile (audit #10).

    Same drive: ``os.link`` fails if the name exists, then the source name is removed. Other drive: copy to a
    temporary name beside the target first, then ``os.link`` it in (exclusive). If hard links are not possible,
    the bytes go through an exclusive create (``xb``). The source is removed only after success."""
    import tempfile

    source, target = Path(source), Path(target)
    refusal = f"같은 이름의 파일이 이미 있어 덮어쓰지 않습니다: {target.name}"
    try:
        os.link(source, target)
    except FileExistsError:
        raise ValueError(refusal) from None
    except OSError:
        pass  # another drive, or no hard links here
    else:
        source.unlink()
        return
    fd, tmp_name = tempfile.mkstemp(prefix=".etl-", suffix=".part", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise ValueError(refusal) from None
        except OSError:
            try:
                with open(target, "xb") as out, open(tmp, "rb") as src:
                    shutil.copyfileobj(src, out)
                    out.flush()
                    os.fsync(out.fileno())
            except FileExistsError:
                raise ValueError(refusal) from None
            shutil.copystat(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    source.unlink()


def places() -> list[Path]:
    """Where a file the user already downloaded by hand usually is."""
    home = Path.home()
    return [home / "Downloads", home / "Documents" / "카카오톡 받은 파일"]


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("p", "br", "div", "li"):
            self.parts.append("\n")


def _plain(html: str | None) -> str:
    parser = _Text()
    parser.feed(html or "")
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _courses(root: Path, token: str, fetch: etl_sync.Fetch) -> tuple[list[tuple[int, str]], list[str]]:
    prefix, subjects = etl_sync.term_prefix(root), etl_sync.timetable(root)
    matched, unmatched = [], []
    for course in etl_sync._all(fetch, f"{etl_sync.BASE}/api/v1/courses?enrollment_state=active&per_page=100", token):
        name = str(course.get("name") or "")
        if not name.startswith(prefix + " "):
            continue
        subject = etl_sync._SECTION.sub("", name[len(prefix) + 1:]).strip()
        folder = subjects.get(subject)
        if folder:
            matched.append((int(course["id"]), folder))
        else:
            unmatched.append(subject)
    return matched, unmatched


def fetch(root: Path, token: str, fetch: etl_sync.Fetch = etl_sync._http) -> dict:
    """Announcements and file lists of this term's timetable courses. A course whose file list is closed is skipped."""
    year = etl_sync.term_prefix(root)[:4]
    courses, unmatched = _courses(root, token, fetch)
    announcements, files, closed = [], [], []
    for course_id, folder in courses:
        url = (f"{etl_sync.BASE}/api/v1/announcements?context_codes[]=course_{course_id}&per_page=50"
               f"&start_date={year}-01-01&end_date={int(year) + 1}-03-01")
        for item in etl_sync._all(fetch, url, token):
            posted = item.get("posted_at") or item.get("delayed_post_at") or ""
            announcements.append({
                "id": f"etl:{item['id']}", "course": folder, "title": str(item.get("title") or "").strip(),
                "posted": datetime.fromisoformat(posted).astimezone(etl_sync.KST).strftime("%Y-%m-%dT%H:%M") if posted else "",
                "text": _plain(item.get("message")), "url": item.get("html_url"),
                "attachments": [str(a.get("display_name") or "") for a in item.get("attachments") or []]})
        try:
            listed = etl_sync._all(fetch, f"{etl_sync.BASE}/api/v1/courses/{course_id}/files?per_page=100", token)
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403, 404):
                raise
            closed.append(folder)  # the course does not show its file list (materials may be in modules only)
            continue
        for item in listed:
            updated = item.get("updated_at") or item.get("created_at") or ""
            files.append({
                "id": int(item["id"]), "course": folder, "name": str(item.get("display_name") or item.get("filename") or ""),
                "size": int(item.get("size") or 0), "url": item.get("url"),
                "updated": datetime.fromisoformat(updated).astimezone(etl_sync.KST).strftime("%Y-%m-%dT%H:%M") if updated else ""})
    announcements.sort(key=lambda a: a["posted"], reverse=True)
    return {"announcements": announcements, "files": files, "unmatched": unmatched, "closed": closed}


def _dir(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "etl"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def refresh(root: Path, token: str, fetch_with: etl_sync.Fetch = etl_sync._http) -> dict:
    """Fetch and write both lists. Raises what the fetch raises; the caller words the error."""
    found = fetch(root, token, fetch_with)
    etl_sync._write(_dir(root) / "announcements.json", found["announcements"])
    etl_sync._write(_dir(root) / "files.json", {"at": time.time(), "files": found["files"], "closed": found["closed"]})
    return {"announcements": len(found["announcements"]), "files": len(found["files"]), "closed": found["closed"]}


def announcements(root: Path) -> list[dict]:
    return _read(_dir(root) / "announcements.json", [])


def _key(name: str) -> tuple[str, str]:
    """A name as two downloads of one file share it: case, Unicode form and the browser's "(1)" aside."""
    path = Path(unicodedata.normalize("NFC", name))
    return _COPY.sub("", path.stem).strip().lower(), path.suffix.lower()


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, etl_sync.KST).strftime("%Y-%m-%dT%H:%M")


def _sha256(path: Path) -> str:
    import hashlib

    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pdf_made(path: Path) -> float | None:
    """When the PDF's content was made, by its own record (CreationDate). None for other files or unreadable ones."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from lecturehub.materials.pdfdoc import PdfDoc

        with PdfDoc(path) as document:
            stamp = document.created
        found = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:([+\-Z])(\d{2})'?(\d{2})?)?", stamp)
        if not found:
            return None
        year, month, day, hour, minute, second = (int(found[i]) for i in range(1, 7))
        zone = etl_sync.KST  # a stamp without a zone was written on this PC's clock
        if found[7] == "Z":
            zone = UTC
        elif found[7] in ("+", "-"):
            offset = timedelta(hours=int(found[8]), minutes=int(found[9] or 0))
            zone = timezone(offset if found[7] == "+" else -offset)
        return datetime(year, month, day, hour, minute, second, tzinfo=zone).timestamp()
    except Exception:  # noqa: BLE001 - a damaged PDF simply gives no extra evidence
        return None


HINTS = {
    "mine": "내가 고친 파일입니다(받은 뒤에 저장했고, eTL 파일은 그 전부터 그대로입니다). 필기·수정본으로 보입니다",
    "etl": "eTL 자료가 바뀌었습니다(내가 받은 뒤에 새로 올라왔고, 내 파일은 받은 그대로입니다)",
    "both": "둘 다 바뀌었습니다: 내가 받은 뒤에 eTL에 새 판이 올라왔고, 내 파일도 받은 뒤에 고쳤습니다",
    "unknown": "어느 쪽이 바뀌었는지 시각만으로는 알 수 없습니다(복사해 온 파일이거나 받은 때가 남아 있지 않음)",
}


class _File(NamedTuple):
    path: Path
    size: int
    mtime: float
    key: tuple[str, str]


def _scan(folder: Path, deep: bool) -> list[_File]:
    """Files in ``folder`` (and below it when ``deep``) with size, mtime and name key, from one scandir
    pass: the directory listing already carries the sizes, so no stat call per file per eTL item."""
    found: list[_File] = []
    stack = [Path(folder)]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as listing:
                entries = list(listing)
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
                elif entry.is_file():
                    stat = entry.stat()
                    found.append(_File(Path(entry.path), stat.st_size, stat.st_mtime, _key(entry.name)))
            except OSError:  # gone or unreadable since the listing
                continue
        if deep:
            stack.extend(reversed(subdirs))
    return found


def _which(item: dict, own: Path, placed: dict | None) -> dict:
    """Who changed: {"case": mine | etl | both | unknown, "certain": bool, ...}.

    With a record of the day this module placed the file: eTL changed when its size or date is no longer
    the recorded one, mine changed when its bytes no longer hash to the recorded value. Without one, three
    times decide — Windows keeps the time a file was made on this PC (a download makes it), so "written
    after it was made" is my edit and "put up on eTL after it was made" is a new version there."""
    stat = own.stat()
    times = {"mine_created": _stamp(stat.st_ctime), "mine_changed": _stamp(stat.st_mtime)}
    if placed and placed.get("sha256"):
        etl_changed = (placed.get("etl_size"), placed.get("etl_updated")) != (item["size"], item["updated"])
        mine_edited = _sha256(own) != placed["sha256"]
        certain = True
    else:
        uploaded = datetime.fromisoformat(item["updated"]).replace(tzinfo=etl_sync.KST).timestamp() if item["updated"] else None
        copied = stat.st_mtime < stat.st_ctime - 120  # made here later than it was last written: a copy, download time lost
        # A second download over the same name keeps the first file's "made" time (Windows), so that time can be
        # older than the content. A PDF says when its content was made: if that is later, the content came later
        # too — and a document made a few minutes before eTL's upload time is that upload (measured 2026-10-11:
        # "made" 14:02, PDF made 14:55:55, eTL 14:56:28 — the same slides, wrongly read as a new version).
        received, margin = stat.st_ctime, 60
        written = _pdf_made(own)
        if written and written > received + 60:
            received, margin = written, 600
        etl_changed = bool(uploaded and not copied and uploaded > received + margin)
        mine_edited = stat.st_mtime > stat.st_ctime + 120
        certain = False
        if copied or uploaded is None:
            return {**times, "case": "unknown", "certain": False, "hint": HINTS["unknown"]}
    case = "both" if etl_changed and mine_edited else "etl" if etl_changed else "mine" if mine_edited else "unknown"
    return {**times, "case": case, "certain": certain, "hint": HINTS[case] + ("" if certain else " — 파일 시각으로 본 추정")}


def compare(root: Path, *, look_in: list[Path] | None = None) -> list[dict]:
    """Every eTL file with what this PC has of it. Reads names, sizes and dates only."""
    root = Path(root)
    listed = _read(_dir(root) / "files.json", {}).get("files", [])
    decided = _read(_dir(root) / "decisions.json", {})
    placed = {entry["path"]: entry for entry in _read(_dir(root) / "placed.json", [])}  # the latest record of a path
    look_in = places() if look_in is None else look_in
    outside = [f for place in look_in for f in _scan(place, deep=False)]
    inside: dict[str, list[_File]] = {}
    rows = []
    for item in listed:
        if item["course"] not in inside:
            inside[item["course"]] = _scan(root / item["course"], deep=True)
        mine = inside[item["course"]]
        key = _key(item["name"])
        row = {**{k: item[k] for k in ("id", "course", "name", "size", "updated")}, "big": item["size"] > BIG}
        same_size = [f for f in mine if f.size == item["size"] and f.path.suffix.lower() == key[1]]
        same_name = [f for f in mine if f.key == key]
        if same_size:
            rows.append({**row, "state": "have", "path": str(same_size[0].path)})
            continue
        if same_name:
            own = max(same_name, key=lambda f: f.mtime)
            if decided.get(str(item["id"]), {}).get("keep_against") == item["size"]:
                rows.append({**row, "state": "kept", "path": str(own.path)})
                continue
            rows.append({**row, "state": "changed", "path": str(own.path), "mine_size": own.size,
                         **_which(item, own.path, placed.get(str(own.path)))})
            continue
        waiting = [f for f in outside if f.size == item["size"] and f.key == key]
        if waiting:
            rows.append({**row, "state": "local", "path": str(waiting[0].path)})
        else:
            rows.append({**row, "state": "missing"})
    return rows


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """eTL hands the file over through a redirect to its file store: followed only while it stays https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "eTL이 https가 아닌 주소로 넘겼습니다", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _checked_url(url: str) -> str:
    """What eTL listed is input from outside: only an https address on the eTL host itself is asked for."""
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme != "https" or parts.hostname != urllib.parse.urlsplit(etl_sync.BASE).hostname:
        raise ValueError("eTL 주소가 아닌 내려받기 주소라 받지 않습니다.")
    return str(url)


def _download(url: str, target: Path) -> None:
    """The file's own eTL link (it carries its one-time check), so the token is not sent to the file host."""
    request = urllib.request.Request(_checked_url(url), headers={"User-Agent": "LectureHub"})
    opener = urllib.request.build_opener(_HttpsOnly)
    with opener.open(request, timeout=60) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out, 1024 * 1024)


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_name(name: str) -> str:
    """A file name as eTL shows it is input from outside: it must be a plain name, never a path."""
    name = str(name)
    if (not name.strip() or name in (".", "..") or _BAD_NAME.search(name) or name != name.strip()
            or name.endswith(".") or Path(name).name != name):
        raise ValueError(f"파일 이름으로 쓸 수 없는 이름이라 넣지 않습니다: {name!r}")
    return name


def _destination(root: Path, row: dict, beside: bool) -> Path:
    folder = Path(root) / row["course"]
    if beside:  # next to the user's own copy, under a name that says what it is
        own = Path(row["path"])
        day = f"{int(row['updated'][5:7])}.{int(row['updated'][8:10])}" if row.get("updated") else "새 판"
        return own.with_name(f"{_COPY.sub('', own.stem)} (eTL {day}){own.suffix}")
    name = _safe_name(row["name"])
    category = organize.guess(name)
    target = (organize.folder_for(folder, category) if category else folder) / name
    if not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError("과목 폴더 밖으로 나가는 경로라 넣지 않습니다.")
    return target


def take(root: Path, file_id: int, *, beside: bool = False, allow_big: bool = False,
         download: Callable[[str, Path], None] = _download, look_in: list[Path] | None = None) -> dict:
    """Put one eTL file into its course folder: moved from Downloads when it is there, fetched otherwise.

    ``beside`` is the user's answer to a changed file: keep mine, add the eTL one next to it."""
    root = Path(root)
    row = next((r for r in compare(root, look_in=look_in) if r["id"] == int(file_id)), None)
    if row is None:
        raise ValueError("eTL 파일 목록에 없는 파일입니다. 먼저 [지금 가져오기]를 눌러 주세요.")
    if row["state"] in ("have", "kept") or (row["state"] == "changed" and not beside):
        raise ValueError("이미 과목 폴더에 있는 파일입니다.")
    if not (root / row["course"]).is_dir():
        raise ValueError(f"과목 폴더가 없습니다: {row['course']}")
    target = _destination(root, row, beside and row["state"] == "changed")
    if target.exists():
        raise ValueError(f"같은 이름의 파일이 이미 있어 덮어쓰지 않습니다: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if row["state"] == "local":
        _move_no_clobber(Path(row["path"]), target)
        how = "moved"
    else:
        if row["big"] and not allow_big:
            raise ValueError(f"큰 파일입니다({row['size'] / 1024 ** 3:.2f}GB). 받으려면 한 번 더 확인해 주세요.")
        url = next((f.get("url") for f in _read(_dir(root) / "files.json", {}).get("files", []) if f["id"] == row["id"]), None)
        if not url:
            raise ValueError("이 파일의 내려받기 주소가 없습니다. [지금 가져오기]를 다시 눌러 주세요.")
        _checked_url(url)  # also when a test or another caller brings its own download function
        part = _dir(root) / "tmp" / f"{row['id']}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        try:
            download(url, part)
            if part.stat().st_size != row["size"]:
                raise ValueError(f"받은 크기가 eTL과 다릅니다({part.stat().st_size:,} / {row['size']:,}바이트). 넣지 않았습니다.")
            shutil.move(str(part), target)
        finally:
            part.unlink(missing_ok=True)
        how = "downloaded"
    log = _read(_dir(root) / "placed.json", [])
    log.append({"at": time.time(), "id": row["id"], "course": row["course"], "path": str(target), "how": how,
                "size": row["size"], "beside": bool(beside),
                # what eTL had and what was put here, so a later difference can be told apart for certain
                "etl_size": row["size"], "etl_updated": row["updated"], "sha256": _sha256(target)})
    etl_sync._write(_dir(root) / "placed.json", log)
    return {"state": how, "path": str(target), "name": target.name}


def take_all(root: Path, *, download: Callable[[str, Path], None] = _download,
             look_in: list[Path] | None = None) -> dict:
    """[빠진 파일 모두 받기] (user request 2026-11-14): every file that is only in Downloads or nowhere, one by one
    through ``take``. Files with another size (the user's notes) and big files are left for their own button."""
    rows = [r for r in compare(root, look_in=look_in) if r["state"] in ("local", "missing")]
    done, failed = [], []
    for row in rows:
        if row["big"]:
            failed.append({"name": row["name"], "course": row["course"], "error": "큰 파일이라 따로 받습니다"})
            continue
        try:
            got = take(root, row["id"], download=download, look_in=look_in)
            done.append({"name": got["name"], "course": row["course"], "state": got["state"]})
        except (ValueError, OSError) as exc:
            failed.append({"name": row["name"], "course": row["course"], "error": str(exc)})
    return {"taken": done, "failed": failed, "moved": sum(d["state"] == "moved" for d in done),
            "downloaded": sum(d["state"] == "downloaded" for d in done)}


def keep(root: Path, file_id: int) -> dict:
    """The user's answer to a changed file: mine stays, do not ask again for this eTL version."""
    row = next((r for r in compare(root, look_in=[]) if r["id"] == int(file_id)), None)
    if row is None or row["state"] != "changed":
        raise ValueError("정할 것이 없는 파일입니다.")
    decided = _read(_dir(root) / "decisions.json", {})
    decided[str(row["id"])] = {"keep_against": row["size"], "at": time.time(), "by": "user"}
    etl_sync._write(_dir(root) / "decisions.json", decided)
    return {"state": "kept"}


def board(root: Path) -> dict:
    """What the app shows: recent announcements and the files that need a click."""
    rows = compare(root)
    todo = [r for r in rows if r["state"] in ("local", "missing", "changed")]
    order = {"changed": 0, "local": 1, "missing": 2}
    todo.sort(key=lambda r: (r["course"], order[r["state"]], r["name"]))
    listed = _read(_dir(root) / "files.json", {})
    return {"announcements": announcements(root)[:30], "files": todo, "have": sum(r["state"] in ("have", "kept") for r in rows),
            "total": len(rows), "closed": listed.get("closed", []), "at": listed.get("at")}
