"""Headless offline QA of the built public page. No native app/window or real accounts."""
import functools
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "work" / "trial-qa"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    sys.path.insert(0, str(ROOT / "work" / "qa-deps"))
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT / "work" / "dist")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    widths = [int(sys.argv[1])] if len(sys.argv) > 1 else [1440, 1100, 900, 600, 390]
    results = []
    if len(widths) == 1 and "--probe" not in sys.argv and "--isolated" not in sys.argv and (OUT / "results.json").is_file():
        results = [r for r in json.loads((OUT / "results.json").read_text(encoding="utf-8")) if r["width"] not in widths]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for width in widths:
            for theme in ("light", "dark"):
                timezone = "America/Los_Angeles" if width == 1100 else "Asia/Seoul"
                context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme,
                                              timezone_id=timezone)
                page = context.new_page()
                page.set_default_timeout(6000)
                errors, external, native, visited = [], [], [], []
                overflows, retired = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("dialog", lambda d: (native.append(d.type), d.dismiss()))

                def route(req):
                    if req.request.url.startswith(origin + "/"):
                        if width == 1100 and req.request.url.endswith("/demo-bridge.js"):
                            # Override only this document's clock. Playwright's global clock injection would also
                            # try to inject scripts into the intentionally script-free sandboxed note frame.
                            prefix = "window.Date = class extends Date { constructor(...a){super(...(a.length?a:[2062065600000]));} static now(){return 2062065600000;} };\n"
                            req.fulfill(content_type="text/javascript", body=prefix + (ROOT / "work/dist/demo-bridge.js").read_text(encoding="utf-8"))
                        else:
                            req.continue_()
                    else:
                        external.append(req.request.url)
                        req.abort()
                page.route("**/*", route)
                page.goto(origin + "/index.html")
                page.wait_for_function("() => window.__demoLog && window.__demoLog.some(x => x[0] === 'week_view')")
                page.wait_for_timeout(500)
                assert page.evaluate("() => { const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` === window.__DEMO_CALLS['init[]'].today_iso; }")
                page.evaluate("t => { document.documentElement.dataset.theme=t; localStorage.setItem('lh.theme',t); }", theme)

                def shot(name):
                    visited.append(name)
                    check_text(name)
                    page.screenshot(path=str(OUT / f"{width}-{theme}-{name}.png"), full_page=True)

                def check_text(name):
                    text = page.locator('body').inner_text()
                    if "체험" in text:
                        retired.append(name)
                    assert not page.locator('#roomBtn:visible').count()
                    issues = page.evaluate("""() => {
                      const found=[];
                      for(const root of document.querySelectorAll('#todayCard,#todo,.home-month,#calGrid,#calSide')) {
                        if(!root.checkVisibility()) continue;
                        for(const e of root.querySelectorAll('*')) {
                          if(!e.checkVisibility() || !e.textContent.trim()) continue;
                          if(e.children.length || getComputedStyle(e).display==='contents') continue;
                          const r=e.getBoundingClientRect(), p=e.parentElement, b=p.getBoundingClientRect();
                          const style=getComputedStyle(p);
                          // A scroller's off-screen contents and deliberately clipped labels cannot paint outside it.
                          if(['auto','scroll','hidden','clip'].includes(style.overflowX) || ['auto','scroll','hidden','clip'].includes(style.overflowY)) continue;
                          if(r.width && r.height && (r.left < b.left-2 || r.right > b.right+2 || r.top < b.top-2 || r.bottom > b.bottom+2))
                            found.push({tag:e.tagName,cls:e.className,text:e.textContent.slice(0,90),child:[r.x,r.y,r.width,r.height],parent:[b.x,b.y,b.width,b.height]});
                        }
                        const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
                        let node;
                        while((node=walker.nextNode())) {
                          const e=node.parentElement;
                          if(!node.textContent.trim() || !e.checkVisibility()) continue;
                          const b=e.getBoundingClientRect(), s=getComputedStyle(e), range=document.createRange();
                          range.selectNodeContents(node);
                          const clipX=['auto','scroll','hidden','clip'].includes(s.overflowX);
                          const clipY=['auto','scroll','hidden','clip'].includes(s.overflowY);
                          for(const r of range.getClientRects()) if(r.width && r.height &&
                            ((!clipX && (r.left<b.left-2 || r.right>b.right+2)) || (!clipY && (r.top<b.top-2 || r.bottom>b.bottom+2))))
                            found.push({tag:e.tagName,cls:e.className,text:node.textContent.slice(0,90),kind:'text',
                              child:[r.x,r.y,r.width,r.height],parent:[b.x,b.y,b.width,b.height]});
                        }
                      }
                      return found;
                    }""")
                    overflows.extend({"screen": name, **i} for i in issues)

                def click(selector):
                    try:
                        page.locator(selector + ":visible").first.click()
                    except Exception:
                        (OUT / "failure.html").write_text(page.content(), encoding="utf-8")
                        page.screenshot(path=str(OUT / "failure.png"), full_page=True)
                        print(json.dumps({"at": selector, "errors": errors, "calls": page.evaluate("window.__demoLog")}, ensure_ascii=False), flush=True)
                        raise
                    page.wait_for_timeout(200)
                    if selector.startswith(".demo-confirm") or "ai-undo" in selector or "auto-reset" in selector:
                        page.wait_for_timeout(1100)  # settings reloads its recorded read models sequentially

                def close():
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)

                shot("home-week")
                click('#engines')
                quota_text = page.locator('.eng-pop').inner_text()
                free_minutes = page.evaluate("Math.floor(window.__DEMO_CALLS['engines[]'].whisper.usage.day_left_s/60)")
                free_requests = page.evaluate("window.__DEMO_CALLS['quota[]'].remaining")
                assert f"오늘 무료 한도 {free_minutes}분 남음" in quota_text
                assert f"오늘 무료 Google 전사 요청: {free_requests}회" in quota_text
                shot("free-quota")
                click('#engines')
                # A temporary probe flag makes the same runner useful while bringing new UI calls into coverage.
                if "--probe" in sys.argv:
                    (OUT / "probe.html").write_text(page.content(), encoding="utf-8")
                    print(json.dumps({"errors": errors, "unanswered": page.evaluate("window.__demoUnanswered"),
                                      "calls": page.evaluate("window.__demoLog")}, ensure_ascii=False))
                    context.close()
                    break
                assert page.locator(".week-class.cancelled").count() >= 1
                assert page.locator(".week-class.changed").count() >= 1
                click('[data-home-mode="month"]')
                shot("home-month")
                click('[data-home-mode="week"]')
                week_start = page.locator(".today-tools").inner_text()
                click('[data-week-step="1"]')
                click('[data-week-step="-1"]')
                click('[data-week-step="0"]')
                assert page.locator(".today-tools").inner_text() == week_start
                click("#homeCandidates")
                page.locator(".cand").first.wait_for()
                assert page.locator(".cand .q").count() >= 2
                assert "녹음 31:00" in page.locator('#calSide').inner_text()
                shot("calendar")
                click('[data-c="etl-sync"]')
                click('[data-fold="cal-submitted"] > summary')
                assert "제출함" in page.locator("#calSide").inner_text()
                click("[data-confirm]")
                assert page.locator(".cand").count() == 2
                click('[data-c="add"]')
                page.locator("#fTitle").fill("수문학 과제 검산")
                page.locator("#fKind").select_option("personal")
                page.locator("#fDate").fill(page.evaluate("() => window.__DEMO_CALLS['init[]'].today_iso"))
                click('[data-c="save"]')
                assert "수문학 과제 검산" in page.locator("#calSide").inner_text()
                click('[data-c="close"]')
                # Course → lecture with transcript → note, via the app's own navigation.
                click('.week-class[data-course="수문학"]')
                visited.append("course")
                if not page.locator(".side").is_visible():
                    click("#sideBtn")
                if not page.locator("[data-lecture]").first.is_visible():
                    click('[data-toggle="수문학"]')
                lecture_id = page.evaluate("() => Object.values(window.__DEMO_CALLS).find(v => v?.lecture?.segments?.some(s => s.text.includes('합리식은')))?.lecture.id")
                click(f'[data-lecture="{lecture_id}"]')
                page.locator("#lvHead").wait_for(state="visible")
                if page.locator("body.html-full").count():
                    click('#htmlFloat [data-size="pane"]')
                if width <= 1100 and page.locator(".side").is_visible():
                    click("#sideBtn")
                shot("lecture")
                click('[data-t="c-html"]')
                page.wait_for_timeout(400)
                assert page.locator("iframe").count() >= 1
                if page.locator('body.html-full').count():
                    click('#htmlFloat [data-size="pane"]')
                shot("lecture-note")
                visited.append("note")
                click("#searchBtn")
                for word in ["유출계수", "도달시간", "단위도", "투수계수", "퀴즈"]:
                    page.locator(".search-panel input").fill(word)
                    page.wait_for_timeout(500)
                    assert page.locator(".search-panel .r").count() >= 1, word
                    check_text("search-" + word)
                shot("search")
                close()
                visited.append("search")
                click("#examBtn")
                page.locator("[data-nc-open]").first.wait_for()
                visited.append("notes-collection")
                close()
                click("#inboxBtn")
                visited.append("inbox")
                close()
                click("#settingsBtn")
                page.locator(".set-tab").first.wait_for()
                assert page.locator(".set-tab").count() == 7
                for group in ["basic", "record", "ai", "connect", "consent", "extras", "about"]:
                    click(f'.set-tab[data-id="{group}"]')
                    assert page.locator(f'.set-pane[data-pane="{group}"]').is_visible()
                    shot("settings-" + group)
                click('.set-tab[data-id="basic"]')
                for selector in ["[data-local-alert=deadlines]", "[data-local-alert=briefing]", "[data-mute-course]", "[data-stay]", "[data-startup]"]:
                    click(selector)
                    click(selector)
                click('[data-a="tt"]')
                page.locator("#tt-start").wait_for()
                shot("timetable")
                click('[data-tt="save"]')
                assert "설치한 앱에서는" in page.locator('body').inner_text()
                click('[data-tt="cancel"]')
                click('[data-a="tt-photo"]')
                click(".demo-confirm [data-yes]")
                page.wait_for_timeout(150)
                click('.set-tab[data-id="ai"]')
                click("[data-ailink-write]")
                page.wait_for_timeout(1100)
                click("[data-ailink-write]")
                page.wait_for_timeout(1100)
                page.locator("[data-ailink-auto]").first.select_option("auto")
                click(".demo-confirm [data-yes]")
                click('[data-a="ailink-auto-all"]')
                click(".demo-confirm [data-yes]")
                assert page.locator('[data-ailink-auto] option:checked').evaluate_all("xs => xs.every(x => x.value === 'auto')")
                click('[data-a="ailink-auto-reset"]')
                page.locator('[data-ailink-auto="save_note"]').select_option("auto")
                click(".demo-confirm [data-yes]")
                page.locator(".ailink").evaluate("e => e.scrollIntoView({block:'start'})")
                shot("settings-ai-controls")
                click('[data-a="ailink-disconnect"][data-id="codex"]')
                click('[data-a="ailink-connect"][data-id="codex"]')
                click(".demo-confirm [data-yes]")
                page.locator('[data-a="ailink-disconnect"][data-id="codex"]').wait_for()
                page.locator(".ailink-app").first.scroll_into_view_if_needed()
                shot("settings-ai-card")
                page.locator(".ai-change").first.evaluate("e => e.scrollIntoView({block:'start'})")
                shot("settings-ai-changes")
                click('[data-a="ai-undo"]')
                assert page.locator(".ai-change").filter(has_text="되돌림").count() == 1
                click('[data-a="ailink-codex"]')
                click('[data-a="done"]')
                before_bare = page.evaluate("""() => ({unanswered:window.__demoUnanswered,failures:window.__demoFailures,
                    unexpected:window.__demoLog.filter(x=>x[1]),calls:window.__demoLog.length})""")
                # The video URL hides only the quiet strip; date pinning and the same real UI still boot.
                page.goto(origin + "/index.html?bare=1")
                page.wait_for_function("() => window.__demoLog?.some(x => x[0] === 'week_view')")
                page.wait_for_timeout(400)
                assert not page.locator('.trial-bar').count()
                shot("home-week-bare")
                results.append({"width": width, "theme": theme, "timezone": timezone,
                                "future_clock": width == 1100, "console_errors": errors,
                                "unanswered": before_bare["unanswered"] + page.evaluate("window.__demoUnanswered"),
                                "bridge_failures": before_bare["failures"] + page.evaluate("window.__demoFailures"),
                                "external_requests": external, "native_dialogs": native, "visited": visited,
                                "overflowing_elements": overflows, "retired_word_occurrences": retired,
                                "unexpected_answers": before_bare["unexpected"] + page.evaluate("window.__demoLog.filter(x => x[1])"),
                                "calls": before_bare["calls"] + page.evaluate("window.__demoLog.length")})
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                context.close()
        browser.close()
    server.shutdown()
    results.sort(key=lambda r: (-r["width"], r["theme"] != "light"))
    result_name = f"results-{widths[0]}.json" if "--isolated" in sys.argv else "results.json"
    (OUT / result_name).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(r[k] for r in results for k in ("console_errors", "unanswered", "bridge_failures", "external_requests", "native_dialogs", "unexpected_answers", "overflowing_elements", "retired_word_occurrences")):
        raise SystemExit("QA failed; see work/trial-qa/results.json")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    main()
