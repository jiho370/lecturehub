"""시간표에서 3시간 공강 찾기(그스룸 예약 추천). 순수 함수 — 시간은 자정 기준 분."""
from __future__ import annotations

from datetime import date, datetime

OPEN_MIN, CLOSE_MIN = 9 * 60, 23 * 60
DUR_MIN = 180
BUFFER_MIN = 10  # 수업 끝나고 이동, 다음 수업 전 이동


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def to_min(text: str) -> int:
    h, m = text.strip().split(":")[:2]
    return int(h) * 60 + int(m)


def free_starts(busy: list[tuple[int, int]], earliest: int = OPEN_MIN) -> list[dict]:
    """빈 구간마다 3시간이 들어가는 가장 이른 30분 단위 시작 하나씩."""
    blocks = sorted((max(OPEN_MIN, s - BUFFER_MIN), min(CLOSE_MIN, e + BUFFER_MIN)) for s, e in busy if e > s)
    gaps, cursor = [], OPEN_MIN
    for s, e in blocks:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < CLOSE_MIN:
        gaps.append((cursor, CLOSE_MIN))
    out = []
    for g0, g1 in gaps:
        start = max(g0, earliest)
        start += (-start) % 30  # 30분 단위로 올림
        if start + DUR_MIN <= g1:
            out.append({"start": hm(start), "end": hm(start + DUR_MIN), "gap": f"{hm(g0)}~{hm(g1)}"})
    return out


def suggestions(days: list[date], classes_by_weekday: dict[int, list[tuple[str, str]]], now: datetime,
                booked_days: set[date]) -> list[dict]:
    """예약 가능한 날(오늘~3일 뒤)마다 공강 3시간 후보. 이미 예약이 있는 날은 건너뛴다(하루 3시간)."""
    out = []
    for day in days:
        if day in booked_days:
            continue
        busy = []
        for start, end in classes_by_weekday.get(day.weekday(), []):
            try:
                busy.append((to_min(start), to_min(end)))
            except (ValueError, IndexError):
                continue
        earliest = OPEN_MIN
        if day == now.date():
            earliest = max(OPEN_MIN, now.hour * 60 + now.minute + 15)
        for window in free_starts(busy, earliest):
            out.append({"date": day.isoformat(), **window})
    return out
