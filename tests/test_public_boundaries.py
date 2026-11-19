"""EDITION-002: excluded features refuse direct calls before touching absent modules, DB or tools."""
import pytest

from lecturehub.ui.app import Api
from lecturehub.ui.backend import Backend
from lecturehub.ui.library import Library


@pytest.fixture(autouse=True)
def public(monkeypatch):
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")


@pytest.mark.parametrize("method,args", [
    ("qa_status", ()), ("qa_check", ()), ("qa_start", ()), ("qa_packs", ()), ("qa_ask", ()), ("qa_stop", ()),
    ("assignment_prepare", ("synthetic",)), ("assignment_consent", ("synthetic",)),
    ("assignment_open", ("synthetic",)), ("assignment_claude", ("synthetic",)),
    ("memorizer_status", ()), ("memorizer_config", ()), ("memorizer_open", ()), ("mai_prepare", ("synthetic",))])
def test_EDITION_002_api_refuses_excluded_features_before_delegate(method, args):
    api = Api(object())
    result = getattr(api, method)(*args)
    assert result["ok"] is False and result["state"] == "disabled"


@pytest.mark.parametrize("method,args", [
    ("qa_status", ()), ("qa_check", ("synthetic",)), ("qa_start", ("synthetic", "synthetic")),
    ("qa_packs", ()), ("qa_ask", ()), ("qa_stop", ()), ("mai_prepare", ("synthetic",)),
    ("_mai_start", ("synthetic", "synthetic", []))])
def test_EDITION_002_backend_refuses_without_database_or_tools(method, args):
    backend = object.__new__(Backend)
    assert getattr(backend, method)(*args)["ok"] is False


@pytest.mark.parametrize("method", ["assignment_prepare", "assignment_consent", "assignment_open", "assignment_claude"])
def test_EDITION_002_library_refuses_without_opening_database(method):
    library = object.__new__(Library)
    assert getattr(library, method)("synthetic")["ok"] is False


def test_EDITION_002_absent_module_helpers_and_native_consent_are_gated():
    api = Api(object())
    for provider in ("claude_assignment", "claude_qa", "mai_playground"):
        assert api.consent_confirm(provider, "synthetic")["ok"] is False
    with pytest.raises(ValueError): api._memorizer()
    with pytest.raises(ValueError): object.__new__(Library).assignment_notice()
