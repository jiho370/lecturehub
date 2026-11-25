"""Read-only source indexing into the existing Lecture Hub domain tables."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath

from lecturehub.core.db import connect, init_db
from lecturehub.core.events import EventStore
from lecturehub.core.ids import new_ulid
from lecturehub.core.paths import database_path
from lecturehub.core.textio import EncodingSuspectError, decode_text, read_text_any
from lecturehub.parsers import detect_engine, parse_file
from lecturehub.parsers.dedupe import sha256_file

from .matching import (
    LectureName,
    explicit_transcript_paths,
    find_note_paths,
    material_ranges,
    note_history_links,
    parse_lecture_name,
    read_match_rows,
    row_matches_name,
)
from .media import MediaProbeError, probe_media
from .scanner import SourceFile, scan_sources

_KST = timezone(timedelta(hours=9))
_WEEKDAYS = {day: index for index, day in enumerate("월화수목금토일")}


def _course_abbr(folder: str) -> str:
    """A new course row's short code: the user's map in settings (``course_abbreviations``), else an ASCII slug of the
    folder name, else the folder name itself (the old fallback). Existing rows are never rewritten."""
    from lecturehub.core import settings

    mapped = settings.course_abbreviations().get(folder)
    if mapped:
        return mapped
    slug = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-")
    return slug if slug and folder.isascii() else folder


@dataclass(frozen=True)
class IndexFailure:
    kind: str
    filename: str
    reason: str


@dataclass
class IndexReport:
    """Per-kind total includes duplicates; matched is independent of registration.

    For media/transcript, matched counts filenames resolved to a Lecture even
    when audio metadata prevents registration. indexed counts new rows only.
    For material, matched means a known course; for note, known lecture links.
    """
    root: str
    stats: dict[str, dict[str, int]] = field(default_factory=lambda: {
        kind: dict.fromkeys(("total", "indexed", "matched", "duplicate", "held"), 0)
        for kind in ("media", "transcript", "material", "note")
    })
    failures: list[IndexFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    courses: int = 0
    schedules: int = 0
    material_matches: int = 0
    auto_dismissed: int = 0  # HOLD-006: held items the app's first pass took out (shown with 되돌리기)

    @property
    def recording_match_rate(self) -> float | None:
        stats = self.stats["media"]
        return stats["matched"] / stats["total"] if stats["total"] else None

    @property
    def transcript_match_rate(self) -> float | None:
        stats = self.stats["transcript"]
        return stats["matched"] / stats["total"] if stats["total"] else None

    def as_dict(self) -> dict:
        return asdict(self) | {
            "recording_match_rate": self.recording_match_rate,
            "transcript_match_rate": self.transcript_match_rate,
        }


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _insert(connection, table: str, values: dict) -> None:
    columns = ",".join(f'"{name}"' for name in values)
    connection.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in values)})', tuple(values.values())
    )


def _evidence(connection, subject_type, subject_id, kind, detail, source="metadata", field_name=None) -> None:
    encoded = _json(detail)
    if connection.execute(
        "SELECT 1 FROM Evidence WHERE subject_type=? AND subject_id=? AND kind=? AND detail=?",
        (subject_type, subject_id, kind, encoded),
    ).fetchone():
        return
    _insert(connection, "Evidence", {
        "id": new_ulid(), "subject_type": subject_type, "subject_id": subject_id,
        "field": field_name, "kind": kind, "detail": encoded, "weight": None,
        "source": source, "created_at": time.time_ns() // 1_000_000,
    })


def _reference(report, source_file, line_start, record, line_end=None) -> dict:
    return {"source_file": source_file, "source_sha256": report.source_hashes[source_file],
            "line_start": line_start, "line_end": line_end or line_start, "record": record}


def _confirmed_evidence(connection, subject_type, subject_id, references) -> None:
    if references:
        _evidence(connection, subject_type, subject_id, "confirmed_source_context",
                  {"decided_by": "user", "references": references}, source="user")


def _answer_reference(answer: dict) -> dict:
    """S5: the user's answer in the app, as the source of a confirmation (like a line of the confirmed table)."""
    return {"source_file": "앱 확인함(S5)", "evidence_id": answer["evidence_id"], "item_id": answer["item_id"],
            "record": {"question": answer["question"], "answer": answer["answer"]}}


def _held(connection, report, source: SourceFile, reason: str, digest: str | None = None, references=()) -> None:
    report.stats[source.kind]["held"] += 1
    report.failures.append(IndexFailure(source.kind, source.path.name, reason))
    suggestion = _json({"reason": reason, "sha256": digest, "kind": source.kind})
    existing = connection.execute("SELECT id,state,suggestion FROM IngestItem WHERE source_path=?", (str(source.path),)).fetchone()
    if not existing:
        item_id = new_ulid()
        _insert(connection, "IngestItem", {
            "id": item_id, "source_path": str(source.path), "suggestion": suggestion, "state": "held",
        })
        EventStore(connection).append("INGEST_DETECTED", "IngestItem", item_id, {"state": "held", "reason": reason})
    else:
        item_id = existing["id"]
        if existing["state"] == "held" and existing["suggestion"] != suggestion:
            previous = json.loads(existing["suggestion"])
            connection.execute("UPDATE IngestItem SET suggestion=? WHERE id=?", (suggestion, item_id))
            EventStore(connection).append("INGEST_REASSESSED", "IngestItem", item_id,
                                          {"previous_reason": previous.get("reason"), "reason": reason})
    _confirmed_evidence(connection, "IngestItem", item_id, references)


def _registered(connection, source: SourceFile, subject_type: str, subject_id: str) -> None:
    """Resolve a prior technical hold after its evidence becomes sufficient."""
    item = connection.execute(
        "SELECT id,suggestion FROM IngestItem WHERE source_path=? AND state='held'", (str(source.path),),
    ).fetchone()
    if item:
        suggestion = json.loads(item["suggestion"])
        confirmed = connection.execute(
            "SELECT 1 FROM Evidence WHERE subject_type=? AND subject_id=? AND source='user' "
            "AND kind='confirmed_source_context'", (subject_type, subject_id),
        ).fetchone()
        suggestion["resolution"] = {"registered_type": subject_type, "registered_id": subject_id,
                                    "decided_by": "user" if confirmed else "index_evidence"}
        connection.execute("UPDATE IngestItem SET state='confirmed',suggestion=? WHERE id=?",
                           (_json(suggestion), item["id"]))
        EventStore(connection).append("INGEST_RESOLVED", "IngestItem", item["id"], suggestion["resolution"])
        if confirmed:
            _evidence(connection, "IngestItem", item["id"], "confirmed_resolution", suggestion["resolution"], source="user")


