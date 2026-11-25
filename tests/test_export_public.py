"""Release regression: retain synthetic parser evidence while sanitizing exports."""
import codecs
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
EXPORT_TOOL = REPO / "tools/export_public.py"
export = None
if EXPORT_TOOL.is_file():  # intentionally absent from the public tree
    spec = importlib.util.spec_from_file_location("export_public", EXPORT_TOOL)
    export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)
needs_export = pytest.mark.skipif(export is None, reason="owner-only export tool is absent")


@pytest.mark.parametrize("raw", [
    codecs.BOM_UTF8 + "합성 전사\r\n00:01\r\n합성 문장\r\n".encode(),
    "합성 문서\n다음 줄\n".encode(),
    codecs.BOM_UTF16_LE + "합성 전사\r\n".encode("utf-16-le"),
])
@needs_export
def test_PARSE_001_export_preserves_encoding_and_line_endings(tmp_path, monkeypatch, raw):
    source = tmp_path / "source"
    rel = Path("tests/fixtures/transcripts/synthetic.txt")
    (source / rel).parent.mkdir(parents=True)
    (source / rel).write_bytes(raw)
    monkeypatch.setattr(export, "REPO", source)
    monkeypatch.setattr(export, "tracked", lambda paths: [rel])
    out = tmp_path / "public"
    export.copy_tree(out)
    assert (out / rel).read_bytes() == raw


@pytest.mark.parametrize("raw", [codecs.BOM_UTF8 + b"00:01\r\nsynthetic\r\n", b"synthetic\r\n", codecs.BOM_UTF16_LE + "합성\r\n".encode("utf-16-le")])
@needs_export
def test_PARSE_001_public_git_keeps_fixture_bytes_under_autocrlf(tmp_path, raw):
    import hashlib
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    attrs = Path(__file__).parents[1] / "tools/public_docs/.gitattributes"
    (tmp_path / ".gitattributes").write_bytes(attrs.read_bytes())
    rel = "tests/fixtures/transcripts/synthetic.txt"
    output = subprocess.run(["git", "-c", "core.autocrlf=true", "hash-object", f"--path={rel}", "--stdin"],
                            cwd=tmp_path, input=raw, capture_output=True, check=True)
    expected = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert output.stdout.decode().strip() == expected


@needs_export
def test_PUB_scan_covers_sql_spec_and_private_terms(tmp_path):
    assert {".sql", ".spec"} <= export.TEXT
    private = tmp_path / "terms.txt"
    private.write_text("# synthetic list\n합성비밀어\n\nSyntheticName\n", encoding="utf-8")
    table = tmp_path / "시간표.csv"
    table.write_text("과목,폴더\n합성과목명,합성폴더명\n", encoding="utf-8")
    terms = export.course_terms([private, table])
    assert {"합성비밀어", "SyntheticName", "합성과목명", "합성폴더명"} <= set(terms) and "# synthetic list" not in terms
    out = tmp_path / "out"
    (out / "packaging").mkdir(parents=True)
    (out / "packaging" / "app.spec").write_text("name = 'SyntheticName'\n", encoding="utf-8")
    (out / "schema.sql").write_text("-- 합성과목명\n", encoding="utf-8")
    hits = export.scan(out, terms)
    assert any(h.startswith("packaging/app.spec") for h in hits) and any(h.startswith("schema.sql") for h in hits)


@needs_export
def test_PUB_scan_flags_a_generic_course_only_next_to_a_date(tmp_path):
    """A generic subject word may stay alone, but a dated file name built from the timetable names a real lecture."""
    generic = sorted(export.GENERIC)[0]
    table = tmp_path / "시간표.csv"
    table.write_text(f"과목,폴더\n{generic},{generic}\n", encoding="utf-8")
    assert export.dated_courses([table]) == [generic] and generic not in export.course_terms([table])
    out = tmp_path / "out"
    out.mkdir()
    (out / "alone.py").write_text(f"# {generic} example\n", encoding="utf-8")
    (out / "dated.py").write_text(f'"""``26.10.16 {generic} (MAI).txt``"""\n', encoding="utf-8")
    hits = export.scan(out, export.course_terms([table]), export.dated_courses([table]))
    assert [h.split(":")[0] for h in hits] == ["dated.py"]


