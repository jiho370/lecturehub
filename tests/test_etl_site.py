"""Another school's Canvas as "eTL" (user decision 2026-11-20): address checks, the token stays bound to its site,
and courses named another school's way still find their timetable folder."""
import sys
import types

import pytest

from lecturehub.core import connections, etl_site
from lecturehub.index import etl_sync


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch, tmp_path):
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "settings.json"))
    store = {}
    module = types.ModuleType("keyring")
    module.get_password = lambda service, name: store.get((service, name))
    module.set_password = lambda service, name, value: store.__setitem__((service, name), value)
    module.delete_password = lambda service, name: store.pop((service, name), None)
    module.errors = types.SimpleNamespace(PasswordDeleteError=KeyError)
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.delenv("LECTUREHUB_ETL_TOKEN", raising=False)
    return store


def test_snu_etl_is_the_default():
    assert etl_site.base() == "https://myetl.snu.ac.kr" == etl_sync.BASE and etl_site.is_default()
    assert etl_site.name() == "eTL"


@pytest.mark.parametrize("typed, kept", [
    ("canvas.example.ac.kr", "https://canvas.example.ac.kr"),
    ("https://Canvas.Example.ac.kr/courses/12", "https://canvas.example.ac.kr"),
    ("https://canvas.example.ac.kr:443/", "https://canvas.example.ac.kr"),
    ("https://canvas.example.ac.kr:8443", "https://canvas.example.ac.kr:8443"),
])
def test_addresses_are_reduced_to_an_https_site(typed, kept):
    assert etl_site.clean(typed) == kept


@pytest.mark.parametrize("typed", ["", "http://canvas.example.ac.kr", "https://user:pw@canvas.example.ac.kr",
                                   "localhost", "https://canvas.example.ac.kr:99999", "ftp://canvas.example.ac.kr"])
def test_odd_addresses_are_refused(typed):
    with pytest.raises(ValueError):
        etl_site.clean(typed)


def test_a_bad_hand_edited_value_falls_back_to_snu():
    from lecturehub.core import settings

    settings.save({"etl_base": "http://evil.example"})
    assert etl_site.base() == etl_site.DEFAULT


def test_the_token_is_not_used_for_a_site_it_was_not_made_for(fake_keyring):
    connections.save("etl", "t-snu")  # saved while the site is SNU's eTL
    assert etl_sync.load_token() == "t-snu" and connections.key_for("etl") == "t-snu"
    moved = etl_site.set_base("canvas.example.ac.kr")
    assert moved == {"base": "https://canvas.example.ac.kr", "default": False, "token_fits": False}
    assert etl_sync.load_token() is None and connections.key_for("etl") is None  # never sent to the new host
    assert not {r["id"]: r for r in connections.status()}["etl"]["connected"]
    etl_sync.save_token("t-other")
    assert etl_sync.load_token() == "t-other" and etl_site.token_fits()
    back = etl_site.set_base("")
    assert back["default"] and not back["token_fits"] and etl_sync.load_token() is None


def test_tokens_saved_before_the_change_belong_to_snu(fake_keyring):
    fake_keyring[("lecturehub", "etl-canvas")] = "t-old"  # no etl_token_host in settings yet
    assert etl_sync.load_token() == "t-old"


def test_the_connection_row_points_at_the_school_site():
    etl_site.set_base("canvas.example.ac.kr")
    meta = connections.meta_for("etl")
    assert meta["test_url"] == "https://canvas.example.ac.kr/api/v1/users/self"
    assert meta["help_url"].startswith("https://canvas.example.ac.kr/") and "canvas.example.ac.kr" in meta["label"]
    row = {r["id"]: r for r in connections.status()}["etl"]
    assert row["site"] == {"base": "https://canvas.example.ac.kr", "default": False}
    seen = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b'{"name": "x"}'

    def opener(request, timeout=0):
        seen.append(request.full_url)
        return Reply()

    assert connections.test("etl", "k", opener=opener)["ok"]
    assert seen == ["https://canvas.example.ac.kr/api/v1/users/self"]
    assert connections.meta_for("gemini") is connections.PROVIDERS["gemini"]


def test_the_token_goes_only_to_the_chosen_site():
    etl_site.set_base("canvas.example.ac.kr")
    assert etl_sync._same_host("https://canvas.example.ac.kr/api/v1/courses")
    assert not etl_sync._same_host("https://myetl.snu.ac.kr/api/v1/courses")
    assert not etl_sync._same_host("https://canvas.example.ac.kr.evil.example/api")


def test_other_schools_course_names_find_the_timetable_subject():
    subjects = {"도시계획론": "도시계획", "합성역학": "역학", "합성역학실험": "역학실험"}
    assert etl_sync.match_subject("도시계획론", subjects) == "도시계획"
    assert etl_sync.match_subject("[2026-2] 도시 계획론-01", subjects) == "도시계획"
    assert etl_sync.match_subject("합성역학실험 (Mechanics Lab)", subjects) == "역학실험"  # the longest subject wins
    assert etl_sync.match_subject("Calculus", subjects) is None


def test_assignments_from_another_school(tmp_path):
    root = tmp_path / "26-2"
    root.mkdir()
    (root / "시간표.csv").write_text("과목,폴더\n도시계획론,도시계획\n", encoding="utf-8")
    etl_site.set_base("canvas.example.ac.kr")
    site = "https://canvas.example.ac.kr"
    pages = {
        f"{site}/api/v1/courses?enrollment_state=active&per_page=100": [{"id": 7, "name": "[26-2] 도시계획론 01분반"},
                                                                         {"id": 8, "name": "Chemistry"}],
        f"{site}/api/v1/courses/7/assignments?per_page=100&order_by=due_at": [
            {"id": 1, "name": "과제 1", "due_at": "2026-11-30T14:59:00Z", "html_url": f"{site}/courses/7/assignments/1"}],
    }
    found = etl_sync.fetch_assignments(root, "t", fetch=lambda url, token: (pages[url], None))
    assert found["courses"] == 1 and found["unmatched"] == ["Chemistry"]
    assert found["items"] == [{"id": "etl:1", "course": "도시계획", "title": "과제 1", "due": "2026-11-30T23:59",
                               "url": f"{site}/courses/7/assignments/1"}]
