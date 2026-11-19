"""Copy the confirmed mockup's CSS verbatim into src/lecturehub/ui/web/app.css (part 1).

Part 2 (app additions) lives after the marker and is preserved on re-runs.
Usage: .venv\\Scripts\\python.exe tools\\extract_mockup_css.py [mockup_main.html]
"""
import sys
from pathlib import Path

MOCKUP = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\dev\_plan\docs\lecture-hub\ui\mockup_main.html")
TARGET = Path(__file__).resolve().parents[1] / "src" / "lecturehub" / "ui" / "web" / "app.css"
MARKER = "/* ===== 2부: 앱 추가 ===== */"
HEAD = """/* Lecture Hub 앱 스타일.
   1부: 확정 시안(docs/lecture-hub/ui/mockup_main.html)의 CSS를 그대로 옮김 — 직접 고치지 말고 tools/extract_mockup_css.py로 다시 만든다.
   2부: 앱에서 추가한 요소(메뉴·대화상자·빈 화면). 시안의 색 토큰만 쓴다. */
@font-face{font-family:"Pretendard";src:url("fonts/PretendardVariable.woff2") format("woff2");font-weight:45 920;font-style:normal;font-display:block}

/* ===== 1부: 시안 ===== */
"""

source = MOCKUP.read_text(encoding="utf-8")
css = source[source.index("<style>") + len("<style>"): source.index("</style>")].strip("\n")
extra = ""
if TARGET.exists() and MARKER in TARGET.read_text(encoding="utf-8"):
    extra = TARGET.read_text(encoding="utf-8").split(MARKER, 1)[1]
TARGET.write_text(HEAD + css + "\n\n" + MARKER + extra, encoding="utf-8")
print(f"{TARGET}: 시안 CSS {len(css)}자")
