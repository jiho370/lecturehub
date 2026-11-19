"""Second pass: shapes of the room (GSR), connections and eTL calls on the trial semester (public edition)."""
import inspect
import json
import os
import sys
from pathlib import Path

os.environ["LECTUREHUB_EDITION"] = "public"
from lecturehub.ui.app import Api, open_library, open_room  # noqa: E402
from lecturehub.ui.backend import Backend  # noqa: E402

backend = Backend(fake_engine=True)
room = open_room(backend)
api = Api(backend, open_library(backend.root), None, room)
print("room object:", type(room).__name__ if room else None)
for name in sorted(dir(api)):
    if name.startswith(("room", "gsr", "conn", "etl", "clova", "calendar", "join", "import")):
        print(name, inspect.signature(getattr(api, name)))
out = {}
for name in ("room", "room_rules", "gsr_rooms_get", "gsr_companions_get", "conn_status", "etl_status", "etl_board"):
    try:
        out[name] = getattr(api, name)()
    except Exception as exc:  # noqa: BLE001
        out[name] = f"{type(exc).__name__}: {exc}"
Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
for k, v in out.items():
    print(k, "=>", json.dumps(v, ensure_ascii=False, default=str)[:700])
