"""CLI for user-confirmed, explicitly dated schedule exceptions."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from . import confirmation
from .cli import RootOption, _fail, _library
from .schedule import add_exceptions, exception_dates, resolve_course


def _iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("날짜는 유효한 YYYY-MM-DD 형식이어야 합니다.") from exc


def _weekday(value: str | None) -> int | None:
    if value is None:
        return None
    day = value.strip().removesuffix("요일")
    if day not in tuple("월화수목금토일"):
        raise typer.BadParameter("--weekday는 월, 화, 수, 목, 금, 토, 일 중 하나입니다.")
    return "월화수목금토일".index(day)


def register(app: typer.Typer) -> None:
    """Attach ``lecturehub schedule except add`` to the main application."""
    schedule_app = typer.Typer(no_args_is_help=True, help="시간표와 사용자 확인 일정 예외")
    exception_app = typer.Typer(no_args_is_help=True, help="날짜가 명확한 휴강·대체·보강 등록")

    @exception_app.command("add")
    def add(
        kind: Annotated[str, typer.Option("--kind", help="휴강 / 대체 / 보강 / 시간변경")],
        note: Annotated[str, typer.Option("--note", help="사용자 확인 근거. 보강·시간변경은 HH:MM-HH:MM 포함")],
        root: RootOption = Path("."),
        on_date: Annotated[str | None, typer.Option("--date", help="한 날짜 YYYY-MM-DD")] = None,
        start: Annotated[str | None, typer.Option("--from", help="매주 반복 시작 YYYY-MM-DD")] = None,
        until: Annotated[str | None, typer.Option("--until", help="매주 반복 마지막 날짜 YYYY-MM-DD, 포함")] = None,
        weekday: Annotated[str | None, typer.Option("--weekday", help="반복 요일 월~일")] = None,
        course: Annotated[str | None, typer.Option("--course", help="과목 ID, 폴더 이름, 코드 또는 약어")] = None,
        all_courses: Annotated[bool, typer.Option("--all-courses", help="모든 과목에 적용")] = False,
        by_user: Annotated[bool, typer.Option("--by-user", help="사용자가 직접 확인했음(없으면 inferred로 남김)")] = False,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """원본 시간표를 바꾸지 않고 DB에 사용자 예외와 근거·이벤트를 저장한다."""
        if (course is None) == (not all_courses):
            raise typer.BadParameter("--course 또는 --all-courses 중 하나만 지정하세요.")
        try:
            dates = exception_dates(on_date=_iso_date(on_date), start=_iso_date(start), until=_iso_date(until),
                                    weekday=_weekday(weekday))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        try:
            with _library(root) as connection, connection:
                course_id = None if all_courses else resolve_course(connection, course)
                result = add_exceptions(connection, dates, course_id=course_id, kind=kind, note=note)
                if by_user:
                    for identity in result.created + result.existing:
                        confirmation.record(connection, "ScheduleException", identity, confirmation.CONFIRMED,
                                            "사용자가 schedule except add --by-user로 직접 확인")
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            typer.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return
        scope = "전 과목" if all_courses else course
        typer.echo(f"일정 예외: {scope} · {kind} · {len(result.dates)}일")
        typer.echo(f"신규 {len(result.created)}건 · 기존 {len(result.existing)}건")
        typer.echo("날짜: " + ", ".join(result.dates))

    @exception_app.command("list")
    def list_exceptions(root: RootOption = Path("."), as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        """일정 예외와 확인 상태(inferred / confirmed_by_user)를 보여 준다."""
        try:
            with _library(root) as connection:
                items = confirmation.schedule_exceptions(connection)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        if as_json:
            typer.echo(json.dumps(items, ensure_ascii=False, indent=2))
            return
        for item in items:
            mark = "확인됨" if item["confirmation"] == confirmation.CONFIRMED else "추정(확인 필요)"
            if item["maybe_online"]:
                mark += " · 원격·동영상 대체 가능(미정)"
            elif item["replacement"] == confirmation.ONLINE:
                mark += " · 온라인(영상) 강의로 대체"
            typer.echo(f"{item['date']} {item['course']} {item['kind']} · {mark} · {item['id']}")

    @exception_app.command("confirm")
    def confirm(
        ids: Annotated[list[str] | None, typer.Argument(help="확인할 예외 ID")] = None,
        root: RootOption = Path("."),
        note: Annotated[str, typer.Option("--note", help="확인 근거")] = "사용자가 직접 확인",
        maybe_online: Annotated[bool, typer.Option(
            "--maybe-online", help="휴강은 맞지만 원격·동영상 강의로 대체될 수 있음(미정)")] = False,
    ) -> None:
        """사용자가 직접 확인한 예외를 confirmed_by_user로 기록한다(원래 행은 그대로)."""
        if not ids:
            raise typer.BadParameter("확인할 예외 ID를 하나 이상 주세요(schedule except list).")
        try:
            with _library(root) as connection, connection:
                known = {item["id"] for item in confirmation.schedule_exceptions(connection)}
                for identity in ids:
                    if identity not in known:
                        raise ValueError(f"없는 예외 ID: {identity}")
                    confirmation.record(connection, "ScheduleException", identity, confirmation.CONFIRMED, note)
                    if maybe_online:
                        confirmation.record_maybe_online(connection, identity, note)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"확인 기록 {len(ids)}건" + (" · 원격·동영상 대체 가능(미정)표시" if maybe_online else ""))

    @exception_app.command("mark-inferred")
    def mark_inferred(
        reason: Annotated[str, typer.Option("--reason", help="추정으로 남기는 이유")],
        root: RootOption = Path("."),
    ) -> None:
        """사용자 확인이 없는 예외 전부에 inferred 재판정 근거를 덧붙인다(append-only)."""
        try:
            with _library(root) as connection, connection:
                pending = [item for item in confirmation.schedule_exceptions(connection)
                           if item["confirmation"] != confirmation.CONFIRMED]
                for item in pending:
                    confirmation.record(connection, "ScheduleException", item["id"], confirmation.INFERRED, reason)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _fail(exc)
        typer.echo(f"inferred 근거 {len(pending)}건 추가")

    schedule_app.add_typer(exception_app, name="except")
    app.add_typer(schedule_app, name="schedule")
