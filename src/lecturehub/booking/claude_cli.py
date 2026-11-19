"""Claude Code CLI(``claude -p --chrome``)로 사용자 Chrome에서 그스룸 작업을 시키는 외부 어댑터(D-003, P-019).

앱은 브라우저를 직접 조작하지 않는다. 사용자 Chrome에 저장된 SNU 로그인을 쓰고, 비밀번호는 다루지 않는다.
Claude는 ``assets/gsr_core.js``를 예약 사이트 탭에 넣고 ``GSR.act(...)`` 결과를 ``RESULT: {json}`` 한 줄로 돌려준다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

CORE_JS = Path(__file__).resolve().parent / "assets" / "gsr_core.js"
TIMEOUT_S = 300
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000
ALLOWED_TOOLS = ["mcp__claude-in-chrome", "Read", "ToolSearch"]
CHROME_TOOLS = ",".join(f"mcp__claude-in-chrome__{t}" for t in (
    "tabs_context_mcp", "tabs_create_mcp", "navigate", "computer", "javascript_tool", "tabs_close_mcp"))
ENTRY = "https://k-rsv.snu.ac.kr/NEW_SNU_BOOKING/LibLogin2.jsp"
SSO_LOGIN = "https://nsso.snu.ac.kr/sso/usr/snu/mfa/login/view"


def find_claude() -> str | None:
    override = os.environ.get("LECTUREHUB_CLAUDE_EXE")
    if override:
        return override
    if os.environ.get("PYTEST_CURRENT_TEST"):  # tests never drive the real Chrome/booking site
        return None
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    return str(local) if local.is_file() else None


def build_prompt(action: dict, core: Path = CORE_JS) -> str:
    call = json.dumps(action, ensure_ascii=False)
    return f"""너는 Lecture Hub 앱의 그스룸(관정관 그룹스터디룸) 예약 어댑터다. 아래 순서만 수행하고 다른 페이지나 작업은 하지 않는다.

1. ToolSearch 한 번으로 로드: select:{CHROME_TOOLS}
2. tabs_context_mcp(createIfEmpty:true)로 탭 그룹을 얻고 새 탭(tabs_create_mcp)에서 {ENTRY} 로 이동한다. 로그인돼 있으면 /pc/booking/floorList 로 넘어간다.
3. 로그인이 풀려 있으면(주소가 /pc/login/form 이거나 nsso.snu.ac.kr): {SSO_LOGIN} 로 이동해 스크린샷으로 '아이디' 로그인 칸에 Chrome이 아이디·비밀번호를 자동입력해 둔 것을 확인하고 로그인 버튼만 누른다. 값을 입력하거나 읽지 않는다. 이어서 뜨는 my.snu.ac.kr 오류 화면(EA0014)은 무시하고 다시 {ENTRY} 로 간다. 자동입력이 비어 있거나 휴대폰·2단계 인증을 요구하면 멈추고 7단계로 가서 login_required를 true로 보고한다. SSOService.do?pname=spLogin 주소는 쓰지 않는다(http 폼 경고).
4. Read로 {core} 파일 전체를 읽는다.
5. javascript_tool 한 번으로 실행한다. 코드 = 4단계 파일 내용을 한 글자도 바꾸지 않고 그대로, 그 다음 줄에 `JSON.stringify(await GSR.act({call}))`
6. 만든 탭을 닫는다.
7. 마지막 줄에 정확히 `RESULT: ` 뒤에 5단계가 돌려준 JSON 문자열을 그대로 붙여 출력한다. 5단계에 이르지 못했으면 `RESULT: {{"ok":false,"error":"<어느 단계에서 왜>","login_required":<true 또는 false>}}`

