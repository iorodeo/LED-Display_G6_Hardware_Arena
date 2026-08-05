#!/usr/bin/env python3
"""Continue the arena panel-header ring placement.

The 12 panel-header footprints P1..P12 (panel_silk:Panel_Silk) sit on a circle
centered on the board's grid origin (the arena center). P1 and P2 are already
placed correctly; this script reads them, derives the ring (center, radius,
angular pitch and rotation pitch), and writes P3..P12 continuing the same ring:
same neighbour distance and same per-panel rotation.

How placement works in the .kicad_pcb:
  - A footprint's "(at x y angle)" sets its position and orientation.
  - Graphics (fp_line/fp_circle/...) are stored in the footprint LOCAL frame and
    rotate with it, so they are left untouched.
  - Text items (property/fp_text) store an ABSOLUTE angle that tracks the
    footprint angle, so each text angle is shifted by the footprint's rotation
    delta.

By default this is a DRY RUN (prints the plan, writes nothing). Pass --apply to
edit the file in place (a .bak copy is made first).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys

# Script lives in <project>/assets/placement/; the PCB is two levels up.
PCB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "arena_12-18.kicad_pcb"))
FIRST_FIXED = ("P1", "P2")          # anchors: read, never modified
PANELS = [f"P{i}" for i in range(1, 13)]

AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")


def fmt(v: float) -> str:
    """Format a number the way KiCad does: up to 6 decimals, no trailing zeros."""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def norm_angle(a: float) -> float:
    a %= 360.0
    return a + 360.0 if a < 0 else a


def find_footprint_blocks(txt: str) -> dict[str, tuple[int, int]]:
    """Return {reference: (start, end)} char spans for each panel footprint."""
    spans: dict[str, tuple[int, int]] = {}
    for m in re.finditer(r'\n\t\(footprint ', txt):
        start = m.start() + 1
        end = txt.find('\n\t(footprint ', m.end())
        if end == -1:
            end = len(txt)
        block = txt[start:end]
        mref = re.search(r'\(property "Reference" "(P\d+)"', block)
        if mref:
            spans[mref.group(1)] = (start, end)
    return spans


def read_at(block: str) -> tuple[float, float, float]:
    """Footprint position/angle = the first (at ...) in the block."""
    m = AT_RE.search(block)
    x, y, a = m.group(1), m.group(2), m.group(3)
    return float(x), float(y), float(a or 0.0)


def get_grid_origin(txt: str) -> tuple[float, float]:
    m = re.search(r'\(grid_origin (-?[\d.]+) (-?[\d.]+)\)', txt)
    if not m:
        sys.exit("No grid_origin found; cannot determine arena center.")
    return float(m.group(1)), float(m.group(2))


def rewrite_block(block: str, nx: float, ny: float, nrot: float) -> str:
    """Set the footprint (at) to (nx,ny,nrot) and shift every text angle by the
    footprint rotation delta. Returns the modified block."""
    _, _, orot = read_at(block)
    delta = nrot - orot

    # Replace the footprint's own (at ...) -- the FIRST one in the block.
    first = AT_RE.search(block)
    new_fp_at = f"(at {fmt(nx)} {fmt(ny)} {fmt(norm_angle(nrot))})"
    block = block[:first.start()] + new_fp_at + block[first.end():]

    # Shift the angle of every remaining (at x y angle) (property / fp_text).
    # These all live after the footprint (at); walk them and add delta.
    out = []
    pos = first.start() + len(new_fp_at)
    out.append(block[:pos])
    for m in AT_RE.finditer(block, pos):
        if m.group(3) is None:      # 2-token (at x y): no angle, leave as is
            continue
        ang = norm_angle(float(m.group(3)) + delta)
        repl = f"(at {m.group(1)} {m.group(2)} {fmt(ang)})"
        out.append(block[pos:m.start()])
        out.append(repl)
        pos = m.end()
    out.append(block[pos:])
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default=PCB, help=f"PCB file (default: {PCB})")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry run)")
    args = ap.parse_args()

    txt = open(args.pcb, encoding="utf-8").read()
    spans = find_footprint_blocks(txt)
    missing = [p for p in PANELS if p not in spans]
    if missing:
        sys.exit(f"Missing panel footprints: {missing}")

    cx, cy = get_grid_origin(txt)
    p1 = read_at(txt[slice(*spans["P1"])])
    p2 = read_at(txt[slice(*spans["P2"])])

    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    a2 = math.atan2(p2[1] - cy, p2[0] - cx)
    r1 = math.hypot(p1[0] - cx, p1[1] - cy)
    r2 = math.hypot(p2[0] - cx, p2[1] - cy)
    R = (r1 + r2) / 2.0
    dpos = math.atan2(math.sin(a2 - a1), math.cos(a2 - a1))   # angular pitch (rad)
    drot = p2[2] - p1[2]                                       # rotation pitch (deg)

    print(f"center (grid origin) = ({fmt(cx)}, {fmt(cy)})")
    print(f"radius  P1={r1:.3f}  P2={r2:.3f}  -> use {R:.3f} mm  (chord={2*R*math.sin(abs(dpos)/2):.3f} mm)")
    print(f"angular pitch = {math.degrees(dpos):+.3f} deg/panel   rotation pitch = {drot:+.3f} deg/panel")
    print(f"P1 = ({fmt(p1[0])}, {fmt(p1[1])}, {fmt(p1[2])})  [fixed]")
    print(f"P2 = ({fmt(p2[0])}, {fmt(p2[1])}, {fmt(p2[2])})  [fixed]\n")

    for n in range(3, 13):
        ang = a1 + (n - 1) * dpos
        x = cx + R * math.cos(ang)
        y = cy + R * math.sin(ang)
        rot = norm_angle(p1[2] + (n - 1) * drot)
        ref = f"P{n}"
        s, e = spans[ref]
        old = read_at(txt[s:e])
        dispx, dispy = x - cx, cy - y     # KiCad display frame (relative to origin, Y up)
        print(f"{ref:>3}: ({fmt(x)}, {fmt(y)}, {fmt(rot)})"
              f"   [display {dispx:+.2f}, {dispy:+.2f}]"
              f"   was ({fmt(old[0])}, {fmt(old[1])}, {fmt(old[2])})")
        new_block = rewrite_block(txt[s:e], x, y, rot)
        txt = txt[:s] + new_block + txt[e:]
        # spans after this block shift if length changed; recompute lazily
        spans = find_footprint_blocks(txt)

    if args.apply:
        shutil.copyfile(args.pcb, args.pcb + ".bak")
        open(args.pcb, "w", encoding="utf-8").write(txt)
        print(f"\nWrote {args.pcb} (backup at {args.pcb}.bak)")
    else:
        print("\nDRY RUN -- no file written. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
