"""Course folders by category: moves only inside a course folder, logged, undoable; rows follow the file."""
import json
import os
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from lecturehub.core import db
from lecturehub.index import materials_pick as mp
from lecturehub.index import organize
from lecturehub.index.scanner import scan_sources
from lecturehub.parsers.dedupe import sha256_file


@pytest.fixture(autouse=True)
def six_categories():
    from lecturehub.core import settings

    settings.set_folder_layout("detailed")  # written for the six-category layout (the public default is 기본)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "26-2"
    course = root / "구조역학"
    (course / "수업 정리 html").mkdir(parents=True)
    (root / "_lecturehub").mkdir()
    files = {"week5": course / "Week 5 Pipe friction.pdf", "lab": course / "2주차 실습.pdf",
             "syl": course / "2026-2 강의계획서_구조역학.pdf", "hw": course / "2026_assmt01.pdf",
             "note": course / "수업 정리 html" / "구조역학_1강.html"}
    for key, path in files.items():
        path.write_bytes(f"synthetic {key}".encode())
    (course / "족보").mkdir()
    (course / "족보" / "2025 기말.pdf").write_bytes(b"synthetic past exam")
    (course / "_작업").mkdir()
    (course / "_작업" / "tmp.pdf").write_bytes(b"work file")
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as connection:
        db.migrate(connection)
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        connection.execute("INSERT INTO Lecture VALUES ('l1','su','2026-09-04',1,NULL,NULL)")
        for key in ("week5", "lab", "syl", "hw"):
            connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) VALUES (?,?,?,?,?,?,0)",
                               (key, "su", str(files[key]), sha256_file(files[key]), "pdf", files[key].stem))
        connection.execute(
            "INSERT INTO StudyArtifact (id,lecture_ids,path,kind,version,mode,inputs,created_by,qa_status) "
            "VALUES ('note','[\"l1\"]',?,'note',1,'new',?,'import','unverified')",
            (str(files["note"]), json.dumps({"source_sha256": sha256_file(files["note"])})))
        connection.commit()
        yield root, connection, files