def _clock_ms(value: str) -> int:
    hour, minute = map(int, value.strip().split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("invalid schedule time")
    return (hour * 60 + minute) * 60000


def _load_schedule(root: Path, connection, report) -> dict[str, str]:
    path = root / "시간표.csv"
    if not path.is_file():  # a new user who entered no class times yet: an empty library, not a failure
        return {}
    text = read_text_any(path)
    report.source_hashes[str(path.relative_to(root))] = sha256_file(path)
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows and text.strip():  # the header alone (setup with no class times): nothing to load
        return {}
    if not rows or not {"폴더", "과목", "요일", "시작", "종료"}.issubset(rows[0]):
        raise ValueError("시간표.csv: required columns are 폴더,과목,요일,시작,종료")
    term_match = re.search(r"\d{2,4}-[12]", root.name)
    term = term_match[0] if term_match else root.name
    courses = {}
    for row in rows:
        folder = row["폴더"].strip()
        if not folder or Path(folder).name != folder or folder.startswith("_") or folder in {".", ".."}:
            raise ValueError("시간표.csv: invalid course folder")
        day = _WEEKDAYS.get(row["요일"].strip().removesuffix("요일"))
        start, end = _clock_ms(row["시작"]), _clock_ms(row["종료"])
        if day is None or end < start:
            raise ValueError("시간표.csv: invalid weekday or time range")
        if folder not in courses:
            found = connection.execute(
                "SELECT id FROM Course WHERE folder_name=? AND term=?", (folder, term),
            ).fetchall()
            if len(found) > 1:
                raise ValueError("시간표.csv: ambiguous existing Course rows")
            course_id = found[0][0] if found else new_ulid()
            if not found:
                _insert(connection, "Course", {
                    "id": course_id, "code": folder, "folder_name": folder,
                    "abbr": _course_abbr(folder), "term": term,
                })
            courses[folder] = course_id
        course_id = courses[folder]
        _evidence(connection, "Course", course_id, "timetable", {
            "name": row["과목"].strip(), "weekday": day, "start_ms": start, "end_ms": end,
        }, source="user")
        exists = connection.execute(
            "SELECT 1 FROM Schedule WHERE course_id=? AND weekday=? AND start=? AND end=?",
            (course_id, day, start, end),
        ).fetchone()
        if not exists:
            # Do not silently overwrite a user-edited conflicting schedule.
            conflict = connection.execute(
                "SELECT 1 FROM Schedule WHERE course_id=? AND weekday=? AND start=?", (course_id, day, start),
            ).fetchone()
            if conflict:
                report.failures.append(IndexFailure("schedule", "시간표.csv", "conflicting schedule requires confirmation"))
                continue
            _insert(connection, "Schedule", {"course_id": course_id, "weekday": day, "start": start, "end": end})
        report.schedules += 1
    report.courses = len(courses)
    return courses


def sync_schedule(root: Path, connection) -> dict:
    """TT-001: after the user edits 시간표.csv in the app, make this term's weekly slots equal the file.

    Courses, lectures and recordings stay; only Schedule rows of this term's courses that the file no longer lists
    are removed, then the file's rows are loaded as in a normal index run. The caller commits.
    """
    root = Path(root)
    term_match = re.search(r"\d{2,4}-[12]", root.name)
    term = term_match[0] if term_match else root.name
    wanted: dict[str, set] = {}
    for row in csv.DictReader(io.StringIO(read_text_any(root / "시간표.csv"))):
        try:
            day = _WEEKDAYS[(row.get("요일") or "").strip().removesuffix("요일")]
            wanted.setdefault((row.get("폴더") or "").strip(), set()).add((day, _clock_ms(row["시작"]), _clock_ms(row["종료"])))
        except (KeyError, ValueError, AttributeError):
            continue
    removed = 0
    for course_id, folder in connection.execute("SELECT id, folder_name FROM Course WHERE term=?", (term,)).fetchall():
        for slot in connection.execute("SELECT weekday, start, end FROM Schedule WHERE course_id=?", (course_id,)).fetchall():
            if tuple(slot) not in wanted.get(folder, set()):
                connection.execute("DELETE FROM Schedule WHERE course_id=? AND weekday=? AND start=? AND end=?",
                                   (course_id, *slot))
                removed += 1
    report = IndexReport(str(root))
    _load_schedule(root, connection, report)
    return {"removed": removed, "schedules": report.schedules, "courses": report.courses,
            "failures": [f.reason for f in report.failures]}


def _copy_lecture(connection, path: Path) -> str | None:
    """The lecture of the app recording that this 녹음본 file is the copy of (archive_copy wrote the path down)."""
    for lecture_id, detail in connection.execute(
            "SELECT m.lecture_id, e.detail FROM Evidence e JOIN MediaAsset m ON m.id = e.subject_id "
            "WHERE e.subject_type = 'MediaAsset' AND e.kind = 'source_alias' AND m.lecture_id IS NOT NULL "
            "AND e.detail LIKE '%archive_copy_D055%' ORDER BY e.created_at DESC, e.id DESC"):
        try:
            data = json.loads(detail)
        except ValueError:
            continue
        if data.get("role") == "archive_copy_D055" and data.get("path") == str(path):
            return lecture_id
    return None


def _lecture(connection, name: LectureName, course_id: str) -> str:
    candidates = connection.execute(
        "SELECT id,title FROM Lecture WHERE course_id=? AND date=?", (course_id, name.date),
    ).fetchall()
    found = [row for row in candidates if row["title"] == name.title or (
        (parsed := parse_lecture_name(row["title"] or "", name.folder)) and parsed.session_label == name.session_label
    )]
    if len(found) > 1:
        raise ValueError("ambiguous existing lecture")
    if found:
        return found[0][0]
    # Exact source labels retain identity; suffix 1 and '1교시' must never collapse.
    desired = int(name.session_label) if name.session_label.isdecimal() else 1
    used = {row[0] for row in connection.execute(
        "SELECT session_index FROM Lecture WHERE course_id=? AND date=?", (course_id, name.date),
    )}
    session_index = desired if desired not in used else max(used) + 1
    lecture_id = new_ulid()
    _insert(connection, "Lecture", {
        "id": lecture_id, "course_id": course_id, "date": name.date, "session_index": session_index,
        "title": name.title, "lecture_type": None,
    })
    _evidence(connection, "Lecture", lecture_id, "filename_keyword", {
        "title": name.title, "session_label": name.session_label,
        "session_index_policy": "stable identity ordinal, not inferred chronological order",
    }, source="heuristic")
    return lecture_id


def _move_records(root, report) -> list[dict]:
    path = root / "녹음본" / "_이동기록.csv"
    if not path.is_file():
        return []
    relative = str(path.relative_to(root))
    report.source_hashes[relative] = sha256_file(path)
    result = []
    reader = csv.DictReader(io.StringIO(read_text_any(path)))
    prior_end = 1
    for row in reader:
        if row.get("이동후"):
            result.append(row | {"_reference": _reference(report, relative, prior_end + 1, row, reader.line_num)})
        prior_end = reader.line_num
    return result


def _path_key(value: str) -> str:
    return PureWindowsPath(value).as_posix().casefold()


def _move_chain(source: SourceFile, moves: list[dict]) -> list[dict]:
    candidates = [row for row in moves if row.get("과목", "").strip() == source.folder
                  and PureWindowsPath(row["이동후"]).name == source.path.name]
    if not candidates:
        return []
    if len(candidates) != 1:
        raise ValueError("conflicting confirmed move records")
    chain = candidates
    while True:
        ancestors = [row for row in moves if _path_key(row["이동후"]) == _path_key(chain[0].get("원본", ""))]
        if not ancestors:
            break
        if len(ancestors) != 1 or ancestors[0] in chain:
            raise ValueError("ambiguous or cyclic move chain")
        chain.insert(0, ancestors[0])
    return chain


def _move_name(source: SourceFile, moves: list[dict]) -> tuple[LectureName | None, list[dict]]:
    """Resolve exact original recording labels; never expand guessed abbreviations."""
    candidates = []
    for row in moves:
        if row.get("과목", "").strip() != source.folder:
            continue
        original = re.sub(r"^(?:다글로 다운로드|카카오톡 받은 파일):\s*", "", row.get("원본", ""))
        original_stem = PureWindowsPath(original).stem
        if original_stem != source.path.stem:
            continue
        chain = [row]
        while True:
            successors = [item for item in moves if _path_key(item.get("원본", "")) == _path_key(chain[-1]["이동후"])]
            if not successors:
                break
            if len(successors) != 1 or successors[0] in chain:
                chain = []
                break
            chain.append(successors[0])
        if not chain:
            continue
        name = parse_lecture_name(PureWindowsPath(chain[-1]["이동후"]).name, source.folder)
        if name:
            candidates.append((name, [item["_reference"] for item in chain]))
    identities = {(name.date, name.session_label) for name, _ in candidates}
    if len(identities) == 1:
        return candidates[0][0], [ref for _, references in candidates for ref in references]
    return None, []


def _point_time(value: str) -> int | None:
    """Preserve explicit estimated points; ranges/decades/before-times are not points."""
    match = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)(?:\s*\(추정\))?\s*", value)
    if not match:
        return None
    return int(datetime.fromisoformat(match[1]).replace(tzinfo=_KST).timestamp() * 1000)


