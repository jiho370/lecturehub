"""D-078 (MAT-HWP-*): text of .hwp / .hwpx / .docx into MaterialPage. Every file is built here from synthetic text;
no real course file is read. The .hwp files are minimal OLE compound files written by ``build_cfb`` below."""
from __future__ import annotations

import hashlib
import itertools
import json
import struct
import zipfile
import zlib
from contextlib import closing

import pytest

from lecturehub.core import db
from lecturehub.materials import cfb, doctext, units
from lecturehub.materials import extract as ex

END, FREE, NOSTREAM, FATSECT = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFD
SECTOR = 512


# --- builders ----------------------------------------------------------------------------------------------------
def build_cfb(streams: dict[str, bytes]) -> bytes:
    """A version 3 compound file holding ``streams`` ("FileHeader", "BodyText/Section0", ...). Streams under 4096
    bytes go to the mini stream, longer ones to their own sectors; siblings are chained through the right pointer."""
    tops: dict = {}
    for path, data in streams.items():
        parts = path.split("/")
        if len(parts) == 1:
            tops[parts[0]] = data
        else:
            tops.setdefault(parts[0], {})[parts[1]] = data
    entries: list[dict] = []

    def add(name, kind, data=None):
        entries.append({"name": name, "kind": kind, "data": data, "left": NOSTREAM, "right": NOSTREAM,
                        "child": NOSTREAM, "start": END, "size": 0})
        return len(entries) - 1

    def link(parent, items):
        ids = []
        for name, value in items.items():
            if isinstance(value, dict):
                index = add(name, 1)
                link(index, value)
            else:
                index = add(name, 2, value)
            ids.append(index)
        for a, b in itertools.pairwise(ids):
            entries[a]["right"] = b
        if ids:
            entries[parent]["child"] = ids[0]

    root = add("Root Entry", 5)
    link(root, tops)
    mini, minifat, big = bytearray(), [], []
    for entry in entries:
        if entry["kind"] != 2:
            continue
        data = entry["data"]
        entry["size"] = len(data)
        if not data:
            continue
        if len(data) < 4096:
            first, count = len(mini) // 64, -(-len(data) // 64)
            entry["start"] = first
            minifat += [first + k + 1 for k in range(count - 1)] + [END]
            mini += data + bytes(count * 64 - len(data))
        else:
            big.append(entry)

    def sectors(n):
        return -(-n // SECTOR)

    dir_n, minifat_n, mini_n = sectors(len(entries) * 128), sectors(len(minifat) * 4), sectors(len(mini))
    big_n = [sectors(len(e["data"])) for e in big]
    others = dir_n + minifat_n + mini_n + sum(big_n)
    fat_n = 1
    while fat_n * (SECTOR // 4) < others + fat_n:
        fat_n += 1
    fat: list[int] = [FATSECT] * fat_n

    def chain(n):
        start = len(fat)
        fat.extend(start + k + 1 if k < n - 1 else END for k in range(n))
        return start if n else END

    dir_start, minifat_start, mini_start = chain(dir_n), chain(minifat_n), chain(mini_n)
    for entry, n in zip(big, big_n):
        entry["start"] = chain(n)
    entries[root]["start"], entries[root]["size"] = (mini_start, len(mini)) if mini else (END, 0)
    fat += [FREE] * (fat_n * SECTOR // 4 - len(fat))
    header = bytearray(512)
    header[:8] = cfb.SIGNATURE
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<9I", header, 0x28, 0, fat_n, dir_start, 0, 4096, minifat_start, minifat_n, END, 0)
    struct.pack_into("<109I", header, 0x4C, *(list(range(fat_n)) + [FREE] * (109 - fat_n)))

    def padded(data, fill=b"\0"):
        return data + fill * ((-len(data)) % SECTOR)

    directory = bytearray()
    for entry in entries:
        name = entry["name"].encode("utf-16-le") + b"\0\0"
        directory += struct.pack("<64sHBBIII16sIQQIQ", name, len(name), entry["kind"], 1, entry["left"],
                                 entry["right"], entry["child"], b"", 0, 0, 0, entry["start"], entry["size"])
    out = bytes(header) + struct.pack(f"<{len(fat)}I", *fat) + padded(bytes(directory))
    out += padded(struct.pack(f"<{len(minifat)}I", *minifat), b"\xff") if minifat else b""
    out += padded(bytes(mini))
    for entry in big:
        out += padded(entry["data"])
    return out


def record(tag: int, payload: bytes, level: int = 0) -> bytes:
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), len(payload)) + payload
    return struct.pack("<I", tag | (level << 10) | (len(payload) << 20)) + payload


def para(text: str, level: int = 0, controls: bytes = b"") -> bytes:
    """PARA_HEADER (ignored by the reader) and PARA_TEXT ending in a paragraph break (13)."""
    body = controls + text.encode("utf-16-le") + struct.pack("<H", 13)
    return record(66, b"\0" * 22, level) + record(67, body, level + 1)


def wide(code: int) -> bytes:
    """An inline/extended control: eight code units, the code first and last."""
    return struct.pack("<8H", code, 0x6C74, 0x6274, 0, 0, 0, 0, code)  # 'tbl' id inside, as a table control has


def file_header(flags: int) -> bytes:
    return (b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05010100, flags)).ljust(256, b"\0")