def _build_app():
    spec = importlib.util.spec_from_file_location("build_app", Path(__file__).parents[1] / "tools/build_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_export
def test_PUB_build_passes_private_terms_and_warns_without_them(tmp_path, monkeypatch, capsys):
    build = _build_app()
    monkeypatch.setenv("LECTUREHUB_SETTINGS", str(tmp_path / "settings.json"))  # no configured semester
    monkeypatch.delenv("LECTUREHUB_ROOT", raising=False)
    monkeypatch.delenv("LECTUREHUB_PRIVATE_TERMS", raising=False)
    assert build.private_term_sources() == []
    assert "WARNING" in capsys.readouterr().err
    private = tmp_path / "terms.txt"
    private.write_text("합성비밀어\n", encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_PRIVATE_TERMS", str(private))
    root = tmp_path / "학기"
    root.mkdir()
    (root / "시간표.csv").write_text("과목,폴더\n합성과목명,합성과목명\n", encoding="utf-8")
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    assert build.private_term_sources() == [private.resolve(), root / "시간표.csv"]
    (root / "_lecturehub").mkdir()
    (root / "_lecturehub" / "private_terms.txt").write_text("합성비밀어2\n", encoding="utf-8")
    assert build.private_term_sources()[-1] == root / "_lecturehub" / "private_terms.txt"
    monkeypatch.setenv("LECTUREHUB_PRIVATE_TERMS", str(build.REPO / "terms.txt"))
    with pytest.raises(SystemExit):
        build.private_term_sources()

@needs_export
def test_public_export_keeps_the_same_app_icon(tmp_path, monkeypatch):
    repo = Path(__file__).parents[1]
    rels = [Path("src/lecturehub/ui/assets/app.png"), Path("src/lecturehub/ui/assets/app.ico")]
    monkeypatch.setattr(export, "tracked", lambda paths: rels)
    out = tmp_path / "public"
    export.copy_tree(out)
    for rel in rels:
        assert (out / rel).read_bytes() == (repo / rel).read_bytes()
    assert not list((out / "src/lecturehub/ui/assets").glob("app_public.*"))


@needs_export
def test_owner_login_is_refused_except_as_the_public_repository_address():
    """core/update.py names the public repository so the app can ask it for the latest version. That one address
    is public by definition; the login anywhere else (the private development repository, a profile link) is not.
    (The login is put together here so that this file itself passes the scan.)"""
    import re

    owner = "jiho" + "370"
    pattern = re.compile(export.PATTERNS["owner github"])
    assert not pattern.search(f'REPO = "{owner}/lecturehub"')
    assert not pattern.search(f"github.com/{owner}/lecturehub/releases")
    assert not pattern.search(f"https://{owner}.github.io/lecture-hub-demo/")  # the public look-around page (README)
    for leak in (owner, f"github.com/{owner}", f"{owner}/lecture-hub", f"{owner}/lecturehub-dev", f"{owner}/lecturehubx",
                 f"{owner}.github.io", f"{owner}.github.io/lecture-hub-demo-old", f"{owner}.github.io/notes"):
        assert pattern.search(leak), leak


@pytest.fixture(scope="module")
def public_tree(tmp_path_factory):
    if export is None:
        return REPO
    out = tmp_path_factory.mktemp("공개판 (D-087)")
    export.copy_tree(out)
    export.add_public_files(out)
    return out


def test_PUBLIC_GSR_005_export_drops_package_assets_and_owner_tests(public_tree):
    assert not (public_tree / "src/lecturehub/booking").exists()
    assert not list((public_tree / "tests").glob("test_booking*.py"))
    assert (public_tree / "tests/test_first_run_setup.py").is_file()
    assert "lecturehub.booking" not in (public_tree / "tests/test_first_run_setup.py").read_text(encoding="utf-8")
    assert (public_tree / "src/lecturehub/core/aside_cli.py").is_file()  # Clova still needs it


def test_PUBLIC_GSR_006_export_page_and_documents_have_no_room_strings(public_tree):
    import re
    import shutil
    import subprocess

    page = public_tree / "src/lecturehub/ui/web"
    paths = list(page.rglob("*")) + list((public_tree / "docs").rglob("*.md"))
    paths += list(public_tree.glob("*.md"))
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {".html", ".js", ".css", ".md"}:
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if path.name == "CHANGELOG.md" and "더 이상 포함되지 않습니다" in line:
                continue
            assert not re.search("gsr|그스룸|그룹스터디룸", line, re.I), str(path.relative_to(public_tree))
    html = (page / "index.html").read_text(encoding="utf-8")
    js = (page / "app.js").read_text(encoding="utf-8")
    assert "roomBtn" not in html and "openRoom" not in js and "api.room" not in js
    if shutil.which("node"):
        subprocess.run(["node", "--check", str(page / "app.js")], check=True, capture_output=True)


@needs_export
def test_PUBLIC_GSR_006_scan_rejects_room_ui_or_docs_but_allows_removal_line(tmp_path):
    (tmp_path / "README.md").write_text("그룹스터디룸 예약", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("그룹스터디룸 예약은 공개판에 더 이상 포함되지 않습니다.", encoding="utf-8")
    hits = export.scan(tmp_path, [])
    assert len(hits) == 1 and hits[0].startswith("README.md:")


@needs_export
def test_PUBLIC_GSR_005_nested_booking_files_are_dropped():
    for name in ("src/lecturehub/booking/__init__.py", "src/lecturehub/booking/assets/gsr_core.js",
                 "src/lecturehub/booking/assets/nested/example.json", "tests/test_booking.py",
                 "tests/test_booking_public.py"):
        assert export.dropped(Path(name)), name


@pytest.mark.skipif(export is not None, reason="run this check in the actual public export")
def test_PUBLIC_GSR_007_public_selfcheck_imports_every_module():
    import sys
    from lecturehub.core import selfcheck

    assert selfcheck._all_modules()
    assert not any(name.startswith("lecturehub.booking") for name in sys.modules)
