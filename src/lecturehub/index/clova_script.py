"""``aside repl``에 넘길 클로바노트 스크립트. 본문은 ``assets/clova_fetch.js``이고 여기서 한 줄로 합친다.

REPL은 줄마다 따로 실행하므로 한 줄이어야 한다(그래서 그 파일은 줄 전체 주석만 쓰고 문장을 세미콜론으로 끝낸다).
한 번 실행에 노트 하나만 받는다 — 녹음이 수십 MB라 REPL의 한 번 제한(120초) 안에 끝내기 위해서다.
결과: ``RESULT: {ok, login_required?, error?, skipped_uploading, more, notes: [{id, created(현지 ISO), duration_ms,
title, files: [{kind: text|audio, path(세션 폴더 기준), suggested}]}]}``
"""
from __future__ import annotations

import json
from pathlib import Path

TIMEOUT_S = 150  # aside repl의 한 번 제한이 120초
SOURCE = Path(__file__).resolve().parent / "assets" / "clova_fetch.js"


def build(known_ids: list[str]) -> str:
    lines = [ln.strip() for ln in SOURCE.read_text(encoding="utf-8").splitlines()]
    body = " ".join(ln for ln in lines if ln and not ln.startswith("//"))
    return body.replace("__KNOWN__", json.dumps(list(known_ids))) + "\nexit\n"