def _header_date(path: Path) -> str | None:
    data = path.read_bytes()
    if detect_engine(data) != "clova":
        return None
    dates = set()
    for line in decode_text(data).splitlines()[:4]:
        if not re.search(r"\d{1,2}:\d{2}", line) or not re.search(r"\d+\s*분", line):
            continue
        found = re.search(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", line)
        if found:
            dates.add(date(*map(int, found.groups())).isoformat())
    return next(iter(dates)) if len(dates) == 1 else None


def _filename_date(filename: str, year: int) -> str | None:
    full = re.match(r"^(\d{2}|\d{4})[.-](\d{1,2})[.-](\d{1,2})(?!\d)", filename)
    if full:
        y, month, day = map(int, full.groups())
        return date(y + 2000 if y < 100 else y, month, day).isoformat()
    short = re.match(r"^(\d{1,2})[.](\d{1,2})(?!\d)", filename)
    return date(year, int(short[1]), int(short[2])).isoformat() if short else None


def _confirmed_dates(root, courses, rows, text, report, year):
    """Collect calendar facts explicitly stated in the user-confirmed document."""
    dates: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if year is None:
        return dates
    relative = "강의자료_매칭.md"
    for row in rows:
        for folder in courses:
            name = parse_lecture_name(row.recording, folder)
            found = [name.date] if name else []
            if row.recording.startswith(folder + " "):
                found.extend(date(year, int(month), int(day)).isoformat()
                             for month, day in re.findall(r"(\d{1,2})/(\d{1,2})", row.recording))
            if row.recording.endswith(" " + folder) and " / " in row.recording:
                full = re.match(r"(\d{2,4})\.(\d+)\.(\d+)", row.recording)
                if full:
                    y = int(full[1]) + (2000 if len(full[1]) == 2 else 0)
                    found.append(date(y, int(full[2]), int(full[3])).isoformat())
                    found.extend(date(y, int(month), int(day)).isoformat()
                                 for month, day in re.findall(r"/\s*(\d+)\.(\d+)", row.recording))
            for day in dict.fromkeys(found):
                dates[(folder, day)].append(_reference(report, relative, row.line_number,
                                                       {"recording": row.recording}))
    for number, line in enumerate(text.splitlines(), 1):
        if "이미 있던 노트" not in line and "기존 노트" not in line:
            continue
        for folder in courses:
            expression = re.escape(folder) + r"\s*(?:(?:\d+(?:[·,]\d+)*)강\()?\s*(\d{1,2})/(\d{1,2})"
            for match in re.finditer(expression, line):
                day = date(year, int(match[1]), int(match[2])).isoformat()
                dates[(folder, day)].append(_reference(report, relative, number, {"existing_note": match[0]}))
    return dates


def _calendar_lecture(connection, folder, day, course_id):
    """Return the sole lecture of a confirmed day; never choose among sessions."""
    found = connection.execute("SELECT id,title FROM Lecture WHERE course_id=? AND date=?", (course_id, day)).fetchall()
    if len(found) > 1:
        return None
    actual = date.fromisoformat(day)
    title = f"{actual.year % 100}.{actual.month}.{actual.day} {folder}"
    name = parse_lecture_name(title, folder)
    if found:
        if found[0]["title"]:
            name = parse_lecture_name(found[0]["title"], folder) or name
        return name, found[0]["id"]
    return name, _lecture(connection, name, course_id)


def _note_metadata_dates(path: Path) -> list[tuple[str, int]]:
    """Read explicit HTML metadata dates, without interpreting note prose."""
    found = []

    class MetadataDates(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def collect(self, value):
            for match in re.finditer(r"(?<!\d)(20\d{2})[./년-]\s*(\d{1,2})[./월-]\s*(\d{1,2})(?!\d)", value):
                try:
                    day = date(*(int(part) for part in match.groups())).isoformat()
                except ValueError:
                    continue
                found.append((day, self.getpos()[0]))

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "meta":
                if attributes.get("name", attributes.get("property", "")).lower() in {"date", "lecture-date", "lecture_date"}:
                    self.collect(attributes.get("content", ""))
                return
            if tag == "time":
                self.collect(attributes.get("datetime", ""))
            if tag in {"img", "br", "hr", "input", "link", "source", "wbr"}:
                return
            active = bool(self.stack and self.stack[-1][1]) or "meta" in attributes.get("class", "").split()
            self.stack.append((tag, active))

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

        def handle_data(self, data):
            if self.stack and self.stack[-1][1]:
                self.collect(data)

    MetadataDates().feed(read_text_any(path))
    return found


def _confirmed_absences(connection, report, courses, sources, rows, text, year, lecture_ids) -> None:
    if year is None:
        return
    declarations: list[tuple[str, str, list[dict]]] = []
    relative = "강의자료_매칭.md"
    for number, line in enumerate(text.splitlines(), 1):
        line = re.sub(r"~~.*?~~", "", line)
        for folder in courses:
            direct = re.search(r"(\d{1,2})/(\d{1,2})(?:\([^)]*\))?\s*" + re.escape(folder)
                               + r"\s+(?:수업\s+)?녹음\s*없음", line)
            if direct:
                day = date(year, int(direct[1]), int(direct[2])).isoformat()
                declarations.append((folder, day, [_reference(report, relative, number, {"claim": direct[0]})]))
        absent_notes = re.search(r"음성이 없는 노트\s*\(([^)]+)\)", line)
        if not absent_notes:
            continue
        for entry in absent_notes[1].split(","):
            entry = entry.strip()
            for folder in courses:
                if not (entry == folder or entry.startswith(folder + " ")):
                    continue
                claim = _reference(report, relative, number, {"absent_note": entry})
                explicit_date = re.search(r"(\d{1,2})/(\d{1,2})", entry)
                ordinal = re.search(r"(?<!\d)(\d+)강", entry)
                if explicit_date:
                    declarations.append((folder, date(year, int(explicit_date[1]), int(explicit_date[2])).isoformat(), [claim]))
                elif ordinal:
                    notes = [source.path for source in sources if source.kind == "note" and source.folder == folder]
                    for path, day, location in note_history_links(text, folder, notes, year):
                        if re.search(rf"(?<!\d){ordinal[1]}강(?!\d)", path.stem):
                            metadata_dates = _note_metadata_dates(path)
                            if {actual for actual, _ in metadata_dates} != {day}:
                                report.warnings.append(f"note absence needs agreeing HTML date metadata: {path.name}")
                                continue
                            metadata_reference = _reference(report, str(path.relative_to(Path(report.root))),
                                                            metadata_dates[0][1], {"lecture_date_metadata": day})
                            declarations.append((folder, day, [claim, _reference(report, relative, location,
                                                                               {"note": path.name, "date": day}),
                                                                metadata_reference]))
                elif entry == folder:
                    # Course-wide shorthand is anchored only to the explicitly listed course dates.
                    for row in rows:
                        if row.recording.startswith(folder + " "):
                            for month, day in re.findall(r"(\d{1,2})/(\d{1,2})", row.recording):
                                declarations.append((folder, date(year, int(month), int(day)).isoformat(),
                                                     [claim, _reference(report, relative, row.line_number,
                                                                        {"recording": row.recording})]))
    for folder, day, references in declarations:
        identity = _calendar_lecture(connection, folder, day, courses[folder])
        if identity is None:
            continue
        _, lecture_id = identity
        observed_audio = any(source.kind == "media" and source.folder == folder
                             and (lecture_ids.get(source.path) == lecture_id or _filename_date(source.path.name, year) == day)
                             for source in sources)
        if observed_audio:
            report.warnings.append("강의자료_매칭.md: absence declaration conflicts with an observed audio file")
            continue
        if connection.execute("SELECT 1 FROM MediaAsset WHERE lecture_id=? AND is_master=1", (lecture_id,)).fetchone():
            report.warnings.append("강의자료_매칭.md: absence declaration conflicts with a registered master")
            continue
        _evidence(connection, "Lecture", lecture_id, "recording_absence_confirmed",
                  {"decided_by": "user", "references": references}, source="user")


def _media_context(source, name, metadata, rows, moves):
    device = origin = recorded_start = None
    evidence = {}
    chain = _move_chain(source, moves)
    if chain:
        origin_label = chain[0].get("원본", "")
        if "소리 녹음" in origin_label:
            origin = "win_recorder"
        elif origin_label.startswith("다글로 다운로드:"):
            origin = "daglo"
        elif origin_label.startswith("카카오톡 받은 파일:"):
            origin = "kakao_phone"
        if "휴대폰" in origin_label:
            device = "phone"
        elif "노트북" in origin_label:
            device = "laptop"
        recorded_start = _point_time(chain[-1].get("녹음시작", ""))
        evidence["move_log"] = {
            "chain": [row["_reference"] for row in chain],
            "start_text": chain[-1].get("녹음시작"), "end_text": chain[-1].get("녹음종료"),
            "estimated_end_ms": _point_time(chain[-1].get("녹음종료", "")),
        }
    matches = [row for row in rows if row_matches_name(row, name)]
    partial = 0
    if len(matches) == 1:
        row = matches[0]
        description = row.clock + " " + row.evidence
        if "휴대폰" in description:
            device = "phone"
        elif "노트북" in description:
            device = "laptop"
        if origin is None:
            if "카톡" in description or "카카오톡" in description:
                origin = "kakao_phone"
            elif "다글로" in description:
                origin = "daglo"
            elif "윈도우 녹음기" in description:
                origin = "win_recorder"
        clocks = re.findall(r"\d{1,2}:\d{2}", row.clock)
        if recorded_start is None and clocks and " 중 " not in row.clock and "대(추정)" not in row.clock:
            index = 0
            if "·" in row.recording and name.session_label.isdecimal():
                index = (int(name.session_label) - 1) * 2
            if index < len(clocks):
                recorded_start = int(datetime.fromisoformat(name.date).replace(tzinfo=_KST).timestamp() * 1000)
                recorded_start += _clock_ms(clocks[index])
        partial = int("도중" in description)
        evidence["match_table"] = {"recording": row.recording, "clock": row.clock, "evidence": row.evidence}
    if recorded_start is None and metadata.recorded_start is not None:
        recorded_start = metadata.recorded_start
        evidence["creation_time_tag"] = recorded_start
    return device, origin, recorded_start, partial, evidence


def _source_digest(root, source, report):
    digest = sha256_file(source.path)
    report.source_hashes[str(source.path.relative_to(root))] = digest
    return digest


def _index_media(root, connection, report, source, name, lecture_id, rows, moves, ffprobe, references=(), answer=None):
    digest = _source_digest(root, source, report)
    references = list(references) + [item["_reference"] for item in _move_chain(source, moves)]
    existing = connection.execute("SELECT id,lecture_id FROM MediaAsset WHERE sha256=?", (digest,)).fetchone()
    if existing:
        if existing["lecture_id"] != lecture_id:
            _held(connection, report, source, "same audio hash has a different lecture identity", digest)
            return
        report.stats["media"]["duplicate"] += 1
        _evidence(connection, "MediaAsset", existing["id"], "source_alias", {"path": str(source.path)})
        _confirmed_evidence(connection, "MediaAsset", existing["id"], references)
        _registered(connection, source, "MediaAsset", existing["id"])
        return
    if connection.execute("SELECT 1 FROM MediaAsset WHERE path=?", (str(source.path),)).fetchone():
        _held(connection, report, source, "registered source bytes changed; original record preserved", digest)
        return
    metadata = probe_media(source.path, ffprobe)
    device, origin, recorded, partial, details = _media_context(source, name, metadata, rows, moves)
    if device is None and answer and answer["question"] == "device":  # S5: the user said which device
        device = answer["answer"]["device_type"]
        details["user_answer"] = {"device_type": device, "evidence_id": answer["evidence_id"]}
        references = list(references) + [_answer_reference(answer)]
        if recorded is None and answer["answer"].get("start"):  # the user also knew when it started
            recorded = int(datetime.fromisoformat(name.date).replace(tzinfo=_KST).timestamp() * 1000)
            recorded += _clock_ms(answer["answer"]["start"])
            details["user_answer"]["start"] = answer["answer"]["start"]
    if device is None or recorded is None:
        _held(connection, report, source, "recording device or start time is unconfirmed", digest, references)
        return
    answered_date = answer is not None and answer["question"] in ("date", "identity")
    if not answered_date and datetime.fromtimestamp(recorded / 1000, _KST).date().isoformat() != name.date:
        report.stats["media"]["matched"] -= 1
        _held(connection, report, source, "filename date conflicts with recording start evidence", digest)
        return
    media_id = new_ulid()
    _insert(connection, "MediaAsset", {
        "id": media_id, "lecture_id": lecture_id, "path": str(source.path), "sha256": digest,
        "duration_ms": metadata.duration_ms, "codec": metadata.codec, "sample_rate": metadata.sample_rate,
        "channels": metadata.channels, "bytes": source.path.stat().st_size, "origin": origin or "other",
        "device_type": device, "is_master": 1, "derived_from": None, "preset_id": None,
        "recorded_start": recorded, "partial": partial, "loudness_profile": "{}",
        "decided_by": "user" if references else "metadata", "confidence_class": "medium",
    })
    _evidence(connection, "MediaAsset", media_id, "source_metadata", details)
    _evidence(connection, "MediaAsset", media_id, "source_alias", {"path": str(source.path)})
    _confirmed_evidence(connection, "MediaAsset", media_id, references)
    EventStore(connection).append("MEDIA_REGISTERED", "MediaAsset", media_id, {"lecture_id": lecture_id})
    _registered(connection, source, "MediaAsset", media_id)
    report.stats["media"]["indexed"] += 1


def _index_transcript(root, connection, report, source, lecture_id, references=(), audio_files=(), answer=None):
    digest = _source_digest(root, source, report)
    existing = connection.execute("SELECT id,lecture_id FROM Transcript WHERE sha256=?", (digest,)).fetchone()
    if existing:
        if existing["lecture_id"] != lecture_id:
            _held(connection, report, source, "same transcript hash has a different lecture identity", digest)
            return
        report.stats["transcript"]["duplicate"] += 1
        _evidence(connection, "Transcript", existing["id"], "source_alias", {"path": str(source.path)})
        _confirmed_evidence(connection, "Transcript", existing["id"], references)
        report.warnings.append(f"duplicate transcript bytes: {source.path.name}")
        _registered(connection, source, "Transcript", existing["id"])
        return
    if connection.execute("SELECT 1 FROM Transcript WHERE source_file=?", (str(source.path),)).fetchone():
        _held(connection, report, source, "registered transcript bytes changed; original record preserved", digest)
        return
    parsed = parse_file(source.path)
    if parsed.engine == "unknown" and answer and answer["question"] == "engine":  # S5: the user named the tool
        from lecturehub.parsers import _PARSERS, ParsedTranscript, TranscriptParseError

        engine = answer["answer"]["engine"]
        try:
            segments = tuple(_PARSERS[engine](read_text_any(source.path)))
        except TranscriptParseError:
            _held(connection, report, source, f"transcript does not read as the answered format ({engine})", digest,
                  references)
            return
        parsed = ParsedTranscript(engine=engine, segments=segments, source_path=source.path)
        references = list(references) + [_answer_reference(answer)]
    if parsed.engine == "unknown":
        _held(connection, report, source, "transcript engine is unknown", digest, references)
        return
    if parsed.engine == "clova" and not (answer and answer["question"] in ("date", "identity")):
        metadata_date = _header_date(source.path)
        expected = connection.execute("SELECT date FROM Lecture WHERE id=?", (lecture_id,)).fetchone()[0]
        if metadata_date and metadata_date != expected:
            report.stats["transcript"]["matched"] -= 1
            _held(connection, report, source, "filename lecture date conflicts with Clova export header", digest, references)
            return
    media = connection.execute("SELECT id FROM MediaAsset WHERE lecture_id=? AND is_master=1", (lecture_id,)).fetchall()
    if len(media) != 1:
        if not references:
            inherited = []
            for evidence in connection.execute(
                "SELECT id,detail FROM Evidence WHERE subject_type='Lecture' AND subject_id=? "
                "AND kind='confirmed_source_context' AND source='user'", (lecture_id,),
            ):
                detail = json.loads(evidence["detail"])
                if detail.get("decided_by") == "user" and detail.get("references"):
                    inherited.extend(dict(reference, inherited_lecture_id=lecture_id,
                                          inherited_evidence_id=evidence["id"])
                                     for reference in detail["references"])
            references = inherited
        if not references:
            _held(connection, report, source,
                  "no_media import requires user-confirmed lecture linkage; filename alone is insufficient", digest)
            return
        media_column = next(row for row in connection.execute("PRAGMA table_info(Transcript)") if row["name"] == "media_id")
        if media_column["notnull"]:
            _held(connection, report, source, "original audio is missing or ambiguous; no_media requires migration 0003", digest)
            return
    media_id = media[0][0] if len(media) == 1 else None
    transcript_id = new_ulid()
    role = "content" if parsed.engine == "mai" else "timeline" if parsed.engine == "gemini_transcribe" else "reference"
    _insert(connection, "Transcript", {
        "id": transcript_id, "lecture_id": lecture_id, "media_id": media_id, "engine": parsed.engine,
        "engine_version": None, "role": role, "role_decided_by": "policy", "chunk_plan_id": None,
        "status": "done", "source_file": str(source.path), "sha256": digest,
    })
    for sequence, segment in enumerate(parsed.segments):
        _insert(connection, "TranscriptSegment", {
            "transcript_id": transcript_id, "seq": sequence, "start_ms": segment.start_ms,
            "end_ms": segment.end_ms, "speaker": segment.speaker, "text": segment.text, "chunk_index": 0,
        })
    _evidence(connection, "Transcript", transcript_id, "import_policy", {
        "role": role, "label": "default policy, not a measured recommendation",
        "chunk_index": "0 denotes the imported file; original chunk boundaries unavailable",
    }, source="heuristic")
    _evidence(connection, "Transcript", transcript_id, "source_alias", {"path": str(source.path)})
    _confirmed_evidence(connection, "Transcript", transcript_id, references)
    if media_id is None:
        reason = "ambiguous_master" if len(media) > 1 else "master_metadata_unconfirmed" if audio_files else "no_registered_master"
        _evidence(connection, "Transcript", transcript_id, "media_link_unresolved", {
            "no_media": True, "reason": reason, "candidate_count": len(media),
            "observed_audio_filenames": list(audio_files), "source_sha256": digest,
            "meaning": "no confirmed media link; this does not assert that the recording never existed",
        })
        report.warnings.append(f"no_media ({reason}): {source.path.name}")
    EventStore(connection).append("TRANSCRIPT_IMPORTED", "Transcript", transcript_id, {"lecture_id": lecture_id})
    _registered(connection, source, "Transcript", transcript_id)
    report.stats["transcript"]["indexed"] += 1


def _follow_moved(connection, table, existing, source) -> None:
    """The same bytes at a new path while the remembered path is gone: the file was moved; follow it."""
    old = existing["path"]
    if old and old != str(source.path) and not Path(old).is_file():
        connection.execute(f"UPDATE {table} SET path=? WHERE id=?", (str(source.path), existing["id"]))
        _evidence(connection, table, existing["id"], "relocated", {"from": old, "to": str(source.path)},
                  field_name="path")


def _index_material(root, connection, report, source, course_id):
    digest = _source_digest(root, source, report)
    existing = connection.execute("SELECT id,course_id,path FROM Material WHERE sha256=?", (digest,)).fetchone()
    if existing:
        if existing["course_id"] != course_id:
            _held(connection, report, source, "same material hash belongs to another course", digest)
            return None
        _follow_moved(connection, "Material", existing, source)
        report.stats["material"]["duplicate"] += 1
        _evidence(connection, "Material", existing["id"], "source_alias", {"path": str(source.path)})
        _registered(connection, source, "Material", existing["id"])
        return existing["id"]
    if connection.execute("SELECT 1 FROM Material WHERE path=?", (str(source.path),)).fetchone():
        _held(connection, report, source, "registered material bytes changed; source review required", digest)
        return None
    material_id = new_ulid()
    _insert(connection, "Material", {
        "id": material_id, "course_id": course_id, "path": str(source.path), "sha256": digest,
        "kind": source.path.suffix.lower().lstrip("."), "title": source.path.stem,
        "etl_file_id": None, "etl_uploaded_at": None, "etl_module": None, "page_count": 0,
    })
    _evidence(connection, "Material", material_id, "import_policy", {"page_count": "0 = not yet extracted"})
    _evidence(connection, "Material", material_id, "source_alias", {"path": str(source.path)})
    EventStore(connection).append("MATERIAL_REGISTERED", "Material", material_id, {"course_id": course_id})
    _registered(connection, source, "Material", material_id)
    report.stats["material"]["indexed"] += 1
    return material_id


def _note_dates(path: Path, year: int) -> list[str]:
    full = re.search(r"(?<!\d)(\d{2}|\d{4})[.-](\d{1,2})[.-](\d{1,2})(?!\d)", path.stem)
    if full:
        numbers = [tuple(map(int, full.groups()))]
    else:
        short = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)", path.stem)
        compact = re.search(r"_(\d{2})(\d{2})(?:-(\d{2})(\d{2}))?(?:_|$)", path.stem)
        numbers = [(year, int(short[1]), int(short[2]))] if short else []
        if compact:
            numbers = [(year, int(compact[1]), int(compact[2]))]
            if compact[3]:
                numbers.append((year, int(compact[3]), int(compact[4])))
    result = []
    for y, month, day in numbers:
        try:
            result.append(date(y + 2000 if y < 100 else y, month, day).isoformat())
        except ValueError:
            pass
    return result


def _index_note(root, connection, report, source, lecture_ids, references=()):
    digest = _source_digest(root, source, report)
    if not lecture_ids:
        _held(connection, report, source, "note lecture is unconfirmed", digest)
        return
    existing = connection.execute(
        "SELECT id,lecture_ids,path FROM StudyArtifact WHERE json_extract(inputs,'$.source_sha256')=?", (digest,),
    ).fetchone()
    if existing:
        if set(json.loads(existing["lecture_ids"])) != set(lecture_ids):
            _held(connection, report, source, "same note hash has different lecture links", digest)
            return
        _follow_moved(connection, "StudyArtifact", existing, source)
        report.stats["note"]["duplicate"] += 1
        _confirmed_evidence(connection, "StudyArtifact", existing["id"], references)
        _registered(connection, source, "StudyArtifact", existing["id"])
        return
    if connection.execute("SELECT 1 FROM StudyArtifact WHERE path=?", (str(source.path),)).fetchone():
        _held(connection, report, source, "registered note bytes changed; version review required", digest)
        return
    artifact_id = new_ulid()
    _insert(connection, "StudyArtifact", {
        "id": artifact_id, "lecture_ids": _json(sorted(lecture_ids)), "path": str(source.path),
        "kind": "note", "version": 1, "parent_version": None, "build_script_path": None,
        "mode": "new", "inputs": _json({"source_sha256": digest}), "created_by": "import",
        "qa_status": "unverified",
    })
    EventStore(connection).append("STUDY_ARTIFACT_INDEXED", "StudyArtifact", artifact_id, {"lecture_ids": sorted(lecture_ids)})
    _confirmed_evidence(connection, "StudyArtifact", artifact_id, references)
    _registered(connection, source, "StudyArtifact", artifact_id)
    report.stats["note"]["indexed"] += 1


def _run_index(root, connection, ffprobe, only_paths=None):
    report = IndexReport(str(root))
    courses = _load_schedule(root, connection, report)
    matching_path = root / "강의자료_매칭.md"
    rows = []
    match_text = ""
    if matching_path.is_file():
        match_text = read_text_any(matching_path)
        rows = read_match_rows(match_text)
        if re.search(r"(?:앞으로|매주)[^\n]*(?:휴강|대체|시간변경)", match_text):
            report.warnings.append("강의자료_매칭.md: free-form recurring notices are not auto-applied; registered per-date ScheduleException rows are used")
        report.source_hashes[str(matching_path.relative_to(root))] = sha256_file(matching_path)
    explicit = explicit_transcript_paths(match_text, list(courses))
    sources = scan_sources(root, list(courses), explicit_transcripts=[path for path, _ in explicit])
    source_refs: dict[Path, list[dict]] = defaultdict(list)
    for relative, number in explicit:
        source_refs[root / relative].append(_reference(report, "강의자료_매칭.md", number,
                                                       {"explicit_transcript": str(relative)}))
    year_match = re.search(r"(\d{2,4})-[12]", root.name)
    year = int(year_match[1]) if year_match else None
    if year is not None and year < 100:
        year += 2000
    confirmed_dates = _confirmed_dates(root, courses, rows, match_text, report, year)
    moves = _move_records(root, report)
    names: dict[Path, LectureName] = {}
    lecture_ids: dict[Path, str] = {}
    named_lectures: dict[str, tuple[LectureName, str]] = {}
    unresolved_reasons: dict[Path, str] = {}
    for source in sources:
        report.stats[source.kind]["total"] += 1
        if source.kind not in {"media", "transcript"}:
            continue
        name = parse_lecture_name(source.path.name, source.folder)
        if name is None and source.kind == "transcript":
            name, references = _move_name(source, moves)
            source_refs[source.path].extend(references)
        if name and source.folder in courses:
            names[source.path] = name
            copied_from = _copy_lecture(connection, source.path) if source.kind == "media" else None
            if copied_from:  # the D-055 copy of an app recording: its lecture exists, the name makes none
                lecture_ids[source.path] = copied_from
                report.stats[source.kind]["matched"] += 1
                continue
            lecture_id = _lecture(connection, name, courses[source.folder])
            lecture_ids[source.path] = lecture_id
            named_lectures[lecture_id] = (name, lecture_id)
            report.stats[source.kind]["matched"] += 1
    for source in sources:
        if source.kind != "transcript" or source.path in names or source.folder not in courses or year is None:
            continue
        try:
            exported = _header_date(source.path)
            named_date = _filename_date(source.path.name, year)
            if exported and named_date and exported != named_date:
                unresolved_reasons[source.path] = "filename date conflicts with Clova export header"
                continue
            references = confirmed_dates.get((source.folder, exported), [])
            if not exported or not references:
                continue
            identity = _calendar_lecture(connection, source.folder, exported, courses[source.folder])
            if identity is None:
                unresolved_reasons[source.path] = "confirmed day contains multiple lectures; session is unresolved"
                continue
            name, lecture_id = identity
            names[source.path], lecture_ids[source.path] = name, lecture_id
            named_lectures[lecture_id] = (name, lecture_id)
            source_refs[source.path].extend(references)
            _evidence(connection, "Lecture", lecture_id, "export_calendar_agreement", {
                "folder": source.folder, "export_date": exported, "filename": source.path.name,
                "source_file": str(source.path.relative_to(root)), "source_sha256": sha256_file(source.path),
                "policy": "unique confirmed course day; filename abbreviation was not interpreted",
            })
            report.stats["transcript"]["matched"] += 1
        except (OSError, ValueError):
            unresolved_reasons[source.path] = "export date could not be verified"
    # S5: identity/date answers given in the app's hold inbox (latest, not revoked) name the file.
    from .holds import latest_answers

    answers = latest_answers(connection)
    for source in sources:
        answer = answers.get(str(source.path))
        if source.kind not in {"media", "transcript"} or not answer or answer["question"] not in ("identity", "date"):
            continue
        folder, day, session = answer["answer"]["folder"], answer["answer"]["date"], answer["answer"].get("session", "")
        if folder not in courses:
            continue
        stamp = date.fromisoformat(day)
        title = f"{stamp.year % 100}.{stamp.month}.{stamp.day} {folder}" + (f" {session}" if session else "")
        name = LectureName(folder, day, title, session)
        lecture_id = _lecture(connection, name, courses[folder])
        if source.path not in lecture_ids:
            report.stats[source.kind]["matched"] += 1
        names[source.path], lecture_ids[source.path] = name, lecture_id
        named_lectures[lecture_id] = (name, lecture_id)
        unresolved_reasons.pop(source.path, None)
        source_refs[source.path].append(_answer_reference(answer))
    for source in sources:
        name = names.get(source.path)
        if name:
            for row in rows:
                if row_matches_name(row, name):
                    source_refs[source.path].append(_reference(report, "강의자료_매칭.md", row.line_number,
                                                               {"recording": row.recording, "clock": row.clock,
                                                                "evidence": row.evidence}))
            _confirmed_evidence(connection, "Lecture", lecture_ids[source.path], source_refs[source.path])

    materials: dict[Path, str] = {}
    for kind in ("media", "transcript", "material"):
        for source in (item for item in sources if item.kind == kind):
            if only_paths is not None and source.path not in only_paths:
                continue  # S5: register just the answered files
            try:
                if kind in {"media", "transcript"} and source.path not in lecture_ids:
                    _held(connection, report, source, unresolved_reasons.get(source.path, "filename course/date/session is unconfirmed"),
                          _source_digest(root, source, report))
                elif kind == "media":
                    _index_media(root, connection, report, source, names[source.path], lecture_ids[source.path],
                                 rows, moves, ffprobe, source_refs[source.path], answers.get(str(source.path)))
                elif kind == "transcript":
                    audio_files = [item.path.name for item in sources if item.kind == "media"
                                   and lecture_ids.get(item.path) == lecture_ids[source.path]]
                    _index_transcript(root, connection, report, source, lecture_ids[source.path],
                                      source_refs[source.path], audio_files, answers.get(str(source.path)))
                else:
                    report.stats[kind]["matched"] += 1
                    material_id = _index_material(root, connection, report, source, courses[source.folder])
                    if material_id:
                        materials[source.path] = material_id
            except (OSError, ValueError, MediaProbeError) as exc:
                # File contents and exception strings can contain private text; report a class/code only.
                if isinstance(exc, MediaProbeError):
                    reason = str(exc)
                elif isinstance(exc, EncodingSuspectError):  # fixed text only (no file contents), AGENTS.md §3
                    legacy = "cp949 인코딩으로 의심됨, " if "cp949" in str(exc) else ""
                    reason = (f"source read/parse failed (EncodingSuspectError): UTF-8로 읽을 수 없음 — {legacy}"
                              "원본은 그대로 두고 UTF-8 사본으로 변환한 뒤 다시 색인하세요")
                else:
                    reason = f"source read/parse failed ({type(exc).__name__})"
                _held(connection, report, source, reason)

    if only_paths is not None:
        return report  # notes, material ranges and absences need the whole semester; a full index does them
    note_links: dict[Path, set[str]] = defaultdict(set)
    note_refs: dict[Path, list[dict]] = defaultdict(list)
    previous_notes: dict[str, list[Path]] = {}
    for row in rows:
        candidates = [item for item in named_lectures.values() if row_matches_name(row, item[0])]
        if not candidates:
            report.failures.append(IndexFailure("material_match", "강의자료_매칭.md",
                                                "recording label has no confirmed lecture"))
        for name, lecture_id in candidates:
            course_notes = [s.path for s in sources if s.kind == "note" and s.folder == name.folder]
            linked = find_note_paths(row.note, course_notes)
            if not linked and "위와 같은 노트" in row.note:
                linked = previous_notes.get(name.folder, [])
            if linked:
                previous_notes[name.folder] = linked
            for path in linked:
                note_links[path].add(lecture_id)
                note_refs[path].append(_reference(report, "강의자료_매칭.md", row.line_number,
                                                  {"recording": row.recording, "note": row.note}))
            course_materials = [p for p in materials if p.parent == root / name.folder]
            ranges = material_ranges(row.material, course_materials)
            for path, first, last in ranges:
                material_id = materials[path]
                if not connection.execute(
                    "SELECT 1 FROM MaterialMatch WHERE lecture_id=? AND material_id=? AND page_from=? AND page_to=?",
                    (lecture_id, material_id, first, last),
                ).fetchone():
                    _insert(connection, "MaterialMatch", {
                        "lecture_id": lecture_id, "material_id": material_id, "page_from": first, "page_to": last,
                        "confidence_class": {"◎": "high", "○": "medium", "△": "low"}.get(row.confidence, "none"),
                        "decided_by": "user",
                    })
                    _evidence(connection, "Lecture", lecture_id, "material_match_seed", {
                        "source": "강의자료_매칭.md", "material_id": material_id,
                        "page_from": first, "page_to": last, "evidence": row.evidence,
                    }, source="user")
                report.material_matches += 1
            if not ranges and "자료 없음" not in row.material:
                report.failures.append(IndexFailure("material_match", "강의자료_매칭.md",
                                                    "material file or explicit page range needs confirmation"))

    if year is not None:
        for folder in courses:
            notes = [source.path for source in sources if source.kind == "note" and source.folder == folder]
            for path, day, number in note_history_links(match_text, folder, notes, year):
                identity = _calendar_lecture(connection, folder, day, courses[folder])
                if identity:
                    _, lecture_id = identity
                    note_links[path].add(lecture_id)
                    reference = _reference(report, "강의자료_매칭.md", number,
                                           {"existing_note": path.name, "lecture_date": day})
                    note_refs[path].append(reference)
                    _confirmed_evidence(connection, "Lecture", lecture_id, [reference])
    for source in (item for item in sources if item.kind == "note"):
        links = note_links[source.path]
        if not links and year is not None:
            for lecture_date in _note_dates(source.path, year):
                found = connection.execute(
                    "SELECT id,title FROM Lecture WHERE course_id=? AND date=?", (courses[source.folder], lecture_date),
                ).fetchall()
                period = re.search(r"(\d+교시)", source.path.name)
                if period:
                    found = [row for row in found if period[1] in (row["title"] or "")]
                if not found and not period:
                    day = date.fromisoformat(lecture_date)
                    name = parse_lecture_name(f"{day.year % 100}.{day.month}.{day.day} {source.folder}", source.folder)
                    found = [{"id": _lecture(connection, name, courses[source.folder])}]
                links.update(row["id"] for row in found)
        if links:
            report.stats["note"]["matched"] += 1
        try:
            _index_note(root, connection, report, source, links, note_refs[source.path])
        except (OSError, ValueError) as exc:
            _held(connection, report, source, f"source read failed ({type(exc).__name__})")
    _confirmed_absences(connection, report, courses, sources, rows, match_text, year, lecture_ids)
    from .holds import triage  # HOLD-006: settle what a rule can, leave the rest for the user

    report.auto_dismissed = len(triage(connection, root)["dismissed"])
    return report


def index_root(root: str | Path, connection: sqlite3.Connection | None = None, *, ffprobe: str | None = None,
               only_paths: set[Path] | None = None) -> IndexReport:
    """Index an existing semester; only the application database is written.

    An explicit connection must be idle and is committed on success. Unknown
    metadata and ambiguous source links remain held IngestItems. ``indexed``
    counts new registrations, while ``matched`` counts resolved source labels.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("학기 폴더를 찾을 수 없습니다. 설정에서 폴더 위치를 확인하세요.")
    app_path = root / "_lecturehub"
    if app_path.is_symlink() or (app_path.exists() and app_path.resolve() != app_path):
        raise ValueError("_lecturehub 폴더가 다른 곳을 가리키는 링크입니다. 실제 폴더여야 합니다.")
    if not app_path.resolve().is_relative_to(root):
        raise ValueError("_lecturehub 폴더가 학기 폴더 밖을 가리킵니다.")
    destination = database_path(root)
    if destination.is_symlink() or destination.resolve() != destination:
        raise ValueError("데이터베이스 파일이 다른 곳을 가리키는 링크입니다. 실제 파일이어야 합니다.")
    owned = connection is None
    if owned:
        init_db(destination)
        connection = connect(destination)
    assert connection is not None
    if connection.in_transaction:
        if owned:
            connection.close()
        raise ValueError("index_root requires an idle database connection")
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            return _run_index(root, connection, ffprobe, only_paths)
    finally:
        if owned:
            connection.close()
