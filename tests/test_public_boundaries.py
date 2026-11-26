"""EDITION-002: excluded features refuse direct calls before touching absent modules, DB or tools."""
import pytest

from lecturehub.ui.app import Api
from lecturehub.ui.backend import Backend
from lecturehub.ui.library import Library


@pytest.fixture(autouse=True)
def public(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")


@pytest.mark.parametrize("method,args", [
    ("qa_status", ()), ("qa_check", ()), ("qa_start", ()), ("qa_packs", ()), ("qa_ask", ()), ("qa_stop", ()),
    ("mai_prepare", ("synthetic",))])
def test_EDITION_002_api_refuses_excluded_features_before_delegate(method, args):
    api = Api(object())
    result = getattr(api, method)(*args)
    assert result["ok"] is False and result["state"] == "disabled"


@pytest.mark.parametrize("method,args", [
    ("qa_status", ()), ("qa_check", ("synthetic",)), ("qa_start", ("synthetic", "synthetic")),
    ("qa_packs", ()), ("qa_ask", ()), ("qa_stop", ()), ("mai_prepare", ("synthetic",)),
    ("_mai_start", ("synthetic", "synthetic", []))])
def test_EDITION_002_backend_refuses_without_database_or_tools(method, args):
    backend = object.__new__(Backend)
    assert getattr(backend, method)(*args)["ok"] is False


def test_EDITION_002_absent_module_helpers_and_native_consent_are_gated():
    api = Api(object())
    for provider in ("claude_assignment", "claude_qa", "mai_playground"):
        assert api.consent_confirm(provider, "synthetic")["ok"] is False
    assert not hasattr(api, "_memorizer")  # the 암기장 is gone in every edition (user 2026-11-25)


def test_EDITION_002_public_edition_has_no_course_board_reader(tmp_path, monkeypatch):
    """The LearningX board is read through its unpublished API, so the public edition leaves it out (user 2026-11-21):
    the export deletes index/etl_board.py, and nothing in the window may import it."""
    import sys

    monkeypatch.setitem(sys.modules, "lecturehub.index.etl_board", None)  # import fails like in the export
    library = object.__new__(Library)
    library.root = tmp_path
    board = library.etl_board()
    assert "posts" not in board and "post_check" not in board
    assert library.etl_board_check()["ok"] is False
    assert library.etl_board_auto() == []
    assert library.etl_board_new() == {"ok": True, "items": []} or library.etl_board_new()["items"] == []
    assert library.etl_board_seen(["lxb:1"])["seen"] == 0


ROOM_CALLS = [
    ("gsr_companions_get", ()), ("gsr_companions_set", (None,)),
    ("gsr_rooms_get", ()), ("gsr_rooms_set", (None,)),
    ("room", ()), ("room_refresh", ()), ("room_book", ("synthetic", "10:00")),
    ("room_return", ("synthetic",)), ("room_cancel", (["synthetic"],)),
    ("room_chat", ("synthetic",)), ("room_chat_confirm", ("book", [], {"day": "synthetic"})),
    ("room_stop", ()), ("room_rules", ([],)),
]


@pytest.fixture
def no_booking_import(monkeypatch):
    import builtins
    import importlib

    original, original_module = builtins.__import__, importlib.import_module

    def guard(name, *args, **kwargs):
        assert not name.startswith("lecturehub.booking"), name
        return original(name, *args, **kwargs)

    def guard_module(name, *args, **kwargs):
        assert not name.startswith("lecturehub.booking"), name
        return original_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(importlib, "import_module", guard_module)


@pytest.mark.parametrize("method,args", ROOM_CALLS)
def test_PUBLIC_GSR_001_api_disabled_before_room_backend_or_dialog(method, args, no_booking_import):
    """D-087: even a supplied service cannot bypass the public boundary."""
    api = Api(object(), room=object())
    api._main = object()
    result = getattr(api, method)(*args)
    assert result["ok"] is False and result["state"] == "disabled"


def test_PUBLIC_GSR_002_every_room_method_is_covered():
    import inspect

    methods = {name for name, _ in inspect.getmembers(Api, inspect.isfunction)
               if name.startswith(("gsr_", "room_")) or name == "room"}
    assert methods == {name for name, _ in ROOM_CALLS}


@pytest.mark.parametrize("method,args", ROOM_CALLS[:4])
def test_PUBLIC_GSR_003_backend_disabled_without_settings_or_booking(method, args, no_booking_import):
    result = getattr(object.__new__(Backend), method)(*args)
    assert result["ok"] is False and result["state"] == "disabled"


def test_PUBLIC_GSR_004_no_startup_service_labs_or_settings_labels(no_booking_import, monkeypatch):
    from lecturehub.core import connections, edition, settings
    from lecturehub.ui.app import open_room

    monkeypatch.setattr(settings, "load", lambda: {"labs": {"gsr": True, "clova": True}})
    assert open_room(object()) is None
    assert edition.has("gsr") is False
    assert settings.labs() == {"clova": True}
    with pytest.raises(KeyError):
        settings.set_lab("gsr", True)
    assert "gsr" not in {row["id"] for row in connections.features({}, [])}
    from lecturehub.core import tools
    monkeypatch.setattr(tools, "status", lambda **kwargs: {})
    monkeypatch.setattr(connections, "status", lambda: [])
    info = object.__new__(Backend).conn_status()
    assert "gsr" not in info["labs"] and "gsr" not in info["lab_names"]
    # Register all public CLI commands without probing the removed package.
    from lecturehub import cli
    cli._register_all()


def test_PUBLIC_GSR_004_full_edition_retains_feature_and_service(monkeypatch, tmp_path):
    from lecturehub.core import edition, settings

    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    monkeypatch.setattr(settings, "load", lambda: {"labs": {"gsr": True}})
    assert edition.has("gsr") and settings.labs()["gsr"]
    assert Api._room_off() is None
    assert object.__new__(Backend)._gsr_prefs_off() is None


def test_D087_tool_list_does_not_name_room_booking_in_the_public_edition(monkeypatch):
    """The first-run tool list said Aside is for "그스룸 예약" in the edition that has no room booking (found in the
    look-around page built from the 1.1.0 public tree, 2026-11-26)."""
    from lecturehub.core import tools

    assert "그스룸" not in tools.status()["aside"]["needed_for"]
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    assert "그스룸" in tools.status()["aside"]["needed_for"]
