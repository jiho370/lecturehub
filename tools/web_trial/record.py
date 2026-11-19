"""Record what the real bridge returns for the trial semester (public edition), for the browser-only demo."""
import json
import os
import sys
from datetime import date
from pathlib import Path

OUT = Path(sys.argv[1])
os.environ["LECTUREHUB_EDITION"] = "public"

from lecturehub.ui.app import Api, open_library, open_room  # noqa: E402
from lecturehub.ui.backend import Backend  # noqa: E402

backend = Backend(fake_engine=True)
api = Api(backend, open_library(backend.root), None, open_room(backend))
calls: dict[str, object] = {}


def rec(name, *args):
    try:
        value = getattr(api, name)(*args)
    except Exception as exc:  # noqa: BLE001 - recorded as the app would show it
        value = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    calls[name + json.dumps(list(args), ensure_ascii=False)] = value
    return value


for name in ("init", "state", "appearance", "ai_status", "sidebar", "note_collection", "note_status", "holds",
             "sessions", "notices", "exam_board", "timetable_get", "audio_cleanup_get", "conn_status", "quota",
             "engines", "captions_status", "suggest_course", "recording_permissions", "etl_status", "etl_board",
             "qa_status", "qa_packs", "setup_info"):
    rec(name)
rec("folder_layout_get", "", [])
today = date.today()
rec("calendar", today.year, today.month)
side = calls["sidebar[]"]
lectures = []
for key in ("courses", "items", "lectures"):
    if isinstance(side, dict) and isinstance(side.get(key), list):
        lectures = side[key]
print("sidebar keys:", list(side) if isinstance(side, dict) else type(side))
coll = calls["note_collection[]"]
print("collection:", json.dumps(coll, ensure_ascii=False)[:600])
for course in coll.get("courses", []):
    rec("course_files", course["folder"])
    for lec in course["lectures"]:
        rec("lecture", lec["id"])
        rec("note_check", lec["id"])
        for note in lec["notes"]:
            rec("note_html", note["path"])
for q in ("수두손실", "레이놀즈", "퀴즈", "시험"):
    rec("search", q)
OUT.write_text(json.dumps(calls, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print("recorded", len(calls))
