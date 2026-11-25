"""Record optional public-edition responses (not native dialogs or actions)."""
import json
import sys
from pathlib import Path

from safety import isolate

isolate()
from lecturehub.core import settings  # noqa: E402
from lecturehub.ui.app import Api, open_library, open_room  # noqa: E402
from lecturehub.ui.backend import Backend  # noqa: E402

if not settings.require_root().resolve().is_relative_to(Path(__file__).resolve().parents[2] / "work"):
    raise SystemExit("Only a scratch semester under work/ may be recorded")
backend = Backend(fake_engine=True)
api = Api(backend, open_library(backend.root), None, open_room(backend))
out = {}
for name in ("room", "gsr_rooms_get", "gsr_companions_get"):
    value = getattr(api, name)()
    if isinstance(value, dict) and value.get("ok") is not False:
        out[name + "[]"] = value
Path(sys.argv[1]).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print(f"Recorded {len(out)} optional read responses")
