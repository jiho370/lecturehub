"""Write the 'note' icon SVGs: 256-unit art + hand-placed 40/32/24/20/16 versions (integer edges)."""
from pathlib import Path

D = Path(__file__).parent
TILE_A, TILE_B, TILE_SOLID = "#3f7650", "#2f5a39", "#33613f"
PAGE, EAR, INK, AMBER = "#fbf7f1", "#e3d5bd", "#2f5a39", "#e2ad48"


def svg(n, body, solid=False, r=None):
    r = r if r is not None else n * 7 / 32
    fill = TILE_SOLID if solid else "url(#g)"
    defs = "" if solid else (f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="{n}" gradientUnits="userSpaceOnUse">'
                             f'<stop offset="0" stop-color="{TILE_A}"/><stop offset="1" stop-color="{TILE_B}"/></linearGradient></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{n}" height="{n}" viewBox="0 0 {n} {n}">{defs}'
            f'<rect x="0" y="0" width="{n}" height="{n}" rx="{r}" fill="{fill}"/>{body}</svg>\n')


def page(x0, y0, x1, y1, ear, r=0):
    """Page with the top-right corner cut by `ear`, and the dog-ear triangle."""
    if r:
        d = (f"M{x0 + r} {y0} H{x1 - ear} L{x1} {y0 + ear} V{y1 - r} Q{x1} {y1} {x1 - r} {y1} H{x0 + r} Q{x0} {y1} {x0} {y1 - r} "
             f"V{y0 + r} Q{x0} {y0} {x0 + r} {y0} Z")
    else:
        d = f"M{x0} {y0} H{x1 - ear} L{x1} {y0 + ear} V{y1} H{x0} Z"
    return f'<path d="{d}" fill="{PAGE}"/><path d="M{x1 - ear} {y0} V{y0 + ear} H{x1} Z" fill="{EAR}"/>'


def rect(x, y, w, h, c, r=0):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{c}"/>'


def bars(xs, w, center, heights, r=0):
    return "".join(rect(x, center - h / 2, w, h, INK, r) for x, h in zip(xs, heights))


# 256-unit art (tile inset 8, like the old icon)
big = ('<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#g)"/>'
       + page(60, 30, 196, 226, 36, r=14)
       + bars([82, 102, 122, 142, 162], 12, 90, [22, 46, 66, 36, 16], r=6)
       + rect(77, 133, 102, 24, AMBER, 7) + rect(84, 140, 88, 10, INK, 5)
       + rect(84, 172, 88, 10, INK, 5) + rect(84, 196, 56, 10, INK, 5))
big_svg = svg(256, big).replace('<rect x="0" y="0" width="256" height="256" rx="56.0" fill="url(#g)"/>', "")

files = {
    "note_256.svg": big_svg,
    "note_40.svg": svg(40, page(9, 4, 31, 36, 6) + bars([13, 16, 19, 22, 25], 2, 13, [4, 10, 14, 8, 4])
                       + rect(12, 21, 16, 7, AMBER) + rect(13, 23, 14, 3, INK) + rect(13, 30, 14, 2, INK) + rect(13, 33, 9, 2, INK)),
    "note_32.svg": svg(32, page(7, 3, 25, 29, 5) + bars([10, 13, 16, 19], 2, 11, [4, 10, 6, 4])
                       + rect(9, 17, 14, 5, AMBER) + rect(10, 19, 12, 2, INK) + rect(10, 24, 12, 2, INK) + rect(10, 27, 7, 1, INK)),
    "note_24.svg": svg(24, page(5, 2, 19, 22, 4) + bars([8, 10, 12, 14, 16], 1, 8, [2, 4, 6, 4, 2])
                       + rect(7, 12, 10, 4, AMBER) + rect(8, 13, 8, 2, INK) + rect(8, 18, 8, 1, INK) + rect(8, 20, 5, 1, INK),
                       solid=True, r=5),
    "note_20.svg": svg(20, page(4, 2, 16, 18, 3) + bars([7, 9, 11, 13], 1, 7, [2, 4, 6, 2])
                       + rect(6, 10, 9, 3, AMBER) + rect(7, 11, 7, 1, INK) + rect(7, 14, 7, 1, INK) + rect(7, 16, 4, 1, INK),
                       solid=True, r=4),
    "note_16.svg": svg(16, page(3, 1, 13, 15, 3) + rect(5, 5, 1, 2, INK) + rect(7, 3, 1, 5, INK) + rect(9, 4, 1, 3, INK)
                       + rect(11, 5, 1, 2, INK) + rect(5, 9, 6, 2, AMBER) + rect(5, 12, 6, 1, INK),
                       solid=True, r=3.5),
}
for name, text in files.items():
    (D / name).write_text(text, encoding="utf-8")
print("wrote", list(files))
