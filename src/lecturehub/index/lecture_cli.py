"""CLI for lecture-centred views and the transcript-in-use pointer (D-049)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from .cli import RootOption, _fail, _library


def register(app: typer.Typer) -> None:
    lecture_app = typer.Typer(no_args_is_help=True, help="회차(Lecture) 단위 보기와 사용본")

    @lecture_app.command("refresh-in-use")
    def refresh_in_use(root: RootOption = Path(".")) -> None:
        """사용자가 고르지 않은 회차의 사용본을 우선순위 규칙으로 기록한다(추가만, 사용자 선택은 유지)."""
        from . import lectures

        try:
            with _library(root) as connection, connection:
                ids = [row[0] for row in connection.execute("SELECT id FROM Lecture ORDER BY date, id")]
                changed = [lecture_id for lecture_id in ids if lectures.refresh_policy(connection, lecture_id)]
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"회차 {len(ids)}개 중 사용본 기록 {len(changed)}건")

    @lecture_app.command("show")
    def show(lecture_id: str, root: RootOption = Path(".")) -> None:
        """회차의 녹음·전사(엔진)·자료·사용본·상태를 보여 준다(전사 본문은 출력하지 않음)."""
        from . import lectures

        try:
            with _library(root) as connection:
                detail = lectures.lecture_detail(connection, lecture_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"{detail['course']} {detail['label']} · {detail['date']}({detail['weekday']}) · {detail['state']['label']}")
        for t in detail["transcripts"]:
            typer.echo(f"  전사 {t['label']}: {t['segments']}문단{' · 사용본' if t['in_use'] else ''}"
                       f"{'' if t['media_id'] else ' · 연결된 원음 없음'}")
        typer.echo(f"  녹음 {len(detail['recordings'])} · 자료 {len(detail['materials'])} · 노트 {len(detail['notes'])}")
        for step in detail["history"]:
            typer.echo(f"  사용본 기록: {step['from']} → {step['to']} ({step['decided_by']}: {step['reason']})")

    @lecture_app.command("join")
    def join_cmd(media_ids: list[str], root: RootOption = Path("."),
                 dry_run: bool = typer.Option(False, "--dry-run", help="만들 내용만 보여 준다")) -> None:
        """같은 날 같은 과목의 녹음들을 하나로 이어 붙인다(원본 그대로, 전사도 시간 맞춰 이어 붙임). 결과는 JSON."""
        import json

        from . import archive_copy, join

        try:
            with _library(root) as connection, connection:
                if dry_run:
                    plan = join.plan(connection, media_ids)
                    result = {k: plan[k] for k in ("duration_ms", "lecture_id", "course", "date", "engines",
                                                   "engines_partial")}
                    result["pieces"] = [{k: p.get(k) for k in ("id", "recorded_start", "duration_ms", "offset_ms",
                                                               "gap_before_ms")} for p in plan["pieces"]]
                else:
                    result = join.join(connection, root, media_ids)
            if not dry_run:
                with _library(root) as connection, connection:
                    result["copy"] = archive_copy.copy_media(connection, root, result["joined_id"])
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("import-audio")
    def import_audio_cmd(file: Path, course: str = typer.Option(..., "--course", help="과목 폴더 이름 또는 수업 외 항목"),
                         day: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
                         clock: str = typer.Option(None, "--time", help="녹음 시작 HH:MM(모르면 생략)"),
                         device: str = typer.Option("phone", "--device", help="phone | laptop | external_mic"),
                         root: RootOption = Path(".")) -> None:
        """앱 밖에서 녹음한 파일(휴대폰 녹음 등)을 회차에 가져온다. 원본은 그대로 두고 _lecturehub\\imports에 복사한다. JSON 한 줄."""
        import json
        from datetime import datetime

        from . import import_audio

        try:
            with _library(root) as connection, connection:
                result = import_audio.commit(connection, root, {"path": str(file), "course": course, "date": day,
                                                                "time": clock, "device": device},
                                             today=datetime.now().astimezone().date())
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("tidy-empty")
    def tidy_empty(root: RootOption = Path("."),
                   apply: bool = typer.Option(False, "--apply", help="지운다(없으면 목록만)")) -> None:
        """녹음본 복사본의 파일 이름으로 잘못 만들어진 빈 회차를 찾아 지운다(다른 데서 가리키는 것은 남긴다)."""
        import json

        from . import tidy

        try:
            with _library(root) as connection, connection:
                found = tidy.remove_empty_copy_lectures(connection) if apply else tidy.empty_copy_lectures(connection)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps({"applied": apply, "lectures": found}, ensure_ascii=False))

    @lecture_app.command("note-pack")
    def note_pack(lecture_id: str, root: RootOption = Path(".")) -> None:
        """정리본을 쓸 때 필요한 것(전사 파일들, 강의자료, 노트 폴더, 형식 기준, 넣을 자리)을 모아 JSON 한 줄로 낸다."""
        import json

        from . import note_pack as packing

        try:
            with _library(root) as connection:
                result = packing.pack(connection, Path(root), lecture_id)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("attach-note")
    def attach_note(lecture_id: str, file: Path, root: RootOption = Path(".")) -> None:
        """정리된 HTML 노트 파일을 회차에 연결한다(파일은 그대로 둔다). 회차 화면에서 바로 열린다. JSON 한 줄."""
        import json

        from . import note_attach

        try:
            with _library(root) as connection, connection:
                result = note_attach.attach(connection, root, lecture_id, file)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("attach-transcript")
    def attach_transcript(lecture_id: str, file: Path, root: RootOption = Path("."),
                          engine: str = typer.Option(None, "--engine", help="도구 이름(clova | daglo), 형식만으로 다르게 읽힐 때"),
                          origin: str = typer.Option("", "--origin", help="어디서 받았는지(예: 동기 클로바노트)")) -> None:
        """다른 사람에게 받은 전사 텍스트를 회차에 연결한다(녹음 없어도 됨, 파일은 그대로 둔다). JSON 한 줄."""
        import json

        from . import note_attach

        try:
            with _library(root) as connection, connection:
                result = note_attach.attach_transcript(connection, root, lecture_id, file, engine=engine, origin=origin)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("attach-captions")
    def attach_captions(file: Path, course: str = typer.Option(..., "--course", help="과목 폴더 이름"),
                        day: str = typer.Option(..., "--date", help="YYYY-MM-DD"),
                        root: RootOption = Path(".")) -> None:
        """실시간 자막 파일(전사본 폴더로 옮긴 것)을 그날 그 과목 회차에 연결한다. 녹음이 없으면 회차를 만든다. JSON 한 줄."""
        import json

        from . import note_attach

        try:
            with _library(root) as connection, connection:
                lecture_id = note_attach.lecture_on(connection, course, day)
                result = note_attach.attach_transcript(connection, root, lecture_id, file,
                                                       engine="live_captions", origin="실시간 자막")
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("set-title")
    def set_title(lecture_id: str, title: str = typer.Argument("", help="회차 이름(비우면 번호로 되돌림)"),
                  root: RootOption = Path(".")) -> None:
        """회차에 이름을 붙인다(예: 무료 특강 1강). 이름 붙은 회차는 'n강' 번호를 차지하지 않는다. JSON 한 줄."""
        import json

        from . import note_attach

        try:
            with _library(root) as connection, connection:
                result = note_attach.set_title(connection, lecture_id, title)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("ingest-app")
    def ingest_app(session_dir: Path, root: RootOption = Path("."),
                   lead_min: int = typer.Option(15, "--lead-min", help="수업 시작 몇 분 전부터 그 수업으로 볼지")) -> None:
        """앱 녹음 세션을 회차에 연결한다(불확실하면 미확정으로 보관). 결과는 JSON 한 줄."""
        import json

        from . import app_ingest

        try:
            with _library(root) as connection, connection:
                result = app_ingest.link_recording(connection, session_dir, lead_min=lead_min)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        result["archive"] = _archive(root, result)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("confirm-pending")
    def confirm_pending(ingest_id: str, course: str = typer.Option(..., "--course"), root: RootOption = Path("."),
                        day: str = typer.Option(None, "--date", help="YYYY-MM-DD(기본: 녹음한 날)")) -> None:
        """미확정 녹음을 사용자가 고른 과목·날짜의 회차에 연결한다(confirmed_by_user)."""
        import json

        from . import app_ingest

        try:
            with _library(root) as connection, connection:
                result = app_ingest.confirm_pending(connection, ingest_id, course, day)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        result["archive"] = _archive(root, result)
        typer.echo(json.dumps(result, ensure_ascii=False))

    def _archive(root: Path, result: dict) -> dict | None:
        """D-055 copy after linking, in its own transaction; a failure is reported, never raised."""
        from . import archive_copy

        if result.get("state") != "linked" or not result.get("media_id"):
            return None
        try:
            with _library(root) as connection, connection:
                return archive_copy.copy_media(connection, Path(root), result["media_id"])
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {"state": "failed", "reason": str(exc)}

    @lecture_app.command("wav-cleanup")
    def wav_cleanup_cmd(root: RootOption = Path("."),
                        dry_run: bool = typer.Option(False, "--dry-run", help="옮기지 않고 상태만 보기")) -> None:
        """완성 전사본까지 끝난 회차의 WAV(1분 조각·병합본)를 휴지통으로 옮긴다. JSON 한 줄."""
        import json

        from . import wav_cleanup

        try:
            with _library(root) as connection, connection:
                result = wav_cleanup.candidates(connection) if dry_run else wav_cleanup.clean(connection)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(result, ensure_ascii=False))

    @lecture_app.command("archive-copy")
    def archive_copy_cmd(root: RootOption = Path(".")) -> None:
        """연결된 앱 녹음 중 녹음본\\<과목>\\에 아직 복사하지 않은 것을 복사한다(D-055, 추가만). JSON 한 줄."""
        import json

        from . import archive_copy

        results = []
        try:
            with _library(root) as connection:
                todo = archive_copy.pending(connection)
            for media_id in todo:
                with _library(root) as connection, connection:
                    results.append({"media_id": media_id, **archive_copy.copy_media(connection, Path(root), media_id)})
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(results, ensure_ascii=False))

    @lecture_app.command("move-transcripts")
    def move_transcripts(root: RootOption = Path("."),
                         dry_run: bool = typer.Option(False, "--dry-run", help="옮길 목록만 보여 줌"),
                         course: str = typer.Option("", "--course", help="이 과목 폴더만")) -> None:
        """_lecturehub\\out에 있는 등록된 전사본을 전사본\\<과목>\\'yy.M.d 과목 (엔진).txt'로 옮긴다(덮어쓰기 없음). JSON 한 줄."""
        import json

        from . import transcript_files

        try:
            with _library(root) as connection, connection:
                moved = transcript_files.move_registered(connection, Path(root), dry_run=dry_run,
                                                         course=course or None)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(moved, ensure_ascii=False))

    @lecture_app.command("pending")
    def pending(root: RootOption = Path(".")) -> None:
        """미확정 녹음 목록(JSON 한 줄)."""
        import json

        from . import app_ingest

        try:
            with _library(root) as connection:
                items = app_ingest.pending_items(connection)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(items, ensure_ascii=False))

    @lecture_app.command("sync-jobs")
    def sync_jobs(root: RootOption = Path("."),
                  allow_mock: bool = typer.Option(False, "--allow-mock", hidden=True,
                                                  help="합성 검증용: 모의 전사도 등록")) -> None:
        """끝난 전사 작업(Google·Whisper·MAI)을 회차의 전사본으로 등록하고 사용본을 갱신한다(여러 번 해도 같음). JSON 한 줄."""
        import json

        from lecturehub.transcription.providers import job_roots

        from . import app_ingest

        try:
            with _library(root) as connection, connection:
                synthetic = (Path(root) / "_lecturehub" / "synthetic_root").is_file()
                results = [result for jobs in job_roots(Path(root))
                           for result in app_ingest.sync_jobs(connection, jobs, allow_mock=allow_mock and synthetic)]
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(json.dumps(results, ensure_ascii=False))

    app.add_typer(lecture_app, name="lecture")
