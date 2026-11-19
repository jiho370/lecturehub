"""Bring a recording made outside the app into a lecture (user request 2026-10-01, P-024).

A phone recorder file, a file received over KakaoTalk, a download. The user's own file is never
moved, renamed or changed: the app copies it to ``_lecturehub\\imports\\<start>_import\\`` (add-only,
sha256 checked) and registers that copy. A file that already lives in the semester folder (e.g.
녹음본) is registered where it is. A format the player and the engines do not read directly (amr,
3ga, ogg, ...) also gets an MP3 made from it, and that MP3 is the registered recording.

Course, date, start time and device are what the user confirmed in the import dialog. The app only
pre-fills them (file name, creation_time tag, file time, timetable) and records what it pre-filled
and from where (Evidence ``imported_from``); nothing is linked without the user's click.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from lecturehub.core import proc
from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid

DIRECT = {".m4a", ".mp3", ".wav"}  # played and transcribed as they are
CONVERT = {".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".3ga"}  # an MP3 is made from these
AUDIO = DIRECT | CONVERT
FOLDER = "imports"
EVIDENCE = "imported_from"
DEVICES = ("phone", "laptop", "external_mic")
KAKAO_FOLDER = "카카오톡 받은 파일"
_BASIS = {"filename": "파일 이름의 날짜·시각", "filename_date": "파일 이름의 날짜",
          "creation_time": "파일 안의 녹음 시각 기록", "mtime": "파일 수정 시각(받은 시각일 수 있음)"}

_FULL = (
    re.compile(r"(?<!\d)(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)[ _T-]+(\d\d)[-_.:]?(\d\d)(?:[-_.:]?(\d\d))?(?!\d)"),
    re.compile(r"(?<!\d)(\d\d)(\d\d)(\d\d)[ _-](\d\d)(\d\d)(\d\d)(?!\d)"),  # 음성 261001_103012
)
_DAY = (
    re.compile(r"(?<!\d)(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)(?!\d)"),
    re.compile(r"(?<!\d)(\d\d)\.(\d{1,2})\.(\d{1,2})(?!\d)"),  # 26.9.29 과목 (the 녹음본 naming)
)
_KOREAN_DAY = re.compile(r"(?<!\d)(\d{1,2})월\s*(\d{1,2})일")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _plausible(day: date, today: date) -> bool:
    return today - timedelta(days=3 * 366) <= day <= today + timedelta(days=1)


def name_stamp(name: str, today: date) -> tuple[date | None, clock_time | None]:
    """(date, time) written in a file name; either may be missing. Implausible dates are ignored."""
    stem = Path(name).stem
    for pattern in _FULL:
        for found in pattern.finditer(stem):
            y, mo, d, h, mi, s = (int(v) if v else 0 for v in found.groups())
            try:
                day, clock = date(y if y > 99 else 2000 + y, mo, d), clock_time(h, mi, s)
            except ValueError:
                continue
            if _plausible(day, today):
                return day, clock
    for pattern in _DAY:
        for found in pattern.finditer(stem):
            y, mo, d = (int(v) for v in found.groups())
            try:
                day = date(y if y > 99 else 2000 + y, mo, d)
            except ValueError:
                continue
            if _plausible(day, today):
                return day, None
    found = _KOREAN_DAY.search(stem)
    if found:
        for year in (today.year, today.year - 1):
            try:
                day = date(year, int(found.group(1)), int(found.group(2)))
            except ValueError:
                break
            if day <= today + timedelta(days=1):
                return day, None
    return None, None


def _local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000).astimezone().replace(tzinfo=None)  # local wall clock


def _epoch_ms(local: datetime) -> int:
    return int(local.astimezone().timestamp() * 1000)


def _day_ms(moment: datetime) -> int:
    return ((moment.hour * 60 + moment.minute) * 60 + moment.second) * 1000


def _course_in_name(connection: sqlite3.Connection, root: Path, name: str) -> str | None:
    from lecturehub.core import extra_courses

    names = [r[0] for r in connection.execute("SELECT folder_name FROM Course")] + extra_courses.names(root)
    found = sorted({n for n in names if n and n in name})
    longest = [n for n in found if not any(n != other and n in other for other in found)]
    return longest[0] if len(longest) == 1 else None


def guess(connection: sqlite3.Connection, root: Path, name: str, *, tag_ms: int | None, mtime_ms: int | None,
          duration_ms: int, today: date) -> dict:
    """What the dialog starts from — a suggestion with its basis, never a decision."""
    from .schedule import daily_schedule

    day, clock = name_stamp(name, today)
    # recorders that never set the clock write 1904/1970 (and Windows cannot even convert a negative time)
    if tag_ms is not None and (tag_ms < 946_684_800_000 or not _plausible(_local(tag_ms).date(), today)):
        tag_ms = None
    stamp = basis = None
    if day and clock:
        stamp, basis = datetime.combine(day, clock), "filename"
    elif tag_ms is not None and (day is None or _local(tag_ms).date() == day):
        stamp, basis = _local(tag_ms), "creation_time"
    elif day is None and mtime_ms is not None and _plausible(_local(mtime_ms).date(), today):
        stamp, basis = _local(mtime_ms), "mtime"
    out = {"date": day.isoformat() if day else None, "time": None, "basis": "filename_date" if day else None,
           "role": None, "course": None, "course_basis": None, "class": None}
    if stamp is not None:
        # A recorder stamps either the start or the end; a file time is the end. The timetable settles which.
        options = [("start", stamp), ("end", stamp - timedelta(milliseconds=duration_ms))]
        if basis == "mtime":
            options.reverse()
        best = None
        for role, start in options:
            at = _day_ms(start)
            for item in daily_schedule(connection, start.date()):
                overlap = min(at + duration_ms, item.end_ms) - max(at, item.start_ms)
                if overlap > 0 and (best is None or overlap > best[0]):
                    best = (overlap, role, start, item)
        role, start = (best[1], best[2]) if best else options[0]
        out.update(date=start.date().isoformat(), time=f"{start.hour:02d}:{start.minute:02d}", basis=basis, role=role)
        if best:
            item = best[3]
            out.update(course=item.course, course_basis="timetable",
                       **{"class": f"{item.start_ms // 3_600_000:02d}:{item.start_ms % 3_600_000 // 60_000:02d}–"
                                   f"{item.end_ms // 3_600_000:02d}:{item.end_ms % 3_600_000 // 60_000:02d}"})
    named = _course_in_name(connection, root, name)
    if named:  # the user's own file name says more than an overlap
        out.update(course=named, course_basis="filename")
    notes = []
    if out["basis"]:
        notes.append(_BASIS[out["basis"]] + ("(끝 시각으로 보고 길이만큼 앞당김)" if out["role"] == "end" else ""))
    if out["course_basis"] == "timetable":
        notes.append(f"시간표의 {out['course']} {out['class']}과 겹침")
    elif out["course_basis"] == "filename":
        notes.append("파일 이름에 과목 이름")
    out["note"] = " · ".join(notes)
    return out


def _duplicate(connection: sqlite3.Connection, digest: str) -> dict | None:
    from .discard import _lecture_label, discarded_ids

    row = connection.execute("SELECT id, lecture_id FROM MediaAsset WHERE sha256 = ?", (digest,)).fetchone()
    if row is None:
        return None
    return {"media_id": row[0], "lecture_id": row[1], "lecture": _lecture_label(connection, row[1]),
            "discarded": row[0] in discarded_ids(connection)}


def _duplicate_reason(found: dict) -> str:
    if found["discarded"]:
        return "버린 녹음과 같은 파일입니다. [녹음 정리]에서 되살리세요"
    return f"이미 있는 녹음입니다{' — ' + found['lecture'] if found['lecture'] else ''}"


def probe(connection: sqlite3.Connection, root: Path, path: str | Path, *, today: date, probe_media=None) -> dict:
    """One dropped or picked file, for the import dialog. Never raises: a file that cannot come in says why."""
    from .media import MediaProbeError
    from .media import probe_media as real_probe

    source = Path(path)
    out = {"path": str(source), "name": source.name, "ok": False, "error": None}
    if not source.is_file():
        return {**out, "error": "파일이 아닙니다(폴더는 가져오지 않습니다)" if source.is_dir() else "파일을 찾지 못했습니다"}
    suffix = source.suffix.lower()
    if suffix not in AUDIO:
        return {**out, "error": f"녹음 파일이 아닙니다({suffix or '확장자 없음'}) — " + " ".join(sorted(AUDIO))}
    if _inside(source, Path(root) / "_lecturehub"):
        return {**out, "error": "앱이 보관하는 파일입니다(이미 앱 안에 있음)"}
    try:
        meta = (probe_media or real_probe)(source)
        info = source.stat()
        found = _duplicate(connection, _sha256(source))
    except MediaProbeError as exc:
        why = ("소리 도구(ffprobe)를 찾지 못했거나 너무 오래 걸렸습니다" if "unavailable" in str(exc)
               else "깨졌거나 소리가 들어 있지 않은 파일입니다")
        return {**out, "error": f"소리를 읽지 못했습니다 — {why}"}
    except OSError as exc:
        return {**out, "error": f"파일을 읽지 못했습니다(다른 프로그램이 쓰는 중일 수 있음): {exc.strerror or exc}"}
    out.update(bytes=info.st_size, duration_ms=meta.duration_ms, convert=suffix in CONVERT,
               kakao=KAKAO_FOLDER in source.parts)
    if found:
        return {**out, "error": _duplicate_reason(found), "duplicate": found}
    out["guess"] = guess(connection, Path(root), source.name, tag_ms=meta.recorded_start,
                         mtime_ms=int(info.st_mtime * 1000), duration_ms=meta.duration_ms, today=today)
    return {**out, "ok": True}


def default_folders(home: Path | None = None) -> list[Path]:
    """Where received recordings usually land: KakaoTalk's folder and Downloads.
    LECTUREHUB_IMPORT_FOLDERS (os.pathsep-separated) replaces the list (verification with a scratch folder)."""
    override = os.environ.get("LECTUREHUB_IMPORT_FOLDERS")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    home = home or Path.home()
    return [home / "Documents" / KAKAO_FOLDER, home / "OneDrive" / "문서" / KAKAO_FOLDER,
            home / "OneDrive" / "Documents" / KAKAO_FOLDER, home / "Downloads"]


def recent(connection: sqlite3.Connection, folders: list[Path], *, now_ms: int, days: int = 30,
           limit: int = 12) -> list[dict]:
    """Recording files that recently arrived in those folders (top level only), newest first."""
    seen = set()
    for (detail,) in connection.execute("SELECT detail FROM Evidence WHERE subject_type = 'MediaAsset' AND kind = ?",
                                        (EVIDENCE,)):
        data = json.loads(detail)
        seen.add((data.get("source_path"), data.get("source_bytes"), data.get("source_mtime_ms")))
    rows = []
    for folder in folders:
        try:
            entries = [p for p in folder.iterdir() if p.suffix.lower() in AUDIO and p.is_file()]
        except OSError:
            continue
        for entry in entries:
            info = entry.stat()
            modified = int(info.st_mtime * 1000)
            if now_ms - modified <= days * 86_400_000:
                rows.append({"path": str(entry), "name": entry.name, "folder": folder.name, "bytes": info.st_size,
                             "mtime_ms": modified, "imported": (str(entry), info.st_size, modified) in seen})
    return sorted(rows, key=lambda r: r["mtime_ms"], reverse=True)[:limit]


def _to_mp3(source: Path, target: Path) -> None:
    from lecturehub.transcription.audio import _tool

    try:
        subprocess.run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-n", "-threads", "1",
                        "-i", str(source), "-map", "0:a:0", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k", str(target)],
                       check=True, capture_output=True, creationflags=proc.BELOW_NORMAL | proc.NO_WINDOW)
    except subprocess.CalledProcessError as exc:
        raise ValueError("MP3로 바꾸지 못했습니다: " + exc.stderr.decode("utf-8", "replace")[-300:]) from exc


def _new_folder(root: Path, start: datetime) -> Path:
    base = Path(root) / "_lecturehub" / FOLDER
    base.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        folder = base / f"{start:%Y%m%d_%H%M%S}{f'_{n}' if n > 1 else ''}_import"
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return folder
    raise ValueError("같은 시각으로 가져온 녹음이 너무 많습니다")


def _copy(source: Path, dest: Path, digest: str) -> None:
    with source.open("rb") as src, dest.open("xb") as out:  # exclusive create: add-only
        shutil.copyfileobj(src, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    if _sha256(dest) != digest:
        raise ValueError("복사본이 원본과 다릅니다. 다시 시도해 주세요")


SIDECAR_ENGINES = ("clova", "daglo")  # tools that export a recording and its transcript side by side
SIDECAR_EVIDENCE = "imported_sidecar"
SIDECAR_SLACK_MS = 120_000  # a transcript may run a little past the audio's measured end, not minutes past it


def _attach_sidecar(connection: sqlite3.Connection, source: Path, folder: Path, lecture_id: str, media_id: str,
                    duration_ms: int | None) -> dict | None:
    """``<녹음 이름>.txt`` next to the imported file (e.g. a Clova Note export of the same note): copy it unchanged
    into the import folder and register it as that recording's transcript. A file that is not a Clova/Daglo
    transcript, or runs past the audio, is left alone and the reason is returned. Caller commits."""
    from lecturehub.parsers import parse_file

    from . import lectures
    from .app_ingest import _evidence

    text = source.with_suffix(".txt")
    if not text.is_file():
        return None
    parsed = parse_file(text)
    if parsed.engine not in SIDECAR_ENGINES or not parsed.segments:
        return {"state": "skipped", "file": text.name, "reason": f"클로바·다글로 전사 형식이 아닙니다({parsed.engine})"}
    last = max(segment.start_ms for segment in parsed.segments)
    if duration_ms and last > duration_ms + SIDECAR_SLACK_MS:
        return {"state": "skipped", "file": text.name, "reason": "전사가 녹음보다 깁니다(다른 노트의 전사일 수 있음)"}
    digest = _sha256(text)
    found = connection.execute("SELECT lecture_id FROM Transcript WHERE sha256 = ?", (digest,)).fetchone()
    if found:
        return {"state": "skipped", "file": text.name, "reason": "같은 전사가 이미 등록돼 있습니다"}
    copied = folder / text.name
    _copy(text, copied, digest)  # the raw transcript is kept exactly as received
    transcript_id = new_ulid()
    connection.execute(
        "INSERT INTO Transcript (id,lecture_id,media_id,engine,engine_version,role,role_decided_by,chunk_plan_id,"
        "status,source_file,sha256) VALUES (?,?,?,?,NULL,'timeline','policy',NULL,'done',?,?)",
        (transcript_id, lecture_id, media_id, parsed.engine, str(copied), digest))
    for seq, segment in enumerate(parsed.segments):
        connection.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,end_ms,speaker,text,chunk_index) "
                           "VALUES (?,?,?,?,?,?,0)", (transcript_id, seq, segment.start_ms, segment.end_ms,
                                                      segment.speaker, segment.text))
    _evidence(connection, "Transcript", transcript_id, SIDECAR_EVIDENCE,
              {"source_path": str(text), "copied_to": str(copied), "sha256": digest, "engine": parsed.engine,
               "media_id": media_id}, "user")
    EventStore(connection).append("TRANSCRIPT_IMPORTED", "Transcript", transcript_id,
                                  {"lecture_id": lecture_id, "engine": parsed.engine, "by": "import_sidecar"})
    lectures.refresh_policy(connection, lecture_id)
    return {"state": "attached", "file": text.name, "transcript_id": transcript_id, "engine": parsed.engine,
            "segments": len(parsed.segments)}


def _start(connection, course_id: str | None, day: date, clock: clock_time | None, item: dict) -> tuple[datetime, str]:
    """(local start, where it came from). The user's own input, the pre-filled value they left, or an assumption."""
    from .schedule import daily_schedule

    prefilled = item.get("guess") or {}
    if clock is not None:
        kept = prefilled.get("time") == f"{clock.hour:02d}:{clock.minute:02d}" and prefilled.get("date") == day.isoformat()
        return datetime.combine(day, clock), (prefilled.get("basis") or "user") if kept else "user"
    classes = [c for c in daily_schedule(connection, day) if c.course_id == course_id] if course_id else []
    if len(classes) == 1:  # no time given: the class start stands in, and the record says so
        return datetime.combine(day, clock_time()) + timedelta(milliseconds=classes[0].start_ms), "timetable_class_start"
    return datetime.combine(day, clock_time()), "unknown"


