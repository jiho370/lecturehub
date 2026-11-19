"""AI-003: fake engines still pass through provider consent and the real evidence validator."""
import pytest
from test_note_job import BLOCKS
from test_note_job import library as _library_fixture

from lecturehub import ai
from lecturehub.study import note_job, notes
from lecturehub.transcription.consent import ConsentStore

library = _library_fixture


class FakeEngine:
    name = "codex"
    def __init__(self, blocks): self.blocks, self.calls = blocks, []
    def run_json(self, prompt, schema, **kw):
        self.calls.append(prompt)
        return {"blocks": self.blocks, "model": "synthetic"}


def grant(root, connection):
    info = note_job.consent_info(connection, root, "l2")
    ConsentStore(root / "_lecturehub" / "consent.json").grant(info["provider"], info["course"], 0, "synthetic test")


def test_AI_003_no_external_call_or_job_before_consent(library):
    root, connection = library
    engine = FakeEngine(BLOCKS)
    with pytest.raises(PermissionError): note_job.run(connection, root, "l2", engine=engine)
    assert not engine.calls and not (root / "_lecturehub" / "jobs").exists()


def test_AI_003_codex_null_citations_are_validated_and_recorded(library):
    root, connection = library
    grant(root, connection)
    engine = FakeEngine([{**BLOCKS[2], "cites": [{"t": None, "s": None, "m": "w5", "p": 6}], "ext": None}, BLOCKS[3]])
    result = note_job.run(connection, root, "l2", engine=engine)
    assert (result["state"], result["blocks"], result["rejected"]) == ("done", 1, 1)
    assert notes.load(connection, "l2")["blocks"][0]["cites"][0]["kind"] == "material"
    assert connection.execute("SELECT executor FROM ProcessingJob").fetchone()[0] == "codex_cli"


def test_AI_003_invalid_cites_cannot_replace_existing_note(library):
    root, connection = library
    grant(root, connection)
    note_job.run(connection, root, "l2", engine=FakeEngine(BLOCKS[:2]))
    old = notes.load(connection, "l2")["artifact_id"]
    bad = [{"key": "bad", "kind": "paragraph", "text": "없는 주장", "cites": [{"t": "missing", "s": 9}]}]
    result = note_job.run(connection, root, "l2", engine=FakeEngine(bad))
    assert result["state"] == "waiting_user" and notes.load(connection, "l2")["artifact_id"] == old


def test_AI_003_native_consent_tokens_cannot_be_forged_or_reused(library):
    from lecturehub.ui.app import Api
    from lecturehub.ui.library import Library
    root, _connection = library
    api = Api(object(), Library(root))
    assert not api._authorize_note("l2", "fake")["ok"]
    class Window:
        def create_confirmation_dialog(self, title, message): return True
    api._main = Window()
    info = api.note_check("l2")
    token = api.consent_confirm(info["provider"], info["course"])["token"]
    assert not api._authorize_note("no-such-lecture", token)["ok"]
    assert api._authorize_note("l2", token)["ok"]
    ai.set_choice("claude", "notes")
    assert not api._authorize_note("l2", token)["ok"]
