"""그스룸 화면의 뒤쪽: Claude 작업 하나씩 돌리기, 결과를 status/log에 남기기, 앱이 켜져 있을 때 자동반납(P-019).

Claude 한 번에 1분 안팎이 걸리므로 작업은 스레드에서 돌리고 화면은 ``state()``를 다시 부른다.
자동반납은 종료 29분 전에 한다(사용자 2026-11-14: 30분 전부터는 다른 사람이 새로 예약할 수 없으니 그 직후에 비운다).
앱 밖의 예약 작업(같은 29분 전)은 log.jsonl에 반납 기록이 있으면
건너뛰므로 같은 예약을 두 번 반납하지 않는다(반납 API는 예약취소와 같아서 코어가 사용중일 때만 보낸다).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import chat as chat_module
from . import browser, slots, store

AUTO_LEAD_MIN = 29  # user 2026-11-14 (was 7)
BOOK_DAYS = 4  # 오늘 + 3일 전부터 예약 가능(도서관 규정)
ACTIVE = (2, 3)  # 예약완료, 사용중
STATUS_TONE = {2: "wait", 3: "ok", 4: "rec", 5: "done"}


def _parse(text: str | None, tz) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def _label(day: date) -> str:
    return f"{day.month}/{day.day}({store.WEEKDAYS[day.weekday()]})"


class RoomService:
    def __init__(self, root: Path, courses=list, clock=lambda: datetime.now().astimezone(),
                 runner=None, start_thread=None, interpreter=None):
        self.root, self._courses, self.clock = Path(root), courses, clock
        self._stop = threading.Event()  # set by stop_job(); the real runner ends Claude when it sees it
        self._runner = runner or (lambda action: browser.run(action, stop=self._stop))  # chrome or aside per call (tests patch it)
        self._start = start_thread or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._interpreter = interpreter or (lambda *args: chat_module.interpret(*args))  # looked up per call (tests patch it)
        self._talk: list[tuple[str, str]] = []  # the last few chat lines, so a question can be answered in the next one
        self._lock = threading.Lock()
        self._job: dict = {"state": "idle"}
        self._attempted: set[str] = set()

    # ---------------------------------------------------------------- read
    def state(self) -> dict:
        now = self.clock()
        settings = store.load_settings(self.root)
        status = store.load_status(self.root)
        open_min, close_min = store.site_bounds(self.root)
        views = [self._view(b, now, settings["auto_return"]) for b in status["bookings"]]
        active = sorted((v for v in views if v["active"]), key=lambda v: v["start"])
        past = sorted((v for v in views if not v["active"]), key=lambda v: v["start"], reverse=True)[:6]
        days = [now.date() + timedelta(days=i) for i in range(BOOK_DAYS)]
        booked = {v["day"] for v in active}
        self._chains(active)
        classes: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for course in self._courses() or []:
            for weekday, start, end in getattr(course, "slots", []):
                if weekday in store.WEEKDAYS and len(weekday) == 1:
                    classes[store.WEEKDAYS.index(weekday)].append((start, end))
        return {
            "ok": True,
            "job": dict(self._job),
            "updated_at": status["updated_at"], "updated_by": status["by"],
            "active": active, "past": past,
            "rules": settings["rules"], "auto_return": settings["auto_return"],
            "days": [{"date": d.isoformat(), "label": _label(d)} for d in days],
            "starts": [slots.hm(m) for m in range(open_min, close_min - store.STEP_MIN + 1, store.STEP_MIN)],
            "site_hours": status["site_hours"],
            "uses": list(store.USES), "hours": [h for h in store.HOURS if not store.public_mode() or h <= 3],
            "weekdays": list(store.WEEKDAYS), "public": store.public_mode(),
            "suggest": slots.suggestions(days, classes, now, {date.fromisoformat(d) for d in booked}),
            "log": list(reversed(store.read_log(self.root, 12))),
            "auto_lead_min": AUTO_LEAD_MIN, "monitor_rooms": list(store.MONITOR_ROOMS),
            "browser": browser.choice(),  # 화면 문구용: "aside"(기본, D-069) 또는 "chrome"(Claude in Chrome)
        }

    @staticmethod
    def _chains(active: list[dict]) -> None:
        """이어진 예약완료 블록(앞 끝 = 뒤 시작)의 첫 줄에 ``chain``(번호·시간대)을 달아 한 번에 취소할 수 있게 한다."""
        run: list[dict] = []
        for view in active + [None]:
            if view is not None and view["can_cancel"] and run and run[-1]["end"] == view["start"]:
                run.append(view)
                continue
            if len(run) > 1:
                run[0]["chain"] = {"ids": [v["id"] for v in run], "when": f"{run[0]['when'].split(' ')[0]} "
                                   f"{run[0]['when'].split(' ')[1].split('~')[0]}~{run[-1]['when'].split('~')[1]}",
                                   "seat": "·".join(dict.fromkeys(v["seat"] for v in run))}
            run = [view] if view is not None and view["can_cancel"] else []

    def _view(self, b: dict, now: datetime, auto: bool) -> dict:
        start, end = _parse(b.get("start"), now.tzinfo), _parse(b.get("end"), now.tzinfo)
        status = b.get("status")
        active = status in ACTIVE and end is not None and end > now
        view = {"id": str(b.get("id")), "seat": b.get("seat"), "status": status, "status_name": b.get("statusName"),
                "tone": STATUS_TONE.get(status, "done"), "start": b.get("start") or "", "active": active,
                "day": start.date().isoformat() if start else "",
                "when": f"{_label(start.date())} {start:%H:%M}~{end:%H:%M}" if start and end else "",
                "can_return": status == 3 and bool(b.get("returnable")) and active, "auto_at": None,
                "end": b.get("end") or "", "can_cancel": status == 2 and active, "chain": None,
                "monitor": b.get("seat") in store.MONITOR_ROOMS}
        if active and auto and end:
            done = store.return_attempted(self.root, view["id"])
            view["auto_at"] = "반납 시도함" if done else f"{end - timedelta(minutes=AUTO_LEAD_MIN):%H:%M} 자동반납"
        return view

    # ---------------------------------------------------------------- actions
    def refresh(self, by: str = "app") -> dict:
        return self._begin("예약 목록 확인", {"action": "list"}, by)

    def book(self, day: str, start: str, use: str = "과제", room: str | None = None, by: str = "app",
             hours=3, *, confirmed: bool = False, companions: list | None = None) -> dict:
        """``companions``: ``[[이름, 학번], ...]`` typed for this booking. Without them a booking from the app or the
        chat only asks for confirmation (the screen then sends them); they go into the action, never into the log."""
        now = self.clock()
        open_min, close_min = store.site_bounds(self.root)
        try:
            hours = store.clean_hours(hours)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            target = date.fromisoformat(str(day))
        except ValueError:
            return {"ok": False, "error": "날짜를 고르세요"}
        if not 0 <= (target - now.date()).days < BOOK_DAYS:
            return {"ok": False, "error": "오늘부터 3일 뒤까지만 예약할 수 있습니다"}
        if not store.valid_start(start, open_min, close_min):
            return {"ok": False, "error": store.start_error(open_min, close_min)}
        if target == now.date() and slots.to_min(start) < now.hour * 60 + now.minute:
            return {"ok": False, "error": "이미 지난 시각입니다"}
        if not store.fits_day(start, hours, close_min):
            return {"ok": False, "error": f"{start}부터 {store.hours_text(hours)}은 {slots.hm(close_min)}을 넘습니다"}
        if use not in store.USES:
            return {"ok": False, "error": "용도를 고르세요"}
        from lecturehub.core import settings

        if companions is not None and len(companions) < settings.MIN_COMPANIONS:
            return {"ok": False, "error": f"함께 쓰는 사람을 나를 빼고 {settings.MIN_COMPANIONS}명 이상 '이름 학번'으로 적어 주세요"
                                          + (f" (지금 {len(companions)}명)" if companions else "")}
        if store.public_mode():
            if by == "task":
                return {"ok": False, "error": "공개판에서는 자동 예약을 실행하지 않습니다"}
            if not room or len(str(room)) != 4 or not str(room).startswith("G") or not str(room)[1:].isdigit():
                return {"ok": False, "error": "예약할 방 번호를 고르세요(예: G405)"}
            begin = datetime.combine(target, datetime.strptime(start, "%H:%M").time(), tzinfo=now.tzinfo)
            end = begin + timedelta(minutes=int(hours * 60))
            ranges = [(_parse(b.get("start"), now.tzinfo), _parse(b.get("end"), now.tzinfo))
                      for b in store.load_status(self.root)["bookings"] if b.get("status") in ACTIVE]
            while True:
                joined = [(a, b) for a, b in ranges if a and b and a <= end and b >= begin]
                if not joined:
                    break
                begin, end = min(begin, *(a for a, _ in joined)), max(end, *(b for _, b in joined))
                ranges = [span for span in ranges if span not in joined]
            if end - begin > timedelta(hours=3):
                return {"ok": False, "error": "기존 예약에 이어 3시간을 넘는 체인 예약은 할 수 없습니다"}
            if not confirmed:
                return {"ok": False, "needs_confirmation": True, "error": "방과 시각을 확인한 뒤 예약해 주세요",
                        "booking": {"day": day, "start": start, "end": slots.hm(slots.to_min(start) + int(hours * 60)),
                                    "use": use, "room": str(room), "hours": hours}}
        if companions is None and by == "chat" and not self._saved_companions():
            return {"ok": False, "needs_confirmation": True, "needs_companions": True,
                    "error": "예약 화면의 '함께 쓰는 사람'에 이름과 학번을 적은 뒤 확인을 눌러 주세요",
                    "booking": {"day": day, "start": start, "end": slots.hm(slots.to_min(start) + int(hours * 60)),
                                "use": use, "room": str(room or ""), "hours": hours}}
        action = {"action": "book", "date": target.strftime("%Y%m%d"), "start": start.replace(":", ""), "useDesc": use}
        if companions:
            action["companions"] = [[str(n), str(no)] for n, no in companions]
        if store.public_mode():
            action.update(user_confirmed=True, maxHours=3)
        if room:
            action["room"] = str(room)
        if hours != 3:
            action["hours"] = hours
        return self._begin(f"{_label(target)} {start} {store.hours_text(hours)} 예약", action, by)

    def return_now(self, booking_id: str, by: str = "app", skip_if_done: bool = False) -> dict:
        if not str(booking_id).isdigit():
            return {"ok": False, "error": "예약 번호가 이상합니다"}
        if skip_if_done and store.return_attempted(self.root, booking_id):
            return {"ok": True, "skipped": True, "why": "이미 반납을 시도한 예약입니다(로그)"}
        return self._begin("반납", {"action": "return", "id": str(booking_id)}, by)

    def cancel(self, ids, by: str = "app") -> dict:
        """예약완료인 예약을 취소한다(사용중이면 반납). 한 번의 Claude 실행으로 여러 건(이어진 블록)을 취소할 수 있다."""
        if isinstance(ids, (str, int)):
            ids = str(ids).split(",")
        ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if not ids or len(ids) > 8 or not all(i.isdigit() for i in ids) or len(set(ids)) != len(ids):
            return {"ok": False, "error": "취소할 예약 번호가 이상합니다"}
        known = {str(b.get("id")): b for b in store.load_status(self.root)["bookings"]}
        for i in ids:  # the core checks again against the live list; this only spares a minute of Claude
            if i in known and known[i].get("status") != 2:
                return {"ok": False, "error": f"{known[i].get('seat')} 예약은 {known[i].get('statusName')} 상태라 취소할 수 없습니다"
                                              + (" (사용중이면 반납을 쓰세요)" if known[i].get("status") == 3 else "")}
        label = "예약 취소" if len(ids) == 1 else f"이어진 예약 {len(ids)}건 취소"
        return self._begin(label, {"action": "cancel", "ids": ids}, by)

    # ---------------------------------------------------------------- chat (the user tells the screen what to do)
    def chat(self, text: str, by: str = "chat") -> dict:
        """한 문장 지시를 해석해 실행한다. 예약·목록 확인은 바로, 취소·반납은 ``confirm``을 돌려줘 사용자가 한 번 더 누르게 한다.

        ``ok`` False는 해석 자체가 안 된 경우(Claude 없음·실패), 그 밖의 말은 ``say``에 담아 ``ok`` True로 돌려준다."""
        text = " ".join(str(text or "").split())[:300]
        if not text:
            return {"ok": False, "error": "지시를 입력하세요"}
        state = self.state()
        got = self._interpreter(text, self.clock(), state["active"], state["days"], list(self._talk))
        if not got.get("ok"):
            return {"ok": False, "error": got.get("error") or "지시를 해석하지 못했습니다"}
        reply = self._apply_plan(got.get("plan") or {}, state, by)
        self._talk = (self._talk + [("me", text), ("bot", reply.get("say", ""))])[-6:]
        store.append_log(self.root, {"ts": self.clock().isoformat(timespec="seconds"), "by": by, "kind": "chat", "ok": True,
                                     "detail": text[:80], "intent": reply.get("intent")})
        return reply

    def confirm(self, intent: str, ids, by: str = "chat") -> dict:
        """chat이 묻고 사용자가 확인한 취소·반납을 실행한다(번호는 지금 목록으로 다시 확인)."""
        ids = [str(i) for i in ids or []]
        active = {v["id"]: v for v in self.state()["active"]}
        if intent == "cancel":
            if not ids or any(i not in active or not active[i]["can_cancel"] for i in ids):
                return {"ok": False, "error": "취소할 수 있는 예약이 아닙니다. 목록을 다시 확인하세요"}
            return self.cancel(ids, by)
        if intent == "return":
            if len(ids) != 1 or ids[0] not in active or not active[ids[0]]["can_return"]:
                return {"ok": False, "error": "지금 반납할 수 있는 예약이 아닙니다"}
            return self.return_now(ids[0], by)
        return {"ok": False, "error": "알 수 없는 작업입니다"}

    def _apply_plan(self, plan: dict, state: dict, by: str) -> dict:
        intent = plan.get("intent")
        active = {v["id"]: v for v in state["active"]}
        ask = str(plan.get("say") or "").strip()[:200]
        if intent == "list":
            started = self.refresh(by)
            return {"ok": True, "intent": "list", "started": bool(started.get("ok")),
                    "say": "예약 목록을 확인합니다." if started.get("ok") else started.get("error", "")}
        if intent == "book":
            try:
                target = date.fromisoformat(str(plan.get("date")))
                hours = store.clean_hours(plan.get("hours"))
            except ValueError as exc:
                return {"ok": True, "intent": "clarify", "say": ask or str(exc) or "날짜와 시간을 다시 말해 주세요."}
            start = str(plan.get("start") or "")
            use = plan.get("use") if plan.get("use") in store.USES else "과제"
            room = str(plan.get("room") or "")
            room = room if len(room) == 4 and room[0] == "G" and room[1:].isdigit() else None
            started = self.book(target.isoformat(), start, use, room, by, hours)
            if started.get("needs_confirmation"):
                b = started["booking"]
                return {"ok": True, "intent": "book", "started": False,
                        "say": f"{b['day']} {b['room'] + ' ' if b['room'] else ''}{b['start']}~{b['end']} 예약입니다. "
                               "아래 '함께 쓰는 사람' 칸에 이름과 학번을 적고 [예약 확인]을 눌러 주세요.",
                        "confirm": {"intent": "book", "ids": [], "booking": b}}
            if not started.get("ok"):
                return {"ok": True, "intent": "book", "started": False, "say": started.get("error", "예약을 시작하지 못했습니다")}
            end = slots.hm(slots.to_min(start) + round(hours * 60))
            return {"ok": True, "intent": "book", "started": True,
                    "say": f"{_label(target)} {start}~{end} {store.hours_text(hours)} 예약을 시작합니다{f' ({room})' if room else ''}. 1분쯤 걸립니다."}
        if intent in ("cancel", "return"):
            ids = [str(i) for i in plan.get("ids") or []]
            usable = [i for i in ids if i in active and active[i]["can_cancel" if intent == "cancel" else "can_return"]]
            if not ids or len(usable) != len(ids) or (intent == "return" and len(ids) != 1):
                what = "취소할 수 있는(예약완료)" if intent == "cancel" else "반납할 수 있는(사용중)"
                return {"ok": True, "intent": "clarify", "say": f"{what} 예약을 찾지 못했습니다. 날짜와 시간으로 다시 말해 주세요."}
            rows = ", ".join(f"{active[i]['seat']} {active[i]['when']}" for i in ids)
            verb = "취소할까요? 방이 다른 사람에게 열립니다." if intent == "cancel" else "반납할까요? 지금 반납하면 예약이 끝납니다."
            return {"ok": True, "intent": intent, "started": False, "say": f"{rows} 예약을 {verb}",
                    "confirm": {"intent": intent, "ids": ids}}
        return {"ok": True, "intent": "clarify", "started": False,
                "say": ask or "무슨 말씀인지 잘 모르겠어요. 날짜·시각·길이를 같이 말해 주세요(예: 내일 2시부터 6시간)."}

    def stop_job(self) -> dict:
        """돌고 있는 Claude 작업을 중지한다. 사이트에 요청이 이미 나갔다면 되돌려지지 않으므로 결과에 목록 확인을 안내한다."""
        with self._lock:
            if self._job.get("state") != "running":
                return {"ok": False, "error": "중지할 그스룸 작업이 없습니다"}
            self._stop.set()
            self._job = {**self._job, "stopping": True}
        return {"ok": True}

    @staticmethod
    def _saved_companions() -> bool:
        """Full build with enough names saved on this PC: a chat booking goes ahead like the owner's app did."""
        from lecturehub.core import edition, settings

        return edition.has("gsr_prefs") and len(settings.gsr_companions()) >= settings.MIN_COMPANIONS

    def due_returns(self, lead_min: int = AUTO_LEAD_MIN) -> list[dict]:
        """반납 작업을 걸어야 할 예약: 예약완료·사용중이고, 끝나기 lead_min+1분 전보다 뒤에 끝나며, 아직 반납 시도가 없는 것."""
        if store.public_mode():
            return []
        now = self.clock()
        out = []
        for b in store.load_status(self.root)["bookings"]:
            end = _parse(b.get("end"), now.tzinfo)
            if b.get("status") in ACTIVE and end and end - timedelta(minutes=lead_min + 1) > now \
                    and not store.return_attempted(self.root, str(b.get("id"))):
                out.append({"id": str(b.get("id")), "seat": b.get("seat"), "start": b.get("start"), "end": b.get("end"),
                            "return_at": (end - timedelta(minutes=lead_min)).isoformat(timespec="seconds")})
        return out

    def rules_for(self, day: date) -> dict:
        """정기 예약: 그날 요일의 켜진 규칙, 그날 이미 있는 예약(있으면 정기 예약은 건너뜀 — 예약 작업이 하루 두 번 돌아도 겹치지 않게)."""
        weekday = store.WEEKDAYS[day.weekday()]
        if store.public_mode():
            return {"date": day.isoformat(), "weekday": weekday, "rules": [], "existing": [], "disabled": True}
        rules = [r for r in store.load_settings(self.root)["rules"] if r.get("enabled") and r.get("weekday") == weekday]
        existing = [b for b in store.load_status(self.root)["bookings"]
                    if b.get("status") in ACTIVE and str(b.get("start", "")).startswith(day.isoformat())]
        for entry in store.read_log(self.root):  # a booking the user cancelled by hand must not be booked again by the rule
            if entry.get("by") == "task":
                continue
            for seat, start, _end, booking_id in store.cancelled_rows(entry):
                if str(start).startswith(day.isoformat()):
                    existing.append({"id": str(booking_id), "seat": seat, "start": f"{start}:00", "status": 0,
                                     "statusName": "사용자가 취소함 — 정기 예약은 다시 잡지 않음"})
        return {"date": day.isoformat(), "weekday": weekday, "rules": rules, "existing": existing}

    def save_rules(self, rules: list[dict], auto_return: bool) -> dict:
        try:
            return {"ok": True, **store.save_settings(self.root, list(rules), auto_return)}
        except (ValueError, TypeError, AttributeError) as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------------------- automatic return (app open)
    def tick(self) -> dict | None:
        now = self.clock()
        if not store.load_settings(self.root)["auto_return"] or self._job.get("state") == "running":
            return None
        for b in store.load_status(self.root)["bookings"]:
            end = _parse(b.get("end"), now.tzinfo)
            booking_id = str(b.get("id"))
            if b.get("status") not in ACTIVE or end is None or booking_id in self._attempted:
                continue
            if end - timedelta(minutes=AUTO_LEAD_MIN) <= now < end - timedelta(minutes=1) \
                    and not store.return_attempted(self.root, booking_id):
                self._attempted.add(booking_id)
                return self._begin(f"자동반납 {b.get('seat')}", {"action": "return", "id": booking_id}, "app-auto")
        return None

    def auto_loop(self, interval_s: float = 30) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001, S110 - a failed check only delays the next one
                pass
            time.sleep(interval_s)

    # ---------------------------------------------------------------- job plumbing
    def _begin(self, label: str, action: dict, by: str = "app") -> dict:
        with self._lock:
            if self._job.get("state") == "running":
                return {"ok": False, "error": "Claude가 다른 그스룸 작업을 하는 중입니다. 끝나면 다시 누르세요"}
            self._stop.clear()
            self._job = {"state": "running", "label": label, "action": action["action"],
                         "started_at": self.clock().isoformat(timespec="seconds")}

        def work():
            try:
                result = self._runner(action)
            except Exception as exc:  # noqa: BLE001 - the screen must leave the running state
                result = {"ok": False, "error": str(exc)}
            self._record(action, result, by)
            with self._lock:
                self._job = {"state": "done" if result.get("ok") else "stopped" if result.get("stopped") else "failed",
                             "label": label,
                             "action": action["action"], "result": result,
                             "finished_at": self.clock().isoformat(timespec="seconds")}

        self._start(work)
        return {"ok": True}

    def _record(self, action: dict, result: dict, by: str) -> None:
        now = self.clock()
        if isinstance(result.get("bookings"), list):
            store.save_status(self.root, result["bookings"], by, now, store.clean_site_hours(result.get("site_hours")))
        entry = {"ts": now.isoformat(timespec="seconds"), "by": by, "kind": action["action"],
                 "ok": bool(result.get("ok"))}
        for key in ("id", "room", "date", "start", "end", "hours", "partial", "skipped", "stopped", "cancelled"):
            value = result.get(key, action.get(key))
            if value not in (None, "", []):
                entry[key] = value
        if action.get("ids") and "cancelled" not in entry:
            entry["ids"] = action["ids"]
        detail = result.get("why") or result.get("error")
        if detail:
            entry["detail"] = detail
        if result.get("login_required"):
            entry["login_required"] = True
        for key in ("cost_usd", "browser", "seconds"):
            if result.get(key) is not None:
                entry[key] = result[key]
        store.append_log(self.root, entry)
