"""Daily request ledger (_lecturehub/quota.json), QUOTA-001, D-054.

Two separate budgets:
- provider: the Gemini free-tier daily limit resets at midnight Pacific time and
  is counted per project (this app's key), so days are counted in
  America/Los_Angeles.
- personal: the user's own daily budget, counted in local days.
A request is allowed only when both have room. A provider "per day" 429 marks
the provider day as used up whatever our count says.

File format v2: {"version": 2, "providers": {name: {"pt": {day: n}, "local": {day: n}}}}.
A v1 file ({name: {local_day: n}}) is read as personal (local) counts.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import write_json

PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaWait(Exception):
    """No requests left; the job should wait (not fail)."""

    def __init__(self, next_eligible_at: int, basis: str = "provider_pt_midnight", remaining_chunks: int | None = None):
        super().__init__("일일 요청 한도에 도달했습니다.")
        self.next_eligible_at = next_eligible_at
        self.basis = basis
        # The provider reset rule is decided (D-054); the provider may still differ in practice.
        self.estimated = basis != "provider_pt_midnight"
        self.remaining_chunks = remaining_chunks


def _next_midnight_ms(now: datetime, zone) -> int:
    local = now.astimezone(zone)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return int(midnight.timestamp() * 1000)


class QuotaLedger:
    def __init__(self, path: Path, provider: str, requests_per_day: int, personal_budget: int | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        if callable(personal_budget):  # backwards-compatible positional clock
            clock, personal_budget = personal_budget, None
        self.path, self.provider, self.limit, self.clock = Path(path), provider, requests_per_day, clock
        self.personal_budget = personal_budget if personal_budget is not None else requests_per_day

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            data = {}
        if data.get("version") == 2:
            return data
        providers = {name: {"pt": {}, "local": dict(days)} for name, days in data.items() if isinstance(days, dict)}
        return {"version": 2, "providers": providers}

    def _days(self, data: dict) -> dict:
        return data["providers"].setdefault(self.provider, {"pt": {}, "local": {}})

    def _pt_day(self) -> str:
        return self.clock().astimezone(PACIFIC).date().isoformat()

    def _local_day(self) -> str:
        return self.clock().date().isoformat()

    def used(self) -> int:
        """Requests counted against the provider's Pacific-time day."""
        return int(self._days(self._load())["pt"].get(self._pt_day(), 0))

    def used_personal(self) -> int:
        return int(self._days(self._load())["local"].get(self._local_day(), 0))

    def remaining(self) -> int:
        return max(0, min(self.limit - self.used(), self.personal_budget - self.used_personal()))

    def next_eligible_at(self) -> int:
        return _next_midnight_ms(self.clock(), PACIFIC)

    def check(self) -> None:
        if self.used() >= self.limit:
            raise QuotaWait(_next_midnight_ms(self.clock(), PACIFIC), "provider_pt_midnight")
        if self.used_personal() >= self.personal_budget:
            raise QuotaWait(_next_midnight_ms(self.clock(), self.clock().tzinfo), "personal_local_midnight")

    def record(self, count: int = 1) -> None:
        data = self._load()
        days = self._days(data)
        for key, day in (("pt", self._pt_day()), ("local", self._local_day())):
            days[key][day] = int(days[key].get(day, 0)) + count
        write_json(self.path, data)

    def mark_exhausted(self) -> None:
        """The provider said the day is used up, whatever our count says."""
        data = self._load()
        days = self._days(data)
        days["pt"][self._pt_day()] = max(self.limit, int(days["pt"].get(self._pt_day(), 0)))
        write_json(self.path, data)


def summary(path: Path, provider: str, requests_per_day: int, personal_budget: int | None = None,
            clock=lambda: datetime.now().astimezone()) -> dict:
    """What is left today, for display (reads the ledger only). Counts only requests this app sent."""
    ledger = QuotaLedger(path, provider, requests_per_day, personal_budget, clock)
    now = clock()
    return {
        "provider": provider, "limit": ledger.limit, "used": ledger.used(),
        "personal_budget": ledger.personal_budget, "used_personal": ledger.used_personal(),
        "remaining": ledger.remaining(),
        "provider_reset_at": _next_midnight_ms(now, PACIFIC),  # D-054: Pacific midnight
        "personal_reset_at": _next_midnight_ms(now, now.tzinfo),
    }
