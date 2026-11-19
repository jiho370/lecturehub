"""Live captions while someone speaks (사용자 요청 2026-11-06: "구글 트랜스크라이브를 통해서 실시간으로 사람들이 말하는 거를 전사").

By default only Gemini 3.5 Transcribe Live runs (user 2026-11-06: "기본을 … 트랜스크라이브 라이브를 기본으로"); its
finished lines are the captions. Chirp 3 can be added as a second, more accurate line (``--confirmed``, the app's
check box), which costs Cloud credit past the free 60 min a month. Bench 2026-11-06 (synthetic Korean lecture
audio, 54 s, clean and with added room noise; see docs/runbooks/transcription_lessons.md L-019):

- **quick line** — Gemini 3.5 Transcribe Live (Live API, the existing Gemini key). Text 0.7 s after speech, but
  error 4.9 % clean / 20 % noisy, and it invented words in noise. Shown grey, replaced as it goes.
- **confirmed lines** — Chirp 3 (Cloud Speech-to-Text V2, the user's gcloud login and project). Error 0.7 % clean /
  4.3 % noisy when it hears the whole stream, but in noise it found no pause and gave nothing until the end. So the
  app cuts the audio itself at a quiet moment 15–25 s in and sends each piece (``recognize``): 1.3 % clean / 5.9 %
  noisy, each line ready about 5–7 s after its piece ends. 8–14 s pieces lost a sentence in noise (30 %).

Written under ``_lecturehub\\captions\\``: ``<YYYY-MM-DD HHMM> <과목>.txt`` (the captions, ``[MM:SS] text``: Gemini's
finished lines alone, or Chirp's when it is on), with Chirp on also `` (빠른).txt`` (Gemini's lines, for comparison), and ``live.json`` (what the app's
caption panel polls: status, grey line, last lines). Nothing else in the semester folder is touched. A ``stop`` file
there (the app's [그만]) or Ctrl+C ends it; the last piece is still sent.

Consent: provider ``google_live`` per course, asked like the other engines (a blank course is asked each time and
never stored). Audio goes only to Google (Gemini API and Cloud Speech-to-Text). The microphone is opened in shared
mode next to the recorder; nothing is recorded to disk here except the text.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .store import app_dir, ensure_inside

PROVIDER = "google_live"
RATE = 16000
PIECE = RATE // 10            # 100 ms of samples
MIN_S, MAX_S = 15.0, 25.0     # Chirp piece length (bench above)
QUIET_RATIO = 0.25            # a 300 ms window under 25 % of the piece's median energy counts as a pause
GEMINI_MODEL = "models/gemini-3.5-transcribe-live"
GEMINI_URL = ("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
              "GenerativeService.BidiGenerateContent")  # the key goes in the x-goog-api-key header, never the URL
GEMINI_SESSION_S = 540        # the Live session ends at 10 min; start a fresh one at 9
CHIRP_REGION = "us"
CHIRP_MODEL = "chirp_3"
HINT_BOOST = 10.0             # phrase hint weight (L-025: 12 class pieces, the course words came out right)
MAX_HOURS = 5                 # a forgotten caption run stops by itself
QUICK_LAG_S = 2.0             # a quick-line final arrives about this long after the words
SHORT_RATIO = 0.5             # confirmed text under half of what the quick line heard there: the piece lost words


def captions_dir(root: Path) -> Path:
    return ensure_inside(app_dir(root) / "captions", root)


def default_project() -> str | None:
    """The gcloud project (env first, then gcloud's own config file) — read only."""
    for name in ("LECTUREHUB_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"):
        if os.environ.get(name):
            return os.environ[name]
    base = Path(os.environ.get("APPDATA") or Path.home()) / "gcloud"
    try:
        active = (base / "active_config").read_text(encoding="utf-8").strip() or "default"
        for line in (base / "configurations" / f"config_{active}").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "project" and value.strip():
                return value.strip()
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- audio
def to_16k_mono(data: bytes, rate: int, channels: int) -> np.ndarray:
    """int16 interleaved at any rate → int16 mono 16 kHz (box low-pass + linear resample; fine for speech)."""
    x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(x):
        step = rate / RATE
        if step > 1.5:
            k = int(round(step))
            x = np.convolve(x, np.ones(k) / k, mode="same")
        positions = np.arange(0, len(x) - 1, step)
        x = np.interp(positions, np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16)


def file_source(path: Path, out: Callable[[bytes], None], stop: threading.Event, *, pace: float = 1.0) -> None:
    """A recording as if it were live (tests, checks): 16 kHz mono s16le ``.pcm`` as is, anything else via ffmpeg."""
    path = Path(path)
    if path.suffix.lower() == ".pcm":
        pcm = path.read_bytes()
    else:
        import subprocess

        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE),
                              "-f", "s16le", "-"], capture_output=True, check=True).stdout
    for i in range(0, len(pcm), PIECE * 2):
        if stop.is_set():
            return
        out(pcm[i:i + PIECE * 2])
        if pace:
            time.sleep(0.1 * pace)


