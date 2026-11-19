"""Trial semester (2026-11-13): a new user sees the whole flow in a minute without their own data.

``make(dest)`` builds a small, fully synthetic semester in an app-private folder (never the user's library):
one course in the timetable, a 3-page slide PDF, one short app recording (generated tone), its transcript
(prepared synthetic lecture text, labelled as the trial's), and a 정리본 made by running the real note pipeline
with a prepared answer — so the evidence check really runs, and one made-up sentence is really thrown out.
Nothing is sent anywhere. Running it again on the same folder reuses what is there.
"""
from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

COURSE = "유체역학 체험"
LECTURE_TITLE = "관수로 마찰손실"

# A synthetic lecture (written for the trial, not a real class). "mm:ss" lines, as the app's transcripts.
TRANSCRIPT = [
    ("00:00", "자 오늘은 관수로 흐름에서 마찰손실을 어떻게 구하는지 보겠습니다."),
    ("00:48", "관 안을 물이 흐르면 벽과의 마찰 때문에 에너지가 조금씩 줄어드는데 이걸 수두손실이라고 불러요."),
    ("02:10", "가장 많이 쓰는 식이 다르시-바이스바흐 식입니다. 손실수두는 마찰계수 곱하기 관 길이 나누기 지름 곱하기 속도수두예요."),
    ("04:05", "여기서 마찰계수 f는 흐름이 층류인지 난류인지에 따라 구하는 방법이 달라요."),
    ("05:30", "레이놀즈 수가 2000 정도보다 작으면 층류라고 보고 이때 f는 64 나누기 레이놀즈 수로 바로 구합니다."),
    ("07:12", "난류에서는 관 벽의 거칠기까지 들어가서 무디 선도를 읽거나 콜브룩 식을 풀어야 해요."),
    ("09:40", "예제를 하나 풀어 볼게요. 지름 0.1미터 길이 100미터 관에 평균 유속 2미터 퍼 세컨드로 물이 흐릅니다."),
    ("11:05", "마찰계수를 0.02로 두면 손실수두는 0.02 곱하기 1000 곱하기 속도수두 0.204 해서 약 4.08미터가 나와요."),
    ("13:20", "이 계산 과정은 중간고사에 그대로 나오니까 단위까지 꼭 맞춰서 연습해 두세요."),
    ("15:02", "그리고 다음 주 수요일 수업 시작할 때 오늘 내용으로 쪽지 퀴즈 볼 거예요."),
    ("16:30", "부차적 손실, 그러니까 밸브나 굽은 관에서 생기는 손실은 다음 시간에 하겠습니다."),
]

SLIDES = [
    ("1주차 · 관수로 마찰손실", ["관 벽 마찰 → 에너지 감소 = 수두손실", "오늘: 다르시-바이스바흐 식, 마찰계수, 예제"]),
    ("다르시-바이스바흐 식", ["h_f = f · (L/D) · V²/2g", "층류(Re < 2000): f = 64/Re", "난류: 무디 선도 / 콜브룩 식"]),
    ("예제", ["D = 0.1 m, L = 100 m, V = 2 m/s, f = 0.02", "V²/2g = 0.204 m", "h_f = 0.02 × 1000 × 0.204 ≈ 4.08 m"]),
]


def default_dest() -> Path:
    from lecturehub.core import settings

    return settings.settings_path().parent / "체험 학기"


def _slides_pdf(path: Path) -> None:
    from lecturehub.materials import pdfdoc  # pypdfium2; PyMuPDF (AGPL) must never be needed at run time

    pdfdoc.write_slides(path, SLIDES)


