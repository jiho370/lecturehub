"""UX-001: appearance persists across app instances in the isolated user settings."""
from lecturehub.core import settings
from lecturehub.ui.backend import Backend


def test_UX_001_appearance_persists_and_rejects_invalid_values():
    first = object.__new__(Backend)
    assert first.appearance() == {"ok": True, "theme": "system", "text_size": "normal"}
    settings.save({"data_root": "synthetic-only"})
    assert first.appearance("dark", "large")["ok"]
    second = object.__new__(Backend)
    assert second.appearance() == {"ok": True, "theme": "dark", "text_size": "large"}
    assert not second.appearance("bad", "normal")["ok"]
    assert second.appearance()["theme"] == "dark"
    assert settings.get("data_root") == "synthetic-only"
