"""A note an AI app sends through ``save_note`` (D-076) → one self-contained, inert HTML page.

The text comes from an AI app and may carry anything a transcript or an eTL notice planted in it, so nothing of it
is trusted: Markdown is turned into a few plain tags with every character escaped, and HTML goes through an
allow-list (``clean_html``) that keeps text and simple structure only — no script, style, form, frame, image,
media or SVG, no event handler or ``style`` attribute, no link that leaves the page (only ``#anchor`` links stay).
The page itself also carries ``Content-Security-Policy: default-src 'none'`` so even a mistake here cannot load or
run anything. Size is capped before and after conversion.

Linear time on any input (integration review: ``html.parser`` took 11 s on ``"<a " * 4000`` and a few inline
Markdown patterns were quadratic): the HTML is split by a small tokenizer of our own that never scans the same
characters twice, the inline Markdown patterns are bounded, and the whole conversion stops with ``ValueError`` when
it runs past ``TIME_LIMIT_S``. ``writes.save_note`` runs this before it opens the database for writing, so even a
slow conversion never holds the write lock.
"""
from __future__ import annotations

import html
import re
import time

MAX_INPUT = 200_000  # characters the AI app may send (Markdown)
MAX_HTML_INPUT = 100_000  # characters when the content is HTML
MAX_PAGE = 1_000_000  # bytes of the written page (UTF-8)
TIME_LIMIT_S = 2.0  # the conversion gives up after this (a page is normally done in a few milliseconds)
MAX_QUOTE_DEPTH = 8
GENERATOR = "lecturehub-ai-app"
TOO_SLOW = "정리본 글의 모양이 너무 복잡해 저장하지 않았습니다. 평범한 Markdown 글로 다시 보내 주세요."

KEEP: frozenset[str] = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "code", "pre",
    "blockquote", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "br", "hr", "sup", "sub", "span",
    "div", "section", "article", "dl", "dt", "dd", "mark", "small", "a", "figcaption"})
VOID: frozenset[str] = frozenset({"br", "hr"})
# raw text: everything up to the matching end tag is dropped unread (an unclosed script/style drops the rest, as a
# browser would; any other unclosed one drops only its tag)
RAW: frozenset[str] = frozenset({"script", "style", "xmp", "iframe", "noembed", "noframes", "textarea", "title"})
EAT_REST: frozenset[str] = frozenset({"script", "style"})
# containers dropped with their content when they are closed; an unclosed one drops only its tag, so a stray
# "<svg>" or "<head>" cannot swallow the rest of the note (integration review)
SKIP: frozenset[str] = frozenset({
    "object", "svg", "math", "template", "noscript", "select", "button", "video", "audio", "canvas", "frameset",
    "head", "applet"})
# ``embed`` and the other void elements have no content: they are dropped like any tag that is not kept
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_NUM = re.compile(r"^[1-9][0-9]?$")
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]{0,40})")
_ATTR = re.compile(r"""([^\s"'<>/=]{1,60})(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'<>=`]+))?""")


class _Clock:
    def __init__(self) -> None:
        self.until = time.monotonic() + TIME_LIMIT_S
        self.ticks = 0

    def check(self) -> None:
        self.ticks += 1
        if self.ticks & 255 == 0 and time.monotonic() > self.until:
            raise ValueError(TOO_SLOW)


def _tokens(text: str, clock: _Clock) -> list[tuple]:
    """("text", s) | ("start", name, attribute_text, self_closing) | ("end", name). Comments, doctype, processing
    instructions and raw-text elements are left out. Each search starts where the last one stopped (linear)."""
    out: list[tuple] = []
    n, i, gt = len(text), 0, -2
    low: str | None = None
    closes: dict[str, int] = {}
    while i < n:
        clock.check()
        lt = text.find("<", i)
        if lt < 0:
            out.append(("text", text[i:]))
            break
        if lt > i:
            out.append(("text", text[i:lt]))
        i = lt
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            i = n if end < 0 else end + 3
            continue
        if gt < i:
            gt = text.find(">", i)
        if gt < 0:  # no ">" anywhere after this: the rest is text
            out.append(("text", text[i:]))
            break
        nxt = text.find("<", i + 1, gt)
        if nxt >= 0:  # "<a <b>": the first "<" never became a tag
            out.append(("text", text[i:nxt]))
            i = nxt
            continue
        if text.startswith(("<!", "<?"), i):  # doctype, CDATA, processing instruction: dropped
            i = gt + 1
            continue
        match = _TAG.match(text, i)
        if not match:
            out.append(("text", "<"))
            i += 1
            continue
        closing, name = match.group(1) == "/", match.group(2).lower()
        inner = text[match.end():gt]
        i = gt + 1
        if closing:
            out.append(("end", name))
            continue
        self_closing = inner.rstrip().endswith("/")
        if name in RAW and not self_closing:
            if low is None:
                low = text.lower()
            at = closes.get(name)
            if at is None or (0 <= at < i):
                at = low.find("</" + name, i)
                closes[name] = at
            if at >= 0:
                stop = text.find(">", at)
                i = n if stop < 0 else stop + 1
            elif name in EAT_REST:
                break
            continue
        out.append(("start", name, inner, self_closing))
    return out