def hwp(path, sections: list[bytes], *, compressed=True, flags=0):
    streams = {"FileHeader": file_header(flags | (1 if compressed else 0))}
    for index, body in enumerate(sections):
        if compressed:
            packer = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = packer.compress(body) + packer.flush()
        streams[f"BodyText/Section{index}"] = body
    streams["DocInfo"] = b"\0" * 16
    path.write_bytes(build_cfb(streams))
    return path


SAMPLE_SECTION = (para("합성 첫 문단 관수로")
                  + para("", controls=wide(11))  # the paragraph that holds the table control
                  + para("칸 가", level=2) + para("칸 나", level=2)
                  + record(66, b"\0" * 22) + record(67, "탭".encode("utf-16-le") + wide(9) + "뒤".encode("utf-16-le")
                                                   + struct.pack("<H", 13), 1)
                  + para("줄바꿈 앞" + "\n" + "줄바꿈 뒤"))

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def hwpx(path, sections: list[str], extra: dict | None = None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", "<opf:package xmlns:opf='http://www.idpf.org/2007/opf/'/>")
        for index, body in enumerate(sections):
            archive.writestr(f"Contents/section{index}.xml",
                             f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">'
                             f"{body}</hs:sec>")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


def hp_p(*runs: str) -> str:
    return "<hp:p>" + "".join(f"<hp:run>{r}</hp:run>" for r in runs) + "</hp:p>"


def hp_table(rows: list[list[str]]) -> str:
    cells = "".join("<hp:tr>" + "".join(f"<hp:tc><hp:subList>{hp_p(f'<hp:t>{c}</hp:t>')}</hp:subList></hp:tc>"
                                        for c in row) + "</hp:tr>" for row in rows)
    return f"<hp:tbl><hp:sz/>{cells}</hp:tbl>"


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path, body: str):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml",
                         f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}'
                         "</w:body></w:document>")
    return path


