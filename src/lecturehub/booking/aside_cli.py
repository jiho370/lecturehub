"""Aside 브라우저 CLI(``aside repl``)로 그스룸 작업을 하는 시험용 어댑터(D-003 외부 어댑터, P-019 보강).

``claude -p --chrome``과 달리 AI가 끼지 않는다. ``aside repl``(Playwright 방식 JS)이 Aside 창에서 예약 사이트 탭을
열고, ``assets/gsr_core.js``를 그 탭에 넣은 뒤 ``GSR.act(...)`` 결과를 ``RESULT: {json}`` 한 줄로 찍는다.
Aside에 가져온 Chrome 프로필을 쓴다. 로그인이 풀려 있으면 nsso 아이디 칸을 눌러 Aside 비밀번호 관리자가 채우게 하고,
채워졌을 때만 로그인 버튼을 누른다(값을 입력하거나 읽지 않음, D-060). 안 되면 ``login_required``를 돌려준다. 스크립트는 stdin으로 넘긴다(코어가 커서 명령줄 길이 제한을 피한다).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import claude_cli

TIMEOUT_S = 150  # aside repl 자체 제한이 120초
ENTRY = claude_cli.ENTRY
FLOOR_LIST = "https://k-rsv.snu.ac.kr/NEW_SNU_BOOKING/pc/booking/floorList"
SSO_LOGIN = claude_cli.SSO_LOGIN
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def find_aside() -> str | None:
    override = os.environ.get("LECTUREHUB_ASIDE_EXE")
    if override:
        return override
    if os.environ.get("PYTEST_CURRENT_TEST"):  # tests never drive the real browser/booking site
        return None
    found = shutil.which("aside")
    if found:
        return found
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Aside" / "CLI" / "current" / "aside.exe"
    return str(local) if local.is_file() else None


def build_script(action: dict, core: Path = claude_cli.CORE_JS) -> str:
    """REPL 한 줄짜리 스크립트. 줄바꿈이 있으면 REPL이 줄마다 따로 실행하므로 코어는 JSON 문자열로 감싼다."""
    src = json.dumps(core.read_text(encoding="utf-8"), ensure_ascii=False)
    call = json.dumps(json.dumps(action, ensure_ascii=False), ensure_ascii=False)
    entry = json.dumps(ENTRY)
    floor = json.dumps(FLOOR_LIST)
    sso = json.dumps(SSO_LOGIN)
    js = (
        "let gsrOut; let gsrTab; let gsrWhy = ''; let gsrActed = false; try {"
        # 탭 연결이 끊기면(오류 페이지에서 다른 사이트로 넘어갈 때 등) 같은 사이트의 탭에 다시 붙는다.
        " const gsrReattach = async () => { const ts = await listBrowserTabs();"
        " const m = ts.find(x => /k-rsv\\.snu\\.ac\\.kr|nsso\\.snu\\.ac\\.kr/.test(String(x.url)));"
        " if (m) { const p = await attachBrowserTab(m.targetId); gsrTab = p || page; } };"
        " const gsrUrlNow = async () => { try { return String(await gsrTab.url()); }"
        " catch (e) { await gsrReattach(); return String(await gsrTab.url()); } };"
        # 그스룸 입구 → SNU 로그인 사이 한 단계가 http로 양식을 보내 '안전하지 않은 양식' 경고가 뜬다(사이트 설계).
        # 이 단계에서만, 경고 문구가 맞을 때만 '그래도 보내기'를 누른다(사용자 결정 2026-11-06).
        " const gsrPassWarning = async () => { try { return await gsrTab.evaluate("
        "\"(() => { const t = document.body ? document.body.innerText : '';"
        " if (!/안전하지 않은|not secure/i.test(t)) return false;"
        " const b = document.getElementById('proceed-button') ||"
        " [...document.querySelectorAll('button, a')].find(x => /보내기|Send anyway/i.test(x.innerText || ''));"
        " if (!b) return false; b.click(); return true; })()\"); } catch (e) { return false; } };"
        # 1) 사이트 세션이 살아 있으면 목록 화면이 바로 열린다(SSO 넘김 없음).
        f" gsrTab = await openTab({floor});"
        " let gsrUrl = await gsrUrlNow();"
        " const gsrEnter = async () => {"
        f" try {{ await gsrTab.goto({entry}); }} catch (e) {{ await gsrReattach(); }}"
        " let gsrPassed = 0;"
        " for (let i = 0; i < 40; i++) { gsrUrl = await gsrUrlNow();"
        " if (/chrome-error/.test(gsrUrl) && gsrPassed < 2 && await gsrPassWarning()) { gsrPassed++; await sleep(500); continue; }"
        " if (/floorList|\\/pc\\/login|nsso\\.snu\\.ac\\.kr|chrome-error/.test(gsrUrl)) break; await sleep(250); } };"
        # 2) 아니면 SSO 입구로(SNU 로그인이 살아 있으면 목록으로 넘어간다).
        " if (!/floorList/.test(gsrUrl)) await gsrEnter();"
        # 3) 그래도 아니면 nsso 아이디 로그인: 아이디 칸을 눌러 Aside 비밀번호 관리자가 채우게 하고, 둘 다 채워졌을 때만
        #    로그인 버튼을 누른다. 값을 입력하거나 읽지 않는다(길이만 본다). 추가 인증 화면이면 멈춘다(D-060과 같은 규칙).
        #    오류 페이지에서 goto하면 탭 연결이 끊기므로 로그인 화면은 새 탭으로 연다.
        " if (!/floorList/.test(gsrUrl)) {"
        " if (!/\\/mfa\\/login\\/view/.test(gsrUrl)) { try { await closeTab(gsrTab); } catch (e) {}"
        f" gsrTab = await openTab({sso}); }}"
        # 로그인 화면은 열린 직후 한 번 다시 그려져 방금 찾은 칸이 사라질 수 있다("Element is detached"): 잠깐 두고 다시 누른다.
        " const gsrClick = async (sel) => { for (let k = 0; k < 4; k++) { try { await gsrTab.locator(sel).click(); return; }"
        " catch (e) { if (k === 3 || !/detached|not attached|stale/i.test(String(e && e.message))) throw e; await sleep(700); } } };"
        " await gsrClick('#login_id'); let gsrFilled = false;"
        " for (let i = 0; i < 12 && !gsrFilled; i++) { await sleep(250); gsrFilled = await gsrTab.evaluate("
        "\"['login_id','login_pwd'].every(i => (document.getElementById(i) || {value: ''}).value.length > 0)\"); }"
        " if (!gsrFilled) { gsrWhy = '자동입력이 비어 있음(Aside 비밀번호 관리자에 SNU 계정이 없음)'; } else {"
        " await gsrClick('#loginProcBtn');"
        " for (let i = 0; i < 40; i++) { await sleep(250); if (!/\\/mfa\\/login\\/view/.test(await gsrUrlNow())) break; }"
        " const gsrAfter = await gsrUrlNow();"
        " if (/nsso\\.snu\\.ac\\.kr/.test(gsrAfter)) { gsrWhy = '로그인 버튼 뒤에도 SNU 로그인 화면(추가 인증 필요?): ' + gsrAfter.slice(0, 80); }"
        " else { await gsrEnter(); } } }"
        " if (!/floorList/.test(gsrUrl)) {"
        " gsrOut = JSON.stringify({ok: false, login_required: true,"
        " error: 'Aside 브라우저에서 그스룸 사이트 로그인이 필요합니다(' + (gsrWhy || gsrUrl.slice(0, 80)) + ')'}); } else {"
        f" await gsrTab.evaluate({src}); gsrActed = true;"
        f" gsrOut = await gsrTab.evaluate('(async () => JSON.stringify(await GSR.act(' + {call} + ')))()'); }}"
        " } catch (e) { gsrOut = JSON.stringify({ok: false, error: 'Aside 실행 중 오류: ' + String(e && e.message || e).slice(0, 200),"
        " login_required: false, acted: gsrActed}); }"
        " finally { if (gsrTab) { try { await closeTab(gsrTab); } catch (e) {} } }"
        " console.log('RESULT: ' + gsrOut);"
    )
    return js + "\nexit\n"


def parse_output(stdout: str) -> dict:
    """``aside repl`` 출력(색 코드·안내 줄 포함)에서 마지막 RESULT 줄을 꺼낸다. 종료 코드는 오류여도 0이라 보지 않는다."""
    text = ANSI.sub("", stdout or "")
    at = text.rfind("RESULT:")
    brace = text.find("{", at) if at >= 0 else -1
    if brace < 0:
        errors = [ln.strip() for ln in text.splitlines() if "Error" in ln or "[error" in ln]
        detail = (errors[0] if errors else text.strip()[-200:]) or "출력 없음"
        return {"ok": False, "error": "Aside가 결과를 돌려주지 않았습니다: " + detail[:200]}
    try:
        result, _ = json.JSONDecoder().raw_decode(text[brace:])
    except ValueError:
        return {"ok": False, "error": "결과 JSON이 깨졌습니다: " + text[brace:brace + 200]}
    if not isinstance(result, dict):
        return {"ok": False, "error": "결과 형식이 다릅니다"}
    if isinstance(result.get("bookings"), list):
        result["bookings"] = claude_cli.expand_bookings(result["bookings"])
    return result


def execute(args: list[str], script: str, *, timeout: int, stop=None):
    """aside를 창 없이 낮은 우선순위로 실행하고 스크립트를 stdin으로 준다. ``(stdout, stderr, returncode)`` 또는 오류 dict."""
    flags = claude_cli.CREATE_NO_WINDOW | claude_cli.BELOW_NORMAL_PRIORITY if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace", creationflags=flags)
    except OSError as exc:
        return {"ok": False, "error": f"Aside를 실행하지 못했습니다: {exc}"}
    deadline = time.monotonic() + timeout
    pending = script  # communicate() takes input only on the first call; retries keep the output
    while True:
        try:
            stdout, stderr = proc.communicate(input=pending, timeout=0.5)
            return stdout, stderr, proc.returncode
        except subprocess.TimeoutExpired:
            pending = None
            if stop is not None and stop.is_set():
                claude_cli._stopped(proc)
                return {"ok": False, "stopped": True, "error": "사용자가 중지했습니다"}
            if time.monotonic() > deadline:
                claude_cli._stopped(proc)
                return {"ok": False, "error": f"Aside가 {max(timeout, 0) // 60 or 1}분 안에 끝내지 못했습니다"}


def run(action: dict, *, timeout: int = TIMEOUT_S, stop=None) -> dict:
    """Aside로 한 번 실행한다. AI를 쓰지 않으므로 비용은 없다(``cost_usd`` 없음)."""
    exe = find_aside()
    if not exe:
        return {"ok": False, "error": "Aside CLI(aside.exe)를 찾지 못했습니다. Aside 설정 > Developers에서 설치하거나 "
                                      "LECTUREHUB_ASIDE_EXE로 경로를 지정하세요"}
    for attempt in range(2):
        out = execute([exe, "repl"], build_script(action), timeout=timeout, stop=stop)
        if isinstance(out, dict):
            return out
        stdout, stderr, _code = out
        result = parse_output(stdout)
        if not result.get("ok") and not (stdout or "").strip() and (stderr or "").strip():
            result["error"] = stderr.strip()[-200:]
        # 로그인 넘김 중 탭 연결이 끊긴 경우만 처음부터 한 번 더(사이트에 요청을 보내기 전이라 두 번 예약될 일 없음).
        # 두 번째에는 로그인이 살아 있어 바로 목록 화면이 열린다.
        detached = "detached" in str(result.get("error", "")).lower()
        # Aside가 꺼져 있다가 켜지는 데 60초를 넘긴 경우도 스크립트가 돌기 전이라 한 번 더 한다.
        starting = "finish starting" in str(result.get("error", "")).lower() and "acted" not in result
        if attempt == 0 and ((detached and result.get("acted") is False) or starting) and not (stop is not None and stop.is_set()):
            continue
        result.pop("acted", None)
        return result
    return result