금지: 페이지 함수(seatBooking, cancleBooking, returnBooking) 호출 금지 — 확인 창이 떠서 브라우저가 멈춘다. /json/setCancelBooking 직접 호출 금지 — 예약취소와 같은 API다. 5단계는 한 번만 실행하고 실패해도 다시 실행하지 않는다. 비밀번호를 입력하지 않는다."""


def parse_envelope(stdout: str) -> dict:
    """``claude -p --output-format json`` 출력의 바깥 JSON: ``{ok, text, cost_usd}`` 또는 ``{ok: False, error}``."""
    data = None
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return {"ok": False, "error": "Claude 출력을 읽지 못했습니다: " + (stdout or "").strip()[-200:]}
    cost = data.get("total_cost_usd")
    text = str(data.get("result") or "")
    if data.get("is_error"):
        return {"ok": False, "error": "Claude 실행 오류: " + text[-200:], "cost_usd": cost}
    return {"ok": True, "text": text, "cost_usd": cost}


def parse_output(stdout: str) -> dict:
    """``claude -p --output-format json`` 출력에서 RESULT 줄을 꺼낸다."""
    envelope = parse_envelope(stdout)
    if not envelope["ok"]:
        return envelope
    cost, text = envelope["cost_usd"], envelope["text"]
    at = text.rfind("RESULT:")
    brace = text.find("{", at) if at >= 0 else -1
    if brace < 0:
        return {"ok": False, "error": "Claude가 결과를 돌려주지 않았습니다: " + text.strip()[-200:], "cost_usd": cost}
    try:  # raw_decode: 뒤에 코드펜스나 말이 붙어도 JSON 부분만 읽는다
        result, _ = json.JSONDecoder().raw_decode(text[brace:])
    except ValueError:
        return {"ok": False, "error": "결과 JSON이 깨졌습니다: " + text[brace:brace + 200], "cost_usd": cost}
    if not isinstance(result, dict):
        return {"ok": False, "error": "결과 형식이 다릅니다", "cost_usd": cost}
    if isinstance(result.get("bookings"), list):
        result["bookings"] = expand_bookings(result["bookings"])
    result["cost_usd"] = cost
    return result


def expand_bookings(rows: list) -> list[dict]:
    """코어의 짧은 행 [id, 방, 'YYYY-MM-DD HH:MM', 끝, 상태, 반납가능, 상태이름] → status.json 형식."""
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, list) and len(row) >= 6:
            out.append({"id": str(row[0]), "seat": row[1], "start": f"{row[2]}:00", "end": f"{row[3]}:00",
                        "status": row[4], "returnable": bool(row[5]),
                        "statusName": row[6] if len(row) > 6 else str(row[4])})
    return out


def _kill_tree(proc) -> None:
    """claude.exe와 그 아래(node·MCP 도우미)까지 끝낸다. 자식만 끝내면 Chrome 쪽 작업이 이어질 수 있다."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False,
                       creationflags=CREATE_NO_WINDOW)
    else:
        proc.kill()


def _stopped(proc) -> None:
    _kill_tree(proc)
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def execute(args: list[str], *, timeout: int, stop=None):
    """claude를 창 없이 낮은 우선순위로 실행한다. ``(stdout, stderr, returncode)`` 또는 오류 dict.

    ``stop``(threading.Event)이 서면 프로세스 트리를 끝내고 ``stopped: True``를 돌려준다."""
    flags = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                cwd=str(CORE_JS.parent), creationflags=flags, stdin=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "error": f"Claude를 실행하지 못했습니다: {exc}"}
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            return stdout, stderr, proc.returncode
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                _stopped(proc)
                return {"ok": False, "stopped": True, "error": "사용자가 중지했습니다"}
            if time.monotonic() > deadline:
                _stopped(proc)
                return {"ok": False, "error": f"Claude가 {max(timeout, 0) // 60 or 1}분 안에 끝내지 못했습니다"}


def run(action: dict, *, timeout: int = TIMEOUT_S, stop=None) -> dict:
    """Claude를 한 번 실행한다(1분 안팎). 네트워크 작업이라 녹음 중에도 낮은 우선순위로 돌린다.

    ``stop``(threading.Event)이 서면 Claude 프로세스 트리를 끝내고 ``stopped: True``를 돌려준다. 이미 사이트에 요청이
    나간 뒤라면 그 요청은 되돌려지지 않으므로, 부른 쪽이 목록을 다시 확인하게 한다."""
    exe = find_claude()
    if not exe:
        return {"ok": False, "error": "Claude Code(claude.exe)를 찾지 못했습니다. LECTUREHUB_CLAUDE_EXE로 경로를 지정하세요"}
    args = [exe, "-p", build_prompt(action), "--chrome", "--allowedTools", *ALLOWED_TOOLS,
            "--output-format", "json"]
    model = os.environ.get("LECTUREHUB_GSR_MODEL")
    if model:
        args += ["--model", model]
    out = execute(args, timeout=timeout, stop=stop)
    if isinstance(out, dict):
        return out
    stdout, stderr, code = out
    result = parse_output(stdout)
    if not result.get("ok") and code and not (stdout or "").strip():
        result["error"] = (stderr or "").strip()[-200:] or result.get("error")
    return result
