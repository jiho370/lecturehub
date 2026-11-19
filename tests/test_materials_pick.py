"""S3: this class's material and page by rules, with a reason; user picks feed the next pick (D-056)."""
import os
import time
from contextlib import closing
from datetime import date, datetime

import pytest

from lecturehub.core import db
from lecturehub.core.ids import new_ulid
from lecturehub.index import materials_pick as mp

H = 3_600_000
PAGES = {"Week 4 Viscous flow": 20, "Week 5 Pipe friction": 36, "Week 6 Pipe system": 30}


def pages(path):
    return next((n for stem, n in PAGES.items() if stem in path), None)


@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "구조역학"
    folder.mkdir()
    with closing(db.connect(tmp_path / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Schedule (course_id,weekday,start,end,id) VALUES ('su',1,?,?,?)",
                           (10 * H, 11 * H + 50 * 60_000, new_ulid()))
        for mid, title in (("syl", "2026-2 강의계획서_구조역학"), ("w4", "Week 4 Viscous flow"),
                           ("w5", "Week 5 Pipe friction"), ("w6", "Week 6 Pipe system")):
            path = folder / f"{title}.pdf"
            path.write_bytes(b"%PDF-1.4 synthetic")
            connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                               "VALUES (?,?,?,?,?,?,0)", (mid, "su", str(path), mid, "pdf", title))
        connection.execute("INSERT INTO Lecture VALUES ('l0922','su','2026-09-22',1,NULL,NULL)")
        connection.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) VALUES ('l0922','w4',12,20,'medium','heuristic')")
        connection.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) VALUES ('l0922','w5',2,5,'medium','heuristic')")
        connection.commit()
        yield connection


def start_ms(day: str, hour: int = 10) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00").astimezone().timestamp() * 1000)


def test_S3_continues_after_last_lectures_pages_with_reason(library):
    result = mp.pick(library, "su", date(2026, 9, 29), start_ms("2026-09-29"), count_pages=pages)
    p = result["pick"]
    assert (p["material_id"], p["page"], p["page_count"], p["rule"]) == ("w5", 6, 36, "continue")
    assert p["reason"] == "지난 시간(9/22) 5쪽까지" and p["confidence"] == "inferred"
    assert "syl" not in [a["material_id"] for a in result["alternatives"]]  # syllabus is not lecture slides
    assert any("Syllabus" in note for note in result["notes"])  # the missing syllabus rule is said, not guessed


def test_S3_finished_material_moves_to_next_in_order(library):
    library.execute("INSERT INTO Lecture VALUES ('l0925','su','2026-09-25',1,NULL,NULL)")
    library.execute("INSERT INTO MaterialMatch (lecture_id,material_id,page_from,page_to,confidence_class,decided_by) VALUES ('l0925','w5',6,36,'medium','heuristic')")
    p = mp.pick(library, "su", date(2026, 9, 29), None, count_pages=pages)["pick"]
    assert (p["material_id"], p["page"], p["rule"]) == ("w6", 1, "next_week")
    assert "끝까지" in p["reason"]


def test_S3_user_override_wins_today_and_is_continued_next_time(library):
    mp.record_choice(library, "su", "2026-09-29", "w6", 3)
    library.commit()
    today = mp.pick(library, "su", date(2026, 9, 29), start_ms("2026-09-29"), count_pages=pages)["pick"]
    assert (today["material_id"], today["page"], today["rule"], today["confidence"]) == ("w6", 3, "user", "exact")
    later = mp.pick(library, "su", date(2026, 10, 6), None, count_pages=pages)["pick"]
    assert (later["material_id"], later["page"], later["rule"]) == ("w6", 3, "continue")
    assert "직접 고른" in later["reason"]
    row = library.execute("SELECT source, kind FROM Evidence WHERE kind='material_choice'").fetchone()
    assert tuple(row) == ("user", "material_choice")
    with pytest.raises(ValueError):
        mp.record_choice(library, "su", "2026-09-29", "nope", 1)


