"""Per-chunk transcript QA (§8.4): TQA-001~003, empty chunk, density.

Input paragraphs carry chunk-local start times in ms.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass

EMPTY_TEXT_CHARS = 20            # transcribe.ps1: < 20 chars → empty result
COVERAGE_GAP_MS = 180_000        # last paragraph starts > 3 min before chunk end → early stop suspected
MONOTONIC_SLACK_MS = 5_000       # a start may exceed the chunk length by encoder padding only
MIN_CHARS_PER_MINUTE = 100       # transcribe.ps1 truncation heuristic (chunks ≥ 3 min)
SHORT_MIN_CHARS_PER_MINUTE = 40  # 1–3 min chunks (2026-10-19: 2.5 min of German as "ko" gave 62 chars and passed)


@dataclass(frozen=True)
class QAIssue:
    check: str   # monotonic | coverage | tail_repetition | empty_chunk | density
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def check_chunk(paragraphs: list[tuple[int, str]], duration_ms: int, *, tail_lines: int = 40) -> list[QAIssue]:
    issues: list[QAIssue] = []
    text = "\n".join(body for _, body in paragraphs).strip()
    if len(text) < EMPTY_TEXT_CHARS:
        return [QAIssue("empty_chunk", "전사 결과가 비어 있음 — 소리가 작거나 말이 없는 구간일 수 있음")]
    starts = [start for start, _ in paragraphs]
    backwards = [i for i in range(1, len(starts)) if starts[i] < starts[i - 1]]
    beyond = [s for s in starts if s > duration_ms + MONOTONIC_SLACK_MS]
    if backwards or beyond:
        issues.append(QAIssue("monotonic", f"시각 역행 {len(backwards)}곳, 조각 길이 초과 {len(beyond)}곳"))
    minutes = duration_ms / 60_000
    if minutes >= 3 and duration_ms - max(starts) > COVERAGE_GAP_MS:
        issues.append(QAIssue("coverage", f"마지막 문단 시작이 조각 끝보다 {(duration_ms - max(starts)) // 1000}초 앞 "
                                          "— 조기 종료 의심"))
    floor = MIN_CHARS_PER_MINUTE if minutes >= 3 else SHORT_MIN_CHARS_PER_MINUTE if minutes >= 1 else 0
    if floor and len(text) / minutes < floor:
        issues.append(QAIssue("density", f"분당 {int(len(text) / minutes)}자 — 잘렸거나, 음성 언어가 전사 언어 설정과 "
                                         "다를 수 있음(--language)"))
    sentences = [s.strip() for s in re.split(r"(?<=[.?!])\s+|\n", text) if len(s.strip()) >= 8]
    repeated = [s for s, n in Counter(sentences[-tail_lines:]).items() if n >= 2]
    if len(repeated) >= 2:
        issues.append(QAIssue("tail_repetition", f"끝 {tail_lines}문장 중 되풀이 {len(repeated)}종 — 반복 루프 의심"))
    else:
        # Short-token loops ("음. 음. 음. …", L-002) slip past the sentence check above (sentences < 8 chars
        # are skipped): look for one token repeated many times in a row.
        token, run = _longest_token_run(text)
        if run >= TOKEN_RUN_LIMIT:
            issues.append(QAIssue("tail_repetition", f"같은 말 '{token[:10]}'이 {run}번 연속 — 반복 환각 의심"))
    return issues


TOKEN_RUN_LIMIT = 20


def _longest_token_run(text: str) -> tuple[str, int]:
    best, best_run, previous, run = "", 0, None, 0
    for token in re.findall(r"\S+", text):
        run = run + 1 if token == previous else 1
        previous = token
        if run > best_run:
            best, best_run = token, run
    return best, best_run
