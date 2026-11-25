"""Per provider × course consent before any audio leaves the machine (CONSENT-001, D-019, D-052).

Each entry carries an ``authority``:
- ``confirmed_by_user``: the user said yes to the notice (CLI prompt or app dialog).
- ``synthetic_test``: recorded by an agent for synthetic test audio; never counts
  as consent for a real course.
- ``invalid``: kept for the record but never honoured (e.g. a blank course).

A blank course ("과목 미지정") is never a course: a yes there applies to that one
job only and is not stored, so unmatched real lectures are never sent silently.
Entries are annotated, never deleted.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .store import read_json, write_json

CONFIRMED, SYNTHETIC, INVALID = "confirmed_by_user", "synthetic_test", "invalid"
UNSORTED = "미분류"  # core.extra_courses.UNSORTED: a recording without a course, shown under this name
ALL_COURSES = "*"  # D-062: a provider-wide entry; a blank course is still asked per recording

# Provider notices are separate from recording permission. Never promise that an upload grants rights.
AUDIO_RIGHTS = "녹음에는 교수님의 강의와 다른 학생의 목소리·이름이 담길 수 있습니다. 외부 전송 허락을 확인하고 개인정보·사담을 빼 주세요.\n"
TEXT_RIGHTS = "전사본·자료 글에도 사람 이름 등 개인정보가 있을 수 있습니다. 보낼 권한을 확인하고 개인정보를 빼 주세요.\n"
CLAUDE_POLICY = ("개인 구독은 계정의 모델 개선 설정이 켜져 있으면 내용이 학습에 쓰일 수 있습니다. "
                 "보관 기간은 계정 종류·설정·약관을 따릅니다. 소리는 보내지 않습니다.\n"
                 "약관: https://www.anthropic.com/news/updates-to-our-consumer-terms")
NOTICES = {
    "gemini_transcribe": AUDIO_RIGHTS + (
        "이 과목의 녹음과 전사 글을 Google(Gemini API)에 보내 전사와 수업 공지 후보를 찾습니다.\n"
        "무료 서비스는 모델·제품 개선과 사람의 검토에 쓰일 수 있으며, 개인정보·민감정보·기밀정보를 보내면 안 됩니다. "
        "유료 서비스는 해당 계정 약관을 확인하세요. 보관도 해당 약관을 따릅니다.\n"
        "업로드한 오디오 파일은 처리 후 삭제를 요청하지만, 서비스 측 사본의 삭제까지 보장하지는 않습니다.\n"
        "약관: https://aistudio.google.com/docs/terms"),
    "whisper_groq": AUDIO_RIGHTS + (
        "이 과목의 녹음을 Groq(Whisper)로 보내 전사합니다. Groq는 자체 모델 학습에 고객 데이터를 쓰지 않는다고 밝힙니다.\n"
        "일반 추론의 입력·출력은 기본 미보관입니다. 오류·남용 조사 때는 최대 30일(법적 예외 가능) 보관될 수 있습니다. "
        "Data Controls의 Zero Data Retention 설정을 확인하세요.\n"
        "정책: https://console.groq.com/docs/your-data · https://trust.groq.com/"),
    "mai_playground": AUDIO_RIGHTS + (
        "이 과목의 녹음 조각을 Microsoft MAI playground에 올립니다. "
        "이 서비스의 학습 사용·보관 조건은 앱에서 확인하지 못했습니다. 조건을 직접 확인한 자료만 보내세요.\n"
        "서비스: https://playground.microsoft.ai/"),
    "google_live": AUDIO_RIGHTS + (
        "수업 소리를 Google에 실시간으로 보내 자막을 만듭니다. 기본 Gemini API 무료 서비스는 제품 개선·사람의 검토에 "
        "쓰일 수 있으므로 개인정보·민감정보·기밀정보를 보내면 안 됩니다.\n"
        "Chirp를 켜면 Google Cloud Speech-to-Text로도 보냅니다. 본인 프로젝트의 요금·보관 정책을 확인하세요. "
        "소리 파일은 남기지 않고 자막 글만 이 PC에 저장합니다. 자리를 비우거나 사담이 나오면 멈추세요.\n"
        "정책: https://aistudio.google.com/docs/terms · https://cloud.google.com/speech-to-text/docs/data-usage-faq"),
    "codex_note": TEXT_RIGHTS + (
        "이 과목의 전사본·강의자료 글을 Codex(OpenAI)로 보내 노트를 만듭니다. HTML 도우미에서는 자료 파일·그림도 읽을 수 있습니다. 소리는 보내지 않습니다.\n"
        "로그인한 ChatGPT 계정과 작업 공간 정책을 따릅니다. ChatGPT Edu는 기본적으로 업무 데이터를 모델 학습에 쓰지 않습니다. "
        "개인 계정은 해당 계정의 데이터 설정을 확인하세요. 보관 기간도 작업 공간의 정책을 따릅니다.\n"
        "CLI 로그인 상태만으로는 Edu 작업 공간인지 알 수 없습니다. 계정에서 직접 확인하세요.\n"
        "정책: https://learn.chatgpt.com/docs/pricing"),
    "codex_notices": TEXT_RIGHTS + (
        "이 과목의 전사 글을 과목 이름·수업 날짜, 이 과목의 시간표와 달력 일정(시간표 변경 포함)과 함께 Codex(OpenAI)로 보내 수업 중에 말한 "
        "일정 공지(휴강·보강·시험·과제 마감 등)를 찾습니다. 과목 없는 일정과 소리는 보내지 않습니다. "
        "찾은 공지는 달력 후보로만 넣고, 확정은 내가 합니다.\n"
        "로그인한 ChatGPT 계정과 작업 공간 정책을 따릅니다. ChatGPT Edu는 기본적으로 업무 데이터를 모델 학습에 쓰지 않습니다. "
        "개인 계정은 해당 계정의 데이터 설정을 확인하세요. 보관 기간도 작업 공간의 정책을 따릅니다.\n"
        "CLI 로그인 상태만으로는 Edu 작업 공간인지 알 수 없습니다. 계정에서 직접 확인하세요.\n"
        "정책: https://learn.chatgpt.com/docs/pricing"),
    "codex_etl_notices": (
        "eTL 공지에도 사람 이름 등 개인정보가 있을 수 있습니다. 보낼 권한을 확인하세요.\n"
        "이 과목의 최근 eTL 공지(제목·본문·올린 날짜)를 과목 이름, 오늘 날짜, 이 과목의 시간표와 달력 일정(시간표 변경 포함)과 함께 "
        "Codex(OpenAI)로 보내 시험·과제 마감·퀴즈·휴강·보강 같은 일정을 찾습니다. 공지마다 한 번 보내고(보내다 실패하면 같은 글을 "
        "최대 3번까지, 제목이나 본문이 고쳐지면 다시 보냅니다), "
        "녹음 중에는 보내지 않습니다. 첨부 파일, 다른 과목, 과목 없는 일정, 소리는 보내지 않습니다. "
        "찾은 일정은 달력 후보로만 넣고, 확정은 내가 합니다. 전사 글을 보내는 '수업 중 공지 찾기' 허용과는 따로입니다.\n"
        "로그인한 ChatGPT 계정과 작업 공간 정책을 따릅니다. ChatGPT Edu는 기본적으로 업무 데이터를 모델 학습에 쓰지 않습니다. "
        "개인 계정은 해당 계정의 데이터 설정을 확인하세요. 보관 기간도 작업 공간의 정책을 따릅니다.\n"
        "CLI 로그인 상태만으로는 Edu 작업 공간인지 알 수 없습니다. 계정에서 직접 확인하세요.\n"
        "정책: https://learn.chatgpt.com/docs/pricing"),
    "claude_note": TEXT_RIGHTS +"이 과목의 전사본·강의자료 글을 Claude Code(Anthropic)로 보내 노트를 만듭니다. HTML 도우미에서는 자료 파일·그림도 읽을 수 있습니다.\n" + CLAUDE_POLICY,
    "claude_qa": TEXT_RIGHTS + "발표 자료 글과 실시간 자막 글을 Claude Code(Anthropic)로 보내 답할 거리를 받습니다.\n" + CLAUDE_POLICY,
    "ai_app_link": TEXT_RIGHTS + (
        "이 과목의 전사 글, 정리본, 강의자료 글, 달력 일정, 앱이 받아 둔 eTL 공지와 과제 목록(제목·마감·제출 상태, 점수 없음)을 내가 연결한 AI 앱"
        "(ChatGPT 데스크톱·Codex, Claude 데스크톱 등)이 물어볼 때 건넵니다. 녹음 파일(소리)은 건네지 않습니다.\n"
        "건넨 글이 어느 회사로 가고 어떻게 보관·사용되는지는 연결한 AI 앱과 그 계정의 약관·설정을 따릅니다. "
        "앱에서 이 과목의 허용을 거두면 다음 요청부터 바로 건네지 않습니다. 이미 건넨 글은 되돌릴 수 없습니다."),
}
# Providers that never take a standing all-courses consent (D-062 does not apply): each course is allowed by itself.
# The two class-notice readers are text going to OpenAI course by course too (integration review: no "*" via CLI).
PER_COURSE_ONLY = frozenset({"ai_app_link", "codex_notices", "codex_etl_notices"})


def _public() -> bool:
    from lecturehub.core import edition

    return edition.name() == "public"


def named(course: str | None) -> bool:
    """A real course name. Blank and "미분류" are not: a yes there is for one recording and is never stored."""
    name = (course or "").strip()
    return bool(name) and name not in (UNSORTED, ALL_COURSES) and name[0] not in "\u0000\ufffd"  # a menu marker is not a name


def authority_of(course: str, entry: dict) -> str:
    """Explicit authority wins; legacy entries are judged conservatively."""
    if not named(course):
        return INVALID
    if entry.get("authority") in (CONFIRMED, SYNTHETIC, INVALID):
        return entry["authority"]
    return CONFIRMED if str(entry.get("granted_by", "")).startswith("user") else INVALID


class ConsentStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self) -> dict:
        return read_json(self.path, {}) or {}

    def granted(self, provider: str, course: str, *, allow_synthetic: bool = False) -> bool:
        if course == ALL_COURSES and (_public() or provider in PER_COURSE_ONLY):
            return False
        courses = self.entries().get(provider, {})
        entry = courses.get(course)
        if entry is None:
            if provider in PER_COURSE_ONLY:
                return False
            # D-062: the user's standing consent for every named course. Never covers a blank course.
            every = courses.get(ALL_COURSES)
            return not _public() and bool(named(course)) and every is not None and every.get("authority") == CONFIRMED
        authority = authority_of(course, entry)
        return authority == CONFIRMED or (allow_synthetic and authority == SYNTHETIC)

    def grant_all(self, provider: str, at_ms: int, by: str, reason: str) -> None:
        """Standing consent for every named course of this provider (user decision D-062)."""
        if _public():
            raise ValueError("공개판에서는 상시 동의를 저장하지 않습니다. 제공자·과목마다 따로 확인해 주세요.")
        if provider in PER_COURSE_ONLY:
            raise ValueError("이 연결은 과목마다 따로 허용합니다. 모든 과목 상시 동의는 저장하지 않습니다.")
        data = self.entries()
        data.setdefault(provider, {})[ALL_COURSES] = {
            "granted_at": at_ms, "granted_by": by, "notice": NOTICES.get(provider, provider),
            "authority": CONFIRMED, "scope": "all_named_courses", "reason": reason}
        write_json(self.path, data)

    def grant(self, provider: str, course: str, at_ms: int, notice: str, by: str = "user",
              authority: str = CONFIRMED) -> None:
        if not named(course):
            raise ValueError("과목 미지정 녹음에는 동의를 저장하지 않습니다(그 작업에만 적용).")
        if authority not in (CONFIRMED, SYNTHETIC):
            raise ValueError("authority must be confirmed_by_user or synthetic_test")
        data = self.entries()
        data.setdefault(provider, {})[course] = {"granted_at": at_ms, "granted_by": by, "notice": notice,
                                                 "authority": authority}
        write_json(self.path, data)

    def listing(self) -> list[dict]:
        """Every stored consent that counts (settings screen): provider, course, when, by whom."""
        rows = []
        for provider, courses in self.entries().items():
            if not isinstance(courses, dict):
                continue
            for course, entry in courses.items():
                if not isinstance(entry, dict):
                    continue
                standing = course == ALL_COURSES
                if standing:  # D-062 standing consent: never counts in the public edition
                    counts = entry.get("authority") == CONFIRMED and not _public() and provider not in PER_COURSE_ONLY
                else:
                    counts = authority_of(course, entry) == CONFIRMED
                if counts:
                    rows.append({"provider": provider, "course": course, "all_courses": standing,
                                 "granted_at": entry.get("granted_at"), "granted_by": entry.get("granted_by", "")})
        return sorted(rows, key=lambda row: (row["provider"], not row["all_courses"], row["course"]))

    def revoke(self, provider: str, course: str, at_ms: int) -> bool:
        """Take back one stored consent: the app asks again before the next send. Returns False when there was
        none. What was already sent cannot be taken back here; the notice of each service says how to ask them.
        A line is kept in ``consent_revoked.json`` (provider, course, when) next to the store."""
        data = self.entries()
        if not isinstance(data.get(provider), dict) or course not in data[provider]:
            return False
        del data[provider][course]
        if not data[provider]:
            del data[provider]
        write_json(self.path, data)
        log_path = self.path.with_name("consent_revoked.json")
        log = read_json(log_path, []) or []
        log.append({"provider": provider, "course": course, "revoked_at": at_ms})
        write_json(log_path, log if isinstance(log, list) else [log])
        return True

    def audit(self, at_ms: int, *, apply: bool = False) -> list[dict]:
        """Annotate legacy entries that must not count as real course consent (append fields only)."""
        data = self.entries()
        changes = []
        for provider, courses in data.items():
            for course, entry in courses.items():
                if "authority" in entry:
                    continue
                if not named(course):
                    verdict, reason = INVALID, "과목 미지정 동의는 과목 동의가 아님(D-052)"
                elif "합성" in course:
                    verdict, reason = SYNTHETIC, "합성 음성 드라이런을 위해 에이전트가 대리 기록(D-052)"
                else:
                    continue
                changes.append({"provider": provider, "course": course, "authority": verdict, "reason": reason})
                if apply:
                    entry.update(authority=verdict, audited_at=at_ms, audit_reason=reason)
        if apply and changes:
            write_json(self.path, data)
        return changes


def ensure_consent(store: ConsentStore, provider: str, course: str, confirm: Callable[[str], bool],
                   now_ms: int) -> bool:
    """Show the notice and ask once per provider×course. Nothing is uploaded before a yes.

    A blank course is asked every time and never stored.
    """
    if named(course) and store.granted(provider, course):
        return True
    notice = NOTICES.get(provider, f"{provider}로 오디오를 보냅니다.")
    scope = f"과목: {course}" if named(course) else "과목 미지정 — 이번 녹음에만 적용됩니다"
    if not confirm(f"{notice}\n{scope} — 보내도 됩니까?"):
        return False
    if named(course):
        store.grant(provider, course, now_ms, notice)
    return True
