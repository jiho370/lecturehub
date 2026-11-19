"""Resource review 2026-11-09 (P-053): helpers stop with the app, qa.json is not rewritten per streamed piece."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from lecturehub.core import edition
from lecturehub.core import parent_watch
if edition.has("qa"):
    from lecturehub.transcription import qa_assist
from lecturehub.ui.backend import Backend


def test_openblas_threads_limited_on_import():
    out = subprocess.run([sys.executable, "-c", "import lecturehub, os; print(os.environ['OPENBLAS_NUM_THREADS'])"],
                         capture_output=True, text=True, env={k: v for k, v in os.environ.items()
                                                              if k != "OPENBLAS_NUM_THREADS"}, check=True)
    assert out.stdout.strip() == "1"


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_streamed_answer_is_written_at_most_every_interval_and_flushed(tmp_path, monkeypatch):
    board = qa_assist.Board(tmp_path, "합성 과목", None, "sonnet")
    index = board.add_card({"answer": "", "done": False})
    writes = []
    real = board.save
    monkeypatch.setattr(board, "save", lambda: (writes.append(1), real()))
    monkeypatch.setattr(board, "saved_at", 0.0)
    for n in range(60):
        board.stream(index, answer="합성 답 " * (n + 1))
    assert len(writes) <= 3  # the first piece at once, the rest wait for the timer (not 60 writes)
    deadline = time.monotonic() + 3
    while board.timer is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    with board.writing:  # save() clears the timer before it writes the file: wait for that write to finish
        pass
    saved = json.loads((tmp_path / "_lecturehub" / "qa" / "qa.json").read_text(encoding="utf-8"))
    assert saved["cards"][index]["answer"] == "합성 답 " * 60


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_final_card_write_is_immediate_and_cancels_the_timer(tmp_path):
    board = qa_assist.Board(tmp_path, "합성 과목", None, "sonnet")
    index = board.add_card({"answer": "", "done": False})
    board.stream(index, answer="앞부분")
    board.stream(index, answer="앞부분 뒤")
    board.card(index, answer="끝", done=True)
    assert board.timer is None
    saved = json.loads((tmp_path / "_lecturehub" / "qa" / "qa.json").read_text(encoding="utf-8"))
    assert saved["cards"][index] == {"answer": "끝", "done": True}


class ReadySession:
    def ask(self, text, on_text=None, timeout=0):
        return {"ok": True, "text": "준비됨"}

    def close(self):
        pass


@pytest.mark.skipif(not edition.has("qa"), reason="Q&A is excluded from the public edition")
def test_helper_stops_by_itself_after_max_hours(tmp_path):
    folder = tmp_path / "자료"
    folder.mkdir()
    (folder / "대본.md").write_text("합성 대본", encoding="utf-8")
    result = qa_assist.run(tmp_path, "합성 과목", folder, session_factory=ReadySession, live_reader=dict,
                           stop=threading.Event(), poll_s=0.01, max_hours=0)
    status = qa_assist.status(tmp_path)
    assert result["ok"] and status["state"] == "stopped" and "시간이 지나" in status["error"]


def test_parent_watch_sets_stop_when_the_parent_ends():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    stop = threading.Event()
    said = []
    parent_watch.watch(child.pid, stop, poll_s=0.05, say=said.append)
    assert not stop.is_set()
    child.wait()
    assert stop.wait(5) and said == ["앱이 닫혀 멈춥니다"]


def test_parent_watch_gone_already_or_no_pid():
    stop = threading.Event()
    assert parent_watch.watch(None, stop) is None and not stop.is_set()
    parent_watch.watch(12345, stop, waiter=lambda pid: None)
    assert stop.is_set()


def test_app_close_stops_running_helpers_only(tmp_path, monkeypatch):
    from lecturehub.transcription import captions

    calls = []
    if edition.has("qa"):
        monkeypatch.setattr(qa_assist, "request_stop", lambda root: calls.append("qa"))
    monkeypatch.setattr(captions, "request_stop", lambda root: calls.append("captions"))
    fake = SimpleNamespace(root=tmp_path, qa_status=lambda: {"running": False},
                           captions_status=lambda: {"running": True})
    assert Backend.stop_live_helpers(fake) == ["captions"] and calls == ["captions"]

    def broken():
        raise OSError("합성 실패")

    fake.qa_status = broken
    assert Backend.stop_live_helpers(fake) == ["captions"]  # never raises while the window closes
