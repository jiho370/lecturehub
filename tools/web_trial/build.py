"""Build the PUBLIC UI with offline playback, sanitized recordings and a fail-closed leak check."""
import json
import base64
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
ABSOLUTE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]+|(?<![\w\\])\\\\[A-Za-z0-9_-]+\\|/(?:Users|home|tmp|mnt|var)/", re.I)
SECRET = re.compile(r"(?:sk-(?:proj-)?[\w-]{16,}|gh[pousr]_[\w]{20,}|github_pat_[\w]{20,}|AIza[\w-]{25,}|"
                    r"ya29\.[\w.-]{20,}|eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY)")


def build(web: Path, work: Path):
    calls = json.loads((work / "calls.json").read_text(encoding="utf-8-sig"))
    calls.update(json.loads((work / "calls2.json").read_text(encoding="utf-8-sig")))
    audio = base64.b64decode(calls["__meta"].pop("audio_base64"))
    calls["__meta"]["recorded_epoch_ms"] = int(datetime.fromisoformat(calls["__meta"]["recorded_at"]).timestamp() * 1000)
    # Replace the synthetic semester prefix first, preserving relative file identities used as bridge arguments.
    root = calls["init[]"]["out_dir"].rsplit("_lecturehub", 1)[0].rstrip("\\/")
    # All other absolute paths (executable detection, settings snippets) become display-only examples.
    def clean(value):
        if isinstance(value, str):
            if root:
                value = value.replace(root, "예시 학기").replace(root.replace("\\", "/"), "예시 학기")
            # Older example directories can also occur as a basename in layout previews.
            value = value.replace("체험 학기", "예시 학기")
            value = re.sub(r"(?<![A-Za-z])[A-Za-z]:[\\/]+[^\n\r\"<>|]*", "앱/파일", value)
            value = EMAIL.sub("문의 주소", value)
            value = re.sub(r"(\d{4}-\d\d-\d\dT\d\d:\d\d[^\s\"]*)\+09:00", r"\1", value)
            return value
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {clean(k): clean(v) for k, v in value.items()}
        return value
    calls = clean(calls)
    dist = (work / "dist").resolve()
    if dist.parent != work.resolve() or dist.name != "dist":
        raise ValueError("Unsafe build destination")
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    (dist / "example-audio.m4a").write_bytes(audio)
    for name in ("app.js", "app.css", "german.js"):
        if (web / name).is_file():
            shutil.copy2(web / name, dist / name)
    shutil.copytree(web / "fonts", dist / "fonts")
    bridge = (HERE / "bridge.js").read_text(encoding="utf-8-sig")
    (dist / "demo-bridge.js").write_text("window.__DEMO_CALLS = " + json.dumps(calls, ensure_ascii=False) + ";\n" + bridge,
                                         encoding="utf-8")
    html = (web / "index.html").read_text(encoding="utf-8-sig")
    html = html.replace('<script src="app.js"></script>', '<script src="demo-bridge.js"></script>\n<script src="app.js"></script>', 1)
    if not (web / "german.js").is_file():
        html = re.sub(r'\s*<script src="german.js"></script>', "", html)
    # Tighten the app's existing policy; even an unhandled action cannot transmit anything.
    html = re.sub(r"connect-src[^;\"]*", "connect-src 'none'", html)
    html = html.replace(" file:", "").replace(" http://127.0.0.1:*", "")
    html = html.replace("<head>", '<head>\n<link rel="icon" href="data:,">', 1)
    assert "demo-bridge.js" in html and "connect-src 'none'" in html
    (dist / "index.html").write_text(html, encoding="utf-8")
    # Scan EVERY shipped text file, not only the JSON; never print a matching secret.
    username = os.environ.get("USERNAME", "")
    for path in dist.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".css", ".js", ".json", ".svg", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8-sig")
        # The public UI may contain the developer's dated note; omit it only in the built copy.
        text = re.sub(r"\s*\(user 2026-10-01: [^)]*\)", "", text)
        for label, pattern in (("absolute path", ABSOLUTE), ("email", EMAIL), ("credential", SECRET)):
            if pattern.search(text):
                raise ValueError(f"{label} leak in {path.name}; build rejected")
        if username and re.search(r"(?<![\w])" + re.escape(username) + r"(?![\w])", text, re.I):
            raise ValueError(f"builder username leak in {path.name}; build rejected")
        if "체험" in text:
            raise ValueError(f"Retired wording in {path.name}; build rejected")
        path.write_text(text, encoding="utf-8")
    print(f"dist: {dist} ({sum(p.stat().st_size for p in dist.rglob('*') if p.is_file()):,} bytes); leak check passed")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    build(Path(sys.argv[1]), Path(sys.argv[2]))
