"""The app's read/write access to the lecture library (DB), isolated from recording.

Every method returns {"ok": False, "error": ...} instead of raising, so a
missing, broken or too-new database (D-051) never blocks the window or the
recorder (D-050). Connections are short-lived.
"""
from __future__ import annotations

import threading
from contextlib import closing
from datetime import date
from pathlib import Path

from lecturehub.core import db
from lecturehub.core.paths import database_path

_CLOVA_LOCK = threading.Lock()  # the [확인] button and the weekly check never run together
_NOTICE_LOCK = threading.Lock()  # one AI reading of class notices at a time (start-up and the loop)
_SETUP_LOCK = threading.Lock()  # the library is built or upgraded once, whichever window call comes first


def _safe(method):
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, **method(self, *args, **kwargs)}
        except Exception as exc:  # noqa: BLE001 - the window shows the reason and keeps working
            return {"ok": False, "error": str(exc)}
    wrapper.__name__ = method.__name__
    return wrapper


def _move_targets(organize, course_dir: Path) -> list[str]:
    """Categories offered as move targets: the layout's own, plus any other category folder that already exists."""
    active = organize.active_categories()
    return [c for c in organize.CATEGORIES if c in active or (course_dir / c).is_dir()]


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = database_path(self.root)
        self._ready = False

    def _open(self):
        self._ensure()
        return closing(db.connect(self.path))

    def _ensure(self) -> None:
        """Once per run, before the first use: build the library of a semester folder that has none yet (the
        window's setup only makes folders; before 2026-11-16 only the trial semester and the command line built
        it, so a new install had an empty library that could not link a recording), and after an app update
        bring an older library up to date with a backup first (installed builds and the public edition)."""
        if self._ready:
            return
        with _SETUP_LOCK:
            if self._ready:
                return
            if not self.path.is_file():
                if not self.root.is_dir():
                    raise FileNotFoundError("학기 폴더를 찾을 수 없습니다. 설정에서 폴더 위치를 확인하세요.")
                from lecturehub.index import index_root

                index_root(self.root)
            elif db.auto_upgrade_allowed():
                db.upgrade(self.path)
            self._ready = True

    @_safe
    def sidebar(self, today: date) -> dict:
        from lecturehub.index import lectures

        with self._open() as connection:
            items = lectures.lecture_list(connection)
            from lecturehub.index.extra import extra_course_ids

            extra = extra_course_ids(connection)
            courses = [{"id": r[0], "folder": r[1], "count": r[2], "extra": r[0] in extra} for r in connection.execute(
                "SELECT c.id, c.folder_name, count(l.id) FROM Course c LEFT JOIN Lecture l ON l.course_id = c.id "
                "GROUP BY c.id ORDER BY c.rowid")]
            from lecturehub.index import holds

            waiting = holds.open_count(connection)
            from datetime import datetime as _dt, time as _time

            from lecturehub.core import timetable

            slots = {c["folder"]: c["slots"] for c in timetable.read(self.root)}
            noon = _dt.combine(today, _time(12))  # TT-001: today's classes first, then the week, then the rest
            courses.sort(key=lambda c: (c["extra"], timetable.order_key(c["folder"], slots.get(c["folder"], []), noon)))
            reviewable = {r[0] for r in connection.execute(
                "SELECT DISTINCT lecture_id FROM Transcript t WHERE EXISTS "
                "(SELECT 1 FROM TranscriptSegment s WHERE s.transcript_id = t.id)")}
        groups: list[dict] = []
        for item in items:
            item["reviewable"] = item["id"] in reviewable
            label = lectures.group_label(item["date"], today)
            if not groups or groups[-1]["label"] != label:
                groups.append({"label": label, "items": []})
            groups[-1]["items"].append(item)
        return {"courses": courses, "groups": groups, "holds": waiting}

    @_safe
    def lecture(self, lecture_id: str) -> dict:
        from lecturehub.index import lectures, media_range
        from lecturehub.study import corrections, notes

        with self._open() as connection:
            detail = lectures.lecture_detail(connection, lecture_id)
            pointer = detail["in_use"]
            if pointer:
                view = corrections.corrected_segments(connection, pointer["transcript_id"])  # S4: raw stays as is
                detail["orphaned_corrections"] = view["orphaned"]
                span = media_range.in_range(connection, pointer["transcript_id"])  # D-057: outside the lecture part
                detail["segments"] = [s for s in view["segments"] if span is None or span[0] <= s["start_ms"] < span[1]]
                detail["hidden_segments"] = len(view["segments"]) - len(detail["segments"])
                detail["media"] = lectures.media_for_transcript(connection, pointer["transcript_id"])
            elif detail["recordings"]:  # no transcript yet: the lecture's own recording can still be played
                first = detail["recordings"][0]
                detail["media"] = {"id": first["id"], "path": first["path"], "duration_ms": first["duration_ms"],
                                   "exists": first["exists"]}
            detail["bookmarks"] = _bookmarks_for(detail.get("media"), detail["recordings"])
            detail["note"] = self._note(connection, lecture_id, notes)
            # the range card goes to the recording that ran long (not always the first one of the lecture)
            suggested = {r["id"]: media_range.suggest(connection, r["id"]) for r in detail["recordings"]}
            asks = [media_id for media_id, s in suggested.items() if s.get("ask")]
            master = asks[0] if asks else detail["recordings"][0]["id"] if detail["recordings"] else None
            detail["range"] = ({"media_id": master, "suggest": suggested[master],
                                "decided": media_range.decided_range(connection, master)} if master else None)
        return {"lecture": detail}

    @_safe
    def note_collection(self) -> dict:
        """Per course, its lectures by date with the HTML 정리본 attached to each (the rail's 정리본 button, 2026-11-13).
        A lecture can get one with [정리본 만들기] once it has a transcript with text."""
        import json

        from lecturehub.index import lectures

        with self._open() as connection:
            items = lectures.lecture_list(connection)
            courses = [{"id": r[0], "folder": r[1]} for r in connection.execute(
                "SELECT id, folder_name FROM Course ORDER BY rowid")]
            readable = {r[0] for r in connection.execute(
                "SELECT DISTINCT lecture_id FROM Transcript t WHERE EXISTS "
                "(SELECT 1 FROM TranscriptSegment s WHERE s.transcript_id = t.id)")}
            attached: dict[str, list[dict]] = {}
            for artifact_id, path, ids in connection.execute(
                    "SELECT id, path, lecture_ids FROM StudyArtifact WHERE kind != 'lecture_note' ORDER BY rowid"):
                if not str(path).lower().endswith((".html", ".htm")):
                    continue
                try:
                    linked = json.loads(ids or "[]")
                except ValueError:
                    linked = []
                for lecture_id in linked if isinstance(linked, list) else []:
                    attached.setdefault(lecture_id, []).append(
                        {"id": artifact_id, "path": path, "name": Path(path).name, "exists": Path(path).is_file()})
        by_course: dict[str, list[dict]] = {}
        for item in sorted(items, key=lambda x: (x["date"], x["session_index"] or 0)):
            by_course.setdefault(item["course_id"], []).append({
                "id": item["id"], "date": item["date"], "label": item["label"],
                "notes": attached.get(item["id"], []), "can_make": item["id"] in readable})
        out = [{**c, "lectures": by_course.get(c["id"], [])} for c in courses]
        for course in out:
            course["with_notes"] = sum(1 for x in course["lectures"] if any(n["exists"] for n in x["notes"]))
        return {"courses": [c for c in out if c["lectures"]]}

    @staticmethod
    def _note(connection, lecture_id: str, notes) -> dict | None:
        """The latest evidence-linked note, with material titles/paths so chips can open the page (D-056)."""
        note = notes.load(connection, lecture_id)
        if note is None:
            return None
        materials = {r[0]: {"title": r[1], "path": r[2]} for r in connection.execute("SELECT id, title, path FROM Material")}
        for block in note["blocks"]:
            for cite in block["cites"]:
                if cite["kind"] == "material":
                    cite.update(materials.get(cite["material_id"], {"title": "", "path": None}))
        return note

    # ---------- TT-001: after the timetable is edited in settings, weekly slots follow 시간표.csv
    @_safe
    def sync_timetable(self) -> dict:
        from lecturehub.index.ingest import sync_schedule

        if not self.path.is_file():
            return {"skipped": "아직 색인된 라이브러리가 없습니다"}
        with self._open() as connection:
            result = sync_schedule(self.root, connection)
            connection.commit()
        return result

    # ---------- trial semester (index/demo.py): a new user tries the whole flow without their own data
    @staticmethod
    def make_trial() -> dict:
        """Build the synthetic trial semester in the app's own folder and point the app at it (restart to open)."""
        from lecturehub.core import settings
        from lecturehub.index import demo

        current = settings.data_root()
        if current is not None and not settings.get("trial"):
            return {"ok": False, "error": "이미 학기 폴더가 정해져 있어 체험 학기로 바꾸지 않습니다."}
        try:
            result = demo.make()
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "error": f"체험 학기를 만들지 못했습니다: {exc}"}
        from lecturehub.ui.setup import write_recorder_config

        root = Path(result["root"])
        table = root / "시간표.csv"
        try:  # without recorder.toml the window refuses to record; a config of the user's own stays as it is
            write_recorder_config(root, table if table.is_file() else None, replace=bool(settings.get("trial")))
        except OSError as exc:
            return {"ok": False, "error": f"체험 학기 녹음 설정을 쓰지 못했습니다: {exc}"}
        settings.save({"data_root": result["root"], "trial": True})
        return {"ok": True, **result}

    # ---------- recordings to the Recycle Bin some days after the Google transcript (index/audio_cleanup.py)
    @_safe
    def audio_cleanup(self) -> dict:
        from lecturehub.index import audio_cleanup

        if not self.path.is_file():
            return {"state": "no_library", "recycled": 0, "bytes": 0}
        with self._open() as connection:
            result = audio_cleanup.run(connection, self.root)
            connection.commit()
        return result

    # ---------- S5: hold inbox (answer once, then only that file is registered)
    @_safe
    def holds(self) -> dict:
        from lecturehub.index import holds

        with self._open() as connection:
            return holds.questions(connection)

    def _rejoin(self, connection, ids: list[str]) -> list[dict]:
        from lecturehub.index import holds, index_root

        connection.commit()
        paths = {Path(r[0]) for r in connection.execute(
            f"SELECT source_path FROM IngestItem WHERE id IN ({','.join('?' * len(ids))})", ids)}
        index_root(self.root, connection, only_paths=paths)
        return holds.outcome(connection, ids)

    @_safe
    def hold_answer(self, ids: list[str], value: dict) -> dict:
        from lecturehub.index import holds

        with self._open() as connection:
            holds.answer(connection, list(ids), dict(value))
            return {"outcome": self._rejoin(connection, list(ids))}

    @_safe
    def hold_defer(self, ids: list[str]) -> dict:
        from lecturehub.index import holds

        with self._open() as connection, connection:
            holds.defer(connection, list(ids))
        return {}

    @_safe
    def hold_reject(self, ids: list[str]) -> dict:
        from lecturehub.index import holds

        with self._open() as connection, connection:
            holds.reject(connection, list(ids))
        return {}

    @_safe
    def hold_undo(self, item_id: str) -> dict:
        from lecturehub.index import holds

        with self._open() as connection, connection:
            previous = holds.undo(connection, item_id)
        return {"previous_state": previous}

    @_safe
    def correct(self, transcript_id: str, seq: int, char_from: int, char_to: int, after: str, shown_text: str) -> dict:
        """The user fixes words in the transcript view: a Correction over the raw segment (S4, CORR-001)."""
        from lecturehub.study import corrections

        with self._open() as connection, connection:
            correction_id = corrections.add(connection, transcript_id, int(seq), int(char_from), int(char_to), after,
                                            shown_text=shown_text)
        return {"correction_id": correction_id}

    @_safe
    def edit_note_block(self, lecture_id: str, key: str, text: str) -> dict:
        from lecturehub.study import notes

        with self._open() as connection, connection:
            artifact_id = notes.edit_block(connection, lecture_id, key, text, "앱에서 사용자가 고침")
        return {"artifact_id": artifact_id}

    @_safe
    def make_note(self, lecture_id: str, block: str | None, busy, consent_provider=None) -> dict:
        """Run the note job (minutes; the app calls this off the UI thread)."""
        from lecturehub.study import note_job

        with self._open() as connection:
            return note_job.run(connection, self.root, lecture_id, only_key=block or None, busy=busy, consent_provider=consent_provider)

    @_safe
    def transcript(self, transcript_id: str) -> dict:
        from lecturehub.index import lectures

        with self._open() as connection:
            return {"segments": lectures.segments(connection, transcript_id),
                    "media": lectures.media_for_transcript(connection, transcript_id)}

    @_safe
    def transcript_export(self, transcript_id: str) -> dict:
        """One transcript as a text file to take away: a short head, then "[h:mm:ss] text" lines with the
        user's corrections applied (the raw file stays as it is). Returns {"name", "text"}."""
        from lecturehub.index import lectures
        from lecturehub.study import corrections

        with self._open() as connection:
            row = connection.execute(
                "SELECT t.lecture_id, c.folder_name, l.date FROM Transcript t JOIN Lecture l ON l.id = t.lecture_id "
                "JOIN Course c ON c.id = l.course_id WHERE t.id = ?", (transcript_id,)).fetchone()
            if row is None:
                raise ValueError("전사본을 찾지 못했습니다")
            detail = lectures.lecture_detail(connection, row[0])
            mine = next((t for t in detail["transcripts"] if t["id"] == transcript_id), None)
            if mine is None:
                raise ValueError("이 회차에서 볼 수 없는 전사본입니다(버린 녹음의 전사)")
            view = corrections.corrected_segments(connection, transcript_id)["segments"]
        if not view:
            raise ValueError("전사 내용이 비어 있습니다")
        fixed = sum(1 for s in view if s["corrections"])

        def clock(ms: int) -> str:
            seconds = max(0, int(ms or 0)) // 1000
            return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

        head = [f"{row[1]} · {row[2]} · {detail['label']}", f"전사 엔진: {mine['label']}"
                + (" (사용본)" if mine.get("in_use") else "") + (f" · 내가 고친 곳 {fixed}문단 반영" if fixed else ""), ""]
        lines = [f"[{clock(s['start_ms'])}] " + (f"{s['speaker']}: " if s.get("speaker") else "") + s["text"].strip()
                 for s in view]
        safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in f"{row[1]}_{row[2]}_{detail['label']}_{mine['label']}")
        return {"name": " ".join(safe.split()) + ".txt", "text": "\n".join(head + lines) + "\n", "segments": len(view)}

    @_safe
    def choose(self, lecture_id: str, transcript_id: str) -> dict:
        from lecturehub.index import lectures

        with self._open() as connection, connection:
            lectures.choose_in_use(connection, lecture_id, transcript_id, decided_by="user",
                                   reason="앱에서 사용자가 사용본으로 선택")
        return {}

    @_safe
    def search(self, query: str) -> dict:
        from lecturehub.index import lectures
        from lecturehub.index.search import search

        query = query.strip()
        if not query:
            return {"results": []}
        with self._open() as connection:
            listed = {item["id"]: item for item in lectures.lecture_list(connection)}
            ordinal = {key: item["n"] for key, item in listed.items()}
            labels = {key: item["label"] for key, item in listed.items()}
            spans: dict = {}
            results = [{
                "kind": r.source_kind, "lecture_id": r.lecture_id, "course": r.course, "date": r.date,
                "n": ordinal.get(r.lecture_id), "label": labels.get(r.lecture_id), "start_ms": r.start_ms,
                "engine": lectures.engine_label(r.engine) if r.engine else None,
                "text": r.text, "before": r.context_before, "after": r.context_after, "page": r.page_no,
                "no_media": r.no_media, "path": r.path if r.source_kind == "material" else None,
            } for r in search(connection, query, limit=40) if self._in_lecture_part(connection, r, spans)]
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for row in connection.execute(
                    "SELECT m.title, m.path, c.folder_name, mm.lecture_id, l.date, mm.page_from FROM Material m "
                    "JOIN Course c ON c.id = m.course_id LEFT JOIN MaterialMatch mm ON mm.material_id = m.id "
                    "LEFT JOIN Lecture l ON l.id = mm.lecture_id WHERE m.title LIKE ? ESCAPE '\\' LIMIT 10", (like,)):
                results.append({"kind": "material_title", "lecture_id": row[3], "course": row[2], "date": row[4],
                                "n": ordinal.get(row[3]), "label": labels.get(row[3]), "start_ms": None,
                                "engine": None, "text": row[0],
                                "before": "", "after": "", "page": row[5], "path": row[1], "no_media": True})
        return {"results": results}

    @staticmethod
    def _in_lecture_part(connection, hit, spans: dict) -> bool:
        """D-057: a transcript hit after the lecture ended (recording ran on) is not shown."""
        from lecturehub.index import media_range

        if hit.source_kind != "transcript" or hit.start_ms is None:
            return True
        if "gone" not in spans:  # D-058: transcripts of a thrown-away recording are not searched
            from lecturehub.index.discard import hidden_ids

            gone = hidden_ids(connection)
            spans["gone"] = {r[0] for r in connection.execute("SELECT id, media_id FROM Transcript") if r[1] in gone}
        if hit.source_id in spans["gone"]:
            return False
        if hit.source_id not in spans:
            spans[hit.source_id] = media_range.in_range(connection, hit.source_id)
        span = spans[hit.source_id]
        return span is None or span[0] <= hit.start_ms < span[1]

    # ---------- D-057: recording range fix (the original is kept; a derived clip is used)
    # ---------- '수업 외' and moving a recording to another lecture
    @_safe
    def relink(self, media_id: str, course: str, day: str, with_transcripts: bool = False) -> dict:
        from lecturehub.index import archive_copy, relink

        with self._open() as connection, connection:
            moved = relink.move(connection, self.root, media_id, course, day, with_transcripts=bool(with_transcripts))
            if moved["moved"]:
                moved["copy"] = archive_copy.copy_media(connection, self.root, media_id)  # into the right folder
        return moved

    # ---------- join recordings of one class (user request 2026-09-29)
    @_safe
    def join_candidates(self, media_id: str) -> dict:
        from lecturehub.index import join

        with self._open() as connection:
            return {"items": join.candidates(connection, media_id)}

    @_safe
    def join(self, media_ids: list[str]) -> dict:
        from lecturehub.index import archive_copy, join

        with self._open() as connection, connection:
            result = join.join(connection, self.root, list(media_ids))
        with self._open() as connection, connection:  # the joined one gets its own 녹음본 copy (D-055)
            try:
                result["copy"] = archive_copy.copy_media(connection, self.root, result["joined_id"])
            except Exception as exc:  # noqa: BLE001 - the join itself is done; copying retries later
                result["copy"] = {"state": "failed", "reason": str(exc)}
        return result

    # ---------- recordings made outside the app (phone, KakaoTalk file): user request 2026-10-01
    @staticmethod
    def import_suffixes(convert_only: bool) -> list[str]:
        """File types that can come in; ``convert_only``: the ones that need ffmpeg (not next to a recording)."""
        try:
            from lecturehub.index import import_audio
        except Exception:  # noqa: BLE001 - the window keeps working without the library layer (D-050)
            return []
        return sorted(import_audio.CONVERT if convert_only else import_audio.AUDIO)

    @_safe
    def import_recent(self, now) -> dict:
        from lecturehub.index import import_audio

        folders = [f for f in import_audio.default_folders() if f.is_dir()]
        with self._open() as connection:
            return {"items": import_audio.recent(connection, folders, now_ms=int(now.timestamp() * 1000)),
                    "folders": [str(f) for f in folders], "clova": self._clova_status()}

    def _clova_status(self) -> dict | None:
        from lecturehub.index import clova_fetch

        return clova_fetch.load_status(self.root)

    @_safe
    def clova_sync(self, auto: bool = False) -> dict:
        """[클로바노트 확인] or the weekly check: new notes go to Downloads (1~2 minutes, network only: allowed while
        recording). One check at a time; the weekly one runs only when ``clova_fetch.due`` and Aside CLI is there."""
        from lecturehub.booking import aside_cli
        from lecturehub.core import settings
        from lecturehub.index import clova_fetch

        if auto and (not settings.labs()["clova"] or not aside_cli.find_aside() or not clova_fetch.due(self.root)):
            return {"result": None, "clova": self._clova_status()}
        if not _CLOVA_LOCK.acquire(blocking=False):
            return {"result": {"ok": False, "message": "이미 확인하는 중입니다"}, "clova": self._clova_status()}
        try:
            with self._open() as connection:
                result = clova_fetch.sync(connection, self.root, limit=3)
        finally:
            _CLOVA_LOCK.release()
        return {"result": {k: result.get(k) for k in ("ok", "login_required", "message", "staged", "duplicate",
                                                       "failed")}, "clova": self._clova_status()}

    @_safe
    def import_probe(self, paths: list[str], today: date) -> dict:
        from lecturehub.index import import_audio

        with self._open() as connection:
            return {"items": [import_audio.probe(connection, self.root, path, today=today) for path in paths]}

    @_safe
    def import_commit(self, item: dict, today: date) -> dict:
        from lecturehub.index import import_audio

        with self._open() as connection, connection:
            return import_audio.commit(connection, self.root, dict(item), today=today)

    # ---------- D-058: throw away a recording (Recycle Bin, never a hard delete)
    @_safe
    def discard_plan(self, media_id: str) -> dict:
        from lecturehub.index import discard

        with self._open() as connection:
            return discard.plan(connection, self.root, media_id)

    @_safe
    def discard(self, media_id: str, with_pair: bool = False) -> dict:
        from lecturehub.index import discard

        with self._open() as connection, connection:
            return discard.discard(connection, self.root, media_id, with_pair=bool(with_pair))

    @_safe
    def range_plan(self, media_id: str) -> dict:
        from lecturehub.index import media_range

        with self._open() as connection:
            return media_range.plan(connection, media_id)

    @_safe
    def lecture_discard_plan(self, lecture_id: str) -> dict:
        from lecturehub.index import discard

        with self._open() as connection:
            return discard.lecture_plan(connection, self.root, lecture_id)

    @_safe
    def lecture_discard(self, lecture_id: str, with_pair: bool = False) -> dict:
        from lecturehub.index import discard

        with self._open() as connection, connection:
            return discard.discard_lecture(connection, self.root, lecture_id, with_pair=bool(with_pair))

    @_safe
    def discard_restore(self, media_id: str) -> dict:
        from lecturehub.index import discard

        with self._open() as connection, connection:
            return discard.restore(connection, media_id)

    @_safe
    def sessions(self) -> dict:
        from lecturehub.index import discard

        with self._open() as connection:
            return {"sessions": discard.sessions(connection, self.root)}

    @_safe
    def discard_session(self, name: str, confirm_crashed: bool = False) -> dict:
        from lecturehub.index import discard

        with self._open() as connection, connection:
            discard.discard_session(connection, self.root, name, confirm_crashed=bool(confirm_crashed))
        return {}

    @_safe
    def recover_session(self, name: str) -> dict:
        from lecturehub.index import discard

        return discard.recover_session(self.root, name)

    @_safe
    def range_cut(self, media_id: str, start_ms: int, end_ms: int) -> dict:
        from lecturehub.index import media_range

        with self._open() as connection, connection:
            return media_range.cut(connection, self.root, media_id, int(start_ms), int(end_ms))

    @_safe
    def range_keep(self, media_id: str) -> dict:
        from lecturehub.index import media_range

        with self._open() as connection, connection:
            media_range.keep_whole(connection, media_id)
        return {}

    @_safe
    def range_revert(self, media_id: str) -> dict:
        from lecturehub.index import media_range

        with self._open() as connection, connection:
            media_range.revert(connection, media_id)
        return {}

    @_safe
    def material(self, now, course_id: str | None = None, day: str | None = None,
                 lecture_id: str | None = None) -> dict:
        """This class's material pick (S3): the current/next class, or ``course_id`` on ``day`` (an opened lecture)."""
        from lecturehub.index import materials_pick

        with self._open() as connection:
            target = materials_pick.class_for_now(connection, now)
            if course_id and (target is None or target["course_id"] != course_id
                              or (day and target["date"] != day)):
                name = connection.execute("SELECT folder_name FROM Course WHERE id = ?", (course_id,)).fetchone()
                if name is None:
                    raise ValueError("과목을 찾을 수 없습니다.")
                target = {"course_id": course_id, "course": name[0], "date": day or now.date().isoformat(),
                          "start_epoch_ms": None, "current": False}
            if target is None:
                return {"class": None, "pick": None, "alternatives": [], "notes": []}
            result = materials_pick.pick(connection, target["course_id"], date.fromisoformat(target["date"]),
                                         target.get("start_epoch_ms"), lecture_id=lecture_id)
        from lecturehub.materials.opener import program_label

        return {"class": target, "program": program_label(), **result}

    @_safe
    def choose_material(self, course_id: str, day: str, material_id: str, page: int) -> dict:
        from lecturehub.index import materials_pick

        with self._open() as connection, connection:
            materials_pick.record_choice(connection, course_id, day, material_id, int(page))
        return {}

    @_safe
    def preview(self, path: str, page: int, allow_convert: bool = True) -> dict:
        """PNG of one page under _lecturehub/cache/pages (None when the file type has no preview).

        While recording (``allow_convert=False``) PowerPoint is never started (AGENTS rule 7):
        slides without an export come back as ``deferred``.
        """
        from lecturehub.index.materials_pick import pdf_page_count
        from lecturehub.materials.opener import SLIDES, cached_pdf, render_page

        target = Path(path)
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("학기 폴더 안의 파일만 미리 볼 수 있습니다.")
        slides = target.suffix.lower() in SLIDES
        if slides and not allow_convert and cached_pdf(target, self.root) is None:
            return {"image": None, "page_count": None, "deferred": True}
        image = render_page(target, int(page), self.root, allow_convert=allow_convert)
        count = None
        if image and slides:  # converted once by PowerPoint; the cached PDF has the pages
            count = pdf_page_count(str(cached_pdf(target, self.root)))
        return {"image": str(image) if image else None, "page_count": count}

    def prewarm(self, now, busy) -> list[str]:
        """Convert today's remaining classes' picked slides ahead of time, so class time uses the cache.

        ``busy()`` is checked before each conversion; nothing starts while recording.
        """
        from lecturehub.index import materials_pick
        from lecturehub.index.schedule import daily_schedule
        from lecturehub.materials.opener import SLIDES, cached_pdf, powerpoint_available, slides_to_pdf

        if not self.path.is_file() or not powerpoint_available():
            return []
        at_ms = ((now.hour * 60 + now.minute) * 60 + now.second) * 1000
        with self._open() as connection:
            targets = []
            for c in daily_schedule(connection, now.date()):
                if c.end_ms <= at_ms:
                    continue
                chosen = materials_pick.pick(connection, c.course_id, now.date(), None)["pick"]
                if chosen and Path(chosen["path"]).suffix.lower() in SLIDES:
                    targets.append(Path(chosen["path"]))
        done = []
        for target in targets:
            if busy():
                break
            if cached_pdf(target, self.root) is None:
                slides_to_pdf(target, self.root)
                done.append(str(target))
        return done

    @_safe
    def open_material(self, path: str, page: int, allow_convert: bool = True) -> dict:
        from lecturehub.materials.opener import open_material

        target = Path(path)
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("학기 폴더 안의 파일만 열 수 있습니다.")
        result = open_material(target, int(page), self.root, allow_convert=allow_convert)
        if not result.pop("ok", True):
            raise ValueError(result.get("error") or "열지 못했습니다.")
        return result

    # ---------- course folders by category (user request 2026-10-01, index/organize.py)
    @_safe
    def course_files(self, course: str) -> dict:
        from lecturehub.index import organize

        with self._open() as connection:
            return {**organize.inventory(connection, self.root, course), "categories": _move_targets(organize, self.root / course),
                    "undo": (organize.batches(connection) or [None])[0]}

    @_safe
    def file_sort(self, path: str, category: str) -> dict:
        """The user files one item of a course folder under a category folder (logged, undoable)."""
        from lecturehub.index import organize

        root = self.root.resolve()
        source = Path(path).resolve()
        if category not in organize.CATEGORIES or not source.is_relative_to(root) or len(source.relative_to(root).parts) < 2:
            raise ValueError("과목 폴더 안의 파일을 정해진 분류로만 옮깁니다.")
        relative = source.relative_to(root)
        target = organize.folder_for(root / relative.parts[0], category).resolve().relative_to(root) / source.name
        if any(p.parts[0].casefold() == "_lecturehub" for p in (relative, target)):  # the app's DB and job folders
            raise ValueError("앱 데이터 폴더(_lecturehub)의 파일은 옮기지 않습니다.")
        with self._open() as connection:
            result = organize.apply(connection, root, [{"from": str(relative), "to": str(target)}])
        if result.get("stopped"):
            raise ValueError(f"옮기지 못했습니다 — {result['stopped']} (파일이 열려 있으면 닫고 다시 해 보세요)")
        return result

    @_safe
    def files_undo(self, batch: int | None = None) -> dict:
        from lecturehub.index import organize

        with self._open() as connection:
            return organize.undo(connection, self.root, batch)

    @_safe
    def files_relocate(self) -> dict:
        """Files moved in Explorer (name kept): follow them by content hash so materials and notes still open."""
        from lecturehub.index import organize

        with self._open() as connection, connection:
            return {"found": organize.relocate(connection, self.root, deep=False)}

    # ---------- calendar (user request 2026-09-29): exams, quizzes, assignments, closures, personal
    @_safe
    def calendar(self, year: int, month: int, today: date) -> dict:
        from lecturehub.index import calendar_events

        with self._open() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_schema WHERE name='CalendarEvent'").fetchone():
                raise ValueError("달력은 라이브러리 DB 업데이트(0006) 뒤에 쓸 수 있습니다. 녹음·전사는 그대로 됩니다.")
            view = calendar_events.month_view(connection, int(year), int(month), today)
            view["courses"] = [{"id": r[0], "folder": r[1]} for r in
                               connection.execute("SELECT id, folder_name FROM Course ORDER BY rowid")]
        return view

    @_safe
    def calendar_add(self, values: dict) -> dict:
        from lecturehub.index import calendar_events

        with self._open() as connection, connection:
            event_id = calendar_events.add(
                connection, kind=values.get("kind", "other"), title=values.get("title", ""), day=values.get("date", ""),
                course_id=values.get("course_id") or None, start=values.get("start") or None,
                end=values.get("end") or None, note=values.get("note") or "")
        return {"id": event_id}

    @_safe
    def calendar_update(self, event_id: str, changes: dict) -> dict:
        from lecturehub.index import calendar_events

        allowed = {"kind", "title", "day", "course_id", "start", "end", "note"}
        with self._open() as connection, connection:
            calendar_events.update(connection, event_id, **{k: (v or None) if k in ("course_id", "start", "end") else v
                                                            for k, v in changes.items() if k in allowed})
        return {}

    # ---------- eTL assignment deadlines, fetched with the user's own Canvas token (read-only)
    @_safe
    def etl_status(self) -> dict:
        from lecturehub.index import etl_sync

        return etl_sync.status(self.root)

    @_safe
    def etl_sync(self, force: bool = False, token: str | None = None) -> dict:
        """Fetch now and import. A new token is kept only when eTL accepts it."""
        from lecturehub.index import calendar_events, etl_sync

        result = etl_sync.sync(self.root, force=bool(force or token), token=(token or "").strip() or None)
        if result["state"] == "error":
            raise ValueError(result["error"])
        if token and result["state"] == "synced":
            etl_sync.save_token(token)
        with self._open() as connection, connection:
            imported = calendar_events.import_etl(connection, self.root / "_lecturehub" / "etl" / "assignments.json")
        board = {}
        if result["state"] == "synced":  # announcements and file lists ride along; their failure never fails the deadlines
            from lecturehub.index import etl_files

            try:
                board = {"board": etl_files.refresh(self.root, (token or "").strip() or etl_sync.load_token())}
            except Exception as exc:  # noqa: BLE001 - shown next to the deadline result
                board = {"board_error": f"공지·파일 목록은 가져오지 못했습니다({type(exc).__name__})"}
        return {**result, **board, "added": imported["added"], "updated": imported["updated"]}

    @_safe
    def etl_board(self) -> dict:
        """eTL announcements and the course files that are not in the course folder yet (index/etl_files.py)."""
        from lecturehub.index import etl_board, etl_files

        return {**etl_files.board(self.root), "posts": etl_board.posts(self.root)[:30],
                "post_check": etl_board.status(self.root)}

    @_safe
    def etl_board_check(self) -> dict:
        """[게시판 확인]: read the course boards now (index/etl_board.py) and add dated posts as calendar candidates."""
        from lecturehub.index import etl_board, etl_sync

        token = etl_sync.load_token()
        if not token:
            raise ValueError("eTL에 연결돼 있지 않습니다. 먼저 [eTL 연결]에서 토큰을 넣어 주세요")
        result = etl_board.refresh(self.root, token)
        with self._open() as connection, connection:
            found = etl_board.import_candidates(connection, etl_board.posts(self.root))
        return {**result, **found}

    def etl_board_auto(self) -> list[dict]:
        """Once a day (app start and the 10-minute loop, never while recording — the caller checks): read the
        boards when due, add dated posts as candidates, and return the new posts to tell (user 2026-11-20)."""
        from lecturehub.index import etl_board, etl_sync

        token = etl_sync.load_token()
        if token and etl_board.due(self.root):
            try:
                etl_board.refresh(self.root, token)
            except Exception:  # noqa: BLE001 - kept in board_sync.json, shown beside [게시판 확인]
                return []
            if self.path.is_file():
                with self._open() as connection, connection:
                    etl_board.import_candidates(connection, etl_board.posts(self.root))
        return etl_board.to_tell(self.root)

    @_safe
    def etl_board_new(self) -> dict:
        """New board posts not marked 확인함 (the banner on top)."""
        from lecturehub.index import etl_board

        return {"items": etl_board.unseen(self.root)}

    @_safe
    def etl_board_seen(self, ids: list[str]) -> dict:
        from lecturehub.index import etl_board

        return {"seen": etl_board.mark_seen(self.root, list(ids))}

    @_safe
    def etl_file_take(self, file_id: int, beside: bool = False, allow_big: bool = False) -> dict:
        from lecturehub.index import etl_files

        return etl_files.take(self.root, int(file_id), beside=bool(beside), allow_big=bool(allow_big))

    @_safe
    def etl_file_take_all(self) -> dict:
        from lecturehub.index import etl_files

        return etl_files.take_all(self.root)

    @_safe
    def etl_file_keep(self, file_id: int) -> dict:
        from lecturehub.index import etl_files

        return etl_files.keep(self.root, int(file_id))

    # ---------- assignment helper (user request 2026-09-29): gather for Claude, then a Claude Code session
    def _consent_store(self):
        from lecturehub.transcription.consent import ConsentStore

        return ConsentStore(self.root / "_lecturehub" / "consent.json")

    @_safe
    def assignment_prepare(self, event_id: str) -> dict:
        """Build the pack. Returns {"need_consent": course, "notice": ...} when the course is not agreed yet."""
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.index import assignment_pack

        try:
            with self._open() as connection:
                manifest = assignment_pack.build(connection, self.root, event_id, consent=self._consent_store())
        except assignment_pack.ConsentNeeded as exc:
            return {"need_consent": exc.course, "notice": assignment_pack.CONSENT_NOTICE}
        files = manifest["files"]
        top = [x["date"] for x in manifest["lectures"][:3]]
        return {"pack": manifest["pack"], "ref": manifest["assignment"]["source_ref"] or manifest["assignment"]["event_id"],
                "counts": {k: len(v) for k, v in files.items()}, "lectures": len(manifest["lectures"]),
                "top_lectures": top, "ai_policy": manifest["ai_policy"]["summary"],
                "etl": bool(manifest.get("etl") and not manifest["etl"].get("error"))}

    def assignment_notice(self) -> str:
        """The consent text the native confirmation window shows (ui/app.py consent_confirm)."""
        from lecturehub.core import edition

        if not edition.has("assignment"):
            raise ValueError("공개판에 포함되지 않은 기능입니다.")
        from lecturehub.index import assignment_pack

        return assignment_pack.CONSENT_NOTICE

    @_safe
    def assignment_consent(self, course: str) -> dict:
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        import time

        from lecturehub.index import assignment_pack

        self._consent_store().grant(assignment_pack.PROVIDER, course, int(time.time() * 1000),
                                    assignment_pack.CONSENT_NOTICE, by="user-app")
        return {}

    def _pack_of(self, event_id: str) -> tuple[Path, str]:
        from lecturehub.index import assignment_pack

        with self._open() as connection:
            item = assignment_pack.find(connection, event_id)
        ref = item["source_ref"] or item["event_id"]
        pack = assignment_pack.pack_dir(self.root, ref)
        if not (pack / "pack.json").is_file():
            raise ValueError("과제 묶음이 아직 없습니다. [과제 준비]를 먼저 누르세요.")
        return pack, ref

    @_safe
    def assignment_open(self, event_id: str, opener=None) -> dict:
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        from lecturehub.core import safe_open

        pack, _ref = self._pack_of(event_id)
        safe_open.startfile(pack, opener)  # a folder; the allow-list refuses anything that could run
        return {"pack": str(pack)}

    @_safe
    def assignment_claude(self, event_id: str, launcher=None) -> dict:
        """Open a console with Claude Code in this app's repository, running /assignment for this pack."""
        from lecturehub.core import edition

        if not edition.has("assignment"):
            return {"ok": False, "error": "공개판에 포함되지 않은 기능입니다.", "reason": "공개판에 포함되지 않은 기능입니다.", "state": "disabled", "running": False}
        import shutil
        import subprocess

        _pack, ref = self._pack_of(event_id)
        repo = Path(__file__).resolve().parents[3]  # src/lecturehub/ui/library.py → repository
        claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
        command = ["cmd", "/c", "start", "Lecture Hub 과제", "cmd", "/k", claude, f"/assignment {ref}"]
        if launcher:
            launcher(command, repo)
        else:
            if not Path(claude).is_file():
                raise ValueError("Claude Code(claude)를 찾지 못했습니다. 터미널에서 claude를 설치했는지 확인하세요.")
            subprocess.Popen(command, cwd=str(repo), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ref": ref, "repo": str(repo)}

    @_safe
    def note_check(self, lecture_id: str) -> dict:
        from lecturehub.study.note_job import consent_info

        with self._open() as connection:
            return consent_info(connection, self.root, lecture_id)

    @_safe
    def note_grant(self, lecture_id: str, provider: str) -> dict:
        from lecturehub.study.note_job import consent_info
        from lecturehub.transcription.consent import ConsentStore
        import time

        with self._open() as connection:
            info = consent_info(connection, self.root, lecture_id)
        if provider != info["provider"]:
            raise PermissionError("AI 엔진이 바뀌었습니다. 동의를 다시 확인하세요.")
        if not info["once_only"]:
            ConsentStore(self.root / "_lecturehub" / "consent.json").grant(
                provider, info["course"], time.time_ns() // 1_000_000, info["notice"], by="user_app_dialog")
        return info

    @_safe
    def note_claude(self, lecture_id: str, launcher=None, consent_provider=None) -> dict:
        """Compatibility API name; opens the selected engine's packaged lecture-note skill."""
        from lecturehub.ai import get_engine
        from lecturehub.core import agent_workspace
        from lecturehub.index import note_pack
        from lecturehub.study.note_job import consent_info

        with self._open() as connection:
            info = consent_info(connection, self.root, lecture_id)
            if info["needs_consent"] and not (info["once_only"] and consent_provider == info["provider"]):
                raise PermissionError("전사본·자료 글을 보내기 전에 제공자·과목별 동의를 확인하세요.")
            found = note_pack.pack(connection, self.root, lecture_id)
        if not found["ready"]:
            raise ValueError("전사가 아직 없습니다. 전사를 먼저 만든 뒤 눌러 주세요.")
        if found["recording_now"]:
            raise RuntimeError("녹음 중에는 정리본 도우미를 열지 않습니다.")
        engine = get_engine("notes")
        if f"{engine.name}_note" != info["provider"]:
            raise PermissionError("AI 엔진이 바뀌었습니다. 다시 확인하세요.")
        import json
        from datetime import datetime

        notes_dir, cues_dir = Path(found["notes_folder"]), Path(found["cues_file"]).parent
        writable = agent_workspace.note_write_dirs(self.root, [notes_dir, cues_dir])  # never the semester folder
        for folder in writable:
            folder.mkdir(parents=True, exist_ok=True)  # the sandbox can only grant folders that exist
        job = self.root / "_lecturehub" / "jobs" / f"note_console_{lecture_id}_{datetime.now():%Y%m%d_%H%M%S_%f}"
        job.mkdir(parents=True, exist_ok=False)
        pack_file = job / "pack.json"
        pack_file.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        repo = agent_workspace.ensure()
        engine.open_console(repo, self.root, lecture_id, launcher=launcher, writable=writable, pack=pack_file)
        return {"engine": engine.name, "target": found["target"], "revise": bool(found["attached_notes"]),
                "transcripts": len(found["transcripts"])}

    @_safe
    def calendar_status(self, event_id: str, status: str) -> dict:
        from lecturehub.index import calendar_events

        with self._open() as connection, connection:
            calendar_events.set_status(connection, event_id, status, "앱에서 사용자가 선택")
        return {}

    # ---------- exam prep (user request 2026-10-11): dates, what was said in class, prep files, practice
    @_safe
    def exam_board(self, today: date) -> dict:
        """The exam screen. New question files under _lecturehub/quiz are read in on the way."""
        from lecturehub.index import exam_prep

        with self._open() as connection, connection:
            banks = exam_prep.import_new(connection, self.root)
            return {**exam_prep.board(connection, self.root, today), "banks": banks}

    @_safe
    def quiz_items(self, course_id: str, mode: str = "mix", topic: str = "", limit: int = 20) -> dict:
        from lecturehub.index import exam_prep

        with self._open() as connection:
            return {"items": exam_prep.items(connection, course_id, mode=mode, topic=topic or "", limit=int(limit)),
                    "stats": exam_prep.stats(connection, course_id)}

    @_safe
    def quiz_answer(self, item_id: str, given: str, correct: bool | None = None) -> dict:
        from lecturehub.index import exam_prep

        with self._open() as connection, connection:
            return exam_prep.answer(connection, item_id, str(given or ""), None if correct is None else bool(correct))

    def calendar_refresh(self) -> dict:
        """App start (not while recording): fetch eTL deadlines (when connected), import them and find new
        spoken dates. Errors are returned."""
        from lecturehub.index import calendar_events, etl_sync

        if not self.path.is_file():
            return {"ok": False, "error": "no library"}
        try:
            synced = etl_sync.sync(self.root)  # at most every few hours; failures are kept in etl\sync.json
            if synced.get("state") == "synced":  # announcements and file lists with it (lists only, nothing is fetched)
                from lecturehub.index import etl_files

                try:
                    etl_files.refresh(self.root, etl_sync.load_token())
                except Exception:  # noqa: BLE001, S110 - the deadlines above are what must not fail
                    pass
            with self._open() as connection, connection:
                etl = calendar_events.import_etl(connection, self.root / "_lecturehub" / "etl" / "assignments.json")
            board_new = self.etl_board_auto()  # course boards once a day; new posts are told (user 2026-11-20)
            from datetime import datetime

            notices = self.notices_scan(datetime.now().astimezone().date())
            spoken = {}
            # The AI reading did not run (no key, an error) or is switched off in settings: the old word rules
            # still find the plain cases. They work on this PC and send nothing. (Before 2026-11-16 the switch
            # being off counted as "read", so nothing looked at the transcripts at all.)
            if not notices.get("ok") or notices.get("disabled"):
                with self._open() as connection, connection:
                    spoken = calendar_events.scan_transcripts(connection)
            else:
                # Lectures the AI reading could not read this round (a send failed, or it is waiting before the
                # next try, or its three sends are used up and it waits for the transcript to change): the word
                # rules look at exactly those, so a plain "다음 주 휴강" is not lost to an outage. Nothing is
                # sent; a candidate that is already there is not added twice.
                unread = sorted({str(item["lecture_id"]) for key in ("errors", "waiting")
                                 for item in notices.get(key) or [] if isinstance(item, dict) and item.get("lecture_id")})
                if unread:
                    with self._open() as connection, connection:
                        spoken = calendar_events.scan_transcripts(connection, unread)
            return {"ok": True, "etl": etl, "notices": notices, "board_new": board_new, **spoken}
        except Exception as exc:  # noqa: BLE001 - e.g. the calendar table does not exist yet
            return {"ok": False, "error": str(exc)}

    # ---------- schedule notices heard in class, read by AI from the whole transcript (user request 2026-10-30)
    def notices_scan(self, today: date, force: bool = False, lecture_ids: list | None = None) -> dict:
        """Read lectures whose transcripts changed (network only, Gemini text). One reading at a time.

        ``force`` and ``lecture_ids`` go on to ``class_notices.scan`` (a reading regardless of the switch in
        settings). Nothing in the app passes them: the two callers, the 10-minute loop and ``calendar_refresh``,
        give the date only, and no window method or command reaches them (checked 2026-11-16)."""
        from lecturehub.index import class_notices

        lock = _NOTICE_LOCK
        if not self.path.is_file():
            return {"ok": False, "error": "no library"}
        if not lock.acquire(blocking=False):
            return {"ok": True, "busy": True, "read": 0, "added": []}
        try:
            with self._open() as connection, connection:
                result = class_notices.scan(connection, self.root, today, force=bool(force),
                                            lecture_ids=list(lecture_ids) if lecture_ids else None)
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001 - no key, consent file broken, …: shown, never raised
            return {"ok": False, "error": str(exc)}
        finally:
            lock.release()

    @_safe
    def notices_setting(self, today: date, set_to: bool | None = None) -> dict:
        """Settings screen: is the background reading on, and how much it sent this month (no database).

        ``readings`` is the number of requests whose body went out this month, answered or not, and
        ``characters`` what they carried. A count that cannot be read shows as zero and never hides the switch."""
        from lecturehub.index import class_notices

        if set_to is not None:
            class_notices.set_enabled(set_to)
        on = class_notices.enabled()
        try:
            use = class_notices.usage(self.root, today)
        except Exception:  # noqa: BLE001 - the switch must stay on screen whatever the count file holds
            use = {"month": today.isoformat()[:7], "readings": 0, "characters": 0}
        return {"on": on, **use}

    @_safe
    def notices(self, today: date) -> dict:
        """Inbox for the banner: unseen notices first; each says whether its calendar candidate is still open."""
        from lecturehub.index import class_notices

        items = class_notices.inbox(self.root)
        states = {}
        if self.path.is_file():
            with self._open() as connection:
                ids = [i["event_id"] for i in items if i.get("event_id")]
                if ids:
                    marks = ",".join("?" * len(ids))
                    states = dict(connection.execute(
                        f"SELECT id, status FROM CalendarEvent WHERE id IN ({marks})", tuple(ids)).fetchall())
        for item in items:
            item["status"] = states.get(item.get("event_id"), "none")
            item["past"] = bool(item.get("date") and item["date"] < today.isoformat())
        unseen = [i for i in items if not i.get("seen")]
        return {"unseen": sorted(unseen, key=lambda i: (i.get("date") or "9999", i["found_at"])),
                "recent": sorted(items, key=lambda i: -i["found_at"])[:30]}

    @_safe
    def notices_seen(self, keys: list) -> dict:
        from lecturehub.index import class_notices

        return {"changed": class_notices.mark_seen(self.root, [str(k) for k in keys or []])}


def _bookmarks_for(media, recordings) -> list[dict]:
    """★ marks pressed while recording (recorder ``bookmarks.jsonl`` beside the session's master file).

    Times are audio time of that recording, the same clock as its transcript. The played file may be the
    D-055 copy under 녹음본, so a recording of the same length in a session folder also counts.
    """
    import json
    from pathlib import Path

    if not media or not media.get("path"):
        return []
    length = media.get("duration_ms") or 0
    candidates = [media.get("path")] + [r.get("path") for r in recordings or []
                                        if r.get("path") and abs((r.get("duration_ms") or 0) - length) <= 2000]
    for raw in candidates:
        found = Path(raw).parent / "bookmarks.jsonl"
        try:
            lines = found.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        marks = []
        for line in filter(None, (x.strip() for x in lines)):
            try:
                item = json.loads(line)
                marks.append({"n": int(item["n"]), "at_ms": int(item["at_ms"])})
            except (ValueError, KeyError, TypeError):
                continue
        return sorted(marks, key=lambda m: m["at_ms"])
    return []
