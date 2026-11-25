"""Run the same five-width QA matrix with at most three headless workers; never opens native UI."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "work" / "trial-qa"
WIDTHS = [1440, 1100, 900, 600, 390]


def run(width):
    with (OUT / f"qa-{width}.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, str(HERE / "qa.py"), str(width), "--isolated"], cwd=ROOT,
                                stdout=log, stderr=subprocess.STDOUT, timeout=300,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
    if result.returncode:
        raise RuntimeError(f"QA failed at {width}px; see work/trial-qa/qa-{width}.log")
    return json.loads((OUT / f"results-{width}.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = [row for pair in pool.map(run, WIDTHS) for row in pair]
    rows.sort(key=lambda r: (-r["width"], r["theme"] != "light"))
    (OUT / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for width in WIDTHS:
        pair = [r for r in rows if r["width"] == width]
        print(json.dumps({"width": width, "contexts": len(pair), "calls": sum(r["calls"] for r in pair),
                          **{k: sum(len(r[k]) for r in pair) for k in
                             ("console_errors", "unanswered", "bridge_failures", "external_requests", "native_dialogs",
                              "overflowing_elements", "retired_word_occurrences", "unexpected_answers")}}, ensure_ascii=False))
