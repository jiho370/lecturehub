"""AI app link (D-072): material page text prepared in the background, and search over transcripts with recordings.

Synthetic data only. Slide PDFs are written with the app's own pypdfium2 writer (materials/pdfdoc.py).
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from test_mcp_access import ALLOWED, HIDDEN, build_library, call

import lecturehub
from lecturehub.core import db, settings
from lecturehub.materials import background

pytest.importorskip("pypdfium2")


def _real_materials(root: Path) -> None:
    """Swap the fixture's placeholder materials for real slide PDFs (and one HWP without text); no page text yet."""
    from lecturehub.materials.pdfdoc import write_slides

    allowed = root / ALLOWED / "강의자료" / "1주차 관수로.pdf"
    hidden = root / HIDDEN / "강의자료" / "숨김.pdf"
    hwp = root / ALLOWED / "강의자료" / "안내문.hwp"
    write_slides(allowed, [("Darcy Weisbach", ["head loss friction factor"]), ("Example", ["Reynolds 2000 laminar"])])
    write_slides(hidden, [("Secretslide QZK", ["hidden course slide"])])
    hwp.write_bytes(b"synthetic hwp")
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        c.execute("DELETE FROM MaterialPage")
        for mid, path in (("ma", allowed), ("mb", hidden)):
            c.execute("UPDATE Material SET sha256 = ? WHERE id = ?", (hashlib.sha256(path.read_bytes()).hexdigest(), mid))
        c.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES "
                  "('mh','ca',?,?, 'hwp','안내문',0)", (str(hwp), hashlib.sha256(hwp.read_bytes()).hexdigest()))
        c.commit()


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    _real_materials(root)
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "full")
    src = str(Path(lecturehub.__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", src)  # the helper child imports this tree
    return root


class _Backend:
    def __init__(self, root, phase="idle"):
        self.root, self.needs_setup, self.phase = root, False, phase

    def state(self):
        return {"phase": self.phase}

    def ai_link_enable(self, on):
        return {"ok": True, **settings.set_ai_link(on)}


def _api(root, phase="idle"):
    from lecturehub.ui.app import Api

    return Api(_Backend(root, phase), type("L", (), {"root": root})())


def _pages(root: Path) -> dict[str, int]:
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        return dict(c.execute("SELECT material_id, count(*) FROM MaterialPage GROUP BY material_id"))


def test_MCP_MAT_001_switching_on_prepares_page_text_and_the_tools_show_it_for_the_allowed_course_only(root, monkeypatch):
    from lecturehub.core import proc

    monkeypatch.setattr(proc, "console_python", lambda: sys.executable)
    api = _api(root)
    assert api._material_text_kick() == "off"  # the link is off: nothing is started
    settings.set_ai_link(True)
    assert api._material_text_kick() == "started"
    assert api._material_text_kick() == "running"  # one at a time
    assert api._material_proc.wait(timeout=120) == 0
    assert _pages(root) == {"ma": 2, "mb": 1}
    tried = json.loads(background.state_path(root).read_text(encoding="utf-8"))["materials"]
    assert set(tried) == {"mh"}  # the HWP gave no text and is written down
    assert api._material_text_kick() == "nothing"  # no tight loop: nothing new to do
    found, _, _ = call("search", {"query": "Weisbach"})
    assert [(r["kind"], r["material_id"], r["page"]) for r in found["results"]] == [("material", "ma", 1)]
    material, _, _ = call("get_material", {"material_id": "ma"})
    assert [p["page"] for p in material["pages"]] == [1, 2] and "Reynolds" in material["pages"][1]["text"]
    hidden, _, raw = call("search", {"query": "Secretslide"})
    assert hidden["count"] == 0 and "QZK" not in raw
    assert call("get_material", {"material_id": "mb"})[1] is True


def test_MCP_MAT_002_never_started_while_recording(root, monkeypatch):
    from lecturehub.core import proc

    started = []
    monkeypatch.setattr(proc, "console_python", lambda: started.append(1) or sys.executable)
    settings.set_ai_link(True)
    assert _api(root, phase="recording")._material_text_kick() == "recording" and started == []
    # the helper itself also refuses when a recorder holds its session lock (a recording outside the app)
    from lecturehub.recorder.heartbeat import SessionLock

    session = root / "_lecturehub" / "recordings" / "20260907_090000_mic"
    session.mkdir(parents=True)
    with SessionLock(session):
        assert background.run(root) == {"state": "recording"}
    assert _pages(root) == {}


def test_MCP_MAT_003_a_file_that_gave_no_text_is_tried_again_only_when_it_changes(root):
    first = background.run(root)
    assert first["state"] == "done" and first["materials"] == 2 and first["no_text"] == 1
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert background.pending(c, root) == []
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        c.execute("UPDATE Material SET sha256 = 'other' WHERE id = 'mh'")  # as if re-indexed with new bytes
        c.commit()
    with closing(db.connect_readonly(root / "_lecturehub" / "lecturehub.sqlite")) as c:
        assert background.pending(c, root) == ["mh"]


def test_MCP_MAT_004_the_mcp_process_does_not_parse_files(root):
    """With no page text stored, get_material says the app is still preparing; nothing is extracted by the call."""
    settings.set_ai_link(True)
    material, error, _ = call("get_material", {"material_id": "ma"})
    assert not error and material["pages"] == [] and "준비하는 중" in material["message"]
    assert _pages(root) == {}


def test_MCP_SEARCH_transcripts_with_a_recording_are_found_and_hidden_recordings_are_not(root):
    """Review finding 2026-11-24: with no hidden recordings, NOT IN (NULL) excluded every transcript with media."""
    settings.set_ai_link(True)
    database = root / "_lecturehub" / "lecturehub.sqlite"
    with closing(db.connect(database)) as c:
        media = ("INSERT INTO MediaAsset VALUES (?, ?, ?, ?, 60000, 'aac', 48000, 1, 9, 'app_mic', 'laptop', 1, NULL, "
                 "NULL, 123, 0, '[]', 'user', 'exact')")
        c.execute(media, ("mda", "a1", str(root / "_lecturehub" / "a1.mp3"), "sha-mda"))
        c.execute("INSERT INTO Transcript (id,lecture_id,media_id,engine,role,role_decided_by,status,source_file,sha256) "
                  "VALUES ('tm','a1','mda','mai','timeline','policy','done',NULL,'tm')")
        c.execute("INSERT INTO TranscriptSegment (transcript_id,seq,start_ms,text,chunk_index) "
                  "VALUES ('tm',0,5000,'관로 유량 측정은 벤투리미터로 합니다',0)")
        c.commit()
    found, _, _ = call("search", {"query": "벤투리미터"})
    assert [(r["lecture_id"], r["engine"], r["in_use"]) for r in found["results"]] == [("a1", "MAI", True)]
    found, _, _ = call("search", {"query": "유량"})  # short query: the LIKE path
    assert found["count"] == 1
    # now that recording is thrown away (D-058): its transcript is hidden everywhere, as in the app
    with closing(db.connect(database)) as c:
        c.execute("INSERT INTO Evidence (id, subject_type, subject_id, field, kind, detail, weight, source, created_at) "
                  "VALUES ('ev-gone', 'MediaAsset', 'mda', NULL, 'discarded', '{}', NULL, 'user', ?)",
                  (int(time.time() * 1000),))
        c.commit()
    assert call("search", {"query": "벤투리미터"})[0]["count"] == 0
    transcript, _, _ = call("get_transcript", {"lecture_id": "a1"})
    assert transcript["engine"] == "Google" and "MAI" not in transcript["available_engines"]
    hidden_engine, _, _ = call("get_transcript", {"lecture_id": "a1", "engine": "MAI"})
    assert hidden_engine["count"] == 0 and "관로 유량" not in hidden_engine.get("text", "")
    lectures, _, _ = call("list_lectures", {"course": ALLOWED})
    first = next(x for x in lectures["lectures"] if x["lecture_id"] == "a1")
    assert first["transcript"]["engine"] == "Google" and "other_transcripts" not in first  # empty lists are left out
    assert call("search", {"query": "다르시"})[0]["count"] == 1  # the transcript without media is still found
