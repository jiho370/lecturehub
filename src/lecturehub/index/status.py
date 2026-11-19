"""Library coverage and a local, self-contained status report."""

from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from lecturehub.index.schedule import daily_schedule


@dataclass(frozen=True)
class LectureStatus:
    lecture_id: str
    course_id: str
    course: str
    date: str
    session_index: int
    title: str | None
    recordings: int
    transcripts: dict[str, int]
    materials: int
    canonical: int
    notes: int


@dataclass(frozen=True)
class MissingRecording:
    course_id: str
    course: str
    date: str
    lecture_id: str | None
    reason: str


@dataclass(frozen=True)
class LibraryStatus:
    as_of: str
    lectures: list[LectureStatus]
    missing_recordings: list[MissingRecording]
    google_backlog: list[LectureStatus]
    warnings: list[str]
    known_absences: list[MissingRecording] = field(default_factory=list)


def library_status(
    connection: sqlite3.Connection, *, as_of: date | None = None, start_date: date | None = None
) -> LibraryStatus:
    """Summarize indexed data and explicit timetable gaps, without reading files.

    Timetable gaps are candidates for confirmation, not proof that a class was
    held. Without a requested range, expected days start at the earliest indexed
    lecture, because the schema does not yet contain semester start/end dates.
    Only lectures with an original recording can enter the Google backlog.
    ``start_date`` bounds both unresolved gaps and confirmed absences, while
    retaining the full indexed lecture list through ``as_of``.
    """
    as_of = as_of or datetime.now(UTC).astimezone().date()
    if start_date is not None and start_date > as_of:
        raise ValueError("--from 날짜는 기준 날짜(--as-of)보다 늦을 수 없습니다.")
    lower_bound = start_date.isoformat() if start_date is not None else None
    rows = connection.execute(
        "SELECT l.id, c.id, c.folder_name, l.date, l.session_index, l.title, "
        "(SELECT count(*) FROM MediaAsset m WHERE m.lecture_id = l.id AND m.is_master = 1), "
        "(SELECT count(DISTINCT mm.material_id) FROM MaterialMatch mm WHERE mm.lecture_id = l.id), "
        "(SELECT count(*) FROM CanonicalTranscript ct WHERE ct.lecture_id = l.id), "
        "(SELECT count(*) FROM StudyArtifact a WHERE a.kind = 'note' AND "
        "EXISTS (SELECT 1 FROM json_each(a.lecture_ids) j WHERE j.value = l.id)) "
        "FROM Lecture l JOIN Course c ON c.id = l.course_id WHERE l.date <= ? "
        "ORDER BY l.date, c.folder_name, l.session_index",
        (as_of.isoformat(),),
    ).fetchall()
    engines: dict[str, dict[str, int]] = {}
    for lecture_id, engine, count in connection.execute(
        "SELECT lecture_id, engine, count(*) FROM Transcript GROUP BY lecture_id, engine ORDER BY engine"
    ):
        engines.setdefault(lecture_id, {})[engine] = count
    lectures = [
        LectureStatus(row[0], row[1], row[2], row[3], row[4], row[5], row[6], engines.get(row[0], {}),
                      row[7], row[8], row[9])
        for row in rows
    ]
    confirmed_absences = {
        row[0] for row in connection.execute(
            "SELECT DISTINCT subject_id FROM Evidence WHERE subject_type='Lecture' "
            "AND kind='recording_absence_confirmed' AND source='user' "
            "AND CASE WHEN json_valid(detail) THEN json_extract(detail,'$.decided_by') END = 'user'"
        )
    }
    known_absences = [
        MissingRecording(item.course_id, item.course, item.date, item.lecture_id, "사용자 확인: 원음 없음")
        for item in lectures if item.recordings == 0 and item.lecture_id in confirmed_absences
        and (lower_bound is None or item.date >= lower_bound)
    ]
    missing = [
        MissingRecording(item.course_id, item.course, item.date, item.lecture_id, "등록 강의에 연결된 원본 녹음 없음")
        for item in lectures if item.recordings == 0 and item.lecture_id not in confirmed_absences
        and (lower_bound is None or item.date >= lower_bound)
    ]
    known_days = {(item.course_id, item.date) for item in lectures}
    warnings = []
    beginning = start_date or (min(date.fromisoformat(item.date) for item in lectures) if lectures else None)
    if beginning is not None:
        day = beginning
        while day <= as_of:
            for occurrence in daily_schedule(connection, day):
                key = occurrence.course_id, occurrence.date
                if key not in known_days:
                    missing.append(
                        MissingRecording(occurrence.course_id, occurrence.course, occurrence.date, None,
                                         "시간표 기준 녹음 미확인 — 휴강·대체 여부 확인 필요")
                    )
                    known_days.add(key)
            day += timedelta(days=1)
        if any(item.lecture_id is None for item in missing):
            warnings.append("시간표 기준 빈 날짜는 미등록 휴강·대체일일 수 있습니다. 실제 수업 여부를 확인하세요.")
    missing.sort(key=lambda item: (item.date, item.course, item.lecture_id or ""))
    backlog = [item for item in lectures if item.recordings > 0 and "gemini_transcribe" not in item.transcripts]
    return LibraryStatus(as_of.isoformat(), lectures, missing, backlog, warnings, known_absences)


