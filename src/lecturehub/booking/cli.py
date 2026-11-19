"""그스룸 CLI(P-019) — 앱 밖의 예약 작업(스케줄러가 돌리는 자동반납·정기 예약)이 앱과 같은 코드·같은 기록 형식을 쓰게 한다.

    lecturehub gsr run list|book|return|cancel ...   Claude in Chrome으로 실행(1분 안팎), status/log 기록, 결과 JSON 한 줄
    lecturehub gsr due                        반납 작업을 걸어야 할 예약(status.json 기준)
    lecturehub gsr rules-for [--date]         정기 예약 규칙 중 그날(기본 3일 뒤) 해당하는 것
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from lecturehub.core.settings import root_option_callback
from . import store

Root = Annotated[Path, typer.Option("--root", callback=root_option_callback, help="학기 폴더(비우면 설정의 학기 폴더)")]


def _service(root: Path):
    from .service import RoomService

    return RoomService(root, start_thread=lambda fn: fn())  # 동기 실행: 명령이 끝나면 결과가 있다


def _echo(data) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False))


def register(app: typer.Typer) -> None:
    gsr = typer.Typer(no_args_is_help=True, help="그스룸(관정관 4층 6인실) 예약 — Claude in Chrome")

    @gsr.command("run")
    def run_command(
        action: Annotated[str, typer.Argument(help="list | book | return | cancel")],
        root: Root = None,
        day: Annotated[str, typer.Option("--date", help="book: YYYY-MM-DD")] = "",
        start: Annotated[str, typer.Option("--start", help="book: HH:MM")] = "",
        hours: Annotated[float, typer.Option("--hours", help="book: 0.5~12(30분 단위, 3시간을 넘으면 3시간 블록을 이어서 예약)")] = 3,
        use: Annotated[str, typer.Option("--use", help="book: 용도")] = "과제",
        room: Annotated[str, typer.Option("--room", help="book: 방 지정(예: G415)")] = "",
        booking_id: Annotated[str, typer.Option("--id", help="return: 예약 번호 · cancel: 예약완료인 예약 번호(쉼표로 여러 개)")] = "",
        by: Annotated[str, typer.Option("--by", help="기록에 남길 실행 주체(task = 예약 작업)")] = "cli",
        skip_if_done: Annotated[bool, typer.Option("--skip-if-done",
                                                   help="return: 이미 반납을 시도했으면 Claude를 부르지 않는다")] = False,
    ) -> None:
        """Claude in Chrome으로 조회·예약·반납을 한 번 하고 결과 JSON을 한 줄로 출력한다."""
        service = _service(root)
        if action == "list":
            started = service.refresh(by)
        elif action == "book":
            started = service.book(day, start, use, room or None, by, hours)
            if started.get("needs_confirmation"):
                b = started["booking"]
                if not typer.confirm(f"{b['day']} {b['room']} {b['start']}~{b['end']} ({b['use']}) 예약할까요?"):
                    raise typer.Exit(1)
                started = service.book(day, start, use, room or None, by, hours, confirmed=True)
        elif action == "return":
            started = service.return_now(booking_id, by, skip_if_done)
            if started.get("skipped"):
                _echo(started)
                return
        elif action == "cancel":
            started = service.cancel(booking_id, by)
        else:
            raise typer.BadParameter("list | book | return | cancel 중 하나")
        if not started.get("ok"):
            _echo(started)
            raise typer.Exit(1)
        result = dict(service.state()["job"].get("result") or {})
        result.pop("bookings", None)
        _echo(result)
        if not result.get("ok") and not result.get("skipped"):
            raise typer.Exit(1)

    @gsr.command("due")
    def due_command(root: Root = None,
                    lead: Annotated[int, typer.Option("--lead", help="끝나기 몇 분 전에 반납할지")] = 29) -> None:
        """반납 작업이 필요한 예약(아직 반납 시도 없음, 끝나기 lead+1분 전보다 뒤)을 JSON 목록으로."""
        if store.public_mode():
            _echo({"ok": False, "error": "공개판에서는 자동 예약·예약 작업용 due 명령을 사용하지 않습니다"})
            raise typer.Exit(2)
        _echo(_service(root).due_returns(lead))

    @gsr.command("rules-for")
    def rules_for_command(root: Root = None,
                          day: Annotated[str, typer.Option("--date", help="YYYY-MM-DD, 기본 = 오늘+3일")] = "") -> None:
        """정기 예약 규칙 중 그날 요일에 켜진 것과 그날 이미 있는 예약."""
        if store.public_mode():
            _echo({"ok": False, "error": "공개판에서는 정기 예약 규칙을 사용하지 않습니다"})
            raise typer.Exit(2)
        target = date.fromisoformat(day) if day else (datetime.now().astimezone().date() + timedelta(days=3))
        _echo(_service(root).rules_for(target))

    app.add_typer(gsr, name="gsr")
