"""Browser-only demo of the real app UI on the trial semester: app.js/app.css unchanged + a recorded bridge."""
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE  # calls.json / calls2.json and dist live here
WEB = Path(sys.argv[1])  # src/lecturehub/ui/web
DIST = (Path(sys.argv[2]) if len(sys.argv) > 2 else HERE) / "dist"
calls_text = (WORK / "calls.json").read_text(encoding="utf-8")
_more = json.loads((WORK / "calls2.json").read_text(encoding="utf-8"))
_base = json.loads(calls_text)
for _name in ("room", "gsr_rooms_get", "gsr_companions_get"):
    if isinstance(_more.get(_name), dict):
        _base[_name + "[]"] = _more[_name]
calls_text = json.dumps(_base, ensure_ascii=False)

# paths of this PC -> a neutral example path (the demo must not carry the builder's folders)
TRIAL = re.compile(r"C:(\\+)Users\1[^\\]+\1AppData\1Local\1Temp\1.*?\1trial_api\1")
HOME = re.compile(r"C:(\\+)Users\1[^\\/\"]+")


def clean(value):
    if isinstance(value, str):
        value = TRIAL.sub(lambda m: "C:" + m[1] + "Users" + m[1] + "me" + m[1] + "AppData" + m[1] + "Roaming" + m[1] + "lecturehub" + m[1], value)
        return HOME.sub(lambda m: "C:" + m[1] + "Users" + m[1] + "me", value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    return value


calls = clean(json.loads(calls_text))
dump = json.dumps(calls, ensure_ascii=False)
leak = re.search(r"Users\\\\+(?!me\\\\)[^\\\\\"]+|AppData\\\\+Local\\\\+Temp", dump)
assert not leak, f"path leak: {dump[max(0, leak.start() - 80):leak.end() + 20]!r}"

if DIST.exists():
    shutil.rmtree(DIST)
DIST.mkdir()
for name in ("app.js", "app.css", "german.js"):
    if (WEB / name).is_file():
        shutil.copy2(WEB / name, DIST / name)
shutil.copytree(WEB / "fonts", DIST / "fonts")

app_js = (DIST / "app.js").read_text(encoding="utf-8")
app_js = re.sub(r"\s*\(user 2026-10-01: [^)]*\)", "", app_js)  # a dev note naming a course the builder takes
(DIST / "app.js").write_text(app_js, encoding="utf-8")
bridge = (HERE / "bridge.js").read_text(encoding="utf-8")
(DIST / "demo-bridge.js").write_text(
    "window.__DEMO_CALLS = " + json.dumps(calls, ensure_ascii=False) + ";\n" + bridge, encoding="utf-8")
html = (WEB / "index.html").read_text(encoding="utf-8")
html = html.replace('<script src="app.js"></script>', '<script src="demo-bridge.js"></script>\n<script src="app.js"></script>', 1)
if not (WEB / "german.js").is_file():
    html = re.sub(r'\s*<script src="german.js"></script>', "", html)
assert "demo-bridge.js" in html
(DIST / "index.html").write_text(html, encoding="utf-8")
print("dist:", DIST, sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) // 1024, "KB")
