#!/usr/bin/env python3
"""Translate the whole board down and snap the working origin to the page grid.

Moves every absolute coordinate in the PCB by a single (dx, dy), chosen so the
board shifts ~`--down` mm downward (+Y) AND the grid origin (the arena-center
working origin) lands on a whole-`--snap`-mm page coordinate. The grid origin
moves with the board and ends up on that clean value.

Only ABSOLUTE coordinates move:
  - footprint's own (at x y angle)               -> shifted (angle kept)
  - segments/vias/arcs, zones, gr_* graphics,
    dimensions, grid_origin, aux_axis_origin      -> shifted
  - footprint-LOCAL coords (pad/fp_text/fp_line/…) -> untouched (they ride along
    with the footprint origin)

Default is a DRY RUN. Pass --apply to write (a .bak copy is made first).
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import shutil
import sys

PCB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "arena_12-18.kicad_pcb"))

# position tokens whose x/y are real coordinates
COORD = re.compile(r"\((start|end|mid|center|at|xy) (-?[\d.]+) (-?[\d.]+)((?: -?[\d.]+)?)\)")


def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default=PCB)
    ap.add_argument("--down", type=float, default=60.0, help="nominal downward (+Y) shift, mm")
    ap.add_argument("--snap", type=float, default=1.0, help="grid (mm) to snap the origin to")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    txt = open(args.pcb, encoding="utf-8").read()

    mo = re.search(r"\(grid_origin (-?[\d.]+) (-?[\d.]+)\)", txt)
    if not mo:
        sys.exit("No grid_origin found; can't snap. Specify one in KiCad first.")
    gx, gy = float(mo.group(1)), float(mo.group(2))
    snap = args.snap
    new_gx = round(gx / snap) * snap
    new_gy = round((gy + args.down) / snap) * snap
    dx, dy = new_gx - gx, new_gy - gy
    print(f"grid origin {(gx, gy)} -> {(new_gx, new_gy)}   shift dx={dx:+.4f} dy={dy:+.4f} mm "
          f"(nominal down {args.down}, snap {snap} mm)")

    # footprint block spans, and each footprint's own-(at) position (first coord token)
    fp_spans = []
    for m in re.finditer(r"\n\t\(footprint ", txt):
        s = m.start() + 1
        e = txt.find("\n\t)", m.end())            # this footprint's own closing paren
        fp_spans.append((s, (e + len("\n\t)")) if e != -1 else len(txt)))
    fp_starts = [s for s, _ in fp_spans]
    fp_ends = [e for _, e in fp_spans]
    fp_own_at = set()
    for s, e in fp_spans:
        mm = COORD.search(txt, s, e)          # first coordinate token == footprint's own (at)
        if mm:
            fp_own_at.add(mm.start())

    def inside_fp(pos: int) -> bool:
        i = bisect.bisect_right(fp_starts, pos) - 1
        return i >= 0 and pos < fp_ends[i]

    out, pos, moved = [], 0, 0
    for m in COORD.finditer(txt):
        out.append(txt[pos:m.start()])
        pos = m.end()
        shift = (not inside_fp(m.start())) or (m.start() in fp_own_at)
        if shift:
            x = float(m.group(2)) + dx
            y = float(m.group(3)) + dy
            out.append(f"({m.group(1)} {fmt(x)} {fmt(y)}{m.group(4)})")
            moved += 1
        else:
            out.append(m.group(0))
    out.append(txt[pos:])
    txt = "".join(out)

    # grid/drill origins (not COORD tokens) -> shift too
    def shift_origin(key, s):
        def repl(mm):
            return f"({key} {fmt(float(mm.group(1)) + dx)} {fmt(float(mm.group(2)) + dy)})"
        return re.sub(rf"\({key} (-?[\d.]+) (-?[\d.]+)\)", repl, s)
    txt = shift_origin("grid_origin", txt)
    txt = shift_origin("aux_axis_origin", txt)

    print(f"shifted {moved} absolute coordinate tokens; footprint-local coords left untouched.")
    if args.apply:
        shutil.copyfile(args.pcb, args.pcb + ".bak")
        open(args.pcb, "w", encoding="utf-8").write(txt)
        print(f"Wrote {args.pcb} (backup at {args.pcb}.bak)")
    else:
        print("DRY RUN -- no file written. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