def _attrs(tag: str, inner: str) -> str:
    kept = []
    for match in _ATTR.finditer(inner):
        name = match.group(1).lower()
        value = html.unescape((match.group(2) or "").strip("\"'"))
        if name == "id" and _ID.match(value):
            kept.append(f' id="{value}"')
        elif name in ("colspan", "rowspan") and tag in ("td", "th") and _NUM.match(value):
            kept.append(f' {name}="{value}"')
        elif name == "href" and tag == "a" and value.startswith("#") and _ID.match(value[1:]):
            kept.append(f' href="{value}"')
    return "".join(kept)


def clean_html(text: str, clock: _Clock | None = None) -> str:
    """The body of an HTML note with only allowed tags and attributes; all text escaped."""
    clock = clock or _Clock()
    tokens = _tokens((text or "").replace("\x00", ""), clock)
    # which dropped containers are closed: those are skipped with their content (per name, nearest open first)
    pairs: dict[int, int] = {}
    waiting: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        if token[0] == "start" and token[1] in SKIP and not token[3]:
            waiting.setdefault(token[1], []).append(index)
        elif token[0] == "end" and waiting.get(token[1]):
            pairs[waiting[token[1]].pop()] = index
    out: list[str] = []
    stack: list[str] = []
    open_count: dict[str, int] = {}
    index = 0
    while index < len(tokens):
        clock.check()
        token = tokens[index]
        index += 1
        if token[0] == "text":
            out.append(html.escape(html.unescape(token[1]), quote=False))
        elif token[0] == "start":
            name = token[1]
            if name in SKIP:
                if index - 1 in pairs:
                    index = pairs[index - 1] + 1
                continue  # unclosed or self-closed: only the tag goes
            if name not in KEEP or (token[3] and name not in VOID):
                continue
            out.append(f"<{name}{_attrs(name, token[2])}>")
            if name not in VOID:
                stack.append(name)
                open_count[name] = open_count.get(name, 0) + 1
        else:
            name = token[1]
            if name not in KEEP or name in VOID or not open_count.get(name):
                continue
            while stack:
                top = stack.pop()
                open_count[top] -= 1
                out.append(f"</{top}>")
                if top == name:
                    break
    return "".join(out) + "".join(f"</{t}>" for t in reversed(stack))


# ------------------------------------------------------------------ Markdown (a small, strict subset)
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# inline marks: bounded, so a line of "[" or " *a" is not read over and over (integration review)
_CODE = re.compile(r"`([^`\n]{1,500})`")
_LINK = re.compile(r"!?\[([^\[\]\n]{0,300})\]\(([^()\s]{0,500})\)")
_STRONG = re.compile(r"\*\*([^*\n]{1,300}?)\*\*|__([^_\n]{1,300}?)__")
_EM = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]{1,300}?)(?<!\s)\*(?![\w*])")
_STRIKE = re.compile(r"~~([^~\n]{1,300}?)~~")


def _inline(text: str) -> str:
    """Escape, then a few inline marks. A link keeps its words; an outside address is shown as text, never linked."""
    out = html.escape(text.replace("\x00", ""), quote=False)
    codes: list[str] = []

    def keep_code(match: re.Match) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(keep_code, out)

    def link(match: re.Match) -> str:
        words, target = match.group(1), match.group(2).strip()
        if target.startswith("#") and _ID.match(target[1:]):
            return f'<a href="{target}">{words}</a>'
        return f"{words} ({target})" if target else words

    out = _LINK.sub(link, out)
    out = _STRONG.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", out)
    out = _EM.sub(r"<em>\1</em>", out)
    out = _STRIKE.sub(r"<s>\1</s>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))] if int(m.group(1)) < len(codes) else "", out)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _heading_text(raw: str) -> str:
    """"제목 ##" → "제목" (closing hashes after a space), without a backtracking pattern."""
    text = raw.strip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1].isspace()):
        text = bare.rstrip()
    return text


