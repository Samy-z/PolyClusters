"""Generate the derived image assets the app ships with.

Produces, from the two source PNGs in assets/:

  PolyClusters_logo_display_dark.png  - wordmark recoloured for the dark UI
  PolyClusters.ico                    - multi-resolution Windows icon

The dark variant exists because the "Poly" half of the wordmark is navy
(rgb 11,29,62) against the app background (rgb 20,22,28) - a contrast ratio of
about 1.08:1, i.e. invisible. "Poly" is turned white; the blue "Clusters" text
and the node graphic are left byte-for-byte untouched.

The navy is isolated by colour, not by position, so the rule survives a change
of layout. Measured on the source art:

    region      n px     mean B-R    B-R percentiles (5 / 50 / 95)
    Poly        11,607       49          6  /  51  /  64
    Clusters     4,454      162        120  / 167  / 194
    nodes        1,568      149         79  / 162  / 239

A dark pixel whose blue channel barely exceeds its red is navy; every blue pixel
in the artwork sits far above that. Thresholding on B-R separates them with no
overlap, which selecting on darkness alone did not - that also caught the dark
edge pixels inside "Clusters" and the nodes and speckled them.

Run:  .venv/Scripts/python.exe scripts/build_assets.py
Requires Pillow (dev-time only; the app itself does not import it).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

SRC_DISPLAY = ASSETS / "PolyClusters_logo_display.png"
SRC_ICON = ASSETS / "PolyClusters_logo_icon.png"
OUT_DARK = ASSETS / "PolyClusters_logo_display_dark.png"
OUT_ICO = ASSETS / "PolyClusters.ico"

# "Poly" becomes plain white.
INK = (255, 255, 255)
# A pixel is navy wordmark when it is dark AND barely blue-shifted. Both bounds
# sit in the empty gap between the two measured distributions above.
DARK_CUTOFF = 110      # luminance
MAX_BLUE_SHIFT = 72    # blue minus red
MAX_BLUE = 110         # absolute blue channel
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def relative_luminance(r: float, g: float, b: float) -> float:
    """WCAG relative luminance, used to report the contrast we fixed."""
    def channel(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = relative_luminance(*fg), relative_luminance(*bg)
    lo, hi = sorted((l1, l2))
    return (hi + 0.05) / (lo + 0.05)


def build_dark_logo() -> None:
    import numpy as np
    from PIL import Image

    im = Image.open(SRC_DISPLAY).convert("RGBA")
    arr = np.array(im).astype(np.int16)
    rgb, alpha = arr[..., :3], arr[..., 3]
    red, blue = rgb[..., 0], rgb[..., 2]

    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    mask = (
        (lum < DARK_CUTOFF)
        & (alpha > 8)
        & ((blue - red) < MAX_BLUE_SHIFT)
        & (blue < MAX_BLUE)
    )

    # Anti-aliasing lives in the alpha channel, which is left alone, so a flat
    # white fill keeps the glyph edges smooth.
    arr[..., :3][mask] = np.array(INK, dtype=np.int16)

    out = Image.fromarray(arr.clip(0, 255).astype("uint8"), "RGBA")
    out.save(OUT_DARK)

    # Anything recoloured to the right of the wordmark would be a stray pixel in
    # "Clusters"; report it rather than let it ship unnoticed.
    xs = np.flatnonzero(mask.any(axis=0))
    before = contrast_ratio((11, 29, 62), (20, 22, 28))
    after = contrast_ratio(INK, (20, 22, 28))
    print(f"  {OUT_DARK.name}: {out.size[0]}x{out.size[1]}, "
          f"{int(mask.sum()):,} px recoloured")
    if xs.size:
        print(f"    recoloured x-range {xs.min()}-{xs.max()} of {out.size[0]} "
              f"(should cover 'Poly' only)")
    print(f"    'Poly' contrast on #14161c: {before:.2f}:1 -> {after:.2f}:1")


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
    missing = [p.name for p in (SRC_DISPLAY, SRC_ICON) if not p.exists()]
    if missing:
        print(f"Missing source asset(s) in {ASSETS}: {', '.join(missing)}")
        return 1
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is required: .venv/Scripts/python.exe -m pip install Pillow")
        return 1

    print("Building assets...")
    build_dark_logo()
    build_ico()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
