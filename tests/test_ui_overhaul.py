"""화면 개편 (2026-11-13): 정리본 모음, live transcription on the recording screen, one HTML 정리본 in the lecture view,
settings for the folder layout and recording cleanup, and the 확인 screen. Synthetic library only."""
import re
from pathlib import Path

from test_note_html import QUOTED
from test_note_job import fake, library  # noqa: F401 - shared synthetic library fixture

from lecturehub.study import note_job

WEB = Path(__file__).resolve().parents[1] / "src" / "lecturehub" / "ui" / "web"
JS = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


def _body(start: str, end: str) -> str:
    return JS[JS.index(start):JS.index(end)]


def test_note_collection_lists_each_lecture_by_date_with_its_html_note(library):  # noqa: F811
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library

    root, connection = library
    connection.execute("INSERT INTO Lecture VALUES ('l3','su','2026-09-29',1,NULL,NULL)")  # no transcript yet
    connection.commit()
    made = note_job.run(connection, root, "l2", executor=fake(QUOTED)[0])
    shown = Api(object(), Library(root)).note_collection()
    assert shown["ok"], shown
    (course,) = shown["courses"]
    assert course["folder"] == "구조역학" and course["with_notes"] == 1
    first, second = course["lectures"]
    assert (first["id"], second["id"]) == ("l2", "l3")  # oldest first, like a course's notes
    assert [n["path"] for n in first["notes"]] == [made["html"]] and first["notes"][0]["exists"]
    assert first["can_make"] and second["notes"] == [] and not second["can_make"]


def test_note_collection_without_a_library_says_so(tmp_path):
    from lecturehub.ui.library import Library

    result = Library(tmp_path / "없는 학기 폴더").note_collection()
    assert result["ok"] is False and "학기 폴더" in result["error"]
    assert Library(tmp_path).note_collection()["ok"] is True  # an empty folder: the library is built on first use


def test_recording_screen_has_live_transcription_instead_of_the_slide_pane():
    for gone in ('id="c-slide"', 'id="pickViewer"', 'id="pickMore"', 'id="c-note"', 'id="noteList"'):
        assert gone not in HTML, gone
    for kept in ('id="c-live"', 'id="liveHead"', 'id="liveBody"', 'id="noteRemake"', 'id="noteEmpty"'):
        assert kept in HTML, kept
    for gone in ("function loadMaterial", "function renderMaterial", "materialMenu(", "function renderNote", "edit_note_block",
                 "data-cite-t", "data-cite-m"):
        assert gone not in JS, gone
    live = _body("const LIVE_KEY", "async function onCaptions()")
    # consent first (native window for a named course), then the engine chosen in settings (default Gemini Live) with the recording's source
    assert live.index('api.consent_confirm("google_live", course)') < live.index("api.captions_start(")
    assert 'api.captions_start(course, liveSource(), consent, token, eng !== "quick", eng !== "chirp")' in live
    assert "api.captions_stop()" in live and "api.captions_pause(paused)" in live  # stops and pauses with the recording
    assert "Gemini 키를 연결하면" in live  # the calm line when captions are not available
    assert "followRecording(ui.state)" in JS and "cap.pendingAuto = true" in JS


def test_lecture_view_makes_and_shows_one_html_note():
    make = _body("async function makeNote(", "async function pollNote(")
    assert make.index("api.note_check(") < make.index("api.consent_confirm(") < make.index("api.make_note(")
    center = _body("function renderLectureCenter()", "async function makeNote(")
    assert "showHtml(pages[0])" in center and 'data-note="make"' in center and "정리본 만들기" in center


def test_rail_button_opens_the_note_collection_and_the_exam_screen_stays_reachable():
    assert '$("examBtn").onclick = () => openNotes();' in JS
    assert '$("examChip").onclick = () => openExam();' in JS
    panel = _body("function openNotes(", "function openExam(")
    for call in ("api.note_collection()", "makeNote(d.ncMake)", "openExam(d.ncExam)", "api.open_path(d.ncBrowser)",
                 "openLecture(d.ncOpen)"):
        assert call in panel, call
    assert re.search(r'id="examBtn"[^>]*>.*<span>정리본</span>', HTML)


def test_settings_and_first_run_offer_the_folder_layout_and_recording_cleanup():
    settings = _body("async function openSettings()", "const TT_DAYS")
    for call in ("api.folder_layout_get()", "api.folder_layout_set(", "api.audio_cleanup_get()", "api.audio_cleanup_set("):
        assert call in settings, call
    assert "일 지난 녹음은 휴지통으로 (정리본이 있는 회차만)" in _body("function cleanupRow(", "async function openSettings()")
    setup = _body("function runSetup()", "function startFailed(")
    assert '"폴더 구성"' in setup and "api.folder_layout_get(st.term" in setup
    assert 'api.setup_apply(st.parent, st.term, rows, st.termStart || "", st.termEnd || "", st.style || "")' in setup


def test_hold_screen_asks_one_question_at_a_time_with_the_plain_reason():
    inbox = _body("async function openInbox()", "const NOTE_CSP")
    for needed in ("c.reason_ko", "왜 묻나요", "나중에", "api.hold_defer(", "api.hold_answer(", "api.hold_reject(",
                   "api.hold_undo(", "질문 ${i + 1} / ${n}"):
        assert needed in inbox, needed


def test_qa_helper_stays_out_of_the_public_edition():
    assert '[["qaBtn", "qa"], ["capBtn", "captions"], ["maiHint", "mai"]].forEach(([id, k]) => { const el = $(id); if (el && !ed(k)) { el.hidden = true; el.style.display = "none"; } });' in JS

def test_side_view_takes_the_right_third_and_comes_back():
    """User 2026-11-14: [옆으로 보기] puts the window on the right third at full height, live transcript + a transcript."""
    from lecturehub.ui import app as ui_app

    web = (Path(ui_app.__file__).parent / "web")
    html, js = (web / "index.html").read_text(encoding="utf-8"), (web / "app.js").read_text(encoding="utf-8")
    assert 'id="sideViewBtn"' in html and 'id="svLive"' in html and 'id="svTr"' in html
    assert "api.side_view(on)" in js
    assert hasattr(ui_app.Api, "side_view") and ui_app.SIDE_MIN[0] < ui_app.MAIN_MIN[0]
    src = Path(ui_app.__file__).read_text(encoding="utf-8")
    assert "(work.right - work.left) // 3" in src

def test_live_engine_setting_drives_what_starts():
    """User 2026-11-14: settings pick Gemini Live, Chirp 3 or both for the live transcript."""
    from lecturehub.core import settings
    from lecturehub.ui.backend import Backend

    api = Backend.__new__(Backend)
    assert api.live_engine_get()["engine"] == "quick"
    assert api.live_engine_set("chirp")["engine"] == "chirp" and settings.live_engine() == "chirp"
    assert not api.live_engine_set("whisper")["ok"]
    from lecturehub.ui import app as ui_app

    js = (Path(ui_app.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert 'captions_start(course, liveSource(), consent, token, eng !== "quick", eng !== "chirp")' in js