def markdown_html(text: str, clock: _Clock | None = None, depth: int = 0) -> str:
    clock = clock or _Clock()
    lines = (text or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    lists: list[str] = []  # "ul"/"ol" currently open (one level: nesting is flattened)
    i = 0

    def flush_para() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    def close_lists() -> None:
        while lists:
            out.append(f"</{lists.pop()}>")

    while i < len(lines):
        clock.check()
        line = lines[i]
        if _FENCE.match(line):
            flush_para()
            close_lists()
            fence = _FENCE.match(line).group(1)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(body), quote=False) + "</code></pre>")
            i += 1
            continue
        if not line.strip():
            flush_para()
            close_lists()
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            close_lists()
            head = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                clock.check()
                rows.append(_cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in rows)
                       + "</tbody></table>")
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_para()
            close_lists()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(_heading_text(heading.group(2)))}</h{level}>")
            i += 1
            continue
        if _RULE.match(line):
            flush_para()
            close_lists()
            out.append("<hr>")
            i += 1
            continue
        quote = _QUOTE.match(line)
        if quote and depth < MAX_QUOTE_DEPTH:
            flush_para()
            close_lists()
            body = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                body.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>" + markdown_html("\n".join(body), clock, depth + 1) + "</blockquote>")
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_para()
            kind = "ul" if bullet else "ol"
            if lists and lists[-1] != kind:
                close_lists()
            if not lists:
                out.append(f"<{kind}>")
                lists.append(kind)
            out.append(f"<li>{_inline((bullet or numbered).group(1))}</li>")
            i += 1
            continue
        close_lists()
        para.append(line.strip())
        i += 1
    flush_para()
    close_lists()
    return "".join(out)


STYLE_EXTRA = (".by{display:inline-block;background:var(--aside);border:1px solid var(--line);border-radius:6px;"
               "padding:0 6px;color:var(--fg);font-size:.85rem}table{border-collapse:collapse;margin:0 0 14px}"
               "th,td{border:1px solid var(--line);padding:4px 8px;text-align:left}pre{background:var(--aside);"
               "padding:10px;overflow:auto}blockquote{border-left:3px solid var(--line);margin:0 0 14px;padding-left:12px;"
               "color:var(--muted)}")


def page(body_text: str, fmt: str, *, title: str, subtitle: str, written_by: str, change_id: str) -> str:
    """The whole page. ``fmt`` is "markdown" or "html"; either way the body ends up allow-listed. ValueError when
    there is nothing to save, the HTML is over ``MAX_HTML_INPUT`` or the conversion runs past ``TIME_LIMIT_S``."""
    from lecturehub.study.note_html import STYLE

    body_text = (body_text or "").replace("\x00", "")
    if fmt == "html" and len(body_text) > MAX_HTML_INPUT:
        raise ValueError(f"HTML 정리본은 {MAX_HTML_INPUT:,}자까지 받습니다. Markdown으로 보내거나 나눠서 저장하세요.")
    clock = _Clock()
    body = clean_html(markdown_html(body_text, clock) if fmt == "markdown" else body_text, clock)
    same = re.match(r"\s*<h1>(.*?)</h1>", body, re.S)
    if same and same.group(1).strip() == html.escape(title).strip():  # the text opens with the title: not twice
        body = body[same.end():]
    if not re.sub(r"<[^>]*>", "", body).strip():
        raise ValueError("저장할 내용이 없습니다(글이 비어 있거나 허용하지 않는 태그뿐입니다).")
    return ("<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"generator\" content=\"{GENERATOR}\"><meta name=\"lecturehub-change\" content=\"{html.escape(change_id)}\">"
            f"<title>{html.escape(title)}</title><style>{STYLE}{STYLE_EXTRA}</style></head><body><main><header>"
            f"<h1>{html.escape(title)}</h1><p><span class=\"by\">{html.escape(written_by)}</span> {html.escape(subtitle)}</p>"
            f"</header>{body}</main></body></html>\n")
