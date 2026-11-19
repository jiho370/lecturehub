"""A finished HTML note attached to a lecture by hand, and a lecture the user named (2026-10-02)."""
import json
import re
from contextlib import closing
from pathlib import Path

import pytest

from lecturehub.core import db
from lecturehub.index import lectures, note_attach
from lecturehub.index.discard import _lecture_label


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "26-2"
    (root / "AI강의" / "정리본").mkdir(parents=True)
    (root / "_lecturehub").mkdir()
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('ai','EXTRA','AI 외부 강의','ex','26-2')")
        for lecture_id, day, index in (("a1", "2026-09-29", 1), ("a2", "2026-09-29", 2), ("f1", "2026-10-02", 1),
                                       ("a3", "2026-10-02", 2)):
            connection.execute("INSERT INTO Lecture VALUES (?, 'ai', ?, ?, NULL, NULL)", (lecture_id, day, index))
        connection.commit()
        yield root, connection


def test_an_attached_note_opens_in_its_lecture_and_a_rebuilt_file_keeps_one_row(library):
    root, connection = library
    note = root / "AI강의" / "정리본" / "AI강의_01강.html"
    note.write_text("<p>합성 노트</p>", encoding="utf-8")
    first = note_attach.attach(connection, root, "a1", note)
    assert first["state"] == "attached"
    assert [n["path"] for n in lectures.lecture_detail(connection, "a1")["notes"]] == [str(note)]
    assert lectures.lecture_detail(connection, "a2")["notes"] == []
    assert note_attach.attach(connection, root, "a1", note)["state"] == "already"

    note.write_text("<p>합성 노트, 고친 판</p>", encoding="utf-8")  # the note was rebuilt
    again = note_attach.attach(connection, root, "a1", note)
    assert again == {"artifact_id": first["artifact_id"], "state": "updated", "lecture_ids": ["a1"]}
    assert connection.execute("SELECT count(*) FROM StudyArtifact").fetchone()[0] == 1
    assert note_attach.attach(connection, root, "a2", note)["lecture_ids"] == ["a1", "a2"]  # one note, two lectures


def test_a_note_moved_to_another_folder_is_followed_when_attached_again(library):
    root, connection = library
    note = root / "AI강의" / "정리본" / "AI강의_09강.html"
    note.write_text("<p>합성 노트 9강</p>", encoding="utf-8")
    first = note_attach.attach(connection, root, "a1", note)
    target = root / "AI 외부 강의" / "정리본" / "AI강의_09강.html"
    target.parent.mkdir(parents=True)
    note.rename(target)  # the user merged two folders of one course (2026-11-05)
    again = note_attach.attach(connection, root, "a1", target)
    assert again == {"artifact_id": first["artifact_id"], "state": "moved", "lecture_ids": ["a1"]}
    assert [n["path"] for n in lectures.lecture_detail(connection, "a1")["notes"]] == [str(target)]
    assert connection.execute("SELECT count(*) FROM StudyArtifact").fetchone()[0] == 1


def test_a_copy_of_a_note_does_not_take_over_a_link_whose_file_still_exists(library):
    root, connection = library
    note = root / "AI강의" / "정리본" / "원본.html"
    note.write_text("<p>같은 내용</p>", encoding="utf-8")
    note_attach.attach(connection, root, "a1", note)
    copy = root / "AI강의" / "사본.html"
    copy.write_text("<p>같은 내용</p>", encoding="utf-8")
    assert note_attach.attach(connection, root, "a1", copy)["state"] == "already"
    assert [n["path"] for n in lectures.lecture_detail(connection, "a1")["notes"]] == [str(note)]


def test_only_html_inside_the_semester_folder_is_attached(library, tmp_path):
    root, connection = library
    outside = tmp_path / "밖.html"
    outside.write_text("<p>x</p>", encoding="utf-8")
    inside_app = root / "_lecturehub" / "노트.html"
    inside_app.write_text("<p>x</p>", encoding="utf-8")
    text = root / "AI강의" / "메모.txt"
    text.write_text("x", encoding="utf-8")
    for path in (outside, inside_app, text, root / "AI강의" / "없는 파일.html"):
        with pytest.raises(ValueError):
            note_attach.attach(connection, root, "a1", path)
    with pytest.raises(ValueError, match="없는 회차"):
        note_attach.attach(connection, root, "nope", outside)
    assert connection.execute("SELECT count(*) FROM StudyArtifact").fetchone()[0] == 0


