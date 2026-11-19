"""클로바노트에서 새 노트(녹음 + 전사)를 받아 오는 단계(사용자 요청 2026-11-05).

브라우저 조작은 Aside CLI(``aside repl``, AI 없음)가 한다 — 그스룸과 같은 실행기(``booking/aside_cli``)를 import만 해서 쓴다.
받은 파일은 **다운로드 폴더**에 ``클로바노트 YYYY-MM-DD HH-MM.m4a`` / ``.txt``로 둔다. 그러면 기존 [가져오기] 창의
'최근 받은 파일'에 녹음이 뜨고(P-024), 사용자가 과목·날짜를 확인해 가져오면 같은 이름의 전사가 함께 들어간다
(``import_audio._attach_sidecar``). 학기 폴더에는 쓰지 않는다. 받은 기록은 ``_lecturehub\\clova\\fetched.jsonl``(추가만).

지키는 것: 클로바노트에서 삭제·공유·수정 금지(목록 정렬과 내려받기만), 비밀번호를 다루지 않음(로그인 화면이 나오면
멈추고 ``login_required``), 다른 기기에서 업로드 중인 노트는 건너뜀, 이미 받은 노트는 건너뜀(노트 id, 같은 녹음의 sha256).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

LEDGER = Path("_lecturehub") / "clova" / "fetched.jsonl"
NAME = "클로바노트 {:%Y-%m-%d %H-%M}"
SESSION_DIR = re.compile(r"sessionDir:\s*(.+?)\s*$", re.MULTILINE)


def stage_folder() -> Path:
    override = os.environ.get("LECTUREHUB_CLOVA_STAGE")
    return Path(override) if override else Path.home() / "Downloads"


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ledger(root: Path) -> list[dict]:
    path = Path(root) / LEDGER
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _append(root: Path, entry: dict) -> None:
    path = Path(root) / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def known_ids(root: Path) -> list[str]:
    return sorted({str(r["note_id"]) for r in ledger(root) if r.get("note_id") and r.get("state") in ("staged", "duplicate")})


def _free(folder: Path, stem: str, suffix: str) -> Path:
    for n in range(1, 100):
        path = folder / f"{stem}{f' ({n})' if n > 1 else ''}{suffix}"
        if not path.exists():
            return path
    raise ValueError("같은 이름의 파일이 너무 많습니다")


def _put(source: Path, dest: Path) -> str:
    """Move the browser's download to its stage name (never overwrite) and check the bytes.

    Aside saves downloads straight into the Downloads folder, so a move (a rename there) leaves one copy, not two.
    Across drives it copies (exclusive create), checks, then removes the browser's file."""
    digest = _sha256(source)
    try:
        os.rename(source, dest)  # Windows: fails when dest exists, so nothing is overwritten
    except FileExistsError:
        raise
    except OSError:
        with source.open("rb") as src, dest.open("xb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        if _sha256(dest) != digest:
            dest.unlink(missing_ok=True)
            raise ValueError(f"옮긴 파일이 원본과 다릅니다: {dest.name}") from None
        source.unlink()
        return digest
    if _sha256(dest) != digest:
        raise ValueError(f"옮긴 파일이 원본과 다릅니다: {dest.name}")
    return digest


def stage(connection: sqlite3.Connection, root: Path, result: dict, session_dir: Path | None, *,
          folder: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Rename what the browser script downloaded (Aside saves into Downloads) to the stage name, one note at a time.

    A note whose recording is already in the app or already staged (same sha256) is not staged again."""
    folder = folder or stage_folder()
    folder.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now().astimezone()
    seen = {r.get("audio_sha256") for r in ledger(root) if r.get("state") == "staged"}
    out = []
    for note in result.get("notes") or []:
        files = {f.get("kind"): Path(session_dir or "", f["path"]) for f in note.get("files") or [] if f.get("path")}
        audio, text = files.get("audio"), files.get("text")
        entry = {"ts": now.isoformat(timespec="seconds"), "note_id": note.get("id"), "created": note.get("created"),
                 "duration": note.get("duration_ms") or note.get("duration"), "title": note.get("title")}
        if audio is None or not audio.is_file():
            out.append({**entry, "state": "failed", "why": note.get("error") or "녹음 파일을 받지 못했습니다",
                        "left": [str(p) for p in files.values() if p.is_file()]})
            _append(root, out[-1])
            continue
        digest = _sha256(audio)
        found = connection.execute("SELECT lecture_id FROM MediaAsset WHERE sha256 = ?", (digest,)).fetchone()
        if found or digest in seen:
            out.append({**entry, "state": "duplicate", "audio_sha256": digest,
                        "why": "이미 앱에 있는 녹음" if found else "이미 받아 둔 녹음",
                        "left": [str(p) for p in files.values() if p.is_file()]})  # the browser's copy stays; not deleted
            _append(root, out[-1])
            continue
        try:
            when = datetime.fromisoformat(str(note.get("created")))
        except ValueError:
            when = now
        stem = NAME.format(when)
        audio_dest = _free(folder, stem, audio.suffix.lower() or ".m4a")
        stem = audio_dest.stem  # the transcript must share the recording's name
        _put(audio, audio_dest)
        text_dest = None
        if text is not None and text.is_file():
            text_dest = folder / f"{stem}.txt"
            if text_dest.exists():
                text_dest = None
            else:
                _put(text, text_dest)
        seen.add(digest)
        out.append({**entry, "state": "staged", "audio": str(audio_dest), "text": str(text_dest) if text_dest else None,
                    "audio_sha256": digest, "bytes": audio_dest.stat().st_size})
        _append(root, out[-1])
    return out


def session_dir_of(stdout: str) -> Path | None:
    match = SESSION_DIR.search(re.sub(r"\x1b\[[0-9;]*m", "", stdout or ""))
    return Path(match.group(1)) if match else None


STATUS = Path("_lecturehub") / "clova" / "status.json"
LOGIN_MESSAGE = "Aside에서 네이버 로그인 필요 — Aside 창에서 네이버에 로그인('로그인 상태 유지')한 뒤 다시 확인하세요"


def load_status(root: Path) -> dict | None:
    """The last check, for the import dialog: when, whether it worked, what the user has to do."""
    try:
        return json.loads((Path(root) / STATUS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _save_status(root: Path, status: dict) -> None:
    path = Path(root) / STATUS
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


EVERY = timedelta(days=7)  # weekly check while the app is open (사용자 결정 2026-11-06: 앱 안 타이머)
RETRY = timedelta(days=1)  # after a failed or logged-out check


def due(root: Path, now: datetime | None = None) -> bool:
    """The app's weekly check: never checked, last good check 7+ days ago, or a failed one a day ago."""
    now = now or datetime.now().astimezone()
    status = load_status(root)
    if not status or not status.get("checked_at"):
        return True
    try:
        last = datetime.fromisoformat(str(status["checked_at"]))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.astimezone()
    return now - last >= (EVERY if status.get("ok") else RETRY)


def sync(connection: sqlite3.Connection, root: Path, *, limit: int = 3, stop=None, execute=None,
         folder: Path | None = None, now: datetime | None = None) -> dict:
    """Fetch up to ``limit`` notes not fetched before, and leave the outcome in status.json.

    Safe to run again and again: notes already fetched (note id) and recordings already in the app or the stage
    folder (sha256) are skipped. ``execute`` stands in for the browser in tests (script → stdout or error dict)."""
    from lecturehub.booking import aside_cli

    from . import clova_script

    now = now or datetime.now().astimezone()
    started = time.monotonic()
    exe = None
    if execute is None:
        exe = aside_cli.find_aside()
        if not exe:
            result = {"ok": False, "error": "Aside CLI(aside.exe)를 찾지 못했습니다. Aside 설정 > Developers에서 설치하세요"}
    staged: list[dict] = []
    for _ in range(max(1, limit)) if execute is not None or exe else ():
        script = clova_script.build(known_ids(root))  # one note per browser run (the REPL stops at 120 s)
        if execute is not None:
            out = execute(script)
        else:
            out = aside_cli.execute([exe, "repl"], script, timeout=clova_script.TIMEOUT_S, stop=stop)
        if isinstance(out, dict):
            result = out
            break
        result = aside_cli.parse_output(out[0])
        if not result.get("ok"):
            break
        got = stage(connection, root, result, session_dir_of(out[0]), folder=folder, now=now)
        staged += got
        if not got or not result.get("more") or any(n["state"] == "failed" for n in got):
            break
    counts = {state: sum(1 for n in staged if n["state"] == state) for state in ("staged", "duplicate", "failed")}
    message = LOGIN_MESSAGE if result.get("login_required") else result.get("error") or (
        f"새 노트 {counts['staged']}개를 다운로드 폴더에 받았습니다 — [가져오기]에서 과목을 확인하세요" if counts["staged"]
        else "새 노트가 없습니다")
    _save_status(root, {"checked_at": now.isoformat(timespec="seconds"), "ok": bool(result.get("ok")),
                        "login_required": bool(result.get("login_required")), "message": message, **counts,
                        "skipped_uploading": result.get("skipped_uploading", 0),
                        "files": [n.get("audio") for n in staged if n["state"] == "staged"]})
    return {**{k: v for k, v in result.items() if k != "notes"}, "notes": staged, "message": message, **counts,
            "seconds": round(time.monotonic() - started, 1)}