def _recording(root: Path, started: datetime) -> Path:
    """A short real app recording session (a quiet synthetic tone), finalized like any other."""
    from lecturehub.recorder.capture import FakeSource
    from lecturehub.recorder.finalize import finalize_session
    from lecturehub.recorder.session import RecordingSession

    source = FakeSource(sample_rate=16000, duration_seconds=3.0)
    session = RecordingSession(source, root / "_lecturehub" / "recordings", course=COURSE, course_source="user_pick")
    folder = session.start()
    while source.active:
        pass
    session.stop()
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    meta.update(started_local=started.isoformat(), started_at=int(started.timestamp() * 1000), demo=True)
    (folder / "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    finalize_session(folder)
    return folder


def _answer(prompt_text: str, schema: dict) -> dict:
    """The prepared note: the shape a real engine returns. Block b06 cites a line that does not exist,
    so the evidence check throws it out — the trial shows that on purpose."""
    tid = re.search(r"전사ID ([^)\s]+)", prompt_text).group(1)
    seq = {clock: n for n, (clock, _) in enumerate(TRANSCRIPT)}
    t = lambda clock, quote=None: {"t": tid, "s": seq[clock], **({"q": quote} if quote else {})}  # noqa: E731
    return {"model": "trial", "blocks": [
        {"key": "b01", "kind": "heading", "text": "수두손실과 다르시-바이스바흐 식"},
        {"key": "b02", "kind": "paragraph", "cites": [t("00:48", "이걸 수두손실이라고 불러요")],
         "text": "관 속을 흐르는 물은 벽과의 마찰로 에너지를 잃는다. 이 손실을 수두손실이라고 한다."},
        {"key": "b03", "kind": "paragraph", "cites": [t("02:10"), t("04:05")],
         "text": "손실수두는 다르시-바이스바흐 식으로 구한다. 마찰계수에 관 길이와 지름의 비, 속도수두를 곱한 값이며, "
                 "마찰계수는 흐름이 층류인지 난류인지에 따라 구하는 방법이 다르다."},
        {"key": "b04", "kind": "paragraph", "cites": [t("05:30", "64 나누기 레이놀즈 수"), t("07:12")],
         "text": "레이놀즈 수가 2000보다 작은 층류에서는 마찰계수가 64를 레이놀즈 수로 나눈 값이다. "
                 "난류에서는 벽의 거칠기까지 고려해 무디 선도나 콜브룩 식을 쓴다."},
        {"key": "b05", "kind": "heading", "text": "예제"},
        {"key": "b06", "kind": "paragraph", "cites": [{"t": tid, "s": 99, "q": "레이놀즈 수는 항상 4000이다"}],
         "text": "교수님은 레이놀즈 수를 항상 4000으로 두라고 했다."},
        {"key": "b07", "kind": "paragraph", "cites": [t("09:40"), t("11:05", "약 4.08미터가 나와요")],
         "text": "지름 0.1 m, 길이 100 m인 관에 평균 유속 2 m/s로 물이 흐를 때 마찰계수를 0.02로 두면 "
                 "손실수두는 0.02 × 1000 × 0.204로 약 4.08 m이다."},
        {"key": "b08", "kind": "item", "cites": [t("13:20", "중간고사에 그대로 나오니까")],
         "text": "이 계산 과정은 중간고사에 그대로 나온다고 했다. 단위까지 맞춰 연습할 것."},
        {"key": "b09", "kind": "item", "cites": [t("15:02", "쪽지 퀴즈 볼 거예요")],
         "text": "다음 주 수요일 수업 시작에 오늘 내용으로 쪽지 퀴즈가 있다."},
        {"key": "b10", "kind": "external", "ext": {"confidence": "high", "source": "textbook"},
         "text": "속도수두 V²/2g는 유속이 만드는 에너지를 물기둥 높이로 나타낸 것이다."},
    ]}


def make(dest: Path | None = None, *, now: datetime | None = None) -> dict:
    """Build (or reuse) the trial semester and return where it is and what is in it."""
    from lecturehub.core import db, timetable
    from lecturehub.study import note_job

    from . import app_ingest, index_root

    dest = Path(dest or default_dest())
    now = (now or datetime.now()).astimezone()
    database = dest / "_lecturehub" / "lecturehub.sqlite"
    if database.is_file():
        with closing(db.connect(database)) as connection:
            done = connection.execute("SELECT count(*) FROM StudyArtifact WHERE kind = 'note'").fetchone()[0]
        if done:
            return {"root": str(dest), "state": "ready", "course": COURSE}
    started = (now - timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
    weekday = timetable.DAYS[started.weekday()]
    dest.mkdir(parents=True, exist_ok=True)
    timetable.write(dest, [{"name": COURSE, "slots": [{"day": weekday, "start": "09:00", "end": "10:15"}]}], now=now)
    course_dir = dest / timetable.folder_name(COURSE)
    _slides_pdf(course_dir / "강의자료" / "1주차 관수로 마찰손실.pdf")
    index_root(dest)
    with closing(db.connect(database)) as connection:
        folder = _recording(dest, started)
        linked = app_ingest.link_recording(connection, folder)
        if linked.get("state") != "linked":
            raise RuntimeError(f"체험 녹음을 회차에 연결하지 못했습니다: {linked.get('reason')}")
        output = dest / "_lecturehub" / "transcripts" / f"{started:%y.%m.%d} {COURSE}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(f"{clock} \n{text}\n\n" for clock, text in TRANSCRIPT), encoding="utf-8-sig")
        app_ingest.register_job_transcript(connection, {
            "job_id": "trial", "state": "done", "audio": linked["master"], "output": str(output),
            "preset": {"verified_at": "trial"}, "chunks": []})
        connection.commit()
        note = note_job.run(connection, dest, linked["lecture_id"], executor=_answer, executor_label="app")
        connection.commit()
    return {"root": str(dest), "state": "made", "course": COURSE, "lecture_id": linked["lecture_id"], "note": note.get("html"),
            "rejected": note.get("rejected")}


if __name__ == "__main__":  # python -m lecturehub.index.demo  (checks the installed build without the window)
    import json

    print(json.dumps(make(), ensure_ascii=False))