def test_S3_new_upload_within_24h_before_class(library, tmp_path):
    path = tmp_path / "구조역학" / "Week 7 Water hammer.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    uploaded = start_ms("2026-09-29") - 3 * H
    library.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count,etl_uploaded_at) "
                    "VALUES ('w7','su',?,'w7','pdf','Week 7 Water hammer',12,?)", (str(path), uploaded))
    p = mp.pick(library, "su", date(2026, 9, 29), start_ms("2026-09-29"), count_pages=pages)["pick"]
    assert (p["material_id"], p["page"], p["rule"]) == ("w7", 1, "new_upload")
    assert "eTL에 올라온" in p["reason"] and "9/29 07:00" in p["reason"]
    # an upload after class start, or more than 24 h before, does not count
    assert mp.pick(library, "su", date(2026, 9, 29), start_ms("2026-09-29", 6), count_pages=pages)["pick"]["rule"] == "continue"
    assert mp.pick(library, "su", date(2026, 9, 30), start_ms("2026-09-30", 11), count_pages=pages)["pick"]["rule"] != "new_upload"


def test_S3_files_copied_in_one_batch_are_not_new_uploads(library):
    """All fixture files were created together just now: a batch copy says nothing about uploads."""
    now_ms = int(time.time() * 1000)
    assert mp._bulk_added(library) >= {"w4", "w5", "w6"}
    p = mp.pick(library, "su", date(2026, 9, 29), now_ms + 60_000, count_pages=pages)["pick"]
    assert p["rule"] == "continue"


def test_S3_existing_lecture_shows_its_own_matched_pages(library):
    p = mp.pick(library, "su", date(2026, 9, 22), None, count_pages=pages, lecture_id="l0922")["pick"]
    assert (p["material_id"], p["page"], p["rule"]) == ("w4", 12, "matched")
    assert p["reason"] == "이 회차에 연결된 자료 12–20쪽 · 추정"


def test_S3_no_record_falls_back_to_first_in_teaching_order(library):
    library.execute("DELETE FROM MaterialMatch")
    p = mp.pick(library, "su", date(2026, 9, 29), None, count_pages=pages)["pick"]
    assert p["material_id"] == "w4" and p["rule"] == "next_week"  # all files are one batch → no "latest" claim


@pytest.mark.parametrize("title, order", [
    ("Week 5 Hydraulics - Pipe line friction", 5), ("Wk 2-1 사업 만들기", 2), ("3주차 실습", 3),
    ("제04장 재무보고", 4), ("도시계획_05_수자원계획_2026", 5), ("1_댐 일반_수공구조물", 1),
    ("091726 환경조절공학 2주차 실습", 20260917), ("농업용토질역학(백곡) 둑높임사업", None),
])
def test_S3_teaching_order_from_file_names(title, order):
    assert mp.order_of(title) == order


def test_S3_class_for_now_is_current_or_next_class(library):
    tz = datetime.now().astimezone().tzinfo
    before = mp.class_for_now(library, datetime(2026, 9, 29, 9, 0, tzinfo=tz))
    assert before["date"] == "2026-09-29" and not before["current"] and before["course"] == "구조역학"
    during = mp.class_for_now(library, datetime(2026, 9, 29, 10, 30, tzinfo=tz))
    assert during["current"]
    after = mp.class_for_now(library, datetime(2026, 9, 29, 12, 0, tzinfo=tz))
    assert after["date"] == "2026-10-06"


