"""Schedule notices spoken in class, read by AI from the whole transcript (user request 2026-10-30).

Why: a professor said in class that a session two weeks later would not meet, in a sentence with no date in it
(for example "그 주 금요일은 학회라서 11월에 봅시다"). The rule scanner in calendar_events missed it three ways: STT heard 학회 as 학기/확인,
"그 주 금요일" is not one of its patterns, and "11월에" alone is not a date. The user asked for a reading of the
whole context instead of fixed expressions, and for a notice right away.

What it does, per lecture whose transcripts changed since the last reading:
- sends every engine's transcript of that lecture (text only) to Gemini and asks for schedule notices (휴강·보강·
  시간/장소 변경·시험·퀴즈·과제 마감·온라인 대체) with the real date worked out from context;
- keeps a notice only when its quote is found in the transcript as written (AGENTS 8: no quote, no output); the
  stored quote is the original text, never the model's copy;
- adds a calendar candidate (never confirmed, D-052) with the quote and the AI's reasoning as Evidence, and an
  entry in ``_lecturehub/notices/inbox.json`` that the app shows as a banner and a Windows notification.

What one request carries besides the transcript (``build_prompt``, ``_context``): the course's folder name, the
lecture's date, today's date, the course's timetable (weekday and times), and from 7 days before the lecture
onward the calendar entries (kind, date, title, confirmed or candidate) and timetable exceptions (date, kind,
note) of that course **and those that have no course at all**. A lecture is sent only when its course has the
Gemini consent on record (the same provider and terms as the audio that produced the transcript); the entries
without a course are not covered by any course's consent and go along all the same.

How often (release review 2026-11-16): a lecture is sent when its transcripts changed. A send that failed is not
repeated every round: one set of transcripts (fingerprint) is sent at most ``MAX_SENDS`` times, the next send
waits ``RETRY_AFTER_MS`` after a failed one, and after the last one the lecture waits until its transcripts
change. Every request whose body went out is counted in ``notices/usage.json``, answered or not.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from lecturehub.core.ids import new_ulid

from . import calendar_events

PROVIDER = "gemini_transcribe"  # consent key: the course's audio already goes to Google under this notice
MODELS = ("gemini-flash-latest", "gemini-2.5-flash")
DAYS_BACK = 28            # lectures older than this are not read again (their notices are mostly past)
HORIZON_DAYS = 150
ENGINE_CHARS = 90_000     # per engine; a 75-minute lecture is about 20–30k characters
TOTAL_CHARS = 320_000
WEEKDAYS = "월화수목금토일"
KINDS = {  # notice kind -> (calendar kind, label)
    "closure": ("closure", "휴강"),
    "makeup": ("other", "보강"),
    "online": ("other", "온라인 대체"),
    "time_change": ("other", "시간 변경"),
    "room_change": ("other", "장소 변경"),
    "exam": ("exam", "시험"),
    "quiz": ("quiz", "퀴즈"),
    "assignment": ("assignment", "과제 마감"),
    "other": ("other", "일정"),
}
CERTAINTY = {"high": "높음", "medium": "중간", "low": "낮음"}
MAX_SENDS = 3                                   # requests for one lecture with the same transcripts, over all rounds
RETRY_AFTER_MS = (3_600_000, 4 * 3_600_000)     # wait after the 1st and the 2nd failed request (1 h, 4 h)
PENDING = "pending"                             # state.json: written before a request, replaced by its outcome


class NoticeError(RuntimeError):
    """The reading could not be made now (no key, quota, network, server). ``scan`` keeps the lecture for a later
    round: the next one when nothing was sent, after the waits of ``RETRY_AFTER_MS`` when something was (and
    not again once ``MAX_SENDS`` requests were made, until the transcripts change)."""


class UnusableAnswer(NoticeError):
    """The request was answered, but not with the JSON asked for (blocked, cut off, an error page). It says
    something about this lecture's answer, not about the service, so the round goes on with the next lecture."""


