"""Build the app icon → src/lecturehub/ui/assets/app.ico + app.png from the drawings in tools/icon/.

2026-11-09 (user: "로고도 구림", picked "노트" after an independent review): a note page with a folded corner on
deep green; a waveform at the top, then note lines, the first one marked with an amber highlighter (lecture voice →
study notes). Colours follow the app (accent #3c6e47/#2f5a39, panel paper #fbf7f1, amber #e2ad48).
- tools/icon/make_svgs.py writes the SVGs: note_256.svg (the art) and note_40/32/24/20/16.svg, drawn per size with
  every edge on a pixel (the art turns to mush below 48 px). 24/20/16 use a flat tile.
- The PNGs are VectorCraft exports of those SVGs (note_1024.png = note_256.svg at 4x; the others at 1x).
  After changing an SVG, export it again (VectorCraft: open the .svg, export png, transparent).
- 48 px and up are downsampled from note_1024.png; 40 px and below are the hand-placed PNGs.
Requires Pillow (dev tool only). Usage: .venv\\Scripts\\python.exe tools\\make_icon.py
"""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent / "icon"
OUT = Path(__file__).resolve().parents[1] / "src" / "lecturehub" / "ui" / "assets"
HAND = (16, 20, 24, 32, 40)  # exported at that exact size
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def frames() -> dict[int, Image.Image]:
    big = Image.open(HERE / "note_1024.png").convert("RGBA")
    out = {n: Image.open(HERE / f"note_{n}.png").convert("RGBA") for n in HAND}
    out.update({n: big.resize((n, n), Image.LANCZOS) for n in SIZES if n not in HAND})
    for n, im in out.items():
        if im.size != (n, n):
            raise SystemExit(f"note_{n}.png is {im.size}, expected {n}x{n}")
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    f = frames()
    f[256].save(OUT / "app.png", optimize=True)
    f[256].save(OUT / "app.ico", sizes=[(n, n) for n in SIZES], append_images=[f[n] for n in SIZES if n != 256])
    print(OUT / "app.ico")


if __name__ == "__main__":
    main()
