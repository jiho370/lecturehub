"""The evidence-checked note becomes one HTML 정리본 with transcript footnotes (2026-11-13). Synthetic only."""
import json
import re

from lecturehub.study import note_job

from test_note_job import BLOCKS, fake, library  # noqa: F401 - shared synthetic library fixture

QUOTED = [
    {"key": "b01", "kind": "heading", "text": "관수로 마찰손실"},
    {"key": "b02", "kind": "paragraph", "text": "오늘은 관수로의 마찰손실을 다룬다.",
     "cites": [{"t": "m", "s": 0, "q": "관수로 마찰손실"}]},
    {"key": "b03", "kind": "paragraph", "text": "레이놀즈 수가 2300보다 작으면 층류다.",
     "cites": [{"t": "m", "s": 2}, {"m": "w5", "p": 6}]},
    {"key": "b04", "kind": "external", "text": "무디 선도로 마찰계수를 읽기도 한다.",
     "ext": {"confidence": "mid", "source": "textbook"}},
    {"key": "b05", "kind": "paragraph", "text": "지어낸 근거", "cites": [{"t": "m", "s": 99}]},
]


def _html(result):
    return open(result["html"], encoding="utf-8").read()


def test_note_is_written_as_html_with_footnotes_and_attached(library):  # noqa: F811
    root, connection = library
    result = note_job.run(connection, root, "l2", executor=fake(QUOTED)[0])
    assert result["state"] == "done" and result["rejected"] == 1 and "html_error" not in result
    page = _html(result)
    assert "\\정리본\\" in result["html"] or "/정리본/" in result["html"]
    assert result["html"].startswith(str(root))
    assert "<h2>관수로 마찰손실</h2>" in page and "지어낸 근거" not in page  # no evidence → not in the note
    assert re.findall(r'<li id="fn(\d+)">', page) == ["1", "2", "3"] and result["footnotes"] == 3
    assert "<q>관수로 마찰손실</q>" in page and "02:00" in page  # quote, and the time of segment 2
    assert "Week 5 합성 6쪽" in page and "배경 설명(수업 밖 지식, 확신도 보통)" in page
    row = connection.execute("SELECT lecture_ids, created_by, qa_status, inputs FROM StudyArtifact "
                             "WHERE kind = 'note' AND path = ?", (result["html"],)).fetchone()
    assert json.loads(row[0]) == ["l2"] and row[1] == "claude_cli" and row[2] == "warning"
    assert json.loads(row[3])["generated_sha256"]


def test_numbers_not_in_the_evidence_are_marked_for_checking(library):  # noqa: F811
    root, connection = library
    page = _html(note_job.run(connection, root, "l2", executor=fake(BLOCKS)[0]))
    assert "마찰계수는 0.026 정도" in page and "(확인 필요)" in page  # 0.026 is not what was said (0.025)


def test_rerun_rewrites_its_own_file_but_never_a_user_edited_one(library):  # noqa: F811
    root, connection = library
    first = note_job.run(connection, root, "l2", executor=fake(QUOTED)[0])["html"]
    second = note_job.run(connection, root, "l2", executor=fake(QUOTED[:3])[0])["html"]
    assert second == first  # unchanged file this module wrote: rewritten in place
    with open(first, "a", encoding="utf-8") as handle:
        handle.write("<!-- user edit -->")
    third = note_job.run(connection, root, "l2", executor=fake(QUOTED)[0])["html"]
    assert third != first and open(first, encoding="utf-8").read().endswith("<!-- user edit -->")