def w_p(*texts: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t>{t}</w:t></w:r>" for t in texts) + "</w:p>"


# --- readers -----------------------------------------------------------------------------------------------------
def test_MAT_HWP_001_hwp_text_in_order_with_table_cells_compressed_and_not(tmp_path):
    for compressed in (True, False):
        path = hwp(tmp_path / f"합성 공지 ({compressed}).hwp", [SAMPLE_SECTION, para("둘째 구역 문단")],
                   compressed=compressed)
        sections = doctext.hwp_sections(path)
        assert [[line for line in s if line] for s in sections] == [
            ["합성 첫 문단 관수로", "칸 가", "칸 나", "탭\t뒤", "줄바꿈 앞\n줄바꿈 뒤"], ["둘째 구역 문단"]]


def test_MAT_HWP_002_controls_are_skipped_and_tab_kept():
    text = doctext._para_text(wide(11) + "앞".encode("utf-16-le") + wide(9) + "뒤".encode("utf-16-le")
                              + struct.pack("<H", 24) + "끝".encode("utf-16-le") + struct.pack("<H", 13))
    assert text == "앞\t뒤-끝"


def test_MAT_HWP_003_big_stream_outside_the_mini_stream(tmp_path):
    long_text = "가나다라마바사 " * 400  # > 4096 bytes uncompressed: its own sectors in the main FAT
    path = hwp(tmp_path / "긴 합성.hwp", [para(long_text)], compressed=False)
    assert doctext.hwp_sections(path) == [[long_text]]


@pytest.mark.parametrize("flag,word", [(0x2, "암호"), (0x4, "배포용"), (0x10, "보안")])
def test_MAT_HWP_004_encrypted_or_protected_is_given_up_with_reason(tmp_path, flag, word):
    path = hwp(tmp_path / "잠긴 합성.hwp", [SAMPLE_SECTION], flags=flag)
    with pytest.raises(ex.NotReadable, match=word):
        doctext.hwp_sections(path)


def _variants(good: bytes):
    yield b""
    yield b"HWP synthetic"
    yield cfb.SIGNATURE + b"\0" * 100
    yield good[:600]
    yield good[:len(good) // 2]
    broken = bytearray(good)
    broken[0x30:0x34] = struct.pack("<I", 0x7FFFFFF0)  # directory start past the end
    yield bytes(broken)
    looped = bytearray(good)
    looped[512:516] = struct.pack("<I", 0)  # FAT entry 0 points at itself
    looped[516:520] = struct.pack("<I", 1)
    yield bytes(looped)
    for offset in range(512, min(len(good), 4096), 37):
        mangled = bytearray(good)
        mangled[offset:offset + 4] = b"\xff\xfe\x00\x7f"
        yield bytes(mangled)


def test_MAT_HWP_005_truncated_or_garbage_files_never_raise_anything_else(tmp_path):
    good = hwp(tmp_path / "원본.hwp", [SAMPLE_SECTION]).read_bytes()
    target = tmp_path / "망가진 합성.hwp"
    outcomes = set()
    for data in _variants(good):
        target.write_bytes(data)
        try:
            doctext.hwp_sections(target)
            outcomes.add("read")
        except (ex.NotReadable, ex.TooLarge):
            outcomes.add("given up")
    assert "given up" in outcomes


def test_MAT_HWP_006_inflated_body_is_bounded(tmp_path, monkeypatch):
    path = hwp(tmp_path / "합성 폭탄.hwp", [para("가" * 3000)])
    monkeypatch.setattr(doctext, "MAX_BODY_BYTES", 1000)
    with pytest.raises(ex.TooLarge):
        doctext.hwp_sections(path)


def test_MAT_HWP_007_hwpx_paragraphs_and_tables_in_order(tmp_path):
    path = hwpx(tmp_path / "합성 안내 (hwpx).hwpx", [
        hp_p("<hp:t>첫 문단 <hp:tab/>탭 뒤</hp:t>") + hp_p("<hp:t>표 앞</hp:t>" + hp_table([["가", "나"], ["다", "라"]]))
        + hp_p("<hp:t>표 뒤</hp:t>"),
        hp_p("<hp:t>둘째 구역</hp:t>")])
    assert [[line for line in s if line] for s in doctext.hwpx_sections(path)] == [
        ["첫 문단 \t탭 뒤", "표 앞", "가 | 나", "다 | 라", "표 뒤"], ["둘째 구역"]]


def test_MAT_HWP_008_zip_bomb_guard_for_hwpx_and_docx(tmp_path, monkeypatch):
    path = hwpx(tmp_path / "합성.hwpx", [hp_p("<hp:t>" + "가" * 5000 + "</hp:t>")])
    word = docx(tmp_path / "합성.docx", w_p("나" * 5000))
    monkeypatch.setattr(ex, "MAX_UNZIPPED_BYTES", 2000)
    for target in (path, word):
        with pytest.raises(ex.TooLarge):
            ex.page_texts(target, tmp_path)
    monkeypatch.setattr(ex, "MAX_UNZIPPED_BYTES", 10**9)
    monkeypatch.setattr(ex, "MAX_ZIP_ENTRIES", 2)
    with pytest.raises(ex.TooLarge):
        ex.page_texts(path, tmp_path)


def test_MAT_HWP_009_xml_with_entities_is_not_parsed(tmp_path):
    path = hwpx(tmp_path / "엔티티.hwpx", [])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Contents/section0.xml", '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>')
    with pytest.raises(ex.NotReadable):
        doctext.hwpx_sections(path)


def test_MAT_HWP_010_docx_runs_tabs_and_tables(tmp_path):
    body = (w_p("워드 ", "첫 문단") + "<w:p><w:r><w:t>앞</w:t><w:tab/><w:t>뒤</w:t><w:br/><w:t>다음 줄</w:t></w:r></w:p>"
            + "<w:tbl><w:tr><w:tc>" + w_p("칸1") + "</w:tc><w:tc>" + w_p("칸2") + "</w:tc></w:tr></w:tbl>"
            + "<w:p><w:r><w:delText>지운 글</w:delText><w:t>남은 글</w:t></w:r></w:p>")
    path = docx(tmp_path / "합성 (워드).docx", body)
    assert [line for line in doctext.docx_sections(path)[0] if line] == [
        "워드 첫 문단", "앞\t뒤\n다음 줄", "칸1 | 칸2", "남은 글"]


def test_MAT_HWP_011_encrypted_docx_is_an_ole_file(tmp_path):
    path = tmp_path / "잠긴.docx"
    path.write_bytes(build_cfb({"EncryptionInfo": b"\0" * 64, "EncryptedPackage": b"\0" * 64}))
    with pytest.raises(ex.NotReadable, match="암호"):
        doctext.docx_sections(path)


def test_MAT_HWP_011b_hancom_files_are_read_by_what_they_are_not_by_their_suffix(tmp_path):
    """An HWP 5 file saved under a .hwpx name (seen among real course files) and the other way round."""
    renamed = hwp(tmp_path / "이름만 hwpx.hwpx", [para("속은 바이너리 한글")])
    other = hwpx(tmp_path / "이름만 hwp.hwp", [hp_p("<hp:t>속은 새 형식</hp:t>")])
    assert ex.page_texts(renamed, tmp_path) == (["속은 바이너리 한글"], "hwp")
    assert ex.page_texts(other, tmp_path) == (["속은 새 형식"], "hwpx")


def test_MAT_HWP_012_chunks_cut_between_lines_and_restart_at_each_section():
    pieces = ex.chunks([["가" * 900, "나" * 900, "다" * 900], ["라"], ["마" * 4500]], size=2000)
    assert pieces == ["가" * 900 + "\n" + "나" * 900, "다" * 900, "라", "마" * 2000, "마" * 2000, "마" * 500]
    assert ex.chunks([["", "  "]]) == []
    assert units.page_unit("hwp") == "구간" and units.page_unit("합성.DOCX") == "구간" and units.page_unit("pdf") == "쪽"


# --- through extract (MaterialPage) and the AI app link ------------------------------------------------------------
@pytest.fixture
def library(tmp_path):
    root = tmp_path / "학기 (합성)"
    folder = root / "구조역학"
    folder.mkdir(parents=True)
    (root / "_lecturehub").mkdir()
    files = {
        "hwp": hwp(folder / "합성 공지.hwp", [SAMPLE_SECTION]),
        "hwpx": hwpx(folder / "합성 안내.hwpx", [hp_p("<hp:t>한글 새 형식 레이놀즈</hp:t>")]),
        "docx": docx(folder / "합성 워드.docx", w_p("워드 본문 마찰손실")),
        "locked": hwp(folder / "잠긴 합성.hwp", [SAMPLE_SECTION], flags=0x2),
        "empty": hwp(folder / "빈 합성.hwp", [para("")]),
    }
    path = root / "_lecturehub" / "lecturehub.sqlite"
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        connection.execute("INSERT INTO Course VALUES ('su','SU','구조역학','su','26-2')")
        for key, file in files.items():
            connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                               "VALUES (?,?,?,?,?,?,0)",
                               (f"m_{key}", "su", str(file), hashlib.sha256(file.read_bytes()).hexdigest(),
                                file.suffix.lstrip("."), file.stem))
        connection.commit()
        yield root, connection, files


def test_MAT_HWP_013_extract_stores_pieces_and_gives_up_with_reasons(library):
    root, connection, _files = library
    report = ex.extract(connection, root)
    texts ={mid: [t for (t,) in connection.execute(
        "SELECT text FROM MaterialPage WHERE material_id = ? ORDER BY page_no", (mid,))]
        for mid in ("m_hwp", "m_hwpx", "m_docx")}
    assert texts["m_hwp"] == ["합성 첫 문단 관수로\n칸 가\n칸 나\n탭 뒤\n줄바꿈 앞\n줄바꿈 뒤"]
    assert texts["m_hwpx"] == ["한글 새 형식 레이놀즈"] and texts["m_docx"] == ["워드 본문 마찰손실"]
    sources = {item["material_id"]: item["source"] for item in report.extracted}
    assert sources == {"m_hwp": "hwp", "m_hwpx": "hwpx", "m_docx": "docx"}
    reasons = {item["material_id"]: item["reason"] for item in report.skipped}
    assert "암호" in reasons["m_locked"] and "글이 없는" in reasons["m_empty"]
    # a piece count is not a page count: Material.page_count stays as indexed
    assert connection.execute("SELECT page_count FROM Material WHERE id='m_hwp'").fetchone()[0] == 0
    event = json.loads(connection.execute(
        "SELECT payload_json FROM Event WHERE type='MATERIAL_TEXT_EXTRACTED' AND subject_id='m_hwp'").fetchone()[0])
    assert event["page_unit"] == "구간" and event["source"] == "hwp"


def test_MAT_HWP_014_background_writes_protected_files_down_once(library):
    from lecturehub.materials import background

    root, connection, _files = library
    connection.close()
    result = background.run(root)
    assert result["state"] == "done" and result["materials"] == 3 and result["no_text"] == 2
    tried = json.loads(background.state_path(root).read_text(encoding="utf-8"))
    assert tried["version"] == ex.EXTRACTOR_VERSION and set(tried["materials"]) == {"m_locked", "m_empty"}
    assert "암호" in tried["materials"]["m_locked"]["reason"]


def test_MAT_HWP_015_older_given_up_list_is_tried_again_after_the_version_bump(library):
    from lecturehub.materials import background

    root, connection, files = library
    background._write_json(background.state_path(root), {"version": 1, "materials": {
        "m_hwp": {"sha256": hashlib.sha256(files["hwp"].read_bytes()).hexdigest(), "reason": "형식"}}})
    assert "m_hwp" in background.pending(connection, root)


def test_MAT_HWP_016_ai_link_returns_hwp_text_for_an_allowed_course_only(tmp_path, monkeypatch):
    """MCP get_material/search hand out the .hwp text (as 구간) for an allowed course and nothing for another."""
    from test_mcp_access import ALLOWED, HIDDEN, build_library, call

    from lecturehub.core import settings

    root = tmp_path / "학기 (합성) 26-2"
    build_library(root)
    shown = hwp(root / ALLOWED / "강의자료" / "합성 안내문.hwp", [para("한글 본문 베르누이 정리")])
    secret = hwp(root / HIDDEN / "강의자료" / "숨김 안내문.hwp", [para("숨긴 한글 본문 QZXHWP")])
    with closing(db.connect(root / "_lecturehub" / "lecturehub.sqlite")) as connection:
        for mid, course, file in (("mh", "ca", shown), ("mx", "cb", secret)):
            connection.execute("INSERT INTO Material (id,course_id,path,sha256,kind,title,page_count) "
                               "VALUES (?,?,?,?,'hwp',?,0)",
                               (mid, course, str(file), hashlib.sha256(file.read_bytes()).hexdigest(), file.stem))
        connection.commit()
        report = ex.extract(connection, root)
        assert {item["material_id"] for item in report.extracted} >= {"mh", "mx"}
    monkeypatch.setenv("LECTUREHUB_ROOT", str(root))
    monkeypatch.setenv("LECTUREHUB_EDITION", "public")
    settings.set_ai_link(True)
    material, error, _ = call("get_material", {"material_id": "mh"})
    assert not error and material["pages"] == [{"page": 1, "text": "한글 본문 베르누이 정리"}]
    assert material["page_unit"] == "구간" and "구간" in material["page_note"] and "page_count" not in material
    found, _, _ = call("search", {"query": "베르누이"})
    assert [(r["material_id"], r["page"], r.get("page_unit")) for r in found["results"]] == [("mh", 1, "구간")]
    _hidden, hidden_error, raw = call("get_material", {"material_id": "mx"})
    assert hidden_error is True and "QZXHWP" not in raw
    nothing, _, raw = call("search", {"query": "QZXHWP"})
    assert nothing["count"] == 0 and "QZXHWP" not in raw


def test_MAT_HWP_017_where_the_number_is_shown_it_says_구간(library, monkeypatch):
    """App search hit carries the unit; the note footnote and the opener say 구간, not 쪽."""
    from lecturehub.materials import opener
    from lecturehub.study import note_html
    from lecturehub.ui.library import Library

    root, connection, files = library
    ex.extract(connection, root)
    connection.commit()
    hit = next(r for r in Library(root).search("레이놀즈")["results"] if r["kind"] == "material")
    assert (hit["page"], hit["unit"]) == (1, "구간")

    class _Sources:
        pass

    note = {"blocks": [{"kind": "p", "text": "합성 문장", "warnings": [],
                        "cites": [{"kind": "material", "material_id": "m_hwpx", "page_no": 1}]}]}
    page = note_html.render(note, title="t", subtitle="s", sources=_Sources(), materials={"m_hwpx": "합성 안내"},
                            chunked={"m_hwpx"})
    assert "합성 안내 구간 1" in page and "1쪽" not in page
    opened = []
    monkeypatch.setattr(opener.os, "startfile", opened.append, raising=False)
    result = opener.open_material(files["hwp"], 3, root)
    assert opened == [str(files["hwp"])] and result["page_applied"] is False and "구간 3" in result["note"]