# ------------------------------------------------------------------ Gemini text client
SCHEMA = {
    "type": "OBJECT",
    "properties": {"notices": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "enum": list(KINDS)},
            "date": {"type": "STRING", "nullable": True, "description": "YYYY-MM-DD, or null when no day can be told"},
            "date_precision": {"type": "STRING", "enum": ["day", "approx", "unknown"]},
            "date_text": {"type": "STRING", "description": "the wording used in class, e.g. 그 주 금요일"},
            "date_reasoning": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "speaker": {"type": "STRING", "enum": ["professor", "ta", "student", "unknown"]},
            "certainty": {"type": "STRING", "enum": ["high", "medium", "low"]},
            "stt_note": {"type": "STRING"},
            "already_known": {"type": "BOOLEAN"},
            "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "line": {"type": "STRING", "description": "line id as given, e.g. E1#12"},
                "text": {"type": "STRING"}}, "required": ["line", "text"]}},
        },
        "required": ["kind", "date", "date_precision", "date_text", "date_reasoning", "summary", "speaker",
                     "certainty", "stt_note", "already_known", "quotes"],
    }}},
    "required": ["notices"],
}


class GeminiText:
    """generateContent with a JSON schema. The API key is never logged or written.

    ``posts`` counts the requests whose body went out over this object's life, answered or not: each model of a
    fall-through, a 4xx/5xx/429 answer, an answer that cannot be used, a timeout or a broken connection after
    sending began. A connection that could not be made is not counted (nothing left this PC). ``scan`` reads the
    number before and after a call for the usage figure and for the limit of ``MAX_SENDS``."""

    def __init__(self, api_key: str, *, models: tuple[str, ...] = MODELS, http=None):
        import httpx

        self._key = api_key
        self.models = models
        self.model = models[0]
        self.posts = 0
        self.http = http or httpx.Client(timeout=httpx.Timeout(900.0, connect=30.0))

    def generate_json(self, prompt: str, schema: dict, *, max_posts: int | None = None, may_send=None) -> dict:
        """``max_posts``: at most this many requests in this call (what is left of the lecture's ``MAX_SENDS``).
        With fewer left than there are models, the later models are asked: at temperature 0 the model that gave
        an unusable answer before would most likely give it again.

        When the server answers a request with an error (model gone or busy), the same text goes to the next
        model at once, in this call. ``may_send`` is asked before each such further request: when it says no
        (the reading was switched off meanwhile) nothing more goes out and the error of the last answer is raised."""
        import httpx

        from lecturehub.transcription.gemini import API, GeminiError, PermanentError, TransientError, classify

        not_sent = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError,
                    httpx.UnsupportedProtocol)  # no connection to the server: the body did not go out
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                     "temperature": 0}}
        models = self.models
        if max_posts is not None and max_posts < len(models):
            models = models[len(models) - max(0, int(max_posts)):]
        last: Exception | None = None
        for number, model in enumerate(models):
            if number and may_send is not None and not may_send():
                break  # switched off while the first request was at the server: no second one
            try:
                response = self.http.post(f"{API}/v1beta/models/{model}:generateContent",
                                          headers={"x-goog-api-key": self._key,
                                                   "Content-Type": "application/json; charset=utf-8"},
                                          content=json.dumps(body, ensure_ascii=False).encode("utf-8"))
            except not_sent as exc:
                raise NoticeError(f"네트워크 오류: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:  # connected, then a timeout or a lost connection: the body may have gone
                self.posts += 1             # out (all or part of it), so it is counted as sent
                raise NoticeError(f"네트워크 오류: {type(exc).__name__}") from exc
            self.posts += 1
            try:
                classify(response)
            except (PermanentError, TransientError) as exc:  # model gone or busy (503 "high demand"): next model
                last = exc
                continue
            except GeminiError as exc:  # quota (429)
                raise NoticeError(str(exc)) from exc
            self.model = model
            try:
                data = json.loads(response.content.decode("utf-8"))
                parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                answer = json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
            except (ValueError, AttributeError, TypeError, IndexError, KeyError) as exc:
                raise UnusableAnswer("AI 응답이 JSON이 아닙니다") from exc
            if not isinstance(answer, dict):
                raise UnusableAnswer("AI 응답이 JSON이 아닙니다")
            return answer
        raise NoticeError(str(last) if last else "쓸 수 있는 모델이 없습니다")


class FakeText:
    """LECTUREHUB_FAKE_GEMINI and tests: sends nothing anywhere. ``posts`` goes up by one per call, standing in
    for one request, so the usage figure and the limit of sends behave as with the real client."""

    model = "fake"

    def __init__(self, answer: dict | None = None):
        self.answer = answer if answer is not None else {"notices": []}
        self.prompts: list[str] = []
        self.posts = 0

    def generate_json(self, prompt: str, schema: dict, *, max_posts: int | None = None, may_send=None) -> dict:
        self.prompts.append(prompt)
        self.posts += 1
        return self.answer(prompt) if callable(self.answer) else self.answer


def default_client():
    if os.environ.get("LECTUREHUB_FAKE_GEMINI"):
        return FakeText()
    from lecturehub.transcription.gemini import GeminiError, load_api_key

    try:
        return GeminiText(load_api_key())
    except GeminiError as exc:
        raise NoticeError(str(exc)) from exc


# ------------------------------------------------------------------ files under _lecturehub/notices
def enabled() -> bool:
    """The background reading can be switched off in settings (``class_notices: false``). A lecture is sent when
    its transcripts changed, for courses whose Google consent is on record, and at most ``MAX_SENDS`` times per
    change when sending fails; this switch is for people who do not want that extra use of their key at all.
    ``scan`` asks again before each lecture, so a round that is running stops sending when it is turned off."""
    from lecturehub.core import settings

    return settings.get("class_notices", True) is not False


def set_enabled(on: bool) -> bool:
    from lecturehub.core import settings

    settings.save({"class_notices": bool(on)})
    return enabled()


def _number(value) -> int:
    """A stored count as a whole number that is not negative; anything else (text, a list, nothing) is 0."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _count_use(root: Path, today: date, sends: int, characters: int) -> bool:
    """How much this feature has sent, per day (``notices/usage.json``). ``readings`` is the number of requests
    whose body went out (the key name is older than that meaning) and ``characters`` the prompt characters of
    each of them added up. The app shows the month's total next to the switch, so the use of the key is not
    invisible (release review 2026-11-16). ``sends`` may be negative to take back a request that was put on
    record before sending and then never left this PC.

    Never raises and never stops the reading: a file of the wrong shape counts as empty, a damaged day as zero,
    the other days are written back as they were. False when the count could not be written."""
    try:
        path = _dir(root) / "usage.json"
        data = _read(path, {})
        if not isinstance(data, dict):
            data = {}
        day = data.get(today.isoformat())
        day = day if isinstance(day, dict) else {}
        data[today.isoformat()] = {**day, "readings": max(0, _number(day.get("readings")) + int(sends)),
                                   "characters": max(0, _number(day.get("characters")) + int(characters))}
        _write(path, data)
        return True
    except Exception:  # noqa: BLE001 - whatever is wrong with the count, the reading and its own records go on
        return False


def usage(root: Path, today: date) -> dict:
    """This month's requests (``readings``) and the characters they carried, for the settings screen. Never
    raises: the switch is drawn from the same answer, and a damaged count must not take the switch away."""
    data = _read(_dir(root) / "usage.json", {})
    month = today.isoformat()[:7]
    days = [value for key, value in (data.items() if isinstance(data, dict) else []) if str(key).startswith(month)
            and isinstance(value, dict)]
    return {"month": month, "readings": sum(_number(d.get("readings")) for d in days),
            "characters": sum(_number(d.get("characters")) for d in days)}


def _dir(root: Path) -> Path:
    return Path(root) / "_lecturehub" / "notices"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return default


def _write(path: Path, data) -> None:
    from lecturehub.core.fileio import atomic_write_json

    atomic_write_json(path, data)  # same dir temp + os.replace, retried on a Windows lock


def inbox(root: Path, *, unseen_only: bool = False) -> list[dict]:
    """The stored notices. A file of the wrong shape counts as empty and entries that are not notices are left
    out, so a damaged inbox cannot make a reading fail after its transcript was already sent."""
    items = _read(_dir(root) / "inbox.json", [])
    items = [i for i in items if isinstance(i, dict) and i.get("key")] if isinstance(items, list) else []
    return [i for i in items if not (unseen_only and i.get("seen"))]


def mark_seen(root: Path, keys: list[str]) -> int:
    path = _dir(root) / "inbox.json"
    items, changed = inbox(root), 0
    for item in items:
        if item["key"] in keys and not item.get("seen"):
            item["seen"], changed = True, changed + 1
    if changed:
        _write(path, items)
    return changed


# ------------------------------------------------------------------ prompt
@dataclass
class Lines:
    engines: dict[str, dict] = field(default_factory=dict)   # "E1" -> {"transcript_id", "engine", "label"}
    segments: dict[str, tuple[str, int, int, str]] = field(default_factory=dict)  # "E1#12" -> (tid, seq, ms, text)


def _clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}" if total >= 3600 else f"{total // 60:02d}:{total % 60:02d}"


def _lines(connection: sqlite3.Connection, transcripts: list[dict]) -> tuple[Lines, str]:
    lines, blocks, total = Lines(), [], 0
    for number, item in enumerate(transcripts, 1):
        tag = f"E{number}"
        lines.engines[tag] = {"transcript_id": item["id"], "engine": item["engine"], "label": item["label"]}
        out, size = [], 0
        for seq, start_ms, text in connection.execute(
                "SELECT seq, start_ms, text FROM TranscriptSegment WHERE transcript_id=? ORDER BY seq", (item["id"],)):
            text = " ".join(str(text).split())
            if not text:
                continue
            key = f"{tag}#{seq}"
            lines.segments[key] = (item["id"], seq, start_ms or 0, text)
            row = f"[{key} {_clock(start_ms or 0)}] {text}"
            if size + len(row) > ENGINE_CHARS or total + len(row) > TOTAL_CHARS:
                out.append("[… 이 전사본의 나머지는 길이 제한으로 생략]")
                break
            out.append(row)
            size, total = size + len(row) + 1, total + len(row) + 1
        blocks.append(f"### {tag} — 전사 엔진: {item['label']}\n" + "\n".join(out))
    return lines, "\n\n".join(blocks)


def _context(connection: sqlite3.Connection, course_id: str, lecture_day: date) -> dict:
    slots = [f"{WEEKDAYS[w]} {s // 3_600_000:02d}:{s // 60_000 % 60:02d}–{e // 3_600_000:02d}:{e // 60_000 % 60:02d}"
             for w, s, e in connection.execute(
                 "SELECT weekday, start, end FROM Schedule WHERE course_id=? ORDER BY weekday, start", (course_id,))]
    since = (lecture_day - timedelta(days=7)).isoformat()
    known = [f"{r[1]} {calendar_events.KIND_LABEL.get(r[0], r[0])} — {r[2]} ({'확정' if r[3] == 'confirmed' else '후보'})"
             for r in connection.execute(
                 "SELECT kind, date, title, status FROM CalendarEvent WHERE (course_id=? OR course_id IS NULL) "
                 "AND status IN ('confirmed','candidate') AND date >= ? ORDER BY date", (course_id, since))]
    known += [f"{r[0]} {r[1]} — {r[2]} (일정 예외)" for r in connection.execute(
        "SELECT date, kind, note FROM ScheduleException WHERE (course_id=? OR course_id IS NULL) AND date >= ? "
        "ORDER BY date", (course_id, since))]
    return {"slots": slots, "known": known}


def build_prompt(course: str, lecture_day: date, today: date, context: dict, transcripts_text: str) -> str:
    day = f"{lecture_day.isoformat()}({WEEKDAYS[lecture_day.weekday()]})"
    return f"""너는 대학 수업 녹음의 전사본을 읽고, 학생이 놓치면 안 되는 **수업 일정 공지**를 찾는다.

과목: {course}
이 수업 날짜: {day}
오늘: {today.isoformat()}({WEEKDAYS[today.weekday()]})
이 과목 정규 시간표: {", ".join(context["slots"]) or "모름"}
달력에 이미 있는 이 과목 일정:
{chr(10).join("- " + k for k in context["known"]) or "- 없음"}

아래는 같은 수업을 여러 전사 엔진이 받아 적은 것이다. 각 줄 앞의 [E1#12 03:25]가 줄 번호와 수업 시작 후 시각이다.
전사는 틀린 글자가 많다(예: '학회'를 '학기'나 '확인'으로 적기). 엔진끼리 비교해서 실제로 한 말을 판단해라.

찾을 것: 휴강, 보강, 시간·장소 변경, 온라인·영상 대체, 시험(날짜·범위·방식), 퀴즈, 과제·보고서 제출 마감, 그 밖에 수업 일정이 바뀌는 공지.
판단 원칙:
1. 특정 단어가 아니라 **전체 맥락**으로 판단한다. "그 주 금요일도 제가 학회가 있어서 11월에 보게 될 거예요"처럼 '휴강'이라는 말이 없어도 수업이 없다는 뜻이면 휴강이다.
2. 상대적인 표현("그 주", "시험 주", "다음 주 금요일", "다음 시간", "11월에")은 이 수업 날짜, 시간표, 앞뒤에서 말한 주를 근거로 **실제 날짜**를 계산한다. 계산 과정을 date_reasoning에 짧게 적는다. 하루로 정할 수 없으면 date_precision을 approx(대략) 또는 unknown(날짜 모름, date는 null)으로 둔다.
3. 수업 **내용** 속 날짜(예: 회계 예제의 "12월 31일 결산", 역사 연도, 문제 속 기한)는 공지가 아니다.
4. 같은 공지가 여러 번 나오면 하나로 합친다. 공지가 수업 중에 바뀌면(예: 마감을 30일에서 11월 1일로 미룸) **최종 내용**만 남기고, 바뀐 사실을 summary에 적는다.
5. 보고서 마감과 시험처럼 한 문장에 둘이 나오면 따로 나눈다. 마감 날짜를 시험 날짜로 착각하지 않는다.
6. quotes에는 근거 줄 번호와 그 줄의 글자를 **고치지 말고 그대로** 옮긴 짧은 구절(10~80자)을 넣는다. 여러 엔진에서 같은 대목이 있으면 각각 넣는다.
7. 잘못 들렸을 가능성이 있는 낱말은 stt_note에 적는다(예: "E1 '학기'는 맥락상 '학회'로 추정"). 없으면 빈 문자열.
8. 확신도: 말이 분명하고 날짜가 확실하면 high, 해석이 필요하면 medium, 추측이 크면 low.
9. 달력에 이미 같은 일정이 있으면 already_known을 true로 한다.
10. summary는 한국어 한 문장(40자 안팎)으로, 무엇이 언제 어떻게 되는지 쓴다. 누가 말했는지는 speaker에 적는다(교수 professor, 조교 ta).
공지가 없으면 notices를 빈 배열로 둔다.

{transcripts_text}
"""


# ------------------------------------------------------------------ quotes must be in the transcript as written
def _squash(text: str) -> tuple[str, list[int]]:
    keep, where = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            keep.append(char.lower())
            where.append(index)
    return "".join(keep), where


def original_span(segment: str, quote: str) -> str | None:
    """The segment's own text for the quote (spaces and punctuation may differ), or None when it is not there."""
    target, _ = _squash(quote)
    if len(target) < 4:
        return None
    flat, where = _squash(segment)
    at = flat.find(target)
    if at < 0:
        return None
    return segment[where[at]:where[at + len(target) - 1] + 1]


def _verify(lines: Lines, quotes: list[dict]) -> list[dict]:
    found = []
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        key, text = str(quote.get("line", "")).strip().strip("[]").split(" ")[0], str(quote.get("text", ""))
        tries = [key] if key in lines.segments else []
        tag = key.split("#")[0]
        tries += [k for k in lines.segments if k.startswith(tag + "#") and k != key]  # the model may miss the line
        for candidate in tries:
            tid, seq, at_ms, segment = lines.segments[candidate]
            span = original_span(segment, text)
            if span:
                found.append({"line": candidate, "transcript_id": tid, "seq": seq, "at_ms": at_ms, "quote": span,
                              "engine": lines.engines[tag]["label"]})
                break
    unique = {(q["transcript_id"], q["seq"], q["quote"]): q for q in found}
    return list(unique.values())


# ------------------------------------------------------------------ scan
def _fingerprint(transcripts: list[dict]) -> str:
    raw = "|".join(f"{t['id']}:{t['segments']}:{t['last_ms']}" for t in sorted(transcripts, key=lambda t: t["id"]))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _ref(lecture_id: str, kind: str, day: str | None, summary: str) -> str:
    tail = day or hashlib.sha1(summary.encode()).hexdigest()[:8]
    return f"ai:{lecture_id}:{kind}:{tail}"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _parse_day(value, lecture_day: date) -> date | None:
    try:
        day = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return day if lecture_day <= day <= lecture_day + timedelta(days=HORIZON_DAYS) else None


def _already(connection: sqlite3.Connection, course_id: str, kind: str, day: date) -> bool:
    return connection.execute(
        "SELECT 1 FROM CalendarEvent WHERE (course_id=? OR course_id IS NULL) AND kind=? AND date=? "
        "AND status IN ('confirmed','candidate')", (course_id, kind, day.isoformat())).fetchone() is not None


def _store(connection, root: Path, lecture: dict, notice: dict, quotes: list[dict], model: str, today: date) -> dict | None:
    """One checked notice → calendar candidate (when it has a day) + inbox entry. None when nothing new."""
    kind = notice.get("kind") if notice.get("kind") in KINDS else "other"
    cal_kind, label = KINDS[kind]
    lecture_day = date.fromisoformat(lecture["date"])
    precision = notice.get("date_precision") if notice.get("date_precision") in ("day", "approx") else "unknown"
    day = _parse_day(notice.get("date"), lecture_day) if precision != "unknown" else None
    if day is None:
        precision = "unknown"
    summary = " ".join(str(notice.get("summary") or label).split())[:120]
    certainty = notice.get("certainty") if notice.get("certainty") in CERTAINTY else "low"
    stt_note = " ".join(str(notice.get("stt_note") or "").split())[:200]
    ref = _ref(lecture["id"], kind, day.isoformat() if day else None, summary)
    if connection.execute("SELECT 1 FROM CalendarEvent WHERE source='transcript' AND source_ref=?", (ref,)).fetchone():
        return None
    if any(item["key"] == ref for item in inbox(root)):
        return None
    event_id = None
    if day is not None:
        if _already(connection, lecture["course_id"], cal_kind, day):
            return None  # the calendar already has it (typed by the user, eTL or an earlier reading)
        note = (f"AI가 {lecture['date'][5:].replace('-', '/')} 수업 전사본 전체를 읽고 찾은 후보(확인 필요) · "
                f"확신도 {CERTAINTY[certainty]}" + (f" · 잘못 들렸을 수 있음: {stt_note}" if stt_note else ""))
        event_id = calendar_events.add(
            connection, kind=cal_kind, title=summary[:60] or label, day=day.isoformat(), course_id=lecture["course_id"],
            note=note, source="transcript", source_ref=ref, status="candidate",
            date_precision="day" if precision == "day" else "approx",
            date_text=str(notice.get("date_text") or "")[:60] or None)
        now = _now_ms()
        for quote in quotes:
            connection.execute(
                "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_ulid(), "CalendarEvent", event_id, "transcript_quote", json.dumps({
                    "lecture_id": lecture["id"], "transcript_id": quote["transcript_id"], "seq": quote["seq"],
                    "at_ms": quote["at_ms"], "quote": quote["quote"], "engine": quote["engine"],
                    "engines_agree": len({q["engine"] for q in quotes})}, ensure_ascii=False), "engine", now))
        connection.execute(
            "INSERT INTO Evidence(id,subject_type,subject_id,kind,detail,source,created_at) VALUES (?,?,?,?,?,?,?)",
            (new_ulid(), "CalendarEvent", event_id, "ai_reading", json.dumps({
                "model": model, "notice_kind": kind, "certainty": certainty, "date_text": notice.get("date_text"),
                "date_reasoning": notice.get("date_reasoning"), "speaker": notice.get("speaker"),
                "stt_note": stt_note, "summary": summary}, ensure_ascii=False), "ai", now))
    first = quotes[0]
    return {"key": ref, "event_id": event_id, "course": lecture["course"], "course_id": lecture["course_id"],
            "lecture_id": lecture["id"], "lecture_date": lecture["date"], "kind": kind, "kind_label": label,
            "date": day.isoformat() if day else None, "date_precision": precision,
            "date_text": notice.get("date_text") or "", "date_reasoning": notice.get("date_reasoning") or "",
            "summary": summary, "certainty": certainty, "certainty_label": CERTAINTY[certainty], "stt_note": stt_note,
            "speaker": notice.get("speaker") or "unknown", "quote": first["quote"], "engine": first["engine"],
            "at_ms": first["at_ms"], "engines": sorted({q["engine"] for q in quotes}), "model": model,
            "found_at": _now_ms(), "seen": bool(day and day < today)}