def _engines(lecture: LectureStatus) -> str:
    return ", ".join(f"{name} {count}" for name, count in lecture.transcripts.items()) or "없음"


def render_status_text(report: LibraryStatus) -> str:
    """Produce a plain-text report for the CLI; it contains no transcript body."""
    lines = [f"강의 현황 · 기준 {report.as_of}"]
    for item in report.lectures:
        lines.append(
            f"{item.date} {item.course} #{item.session_index} | 녹음 {item.recordings} | "
            f"전사 {_engines(item)} | 자료 {item.materials} | 통합 전사 {item.canonical} | 노트 {item.notes}"
        )
    if not report.lectures:
        lines.append("색인된 강의가 없습니다.")
    lines.append(f"\n빠진 녹음·확인 필요: {len(report.missing_recordings)}건")
    lines.extend(f"{item.date} {item.course} — {item.reason}" for item in report.missing_recordings)
    lines.append(f"\n원음 없음·사용자 확인 완료: {len(report.known_absences)}건")
    lines.extend(f"{item.date} {item.course} — {item.reason}" for item in report.known_absences)
    lines.append(f"\nGoogle 전사 백로그: {len(report.google_backlog)}강의")
    lines.extend(f"{item.date} {item.course} #{item.session_index}" for item in report.google_backlog)
    lines.extend(f"확인: {warning}" for warning in report.warnings)
    return "\n".join(lines)