def test_S3_render_and_open_stay_inside_cache(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    from lecturehub.materials import opener

    pdf = tmp_path / "구조역학" / "합성 자료.pdf"
    pdf.parent.mkdir()
    document = pymupdf.open()
    for n in range(3):
        document.new_page().insert_text((72, 72), f"synthetic page {n + 1}")
    document.save(pdf)
    document.close()
    before = pdf.read_bytes()
    image = opener.render_page(pdf, 2, tmp_path, width=320)
    assert image.is_file() and image.parent == tmp_path / "_lecturehub" / "cache" / "pages"
    assert opener.render_page(pdf, 2, tmp_path, width=320) == image  # cached
    assert pdf.read_bytes() == before  # the original is only read
    assert mp.pdf_page_count(str(pdf)) == 3

    launched = []
    monkeypatch.setattr(opener, "default_pdf_program", lambda: ("ChromePDF", str(pdf)))  # any existing file as "exe"
    monkeypatch.setattr(opener.subprocess, "Popen", lambda args, **kw: launched.append(args))
    result = opener.open_material(pdf, 2, tmp_path)
    assert result["page_applied"] and launched[0][1].startswith("file:///") and launched[0][1].endswith("#page=2")
    assert opener.open_material(tmp_path / "없음.pdf", 1, tmp_path)["ok"] is False
    if os.name == "nt":
        monkeypatch.setattr(opener.os, "startfile", lambda p: launched.append(["startfile", p]), raising=False)
        monkeypatch.setattr(opener, "default_pdf_program", lambda: ("AcroExch.Document", None))
        assert opener.open_material(pdf, 2, tmp_path)["page_applied"] is False  # honest: page not applied


def test_S3_powerpoint_is_quit_only_if_we_started_it():
    """Review S3-1: New-Object attaches to the user's open PowerPoint; never close their windows."""
    from pathlib import Path

    from lecturehub.materials import opener

    command, env = opener.convert_command(Path("C:/구조역학/it's.pptx"), Path("C:/cache/x.pdf"))
    assert "Get-Process POWERPNT" in command and "if (-not $running) { $app.Quit() }" in command
    assert "finally" in command and "$doc.Close()" in command and env["LH_SRC"].endswith("it's.pptx")
    assert command.count("$app.Quit()") == 1


def test_SEC_powershell_paths_never_enter_the_command(tmp_path, monkeypatch):
    """Security review 2026-11-05: curly quotes end a PowerShell '...' literal, so a slide named
    ``a’); Start-Process calc; (’.pptx`` must not reach the command text at all — paths go by env."""
    from pathlib import Path

    from lecturehub.materials import opener

    evil = tmp_path / "a\u2019); Start-Process calc; $x=(\u2018 \u201a\u201b.pptx"
    out = tmp_path / "cache" / "x\u2019;$(calc).pdf"
    command, env = opener.convert_command(evil, out)
    for piece in (str(evil), str(out), "Start-Process", "calc", "\u2019", "\u2018", str(tmp_path)):
        assert piece not in command
    assert "$env:LH_SRC" in command and "$env:LH_OUT" in command
    assert env["LH_SRC"] == str(evil) and env["LH_OUT"] == str(out)

    evil.write_bytes(b"synthetic pptx")
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["env"] = args, kwargs.get("env")
        Path(kwargs["env"]["LH_OUT"]).write_bytes(b"%PDF")
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(opener.subprocess, "run", fake_run)
    pdf = opener.slides_to_pdf(evil, tmp_path)
    assert pdf.is_file() and seen["env"]["LH_SRC"] == str(evil)
    assert all(str(evil) not in a and "calc" not in a for a in seen["args"])


def test_S3_no_powerpoint_conversion_while_recording(tmp_path, monkeypatch):
    """Review S3-2 (AGENTS rule 7): while recording, slides use an existing export or open as they are."""
    from lecturehub.materials import opener
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library

    slides = tmp_path / "토질역학" / "합성 슬라이드.pptx"
    slides.parent.mkdir()
    slides.write_bytes(b"synthetic pptx")
    calls = []
    monkeypatch.setattr(opener, "slides_to_pdf", lambda *a, **k: calls.append("convert"))
    monkeypatch.setattr(opener, "powerpoint_available", lambda: True)
    monkeypatch.setattr(opener.os, "startfile", lambda p: calls.append(("startfile", p)), raising=False)

    class Backend:
        phase = "recording"

        def state(self):
            return {"phase": self.phase}

    backend = Backend()
    api = Api(backend, Library(tmp_path), None)
    assert api.material_preview(str(slides), 1) == {"ok": True, "image": None, "page_count": None, "deferred": True}
    opened = api.open_material(str(slides), 3)
    assert opened["ok"] and not opened["page_applied"] and "녹음 중" in opened["note"]
    assert calls == [("startfile", str(slides))]  # the original, no conversion
    assert opener.render_page(slides, 1, tmp_path, allow_convert=False) is None
    assert calls == [("startfile", str(slides))]
    api._prewarm()  # never converts while recording
    assert "convert" not in calls