def lectures_to_read(connection: sqlite3.Connection, today: date, days_back: int = DAYS_BACK,
                     lecture_ids: list[str] | None = None) -> list[dict]:
    if lecture_ids:
        marks = ",".join("?" * len(lecture_ids))
        rows = connection.execute(
            f"SELECT l.id, l.date, l.course_id, c.folder_name FROM Lecture l JOIN Course c ON c.id=l.course_id "
            f"WHERE l.id IN ({marks}) ORDER BY l.date", tuple(lecture_ids)).fetchall()
    else:
        rows = connection.execute(
            "SELECT l.id, l.date, l.course_id, c.folder_name FROM Lecture l JOIN Course c ON c.id=l.course_id "
            "WHERE l.date BETWEEN ? AND ? ORDER BY l.date",
            ((today - timedelta(days=days_back)).isoformat(), today.isoformat())).fetchall()
    return [{"id": r[0], "date": r[1], "course_id": r[2], "course": r[3]} for r in rows]


def _entry(state: dict, lecture_id: str) -> dict:
    """A lecture's record in state.json; one of the wrong shape counts as none."""
    value = state.get(lecture_id)
    return value if isinstance(value, dict) else {}


def _failed(entry: dict, mark: str) -> tuple[int, int]:
    """(requests made for these transcripts that brought no usable answer, time of the last one in ms)."""
    failed = entry.get("failed")
    if not isinstance(failed, dict) or failed.get("fingerprint") != mark:
        return 0, 0
    return _number(failed.get("sends")), _number(failed.get("at"))