def test_a_named_lecture_shows_its_name_and_takes_no_number(library):
    _root, connection = library
    note_attach.set_title(connection, "f1", "  무료 특강  1강 ")
    by_id = {x["id"]: x for x in lectures.lecture_list(connection)}
    assert [by_id[k]["label"] for k in ("a1", "a2", "f1", "a3")] == ["1강", "2강", "무료 특강 1강", "3강"]
    assert by_id["f1"]["n"] is None and by_id["a3"]["n"] == 3
    detail = lectures.lecture_detail(connection, "f1")
    assert (detail["n"], detail["name"], detail["label"]) == (None, "무료 특강 1강", "무료 특강 1강")
    assert lectures.lecture_detail(connection, "a3")["label"] == "3강"
    assert _lecture_label(connection, "f1") == "AI 외부 강의 무료 특강 1강 (10/2)"
    assert _lecture_label(connection, "a3") == "AI 외부 강의 3강 (10/2)"

    note_attach.set_title(connection, "f1", "")  # back to a number
    assert {x["id"]: x["label"] for x in lectures.lecture_list(connection)}["f1"] == "3강"
    with pytest.raises(ValueError):
        note_attach.set_title(connection, "f1", "가" * 41)


def test_a_transcript_from_a_classmate_is_attached_to_a_lecture_without_a_recording(library, tmp_path):
    root, connection = library
    folder = root / "전사본" / "합성과목"
    folder.mkdir(parents=True)
    plain = folder / "26.9.29 합성 (동기).txt"  # a Clova Note export without its header reads as the Daglo layout
    plain.write_bytes("00:01 \r\n합성 수로를 설명합니다.\r\n\r\n00:42 \r\n가상 물의 흐름입니다.\r\n".encode())
    made = note_attach.attach_transcript(connection, root, "a1", plain, engine="clova", origin="동기 클로바노트(합성)")
    assert made["state"] == "attached" and made["engine"] == "clova" and made["segments"] == 2
    detail = lectures.lecture_detail(connection, "a1")
    assert [t["label"] for t in detail["transcripts"]] == ["클로바"] and detail["in_use"]["transcript_id"] == made["transcript_id"]
    assert detail["recordings"] == []
    import json
    evidence = json.loads(connection.execute(
        "SELECT detail FROM Evidence WHERE subject_type='Transcript' AND kind='user_attached'").fetchone()[0])
    assert evidence["layout_read_as"] == "daglo" and evidence["engine_named"] == "clova" and evidence["named_by_user"]
    assert note_attach.attach_transcript(connection, root, "a1", plain)["state"] == "already"
    with pytest.raises(ValueError, match="다른 회차"):
        note_attach.attach_transcript(connection, root, "a2", plain)
    outside = tmp_path / "밖.txt"
    outside.write_bytes(plain.read_bytes() + b"x")
    junk = folder / "메모.txt"
    junk.write_text("시각이 없는 글", encoding="utf-8")
    for path, kwargs in ((outside, {}), (junk, {}), (plain.with_suffix(".md"), {})):
        with pytest.raises(ValueError):
            note_attach.attach_transcript(connection, root, "a2", path, **kwargs)
    assert connection.execute("SELECT count(*) FROM Transcript").fetchone()[0] == 1


def test_live_captions_moved_to_the_transcript_folder_join_the_day_s_lecture_made_if_missing(library):
    root, connection = library
    folder = root / "전사본" / "AI 외부 강의"
    folder.mkdir(parents=True)
    path = folder / "2026-10-05 1016 실시간 자막.txt"  # a talk with captions only, no recording (user 2026-11-18)
    path.write_text("# 실시간 자막 · AI 외부 강의 · 2026-10-05 10:16 시작\n[00:07] 합성 발표를 시작합니다.\n"
                    "# 10:18~10:20 자막 없음\n[04:37] 합성 질문을 받겠습니다.\n[01:02:03] 끝.\n", encoding="utf-8")
    from lecturehub.parsers import parse_file
    parsed = parse_file(path)
    assert parsed.engine == "live_captions"
    assert [(s.start_ms, s.text) for s in parsed.segments] == [(7000, "합성 발표를 시작합니다."), (277000, "합성 질문을 받겠습니다."),
                                                              (3723000, "끝.")]
    lecture_id = note_attach.lecture_on(connection, "AI 외부 강의", "2026-10-05")
    assert note_attach.lecture_on(connection, "AI 외부 강의", "2026-10-05") == lecture_id  # made once
    made = note_attach.attach_transcript(connection, root, lecture_id, path, engine="live_captions", origin="실시간 자막")
    assert made["engine"] == "live_captions" and made["segments"] == 3
    detail = lectures.lecture_detail(connection, lecture_id)
    assert [t["label"] for t in detail["transcripts"]] == ["실시간 자막"] and detail["recordings"] == []
    with pytest.raises(ValueError, match="여러 개"):
        note_attach.lecture_on(connection, "AI 외부 강의", "2026-09-29")
    with pytest.raises(ValueError, match="없는 과목"):
        note_attach.lecture_on(connection, "없는 과목", "2026-10-05")
    daglo = folder / "다글로.txt"
    daglo.write_bytes("00:01 \n합성 문장입니다.\n".encode())
    with pytest.raises(ValueError):  # only a captions file may be named live_captions
        note_attach.attach_transcript(connection, root, "a2", daglo, engine="live_captions")