def commit(connection: sqlite3.Connection, root: Path, item: dict, *, today: date, convert=None,
           probe_media=None) -> dict:
    """Copy (and convert when needed), then register under the course/date the user confirmed. Caller commits."""
    from lecturehub.core import extra_courses

    from .app_ingest import _evidence, _lecture_for
    from .discard import hidden_ids
    from .media import probe_media as real_probe

    root, source = Path(root), Path(item.get("path") or "")
    suffix = source.suffix.lower()
    if not source.is_file():
        raise ValueError(f"파일을 찾지 못했습니다: {source.name}")
    if suffix not in AUDIO:
        raise ValueError(f"녹음 파일이 아닙니다: {source.name}")
    if _inside(source, root / "_lecturehub"):
        raise ValueError("앱이 보관하는 파일입니다(이미 앱 안에 있음)")
    course = " ".join(str(item.get("course") or "").split())
    if not course:
        raise ValueError("과목을 골라 주세요")
    try:
        day = date.fromisoformat(str(item.get("date") or ""))
        clock = clock_time.fromisoformat(item["time"]) if item.get("time") else None
    except ValueError:
        raise ValueError("날짜(YYYY-MM-DD)와 시각(HH:MM)을 확인해 주세요") from None
    if day > today:
        raise ValueError("오늘보다 뒤 날짜로는 가져올 수 없습니다")
    device = item.get("device") or "phone"
    if device not in DEVICES:
        raise ValueError(f"알 수 없는 기기: {device}")
    extra = extra_courses.is_extra(root, course)
    row = connection.execute("SELECT id FROM Course WHERE folder_name = ?", (course,)).fetchone()
    if not extra and row is None:
        raise ValueError(f"없는 과목: {course}")
    if not extra and connection.execute("SELECT count(*) FROM Lecture WHERE course_id = ? AND date = ?",
                                        (row[0], day.isoformat())).fetchone()[0] > 1:
        raise ValueError("그 날짜에 같은 과목 회차가 여러 개라 자동으로 고르지 않습니다")
    digest = source_digest = _sha256(source)
    found = _duplicate(connection, digest)
    if found:
        raise ValueError(_duplicate_reason(found))
    start, basis = _start(connection, None if extra else row[0], day, clock, item)
    info = source.stat()
    in_place = _inside(source, root)  # already in the semester folder: registered where it is (never copied twice)
    folder = None if in_place and suffix in DIRECT else _new_folder(root, start)
    master = copied = source if in_place else folder / source.name
    try:
        if not in_place:
            _copy(source, copied, digest)
        if suffix in CONVERT:
            master = folder / (source.stem + ".mp3")
            (convert or _to_mp3)(copied, master)
            digest = _sha256(master)
            found = _duplicate(connection, digest)
            if found:
                raise ValueError(_duplicate_reason(found))
        meta = (probe_media or real_probe)(master)
        if extra:
            from .extra import ensure_course, new_lecture

            lecture_id = new_lecture(connection, ensure_course(connection, course), day.isoformat())
            others = transcripts = 0
        else:
            lecture_id = _lecture_for(connection, row[0], day.isoformat())
            gone = hidden_ids(connection)
            others = sum(1 for r in connection.execute(
                "SELECT id FROM MediaAsset WHERE lecture_id = ? AND is_master = 1", (lecture_id,)) if r[0] not in gone)
            transcripts = connection.execute("SELECT count(*) FROM Transcript WHERE lecture_id = ?",
                                             (lecture_id,)).fetchone()[0]
        media_id = new_ulid()
        connection.execute(
            "INSERT INTO MediaAsset (id, lecture_id, path, sha256, duration_ms, codec, sample_rate, channels, bytes, "
            "origin, device_type, is_master, derived_from, preset_id, recorded_start, partial, loudness_profile, "
            "decided_by, confidence_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,?,0,'{}','user','exact')",
            (media_id, lecture_id, str(master), digest, meta.duration_ms, meta.codec, meta.sample_rate, meta.channels,
             master.stat().st_size, "kakao_phone" if KAKAO_FOLDER in source.parts else "other", device,
             _epoch_ms(start)))
        detail = {"source_path": str(source), "source_name": source.name, "source_sha256": source_digest,
                  "source_bytes": info.st_size, "source_mtime_ms": int(info.st_mtime * 1000),
                  "copied_to": None if in_place else str(copied), "in_place": in_place, "converted": suffix in CONVERT,
                  "course": course, "date": day.isoformat(), "time": item.get("time") or None, "start_basis": basis,
                  "device": device, "prefilled": item.get("guess") or None, "decided_by": "user"}
        _evidence(connection, "MediaAsset", media_id, EVIDENCE, detail, "user")
        EventStore(connection).append("MEDIA_REGISTERED", "MediaAsset", media_id, {"lecture_id": lecture_id,
                                                                                  "by": "import"})
        if folder is not None:  # session.json: the transcription CLI reads the course from it
            (folder / "import.json").write_text(json.dumps({**detail, "media_id": media_id, "imported_at_ms":
                                                            time.time_ns() // 1_000_000}, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
            (folder / "session.json").write_text(json.dumps(
                {"course": course, "course_source": "user_pick", "mode": "import",
                 "started_local": start.astimezone().isoformat(), "started_at": _epoch_ms(start)},
                ensure_ascii=False), encoding="utf-8")
        sidecar = _attach_sidecar(connection, source, folder, lecture_id, media_id, meta.duration_ms)             if folder is not None else None
    except BaseException:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)  # our own folder, made just above; the source is untouched
        raise
    return {"state": "linked", "lecture_id": lecture_id, "media_id": media_id, "course": course, "master": str(master),
            "duration_ms": meta.duration_ms, "copied": not in_place, "converted": suffix in CONVERT,
            "start_basis": basis, "other_recordings": others, "transcripts": transcripts, "sidecar": sidecar}