def _wait_ms(sends: int) -> int | None:
    """How long the next request waits after ``sends`` (1 or more) failed ones. None: no further request until
    the transcripts change."""
    if sends >= MAX_SENDS:
        return None
    return RETRY_AFTER_MS[min(sends, len(RETRY_AFTER_MS)) - 1]


def _posts(client) -> int | None:
    value = getattr(client, "posts", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _save(path: Path, state: dict) -> bool:
    try:
        _write(path, state)
        return True
    except OSError:
        return False


def scan(connection: sqlite3.Connection, root: Path, today: date, *, client=None, consent=None,
         lecture_ids: list[str] | None = None, force: bool = False, days_back: int = DAYS_BACK,
         now_ms=None) -> dict:
    """Read lectures whose transcripts changed. Returns what was added for the banner.

    Each lecture's findings are committed (and inbox/state saved) before the next network call, so the
    SQLite write lock is never held while waiting for Gemini (review #1 — a recording's ``ingest-app``
    used to fail with 'database is locked'). Re-reading is safe: ``_store`` skips known ``source_ref``s.

    The switch in settings is asked before the round and again right before each lecture would be sent, so a
    round that is running sends nothing more once it is off (``"disabled": True`` in the result; the answer to
    a request already on its way is still used). ``force`` or ``lecture_ids`` read regardless of the switch,
    and ``force`` also regardless of earlier readings and failures; no screen or command of the app passes
    either (2026-11-16), only the tests do.

    Sends (release review 2026-11-16). A request is put on record in state.json and usage.json *before* it goes
    out and corrected afterwards, so a request the app did not live to see the end of is still counted, and
    while state.json cannot be written nothing is sent (the same lecture would go out again every round). A
    lecture whose request brought no usable answer waits: one set of transcripts gets at most ``MAX_SENDS`` requests
    over all rounds (each model of a fall-through is one), the next not before ``RETRY_AFTER_MS`` after the
    1st and 2nd, none after the last until the transcripts change. While it waits, later lectures are read.
    After a failure the round ends (no connection, quota, timeout, server error: the others would fail the
    same way), except after an answer that cannot be used, which is about that lecture only.

    ``now_ms``: the clock for those waits (epoch milliseconds, kept in the file so they hold over restarts).
    A client without a ``posts`` count is taken to have sent one request per call, whatever came of it.
    """
    from . import lectures as lecture_index

    root = Path(root)
    clock = now_ms or _now_ms
    background = not force and not lecture_ids  # the rounds the switch in settings governs
    if background and not enabled():
        return {"read": 0, "added": [], "skipped": [], "errors": [], "waiting": [], "disabled": True}
    state_path = _dir(root) / "state.json"
    state = _read(state_path, {})
    if not isinstance(state, dict):  # damaged: as if nothing had been read yet
        state = {}
    if consent is None:
        from lecturehub.transcription.consent import ConsentStore

        consent = ConsentStore(root / "_lecturehub" / "consent.json")
    added, read, skipped, errors, waiting = [], 0, [], [], []
    stopped = dirty = False
    for lecture in lectures_to_read(connection, today, days_back, lecture_ids):
        transcripts = [t for t in lecture_index._transcripts(connection, lecture["id"]) if t["segments"]]
        if not transcripts:
            continue
        mark = _fingerprint(transcripts)
        entry = _entry(state, lecture["id"])
        if not force and entry.get("fingerprint") == mark:
            continue
        if not consent.granted(PROVIDER, lecture["course"]):
            skipped.append({"lecture_id": lecture["id"], "reason": f"{lecture['course']}: Google 전송 동의 없음"})
            continue
        sends, failed_at = _failed(entry, mark)
        if sends and not force:
            wait, now = _wait_ms(sends), clock()
            if wait is not None and failed_at > now:
                # the PC clock was set back: the wait runs from now, not from a time that has not come yet
                failed_at = now
                entry = {**entry, "failed": {**entry["failed"], "at": now}}
                state[lecture["id"]], dirty = entry, True
            if wait is None or now - failed_at < wait:
                waiting.append({"lecture_id": lecture["id"], "sends": sends,
                                "until_ms": None if wait is None else failed_at + wait})
                continue
        if background and not enabled():  # switched off while this round was running: nothing more goes out
            stopped = True
            break
        if client is None:
            client = default_client()
        lines, text = _lines(connection, transcripts)
        prompt = build_prompt(lecture["course"], date.fromisoformat(lecture["date"]), today,
                              _context(connection, lecture["course_id"], date.fromisoformat(lecture["date"])), text)
        if connection.in_transaction:
            connection.commit()  # never wait on the network holding the write lock
        pending = {**entry, "failed": {"fingerprint": mark, "sends": sends + 1, "at": clock(), "error": PENDING}}
        state[lecture["id"]] = pending
        if not _save(state_path, state):
            errors.append({"lecture_id": lecture["id"],
                           "error": "읽은 기록을 저장할 수 없어 전사 글을 보내지 않았습니다"})
            break
        counted = _count_use(root, today, 1, len(prompt))
        before, answer, failure = _posts(client), None, None
        try:
            answer = client.generate_json(prompt, SCHEMA, max_posts=None if force else MAX_SENDS - sends,
                                          may_send=enabled if background else None)
            if not isinstance(answer, dict):
                raise UnusableAnswer("AI 응답을 읽을 수 없습니다")
        except NoticeError as exc:
            failure = exc
        after = _posts(client)
        posted = max(0, after - before) if before is not None and after is not None else 1
        extra = posted - (1 if counted else 0)
        if extra:
            _count_use(root, today, extra, extra * len(prompt))
        now = clock()
        if failure is not None:
            errors.append({"lecture_id": lecture["id"], "error": str(failure)})
            if posted:
                state[lecture["id"]] = {**entry, "failed": {"fingerprint": mark, "sends": sends + posted, "at": now,
                                                            "error": str(failure)[:200]}}
            elif entry:  # nothing left this PC: as it was, the next round tries again
                state[lecture["id"]] = entry
            else:
                state.pop(lecture["id"], None)
            _save(state_path, state)
            if posted and isinstance(failure, UnusableAnswer):
                continue
            break
        read += 1
        if posted != 1:  # the true number is on record before the answer is worked through
            state[lecture["id"]] = {**pending, "failed": {**pending["failed"], "sends": sends + posted}}
            _save(state_path, state)
        kept = dropped = 0
        fresh = []
        notices = answer.get("notices")
        for notice in notices if isinstance(notices, list) else []:
            quotes = _verify(lines, notice.get("quotes")) if isinstance(notice, dict) else []
            if not quotes:
                dropped += 1  # AGENTS 8: a notice whose quote is not in the transcript is thrown away
                continue
            item = _store(connection, root, lecture, notice, quotes, getattr(client, "model", "?"), today)
            if item:
                fresh.append(item)
            kept += 1
        state[lecture["id"]] = {"fingerprint": mark, "read_at": now, "model": getattr(client, "model", "?"),
                                "notices": kept, "dropped_without_quote": dropped, "sends": sends + posted}
        if fresh:
            _write(_dir(root) / "inbox.json", inbox(root) + fresh)
            added.extend(fresh)
        # Not raised when it fails: the candidates above are committed below and the inbox already names them.
        # On disk the request stays recorded as pending (a failed send, so the next one waits), and the next
        # lecture's record before sending, if the file still cannot be written, ends the round.
        _save(state_path, state)
        if connection.in_transaction:
            connection.commit()  # short write transaction per lecture
    if dirty:
        _save(state_path, state)
    result = {"read": read, "added": added, "skipped": skipped, "errors": errors, "waiting": waiting}
    if stopped:
        result["disabled"] = True
    return result