def render_status_html(report: LibraryStatus) -> str:
    """Render escaped status data with the reference paper/sage design tokens.

    The caller chooses and validates the output path. This function is pure and
    includes no external resources, scripts, or source transcript contents.
    """
    esc = html.escape
    cards = []
    for item in report.lectures:
        cards.append(
            '<article class="card">'
            f'<p class="meta">{esc(item.date)} · {item.session_index}강</p>'
            f"<h3>{esc(item.course)}</h3>"
            f'<p class="title">{esc(item.title or "강의")}</p>'
            '<dl>'
            f"<div><dt>녹음</dt><dd>{item.recordings}개</dd></div>"
            f"<div><dt>전사</dt><dd>{esc(_engines(item))}</dd></div>"
            f"<div><dt>자료</dt><dd>{item.materials}개</dd></div>"
            f"<div><dt>통합 전사</dt><dd>{item.canonical}개</dd></div>"
            f"<div><dt>노트</dt><dd>{item.notes}개</dd></div>"
            "</dl></article>"
        )
    missing = "".join(
        f"<li><strong>{esc(item.date)} {esc(item.course)}</strong><br>{esc(item.reason)}</li>"
        for item in report.missing_recordings
    ) or "<li>확인할 녹음이 없습니다.</li>"
    known_absences = "".join(
        f"<li><strong>{esc(item.date)} {esc(item.course)}</strong><br>{esc(item.reason)}</li>"
        for item in report.known_absences
    ) or "<li>사용자가 원음 없음을 확인한 강의가 없습니다.</li>"
    backlog = "".join(
        f"<li>{esc(item.date)} {esc(item.course)} · {item.session_index}강</li>"
        for item in report.google_backlog
    ) or "<li>대기 중인 강의가 없습니다.</li>"
    warnings = "".join(f'<p class="notice">{esc(warning)}</p>' for warning in report.warnings)
    if not cards:
        cards.append('<p class="empty">색인된 강의가 없습니다.</p>')
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lecture Hub · 강의 현황</title>
<style>
:root{{--ink:#1c211b;--sub:#6b7266;--line:#e6e8e1;--paper:#ffffff;--bg:#e9ebe3;
--green:#3c6e47;--green-deep:#25452e;--green-soft:#e7f0e8;--amber:#9c5a24;--amber-soft:#f6ece2;}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.7 "Pretendard","Malgun Gothic",system-ui,sans-serif;word-break:keep-all;overflow-wrap:anywhere}}
main{{max-width:1040px;margin:24px auto;background:var(--paper);padding:36px;border-radius:18px}}
header{{border-bottom:1px solid var(--line);padding-bottom:20px}}h1{{font-size:2rem;line-height:1.3;margin:6px 0}}
h2{{font-size:1.3rem;margin:30px 0 14px}}h3{{font-size:1.2rem;margin:2px 0}}
.eyebrow{{color:var(--green);letter-spacing:.12em;font-size:.8rem;font-weight:700}}
.meta,.title{{color:var(--sub);margin:0 0 8px}}.summary{{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}}
.summary span{{padding:7px 13px;background:var(--green-soft);color:var(--green-deep);border-radius:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:14px}}
.card{{border:1px solid var(--line);padding:18px;border-radius:12px;min-width:0}}dl{{margin:14px 0 0}}
dl div{{display:grid;grid-template-columns:5em minmax(0,1fr);gap:12px;border-top:1px solid var(--line);padding:6px 0}}
dt{{color:var(--sub)}}dd{{margin:0}}ul{{padding-left:22px}}li{{margin:10px 0}}
.notice{{padding:12px 16px;background:var(--amber-soft);color:var(--amber);border-radius:8px}}
footer{{border-top:1px solid var(--line);margin-top:30px;padding-top:18px;color:var(--sub);font-size:.85rem}}
@media(max-width:600px){{main{{margin:0;padding:22px 16px;border-radius:0}}h1{{font-size:1.7rem}}}}
@media print{{body{{background:white}}main{{max-width:none;margin:0;padding:0}}.card{{break-inside:avoid}}}}
</style></head><body><main>
<header><div class="eyebrow">LECTURE HUB · LIBRARY</div><h1>강의 현황</h1>
<p class="meta">기준 {esc(report.as_of)} · 로컬 라이브러리 색인 기준</p>
<div class="summary"><span>강의 {len(report.lectures)}</span>
<span>녹음 확인 {len(report.missing_recordings)}</span><span>원음 없음 확인 완료 {len(report.known_absences)}</span>
<span>Google 전사 대기 {len(report.google_backlog)}</span></div>
</header><section aria-labelledby="lectures"><h2 id="lectures">강의별 준비 상태</h2>
<div class="grid">{''.join(cards)}</div></section>
<section aria-labelledby="missing"><h2 id="missing">빠진 녹음 · 확인 필요</h2>{warnings}<ul>{missing}</ul></section>
<section aria-labelledby="known-absences"><h2 id="known-absences">원음 없음 · 사용자 확인 완료</h2>
<p class="meta">원음 없음이 명시적으로 확인된 강의이며, 추가 확인 대상에서 분리했습니다.</p>
<ul>{known_absences}</ul></section>
<section aria-labelledby="backlog"><h2 id="backlog">Google 전사 백로그</h2>
<p class="meta">원본 녹음이 있고 Transcribe 전사본이 아직 없는 강의입니다.</p><ul>{backlog}</ul></section>
<footer>현황은 색인된 파일과 등록된 일정 예외에 따라 달라집니다. 원본 파일은 변경하지 않습니다.</footer>
</main></body></html>"""