def test_clova_text_with_the_time_before_the_speaker_is_read_as_clova():
    from lecturehub.parsers import clova
    from lecturehub.parsers.detect import detect_engine

    text = "00:11 참석자 1\n응 \n\n00:45 참석자 2\n합성 토질역학를 설명합니다.\n10:30 참석자 1 명만 오세요\n\n01:02:03 참석자 1\n끝.\n"
    assert detect_engine(text.encode()) == "clova"
    segments = clova.parse(text)
    assert [(s.start_ms, s.speaker) for s in segments] == [(11_000, "참석자 1"), (45_000, "참석자 2"), (3_723_000, "참석자 1")]
    assert "10:30 참석자 1 명만 오세요" in segments[1].text  # words after the speaker: speech, not a new block
    assert detect_engine("00:11 참석자 1\n한 번뿐\n".encode()) == "unknown"


def test_the_note_pack_gathers_transcripts_slides_and_where_the_note_goes(library):
    from lecturehub.index import note_pack

    root, connection = library
    slides = root / "AI강의" / "강의자료"
    slides.mkdir()
    (slides / "1강.pdf").write_bytes(b"%PDF synthetic")
    connection.execute("UPDATE Course SET folder_name = 'AI강의 영상' WHERE id = 'ai'")   # "<과목> …" shares the course folder
    empty = note_pack.pack(connection, root, "a1")
    assert not empty["ready"] and empty["transcripts"] == []
    text = root / "전사본" / "합성과목" / "26.9.29 합성.txt"
    text.parent.mkdir(parents=True)
    text.write_bytes("00:01 \r\n합성 수로를 설명합니다.\r\n\r\n00:42 \r\n가상 물의 흐름입니다.\r\n".encode())
    note_attach.attach_transcript(connection, root, "a1", text, engine="clova")
    older = root / "AI강의" / "정리본" / "AI강의_01강_강의노트.html"
    older.write_text("<p>합성</p>", encoding="utf-8")
    made = note_pack.pack(connection, root, "a1")
    assert made["ready"] and [t["engine"] for t in made["transcripts"]] == ["클로바"] and made["transcripts"][0]["in_use"]
    assert made["materials_in_course_folder"] == [str(slides / "1강.pdf")]
    assert made["notes_folder"] == str(root / "AI강의" / "정리본") and made["style_from"] == str(older)
    assert made["target"].startswith(str(root / "AI강의" / "정리본")) and made["target"].endswith("_강의노트.html")
    assert made["attached_notes"] == []
    assert made["cues_file"].endswith(".cues.json") and "quiz" in made["cues_file"]
    assert made["recording_now"] is False
    session = root / "_lecturehub" / "recordings" / "20260929_100000_mic"
    session.mkdir(parents=True)
    (session / ".recording.lock").write_text("1", encoding="utf-8")
    assert note_pack.pack(connection, root, "a1")["recording_now"] is True
    (session / "stop.json").write_text("{}", encoding="utf-8")
    note_attach.attach(connection, root, "a1", older)
    again = note_pack.pack(connection, root, "a1")
    assert again["attached_notes"] == [str(older)] and again["target"] == str(older)   # revise that file in place


def test_restoring_the_mai_window_finds_nothing_when_there_is_none():
    from lecturehub.transcription import mai_window

    assert mai_window.restore("이런 제목의 창은 없음 9f3c") == []


