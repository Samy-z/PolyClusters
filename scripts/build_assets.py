"""Generate the Windows icon from the source mark.

Produces, from assets/PolyClusters_logo_icon.png:

  PolyClusters.ico  - multi-resolution Windows icon

Run:  .venv/Scripts/python.exe scripts/build_assets.py
Requires Pillow (dev-time only; the app itself does not import it).

Note on the logo: PolyClusters_logo_display_dark.png is a hand-maintained
source asset, not a generated one. An earlier version of this script derived it
from the light wordmark by recolouring, which was the wrong approach - the
result needed manual pixel fixes, and regenerating would have silently thrown
them away. Nothing here writes to that file. Edit it directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

SRC_ICON = ASSETS / "PolyClusters_logo_icon.png"
OUT_ICO = ASSETS / "PolyClusters.ico"

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def build_ico() -> None:
    from PIL import Image

    im = Image.open(SRC_ICON).convert("RGBA")

    # Trim the transparent margin so the glyph fills the icon box; a padded
    # source renders as a tiny smudge at 16x16 in the taskbar.
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)

    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)

    canvas.save(OUT_ICO, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"  {OUT_ICO.name}: square {side}x{side} source, "
          f"sizes {', '.join(str(s) for s in ICO_SIZES)}")


def main() -> int:
    if not SRC_ICON.exists():
        print(f"Missing source asset: {SRC_ICON}")
        return 1
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is required: .venv/Scripts/python.exe -m pip install Pillow")
        return 1

    print("Building assets...")
    build_ico()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
