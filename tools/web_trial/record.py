"""Record real read models on a synthetic semester, never the builder's configuration."""
import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from safety import isolate

isolate()
from lecturehub.core import settings  # noqa: E402
from lecturehub.ui.app import Api, open_library, open_room  # noqa: E402
from lecturehub.ui.backend import Backend  # noqa: E402

root = settings.require_root()
if not root.resolve().is_relative_to(Path(__file__).resolve().parents[2] / "work"):
    raise SystemExit("Only a scratch semester under work/ may be recorded")
backend = Backend(fake_engine=True)
api = Api(backend, open_library(backend.root), None, open_room(backend))
clock = datetime.now().astimezone()
calls = {"__meta": {"recorded_at": clock.isoformat(), "version": "1.1.0"}}
calls["__meta"]["audio_base64"] = base64.b64encode((root / "_lecturehub" / "example_audio.m4a").read_bytes()).decode("ascii")


def rec(name, *args):
    value = getattr(api, name)(*args)
    if name == "lecture" and value.get("ok"):
        media = value["lecture"].get("media")
        if media and media.get("exists") and Path(media["path"]).resolve().is_relative_to(root.resolve()):
            media["url"] = "example-audio.m4a"
    if isinstance(value, dict) and value.get("ok") is False:
        raise RuntimeError(f"{name}: recording refused (fix the synthetic fixture)")
    calls[name + json.dumps(list(args), ensure_ascii=False, separators=(",", ":"))] = value
    return value


for name in ("init", "state", "appearance", "ai_status", "sidebar", "note_collection", "note_status", "holds",
             "sessions", "notices", "exam_board", "timetable_get", "audio_cleanup_get", "conn_status", "quota",
             "engines", "captions_status", "suggest_course", "recording_permissions", "etl_status", "etl_board",
             "setup_info", "folder_layout_get", "week_view", "today_briefing",
             "reminders_get", "stay_get", "consent_list", "ai_link_state", "ai_link_status", "notices_setting_get",
             "etl_notify_get", "live_engine_get", "update_status", "about", "notices_attention", "etl_news_new",
             "import_recent"):
    rec(name)
for offset in range(-8, 9):
    rec("week_view", offset)
for offset in range(-12, 13):
    month = clock.year * 12 + clock.month - 1 + offset
    rec("calendar", month // 12, month % 12 + 1)
coll = calls["note_collection[]"]
for course in coll.get("courses", []):
    rec("course_files", course["folder"])
    for lec in course["lectures"]:
        detail = rec("lecture", lec["id"])
        for recording in detail["lecture"].get("recordings", []):
            rec("recording_engines", recording["path"], detail["lecture"]["course"])
        rec("note_check", lec["id"])
        for note in lec["notes"]:
            rec("note_html", note["path"])
for q in ("유출계수", "도달시간", "단위도", "투수계수", "퀴즈"):
    rec("search", q)
# Mutation calls are audited but played back, never executed while recording.
web = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("src/lecturehub/ui/web")
calls["__meta"]["api_calls"] = sorted(set(re.findall(r"api\.(\w+)", (web / "app.js").read_text(encoding="utf-8-sig"))))
Path(sys.argv[1]).write_text(json.dumps(calls, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print(f"Recorded {len(calls) - 1} successful read responses; {len(calls['__meta']['api_calls'])} API names audited")