def test_the_note_button_opens_selected_codex_with_the_lecture(library, monkeypatch):
    from lecturehub.ui.library import Library

    root, connection = library
    text = root / "전사본" / "합성과목" / "26.9.29 합성.txt"
    text.parent.mkdir(parents=True)
    text.write_bytes("00:01 \r\n합성 수로를 설명합니다.\r\n\r\n00:42 \r\n가상 물의 흐름입니다.\r\n".encode())
    opened = []
    assert Library(root).note_claude("a1", launcher=lambda command, repo: opened.append(command))["ok"] is False  # no transcript yet
    note_attach.attach_transcript(connection, root, "a1", text, engine="clova")
    connection.commit()
    from lecturehub.transcription.consent import ConsentStore
    from lecturehub.ai.codex import CodexEngine

    info = Library(root).note_check("a1")
    ConsentStore(root / "_lecturehub" / "consent.json").grant(info["provider"], info["course"], 0, "synthetic test")
    monkeypatch.setattr(CodexEngine, "executable", lambda self: "codex.cmd")
    made = Library(root).note_claude("a1", launcher=lambda command, repo: opened.append(command))
    assert made["ok"] and made["transcripts"] == 1 and not made["revise"]
    assert len(opened) == 1 and "$lecture-note a1" in opened[0] and "workspace-write" in opened[0]
    # write access: the course's 정리본 folder and its cues folder only, never the semester folder (M2)
    added = re.findall(r'"--add-dir" "([^"]+)"', opened[0])
    resolved = root.resolve()
    assert added and str(resolved) not in added and str(root) not in added
    assert all(Path(a).is_dir() and Path(a).is_relative_to(resolved) for a in added)
    assert any(Path(a).name == "정리본" for a in added)
    assert any(Path(a).parent == resolved / "_lecturehub" / "quiz" for a in added)
    assert '"-a" "on-request"' in opened[0]
    packs = list((root / "_lecturehub" / "jobs").glob("note_console_a1_*/pack.json"))
    assert len(packs) == 1 and json.loads(packs[0].read_text(encoding="utf-8"))["lecture_id"] == "a1"

    from lecturehub.ui.app import Api

    class Recording:
        def state(self):
            return {"phase": "recording"}

    busy = Api(Recording(), library=Library(root)).note_claude("a1")
    assert busy["ok"] is False and "녹음 중" in busy["error"] and len(opened) == 1   # nothing is opened next to a recording


def test_the_style_and_name_follow_the_course_notes_not_the_exam_pages(library):
    import os
    import time

    from lecturehub.index import note_pack

    root, connection = library
    notes = root / "AI강의" / "정리본"
    (notes / "환경공_1008_실습노트.html").write_text("<p>합성</p>", encoding="utf-8")
    later = notes / "환경공_중간고사_대비.html"
    later.write_text("<p>합성</p>", encoding="utf-8")
    os.utime(later, (time.time() + 60, time.time() + 60))   # newer, but not a lecture note
    connection.execute("UPDATE Course SET folder_name = 'AI강의' WHERE id = 'ai'")
    made = note_pack.pack(connection, root, "a3")           # 2026-10-02
    assert made["style_from"].endswith("환경공_1008_실습노트.html")
    assert made["target"].endswith("환경공_1002_실습노트.html")
    for style, name in (("26.10.12_기초어학_강의노트", "26.10.2_기초어학_강의노트"), ("9.28_농촌수자원계획_강의노트", "10.2_농촌수자원계획_강의노트"),
                        ("구조역학_0929_강의노트", "구조역학_1002_강의노트"), ("26-09-04 경제학원론 제1장", "26-10-02 경제학원론 제1장")):
        assert note_pack._named_like(Path(style + ".html"), "x", "2026-10-02", "3강") == name
    assert note_pack._named_like(None, "구조역학", "2026-10-02", "3강") == "구조역학_1002_강의노트"


def test_file_sort_never_moves_the_app_data_folder(library):
    """Security review 2026-11-13 (L5): the sort button cannot move _lecturehub\\lecturehub.sqlite or job files."""
    from lecturehub.ui.library import Library

    root, _connection = library
    lib = Library(root)
    database = root / "_lecturehub" / "lecturehub.sqlite"
    for path in (database, root / "_LectureHub" / "lecturehub.sqlite"):
        result = lib.file_sort(str(path), "강의자료")
        assert result["ok"] is False and "_lecturehub" in result["error"]
    assert database.is_file() and not (root / "_lecturehub" / "강의자료").exists()
    slide = root / "AI 외부 강의" / "합성.pdf"
    slide.parent.mkdir()
    slide.write_bytes(b"%PDF synthetic")
    moved = lib.file_sort(str(slide), "강의자료")
    assert moved["ok"] and not slide.exists()