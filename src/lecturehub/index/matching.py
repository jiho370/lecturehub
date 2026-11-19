"""Conservative filename and existing Markdown match-table interpretation."""

import fnmatch
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PureWindowsPath

from .scanner import MATERIAL_EXTENSIONS

_DATE = re.compile(r"^(?P<year>\d{2}|\d{4})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})\s+(?P<rest>.+)$")
_ENGINE_SUFFIX = re.compile(r"\s*\((?:MAI|다글로|Transcribe|클로바|Clova|3\.1 Pro 통째)\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class LectureName:
    folder: str
    date: str
    title: str
    session_label: str


@dataclass(frozen=True)
class MatchRow:
    recording: str
    clock: str
    material: str
    evidence: str
    confidence: str
    note: str
    line_number: int = 0


def parse_lecture_name(filename: str, folder: str) -> LectureName | None:
    stem = Path(filename).stem if Path(filename).suffix.lower() in {
        ".txt", ".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma",
    } else filename
    stem = _ENGINE_SUFFIX.sub("", stem).strip()
    match = _DATE.fullmatch(stem)
    if not match:
        return None
    rest = match.group("rest")
    if not (rest == folder or rest.startswith((folder + " ", folder + "("))):
        return None
    year = int(match.group("year"))
    try:
        day = date(year + 2000 if year < 100 else year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None
    return LectureName(folder, day.isoformat(), stem, rest[len(folder):].strip())


def read_match_rows(text: str) -> list[MatchRow]:
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] == "녹음" or re.fullmatch(r"[-: ]+", cells[0]):
            continue
        rows.append(MatchRow(*cells, line_number=number))
    return rows


def explicit_transcript_paths(text: str, folders: list[str]) -> list[tuple[Path, int]]:
    """Only direct course TXT paths explicitly named in the user's match document."""
    references = []
    for number, line in enumerate(text.splitlines(), 1):
        for folder in folders:
            for match in re.finditer(re.escape(folder) + r"[\\/]([^`|\n\\/]+?\.txt)", line):
                filename = match[1]
                if Path(filename).name == filename:
                    references.append((Path(folder) / filename, number))
    return list(dict.fromkeys(references))


def note_history_links(text: str, folder: str, notes: list[Path], year: int) -> list[tuple[Path, str, int]]:
    """Read explicit note ordinals and dates from the user's existing-note list.

    An ordinal identifies a note filename, never an inferred recording session.
    Ambiguous filenames and invalid dates are left unresolved.
    """
    references = []
    expression = re.compile(re.escape(folder) + r"\s*(\d+(?:[·,]\d+)*)강\s*\((\d{1,2})/(\d{1,2})\)")
    for number, line in enumerate(text.splitlines(), 1):
        if "이미 있던 노트" not in line and "기존 노트" not in line:
            continue
        for match in expression.finditer(line):
            try:
                lecture_date = date(year, int(match[2]), int(match[3])).isoformat()
            except ValueError:
                continue
            for ordinal in re.split(r"[·,]", match[1]):
                candidates = [path for path in notes if re.search(rf"(?<!\d){ordinal}강(?!\d)", path.stem)]
                if len(candidates) == 1:
                    references.append((candidates[0], lecture_date, number))
    return references


def row_matches_name(row: MatchRow, name: LectureName) -> bool:
    """An exact recording label is strongest; explicitly grouped labels also work."""
    label = row.recording.strip().strip("`")
    if label == name.title:
        return True
    parsed = parse_lecture_name(label, name.folder)
    if parsed and parsed.date == name.date:
        if parsed.session_label == name.session_label:
            return True
        if parsed.session_label == "(설계)" and not name.session_label:
            return True
        if re.fullmatch(r"\d+(?:·\d+)+", parsed.session_label):
            return name.session_label in parsed.session_label.split("·")
    # Explicit date groups such as 26.9.22 / 9.23 토질역학.
    if label.endswith(" " + name.folder) and " / " in label:
        first = re.match(r"(\d{2,4})\.(\d+)\.(\d+)", label)
        if first:
            year = int(first[1]) + (2000 if len(first[1]) == 2 else 0)
            dates = {(int(first[2]), int(first[3]))}
            dates.update((int(a), int(b)) for a, b in re.findall(r"/\s*(\d+)\.(\d+)", label))
            actual = date.fromisoformat(name.date)
            return actual.year == year and (actual.month, actual.day) in dates and not name.session_label
    return False


def find_note_paths(cell: str, notes: list[Path]) -> list[Path]:
    # Existing hand-written references abbreviate the extension too: ``…html``.
    references = re.findall(r"`([^`]+html?)`", cell)
    matches: list[Path] = []
    for reference in references:
        pattern = reference.replace("\\", "/").split("/")[-1].replace("…", "*")
        candidates = [path for path in notes if fnmatch.fnmatchcase(path.name, pattern)]
        if len(candidates) == 1:
            matches.extend(candidates)
    return list(dict.fromkeys(matches))


def material_ranges(cell: str, materials: list[Path]) -> list[tuple[Path, int, int]]:
    """Only import uniquely identified files with explicit page/slide ranges."""
    markers = list(re.finditer(r"`(?P<title>[^`]+)`|(?P<week>Week\s+\d+)", cell))
    results: list[tuple[Path, int, int]] = []
    for index, marker in enumerate(markers):
        title = marker.group("title") or marker.group("week")
        title_path = PureWindowsPath(title)
        if title_path.suffix.lower() in MATERIAL_EXTENSIONS:
            title = title_path.stem
        normalized = re.sub(r"[\s_]+", "", title).casefold()
        candidates = [p for p in materials if normalized in re.sub(r"[\s_]+", "", p.stem).casefold()]
        if marker.group("week"):
            number = re.search(r"\d+", title)[0]
            candidates = [p for p in materials if re.search(rf"\bWeek\s+{number}\b", p.stem, re.IGNORECASE)]
        # A source document's PDF and slideshow are separate materials; never choose between them silently.
        if len(candidates) != 1:
            continue
        tail = cell[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(cell)]
        # Page ranges precede parenthesized content descriptions.
        tail = tail.split("(", 1)[0].replace("**", "")
        intervals = []
        for match in re.finditer(r"(?<!\d)(\d+)\s*[~–−-]\s*(\d+)(?!\d)", tail):
            first, last = int(match[1]), int(match[2])
            if first >= 1 and last >= first:
                intervals.append((first, last))
        # Unit-terminated page lists can include single pages before a range.
        page_lists = re.finditer(
            r"(?<![\w.])(\d+(?:\s*[~–−-]\s*\d+)?(?:\s*[,·]\s*\d+(?:\s*[~–−-]\s*\d+)?)*)"
            r"\s*(?:쪽|번|페이지|슬라이드)", tail,
        )
        for listed in page_lists:
            for item in re.split(r"[,·]", listed[1]):
                endpoints = re.split(r"\s*[~–−-]\s*", item.strip())
                first, last = int(endpoints[0]), int(endpoints[-1])
                if first >= 1 and last >= first:
                    intervals.append((first, last))
        for single in re.finditer(r"(?:슬라이드|페이지)\s+(\d+)(?!\d|\s*[~–−-])", tail):
            if int(single[1]) >= 1:
                intervals.append((int(single[1]), int(single[1])))
        results.extend((candidates[0], first, last) for first, last in dict.fromkeys(intervals))
    return results