def device_source(mode: str, out: Callable[[bytes], None], stop: threading.Event, *, device_name: str | None = None) -> None:
    """The microphone (``mic``) or PC sound (``loopback``) in WASAPI shared mode, next to the recorder."""
    from lecturehub.recorder.devices import _load_pyaudio, select_device

    device = select_device(mode, name=device_name)
    backend = _load_pyaudio()
    audio = backend.PyAudio()
    frames = max(256, device.sample_rate // 10)
    stream = audio.open(format=backend.paInt16, channels=device.channels, rate=device.sample_rate, input=True,
                        input_device_index=device.index, frames_per_buffer=frames)
    buffer = np.zeros(0, dtype=np.int16)
    try:
        while not stop.is_set():
            data = stream.read(frames, exception_on_overflow=False)
            buffer = np.concatenate([buffer, to_16k_mono(data, device.sample_rate, device.channels)])
            while len(buffer) >= PIECE:
                out(buffer[:PIECE].tobytes())
                buffer = buffer[PIECE:]
    finally:
        stream.stop_stream()
        stream.close()
        audio.terminate()


# ---------------------------------------------------------------- cutting
class Segmenter:
    """Collects 100 ms pieces and cuts at a pause 15–25 s in (or at the quietest moment at 25 s)."""

    def __init__(self, emit: Callable[[bytes, float, float], None], *, min_s: float = MIN_S, max_s: float = MAX_S):
        self.emit, self.min_s, self.max_s = emit, min_s, max_s
        self.pieces: list[np.ndarray] = []
        self.start_s = 0.0

    def _energy(self) -> np.ndarray:
        return np.array([float(np.mean(p.astype(np.float32) ** 2)) for p in self.pieces])

    def add(self, piece: bytes) -> None:
        self.pieces.append(np.frombuffer(piece, dtype=np.int16))
        n = len(self.pieces)
        if n < self.min_s * 10:
            return
        energy = self._energy()
        floor = float(np.median(energy)) * QUIET_RATIO
        last3 = energy[-3:]
        if len(last3) == 3 and float(last3.mean()) <= floor:
            self._cut(n - 1)  # inside a pause: cut in its middle
        elif n >= self.max_s * 10:
            lo = int(self.min_s * 10)
            window = np.convolve(energy[lo:], np.ones(3) / 3, mode="same")
            self._cut(lo + int(np.argmin(window)) + 1)

    def _cut(self, at: int) -> None:
        head, self.pieces = self.pieces[:at], self.pieces[at:]
        audio = np.concatenate(head).tobytes()
        seconds = len(head) / 10
        self.emit(audio, self.start_s, self.start_s + seconds)
        self.start_s += seconds

    def flush(self) -> None:
        if self.pieces:
            self._cut(len(self.pieces))


# ---------------------------------------------------------------- engines
def chirp_config(phrases=()):
    """Chirp 3 settings; ``phrases`` (the course's pack terms, P-051) go in as an inline phrase set."""
    from google.cloud.speech_v2.types import cloud_speech as cs

    extra = {}
    if phrases:
        extra["adaptation"] = cs.SpeechAdaptation(phrase_sets=[cs.SpeechAdaptation.AdaptationPhraseSet(
            inline_phrase_set=cs.PhraseSet(phrases=[cs.PhraseSet.Phrase(value=p, boost=HINT_BOOST) for p in phrases]))])
    return cs.RecognitionConfig(
        explicit_decoding_config=cs.ExplicitDecodingConfig(
            encoding=cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16, sample_rate_hertz=RATE, audio_channel_count=1),
        language_codes=["ko-KR"], model=CHIRP_MODEL,
        features=cs.RecognitionFeatures(enable_automatic_punctuation=True), **extra)


def chirp_recognizer(project: str, phrases=()):
    """Returns ``recognize(pcm) -> text`` for Chirp 3 on the user's gcloud login (Application Default Credentials)."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech as cs

    client = SpeechClient(client_options=ClientOptions(api_endpoint=f"{CHIRP_REGION}-speech.googleapis.com"))
    config = chirp_config(phrases)
    recognizer = f"projects/{project}/locations/{CHIRP_REGION}/recognizers/_"

    def recognize(pcm: bytes) -> str:
        response = client.recognize(request=cs.RecognizeRequest(recognizer=recognizer, config=config, content=pcm),
                                    timeout=30)
        return " ".join(r.alternatives[0].transcript.strip() for r in response.results if r.alternatives).strip()

    return recognize


def gemini_setup(language: str = "ko-KR") -> dict:
    return {"setup": {"model": GEMINI_MODEL, "generationConfig": {"responseModalities": ["TEXT"]},
                      "inputAudioTranscription": {"languageCodes": [language] if language else [], "mode": "VERBATIM"}}}


def gemini_audio(piece: bytes) -> dict:
    return {"realtimeInput": {"audio": {"data": base64.b64encode(piece).decode(), "mimeType": f"audio/pcm;rate={RATE}"}}}


def gemini_event(message: dict) -> tuple[str, str] | None:
    """('interim'|'final', text) from one Live API server message, else None."""
    content = message.get("serverContent") or {}
    final = (content.get("inputTranscription") or {}).get("text")
    if final:
        return "final", final
    interim = (content.get("interimInputTranscription") or {}).get("text")
    if interim:
        return "interim", interim
    return None


class GeminiLive:
    """The quick grey line. Opens a new Live session every 9 minutes (sessions end at 10)."""

    def __init__(self, key: str, on_event: Callable[[str, str], None], on_error: Callable[[str], None]):
        self.key, self.on_event, self.on_error = key, on_event, on_error
        self.inbox: queue.Queue[bytes | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def feed(self, piece: bytes | None) -> None:
        self.inbox.put(piece)

    def _run(self) -> None:
        from websockets.sync.client import connect

        failures = 0
        while True:
            try:
                with connect(GEMINI_URL, additional_headers={"x-goog-api-key": self.key}, max_size=None,
                             open_timeout=15) as ws:
                    ws.send(json.dumps(gemini_setup()))
                    if "setupComplete" not in json.loads(ws.recv(timeout=15)):
                        raise RuntimeError("Live 세션 설정 실패")
                    failures = 0
                    reader = threading.Thread(target=self._read, args=(ws,), daemon=True)
                    reader.start()
                    began = time.monotonic()
                    while time.monotonic() - began < GEMINI_SESSION_S:
                        try:
                            piece = self.inbox.get(timeout=1)
                        except queue.Empty:
                            continue
                        if piece is None:
                            ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                            reader.join(timeout=6)
                            return
                        ws.send(json.dumps(gemini_audio(piece)))
                    ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                    reader.join(timeout=4)
            except Exception as exc:  # noqa: BLE001 - the quick line is optional; the confirmed lines go on
                failures += 1
                self.on_error(f"빠른 자막(Gemini Live) 오류: {str(exc)[:160]}")
                if failures >= 3:
                    self.on_error("빠른 자막을 끕니다(세 번 연속 실패) — 확정 자막은 계속")
                    while self.inbox.get() is not None:  # drain until the end
                        pass
                    return
                time.sleep(2 * failures)

    def _read(self, ws) -> None:
        try:
            for raw in ws:
                event = gemini_event(json.loads(raw))
                if event:
                    self.on_event(*event)
        except Exception:  # noqa: BLE001, S110 - the session closes at rotation or the end
            pass


# ---------------------------------------------------------------- output
@dataclass
class Board:
    """The text files and live.json the app's panel reads."""

    root: Path
    course: str
    engines: dict
    started: datetime = field(default_factory=lambda: datetime.now().astimezone())
    lines: list = field(default_factory=list)
    quick_lines: list = field(default_factory=list)
    quick_recent: list = field(default_factory=list)  # Gemini's finished lines for live.json (the Q&A helper)
    interim: str = ""
    errors: list = field(default_factory=list)
    state: str = "running"
    hints: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    _written_at: float = 0.0

    def __post_init__(self):
        self.dir = captions_dir(self.root)
        self.dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in (self.course or "미지정") if ch not in '\\/:*?"<>|').strip() or "미지정"
        stem = f"{self.started:%Y-%m-%d %H%M} {safe}"
        n = 2
        self.text_path = self.dir / f"{stem}.txt"
        while self.text_path.exists() or (self.dir / f"{self.text_path.stem} (빠른).txt").exists():
            self.text_path, n = self.dir / f"{stem} ({n}).txt", n + 1
        # Gemini alone: its finished lines are the captions, written once to the main file (no second file)
        self.solo = not self.engines.get("confirmed")
        self.quick_path = self.text_path if self.solo else self.dir / f"{self.text_path.stem} (빠른).txt"
        for path in dict.fromkeys((self.text_path, self.quick_path)):
            with path.open("x", encoding="utf-8-sig", newline="\r\n") as out:
                out.write(f"# 실시간 자막 · {self.course or '과목 미지정'} · {self.started:%Y-%m-%d %H:%M} 시작\n")
        self.save(force=True)

    @staticmethod
    def clock(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"

    def confirmed(self, start_s: float, end_s: float, text: str) -> None:
        """One Chirp piece. When it came back with much less speech than the quick line heard in the same window
        (2026-11-06 bench: a piece crossing into noise lost two sentences), the quick line's words are kept too,
        marked "(빠른 줄)", so nothing said is silently dropped."""
        with self.lock:
            heard = " ".join(t for at, t in self.quick_lines if start_s < at - QUICK_LAG_S <= end_s)
            rows = [{"at": round(start_s, 1), "end": round(end_s, 1), "text": text}] if text else []
            if _syllables(heard) >= 15 and _syllables(text) < SHORT_RATIO * _syllables(heard):
                rows.append({"at": round(start_s, 1), "end": round(end_s, 1), "text": heard, "from": "quick"})
            if not rows:
                return
            self.lines += rows
            with self.text_path.open("a", encoding="utf-8", newline="\r\n") as out:
                for row in rows:
                    mark = " (빠른 줄)" if row.get("from") == "quick" else ""
                    out.write(f"[{self.clock(start_s)}]{mark} {row['text']}\n")
        self.save(force=True)

    def quick(self, kind: str, text: str) -> None:
        with self.lock:
            if kind == "final":
                self.interim = ""
                at = self.elapsed()
                self.quick_lines.append((at, text))
                self.quick_lines = self.quick_lines[-200:]
                if text.strip():
                    self.quick_recent = (self.quick_recent + [{"at": round(at, 1), "text": text.strip()}])[-60:]
                if self.solo and text.strip():
                    self.lines.append({"at": round(at, 1), "text": text.strip()})
                with self.quick_path.open("a", encoding="utf-8", newline="\r\n") as out:
                    out.write(f"[{self.clock(at)}] {text}\n")
            else:
                self.interim = text
        self.save()

    def mark(self, text: str) -> None:
        """A note line in the captions file, e.g. "# 일시중단 12:30" (P-050); the helper skips "#" lines."""
        with self.lock:
            for path in dict.fromkeys((self.text_path, self.quick_path)):
                with path.open("a", encoding="utf-8", newline="\r\n") as out:
                    out.write(f"# {text} {self.clock(self.elapsed())}\n")
        self.save(force=True)

    def error(self, message: str) -> None:
        with self.lock:
            self.errors = (self.errors + [{"at": round(self.elapsed(), 1), "message": message}])[-5:]
        self.save(force=True)

    def elapsed(self) -> float:
        return (datetime.now().astimezone() - self.started).total_seconds()

    def save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._written_at < 0.25:
            return
        with self.lock:
            self._written_at = now
            status = {"state": self.state, "pid": os.getpid(), "course": self.course,
                      "started_at": self.started.isoformat(timespec="seconds"), "elapsed_s": round(self.elapsed(), 1),
                      "engines": self.engines, "hints": self.hints, "interim": self.interim, "lines": self.lines[-60:],
                      "line_count": len(self.lines), "errors": self.errors, "file": str(self.text_path),
                      "quick": self.quick_recent[-60:],
                      "quick_file": "" if self.solo else str(self.quick_path)}
            path = self.dir / "live.json"
            temp = path.with_name(f"live.{os.getpid()}.tmp")
            temp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            for _ in range(5):
                try:
                    os.replace(temp, path)
                    break
                except PermissionError:  # the app is reading it this instant
                    time.sleep(0.02)


def _syllables(text: str) -> int:
    return sum(1 for ch in text or "" if ch.isalnum())


# ---------------------------------------------------------------- run
def run(root: Path, course: str, *, source: Callable[[Callable[[bytes], None], threading.Event], None],
        recognize: Callable[[bytes], str] | None, gemini_key: str | None, stop: threading.Event | None = None,
        say: Callable[[str], None] | None = None, quick_factory=GeminiLive, quick_on: bool = True,
        hints: int = 0) -> dict:
    """Run until the source ends, ``stop`` is set, a ``stop`` file appears, or MAX_HOURS pass.

    With a Gemini key, Gemini Live can be switched while running: a ``quick.on`` / ``quick.off`` file in the captions
    folder (``request_quick``) — e.g. Chirp alone through the class, Gemini Live added for the Q&A after a talk.
    ``pause.on`` / ``pause.off`` (``request_pause``, P-050) stop and resume listening into the same file: while paused
    no audio goes to Chirp or Gemini (nothing is billed), the piece in progress is finished first, and Gemini Live
    is closed and opened again on resume (an idle Live session would be dropped by the server)."""
    say = say or (lambda text: None)
    stop = stop or threading.Event()
    board = Board(Path(root), course, {"confirmed": f"{CHIRP_MODEL}@{CHIRP_REGION}" if recognize else None,
                                       "quick": GEMINI_MODEL.split("/")[-1] if gemini_key and quick_on else None})
    board.hints = hints  # how many phrase hints Chirp got (P-051), shown in the app
    stop_file = board.dir / "stop"
    stop_file.unlink(missing_ok=True)
    pieces: queue.Queue[tuple[bytes, float, float] | None] = queue.Queue()

    def confirm_worker():
        while True:
            item = pieces.get()
            if item is None:
                return
            audio, start_s, end_s = item
            for attempt in range(3):
                try:
                    text = recognize(audio)
                    board.confirmed(start_s, end_s, text)
                    if text:
                        say(f"[{board.clock(start_s)}] {text}")
                    break
                except Exception as exc:  # noqa: BLE001 - one lost piece must not end the captions
                    if attempt == 2:
                        board.error(f"확정 자막 실패({board.clock(start_s)}~): {str(exc)[:160]}")
                    else:
                        time.sleep(1 + attempt * 2)

    worker = threading.Thread(target=confirm_worker, daemon=True) if recognize else None
    if worker:
        worker.start()
    segmenter = Segmenter(lambda audio, a, b: pieces.put((audio, a, b)))
    live = {"quick": None, "ended": [], "checked": 0.0, "paused": False, "quick_before": False}
    for name in ("quick.on", "quick.off", "pause.on", "pause.off"):
        (board.dir / name).unlink(missing_ok=True)

    def quick_switch(on: bool) -> None:
        if on and live["quick"] is None and gemini_key:
            live["quick"] = quick_factory(gemini_key, board.quick, board.error)
            live["quick"].start()
            board.engines["quick"] = GEMINI_MODEL.split("/")[-1]
        elif not on and live["quick"] is not None:
            live["quick"].feed(None)
            live["ended"].append(live["quick"])
            live["quick"] = None
            board.engines["quick"] = None
            with board.lock:
                board.interim = ""
        board.save(force=True)

    if not recognize and not quick_on:
        quick_on = True  # nothing else would run
    quick_switch(bool(quick_on))
    deadline = time.monotonic() + MAX_HOURS * 3600

    def pause_switch(on: bool) -> None:
        if on == live["paused"]:
            return
        live["paused"] = on
        if on:
            if recognize:
                segmenter.flush()  # what was said up to now is still recognized
            live["quick_before"] = live["quick"] is not None
            quick_switch(False)
            board.state = "paused"
            board.mark("일시중단")
        else:
            board.state = "running"
            board.mark("이어서")
            if live["quick_before"]:
                quick_switch(True)
        board.save(force=True)

    def take(piece: bytes) -> None:
        now = time.monotonic()
        if now - live["checked"] >= 0.5:
            live["checked"] = now
            for name, on in (("pause.on", True), ("pause.off", False)):
                if _take_flag(board.dir / name):
                    pause_switch(on)
            for name, on in (("quick.on", True), ("quick.off", False)):
                if _take_flag(board.dir / name):
                    if live["paused"]:
                        live["quick_before"] = on  # applied on resume
                    else:
                        quick_switch(on)
        if not live["paused"]:  # flags first, so a pause holds back the piece that came with it
            if recognize:
                segmenter.add(piece)
            if live["quick"] is not None:
                live["quick"].feed(piece)
        if stop_file.exists() or now > deadline:
            stop.set()

    try:
        source(take, stop)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - say why the audio stopped, keep what was captured
        board.error(f"오디오 입력 오류: {str(exc)[:160]}")
    finally:
        board.state = "finishing"
        board.save(force=True)
        if recognize:
            segmenter.flush()
            pieces.put(None)
        if live["quick"] is not None:
            live["quick"].feed(None)
            live["ended"].append(live["quick"])
        if worker:
            worker.join(timeout=60)
        for ended in live["ended"]:
            ended.thread.join(timeout=10)
        board.interim = ""
        board.state = "stopped"
        board.save(force=True)
        stop_file.unlink(missing_ok=True)
    return {"ok": True, "file": str(board.text_path), "quick_file": "" if board.solo else str(board.quick_path),
            "lines": len(board.lines),
            "seconds": round(board.elapsed(), 1), "errors": board.errors}


def _take_flag(flag: Path) -> bool:
    """A switch file from the app, removed as it is taken. Windows can hold a fresh file a moment (being written,
    virus scan): then it stays and is taken at the next check, instead of the error ending the captions."""
    if not flag.exists():
        return False
    try:
        flag.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _set_flag(folder: Path, name: str, opposite: str) -> None:
    """Write a switch file for the running captions: the opposite one goes first, the new one appears whole."""
    for _ in range(20):
        try:
            (folder / opposite).unlink(missing_ok=True)
            break
        except PermissionError:
            time.sleep(0.05)
    temp = folder / f"{name}.{os.getpid()}.tmp"
    temp.write_text("1", encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(temp, folder / name)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(temp, folder / name)


def request_quick(root: Path, on: bool) -> None:
    """Switch Gemini Live on or off in the running captions (picked up within about half a second)."""
    folder = captions_dir(Path(root))
    folder.mkdir(parents=True, exist_ok=True)
    _set_flag(folder, "quick.on" if on else "quick.off", "quick.off" if on else "quick.on")


def request_pause(root: Path, on: bool) -> None:
    """Pause or resume the running captions (picked up within about half a second; same file, P-050)."""
    folder = captions_dir(Path(root))
    folder.mkdir(parents=True, exist_ok=True)
    _set_flag(folder, "pause.on" if on else "pause.off", "pause.off" if on else "pause.on")


def status(root: Path) -> dict:
    """live.json, with ``state`` corrected to ``stopped`` when its process is gone."""
    path = captions_dir(Path(root)) / "live.json"
    for attempt in range(4):  # Windows: reading while the captions replace the file fails for a moment (P-050)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            if attempt == 3 or not path.exists():
                return {"state": "none"}
            time.sleep(0.03)
    if data.get("state") in ("running", "paused", "finishing") and not _alive(int(data.get("pid") or 0)):
        data["state"] = "stopped"
    return data


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def request_stop(root: Path) -> None:
    folder = captions_dir(Path(root))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stop").write_text("stop", encoding="utf-8")
