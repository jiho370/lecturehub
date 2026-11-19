"""GSR-018: public bookings require an exact, native-confirmed slot. No live site calls."""
from datetime import datetime

import pytest
import typer
from typer.testing import CliRunner

from lecturehub.booking import browser, cli, store
from lecturehub.booking.service import RoomService
from lecturehub.ui.app import Api

CREW_TEXT = "가나 20200001\n다라 20200002\n마바 20200003"  # typed on the screen for one booking (synthetic)


@pytest.fixture(autouse=True)
def public_edition(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")


@pytest.fixture
def service(tmp_path):
    calls = []
    root = tmp_path / "학기 (합성 예약)"
    root.mkdir()
    svc = RoomService(root, clock=lambda: datetime(2026, 11, 12, 10).astimezone(),
                      runner=lambda action: calls.append(action) or {"ok": True, "bookings": []},
                      start_thread=lambda fn: fn())
    return svc, calls


@pytest.mark.parametrize("hours", [3.5, 6, 12, float("nan"), float("inf"), 0.25])
def test_GSR_018_long_or_invalid_bookings_never_reach_the_browser(service, hours):
    svc, calls = service
    result = svc.book("2026-11-13", "10:00", room="G405", hours=hours, confirmed=True)
    assert not result["ok"] and calls == []


def test_GSR_018_a_preview_cannot_book_without_native_confirmation(service):
    svc, calls = service
    assert svc.state()["auto_return"] is False
    preview = svc.book("2026-11-13", "10:00", room="G405", hours=1.5)
    assert preview["needs_confirmation"] and preview["booking"]["end"] == "11:30" and calls == []
    api = Api(object(), room=svc)
    assert api.room_book("2026-11-13", "10:00", room="G405", companions=CREW_TEXT)["needs_confirmation"]
    assert "3명 이상" in api.room_book("2026-11-13", "10:00", room="G405")["error"]  # nothing typed: asked before any dialog
    assert calls == []


@pytest.mark.parametrize("yes", [False, True])
def test_GSR_018_native_confirmation_names_the_room_and_exact_times(service, yes):
    svc, calls = service
    prompts = []

    class Window:
        def create_confirmation_dialog(self, title, message):
            prompts.append(message)
            return yes

    api = Api(object(), room=svc)
    api._main = Window()
    result = api.room_book("2026-11-13", "10:00", room="G405", hours=1.5, companions=CREW_TEXT)
    assert all(value in prompts[0] for value in ("G405", "2026-11-13", "10:00~11:30"))
    assert bool(result["ok"]) == yes and len(calls) == int(yes)
    if yes:
        assert calls[0]["maxHours"] == 3 and calls[0]["user_confirmed"] is True


def test_GSR_018_adjacent_cached_bookings_cannot_be_used_to_extend_a_chain(service):
    svc, calls = service
    store.save_status(svc.root, [{"id": "1", "status": 2, "start": "2026-11-13 10:00:00",
                                "end": "2026-11-13 13:00:00", "seat": "G405"}], "synthetic", svc.clock())
    result = svc.book("2026-11-13", "13:00", room="G407", hours=1, confirmed=True)
    assert not result["ok"] and "체인" in result["error"] and calls == []


def test_GSR_018_old_rules_are_ignored_and_automation_commands_are_disabled(service, monkeypatch):
    svc, calls = service
    store._write_json(store.folder(svc.root) / "rules.json", {
        "rules": [{"weekday": "금", "start": "10:00", "hours": 6, "enabled": True}], "auto_return": False})
    assert svc.state()["rules"] == [] and max(svc.state()["hours"]) == 3
    assert svc.rules_for(svc.clock().date())["rules"] == [] and svc.due_returns() == []
    assert not svc.save_rules([{"weekday": "금", "start": "10:00"}], True)["ok"]
    assert not svc.book("2026-11-13", "10:00", room="G405", by="task", confirmed=True)["ok"]
    monkeypatch.setattr(cli, "_service", lambda root: svc)
    app = typer.Typer()
    cli.register(app)
    for command in ("due", "rules-for"):
        result = CliRunner().invoke(app, ["gsr", command, "--root", str(svc.root)])
        assert result.exit_code == 2 and "공개판" in result.output
    assert calls == []


def test_GSR_018_chat_returns_a_confirmation_instead_of_booking(service):
    svc, calls = service
    svc._interpreter = lambda *args: {"ok": True, "plan": {
        "intent": "book", "date": "2026-11-13", "start": "10:00", "hours": 2, "room": "G405"}}
    result = svc.chat("가상 예약 지시")
    assert not result["started"] and result["confirm"]["booking"]["room"] == "G405" and calls == []


def test_GSR_018_direct_adapter_calls_still_require_confirmation(service, monkeypatch):
    calls = []
    monkeypatch.setattr(browser.aside_cli, "run", lambda action, **kwargs: calls.append(action))
    for action in ({"action": "book", "hours": 6, "user_confirmed": True}, {"action": "book", "hours": 3}):
        assert not browser.run(action, which="aside")["ok"]
    assert calls == []


def test_GSR_018_site_core_does_not_shorten_a_confirmed_slot_or_start_a_long_chain(tmp_path):
    from test_booking import BOOKING_STUBS, run_core

    result = run_core(tmp_path, BOOKING_STUBS + """
rooms4 = listing(['G416'], 9, 11);
(async () => {
  const short = await GSR.run({ companions: CREW, date: '20261113', start: '0900', hours: 3,
                              room: 'G416', maxHours: 3, user_confirmed: true });
  const long = await GSR.run({ companions: CREW, date: '20261113', start: '0900', hours: 6,
                             room: 'G416', maxHours: 3, user_confirmed: true }).catch(e => ({ error: e.message }));
  console.log(JSON.stringify({ short, long, count: sent.length }));
})();
""", raw_fetch=True)
    assert not result["short"]["ok"] and "3시간" in result["long"]["error"] and result["count"] == 0