def path_of(connection, table, row_id):
    return connection.execute(f"SELECT path FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_inventory_groups_by_folder_and_suggests_for_unsorted_files(library):
    root, connection, _ = library
    view = organize.inventory(connection, root, "구조역학")
    assert [item["name"] for item in view["top"]] == ["2026-2 강의계획서_구조역학.pdf"]  # the syllabus stays
    assert {item["name"]: item["suggest"] for item in view["loose"]} == {
        "2026_assmt01.pdf": "과제", "2주차 실습.pdf": "실습자료", "Week 5 Pipe friction.pdf": "강의자료", "족보": "참고자료"}
    assert [(g["category"], [i["name"] for i in g["items"]]) for g in view["groups"]] == [("정리본", ["구조역학_1강.html"])]
    assert view["groups"][0]["items"][0]["known"] == "note"
    assert "_작업" not in json.dumps(view, ensure_ascii=False)  # work folders are not listed


def test_a_plan_moves_files_and_the_rows_follow_then_undo_puts_everything_back(library):
    root, connection, files = library
    plan = [{"from": "구조역학/Week 5 Pipe friction.pdf", "to": "구조역학/강의자료/Week 5 Pipe friction.pdf"},
            {"from": "구조역학/2026_assmt01.pdf", "to": "구조역학/과제/과제1/2026_assmt01.pdf"},
            {"from": "구조역학/수업 정리 html", "to": "구조역학/정리본"},
            {"from": "구조역학/족보", "to": "구조역학/참고자료/족보"}]
    assert organize.check(connection, root, plan) == []
    result = organize.apply(connection, root, plan)
    assert len(result["moved"]) == 4 and result["rows"] == 3 and "stopped" not in result
    course = root / "구조역학"
    assert (course / "강의자료" / "Week 5 Pipe friction.pdf").is_file() and not files["week5"].exists()
    assert (course / "참고자료" / "족보" / "2025 기말.pdf").is_file() and not (course / "수업 정리 html").exists()
    assert path_of(connection, "Material", "week5") == str(course / "강의자료" / "Week 5 Pipe friction.pdf")
    assert path_of(connection, "Material", "hw") == str(course / "과제" / "과제1" / "2026_assmt01.pdf")
    assert path_of(connection, "StudyArtifact", "note") == str(course / "정리본" / "구조역학_1강.html")
    assert connection.execute("SELECT count(*) FROM MoveLog WHERE at = ?", (result["batch"],)).fetchone()[0] == 4
    assert connection.execute("SELECT count(*) FROM Evidence WHERE kind = 'user_move' AND source = 'user'").fetchone()[0] == 3
    assert organize.category_of(course, course / "정리본" / "구조역학_1강.html") == "정리본"
    assert organize.folder_for(course, "정리본") == course / "정리본"

    back = organize.undo(connection, root)
    assert len(back["moved"]) == 4
    assert all(path.is_file() for path in files.values()) and (course / "족보" / "2025 기말.pdf").is_file()
    assert not (course / "강의자료").exists() and not (course / "과제").exists()  # emptied folders go away
    assert path_of(connection, "Material", "week5") == str(files["week5"])
    assert path_of(connection, "StudyArtifact", "note") == str(files["note"])


def test_nothing_is_overwritten_and_nothing_leaves_its_course_folder(library):
    root, connection, files = library
    (root / "구조역학" / "강의자료").mkdir()
    (root / "구조역학" / "강의자료" / "Week 5 Pipe friction.pdf").write_bytes(b"another file")
    (root / "녹음본").mkdir()
    connection.execute("INSERT INTO Transcript (id,lecture_id,engine,role,role_decided_by,status,source_file,sha256) "
                       "VALUES ('t1','l1','clova','reference','policy','done',?,'x')", (str(root / "구조역학" / "9.4 구조역학.txt"),))
    (root / "구조역학" / "9.4 구조역학.txt").write_text("전사", encoding="utf-8")
    problems = organize.check(connection, root, [
        {"from": "구조역학/Week 5 Pipe friction.pdf", "to": "구조역학/강의자료/Week 5 Pipe friction.pdf"},
        {"from": "구조역학/2주차 실습.pdf", "to": "녹음본/2주차 실습.pdf"},
        {"from": "구조역학/2주차 실습.pdf", "to": "../밖.pdf"},
        {"from": "구조역학/없는 파일.pdf", "to": "구조역학/강의자료/없는 파일.pdf"},
        {"from": "구조역학/9.4 구조역학.txt", "to": "구조역학/정리본/9.4 구조역학.txt"},
        {"from": "구조역학/족보", "to": "구조역학/족보/안/족보"}])
    assert [p.split(": ")[-1] for p in problems] == [
        "같은 이름이 이미 있습니다", "같은 과목 폴더 안에서만 옮깁니다", "../밖.pdf",
        "옮길 파일이 없습니다", "전사 원본·녹음 파일은 옮기지 않습니다", "자기 안으로는 옮길 수 없습니다"]
    with pytest.raises(ValueError, match="옮길 수 없습니다"):
        organize.apply(connection, root, [{"from": "구조역학/Week 5 Pipe friction.pdf",
                                           "to": "구조역학/강의자료/Week 5 Pipe friction.pdf"}])
    assert files["week5"].read_bytes() == b"synthetic week5"
    assert connection.execute("SELECT count(*) FROM MoveLog").fetchone()[0] == 0


def test_a_plan_is_checked_in_order(library):
    root, connection, _ = library
    plan = [{"from": "구조역학/족보", "to": "구조역학/참고자료/족보"},
            {"from": "구조역학/참고자료/족보/2025 기말.pdf", "to": "구조역학/참고자료/2025 기말.pdf"},
            {"from": "구조역학/2주차 실습.pdf", "to": "구조역학/참고자료/족보"}]  # the folder an earlier step made
    assert [p.split(": ")[-1] for p in organize.check(connection, root, plan)] == ["같은 이름이 이미 있습니다"]
    assert organize.check(connection, root, plan[:2]) == []
    assert len(organize.apply(connection, root, plan[:2])["moved"]) == 2
    assert (root / "구조역학" / "참고자료" / "2025 기말.pdf").is_file()


def test_a_file_moved_in_explorer_is_found_by_its_bytes(library):
    root, connection, files = library
    target = root / "구조역학" / "내가 만든 폴더" / "실습 2주차.pdf"  # moved and renamed by hand
    target.parent.mkdir()
    os.rename(files["lab"], target)
    (root / "구조역학" / "강의자료").mkdir()
    os.rename(files["note"], root / "구조역학" / "강의자료" / files["note"].name)
    quick = organize.relocate(connection, root, deep=False)  # app start: only files that kept their name
    assert [item["to"] for item in quick] == [str(Path("구조역학", "강의자료", "구조역학_1강.html"))]
    found = organize.relocate(connection, root)
    assert [item["to"] for item in found] == [str(Path("구조역학", "내가 만든 폴더", "실습 2주차.pdf"))]
    assert path_of(connection, "Material", "lab") == str(target)
    assert organize.relocate(connection, root) == []
    assert connection.execute("SELECT source FROM Evidence WHERE kind = 'relocated' AND subject_id = 'lab'").fetchone()[0] == "metadata"


def test_the_scanner_reads_sorted_folders_but_not_work_folders(library):
    root, connection, _ = library
    organize.apply(connection, root, [{"from": "구조역학/Week 5 Pipe friction.pdf", "to": "구조역학/강의자료/5주/Week 5 Pipe friction.pdf"},
                                      {"from": "구조역학/수업 정리 html", "to": "구조역학/정리본"}])
    (root / "구조역학" / "과제").mkdir()
    (root / "구조역학" / "과제" / "초안.html").write_text("<p>초안</p>", encoding="utf-8")  # not a lecture note
    found = {(s.kind, s.path.name) for s in scan_sources(root, ["구조역학"])}
    assert ("material", "Week 5 Pipe friction.pdf") in found and ("note", "구조역학_1강.html") in found
    assert ("material", "2025 기말.pdf") in found
    assert not any(name in ("tmp.pdf", "초안.html") for _, name in found)


def test_only_slides_and_handouts_are_offered_as_class_material(library):
    root, connection, _ = library
    organize.apply(connection, root, [
        {"from": "구조역학/Week 5 Pipe friction.pdf", "to": "구조역학/강의자료/Week 5 Pipe friction.pdf"},
        {"from": "구조역학/2주차 실습.pdf", "to": "구조역학/참고자료/2주차 실습.pdf"}])
    result = mp.pick(connection, "su", date(2026, 9, 29), None, count_pages=lambda path: 10)
    assert [(a["title"], a["category"]) for a in result["alternatives"]] == [("Week 5 Pipe friction", "강의자료")]


def test_ORG_LOCK_001_hashing_does_not_hold_the_write_lock(library, monkeypatch):
    """Review #1: while relocate hashes the 2nd moved file, another connection (ingest-app) can still write."""
    root, connection, files = library
    connection.commit()
    for key in ("lab", "week5"):  # two files moved by hand, names kept
        (root / "구조역학" / "옮김").mkdir(exist_ok=True)
        os.rename(files[key], root / "구조역학" / "옮김" / files[key].name)
    real, calls = organize._find, []

    def find_then_write(*args, **kwargs):
        with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as other:
            other.execute("PRAGMA busy_timeout = 200")
            other.execute("BEGIN IMMEDIATE")  # 'database is locked' if relocate already has a write pending
            other.execute("INSERT INTO Lecture VALUES (?,'su','2026-09-11',2,NULL,NULL)", (f"x{len(calls)}",))
            other.commit()
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(organize, "_find", find_then_write)
    found = organize.relocate(connection, root)
    connection.commit()
    assert len(calls) == 2 and sorted(Path(f["to"]).name for f in found) == sorted(files[k].name for k in ("lab", "week5"))
    assert path_of(connection, "Material", "lab") == str(root / "구조역학" / "옮김" / files["lab"].name)
    assert connection.execute("SELECT count(*) FROM Lecture WHERE id LIKE 'x%'").fetchone()[0] == 2
