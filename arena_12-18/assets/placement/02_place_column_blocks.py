#!/usr/bin/env python3
"""Replicate the P2 panel-column component block to every other panel column.

Each panel column P1..P12 has a panel-header connector (P1..P12, the panel_silk
footprint that 01_place_panels.py positions on the ring) plus all its associated
parts. Those parts come from two hierarchical sub-schematics:
  - the panel-column sub-schematic (connectors J*, buffers U*, resistors R*,
    capacitors C*), instanced once per column, and
  - the CIPO-enable sub-schematic ("CIPO B0/B1 Enable for Pn"), also one per
    column, associated with the connector by the "for Pn" in its sheet name.

Panel column P2 is the TEMPLATE: this script measures every P2 part's placement
(both blocks) relative to the P2 connector (offset + rotation in the connector's
frame), then reproduces that same relative placement in every other column,
anchored to that column's own connector. The connectors themselves are left
exactly where 01_place_panels.py put them; only the associated parts move.

Matching across columns is by the footprint's hierarchical leaf UUID (the last
segment of its (path ...)), which is shared across all instances of a shared
sub-schematic. This is future-proof: any part added to the P2 column is picked
up automatically as long as the same instance exists in the target columns.

KiCad placement notes (same handling as 01_):
  - A footprint's "(at x y angle)" sets position and orientation.
  - Pad/graphic x/y and fp_line/circle/etc. are in the footprint LOCAL frame and
    rotate with it automatically, so their coordinates are left untouched.
  - Pad and text (fp_text/property) ANGLES are stored ABSOLUTE (board frame) in a
    placed footprint, so each is shifted by the footprint's rotation delta to
    stay locked to the footprint (relative angle preserved).

Default is a DRY RUN (prints the plan, writes nothing). Pass --apply to edit the
file in place (a .bak copy is made first).
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

TEMPLATE_COL = "P2"                       # column whose block is copied
CONNECTORS = [f"P{i}" for i in range(1, 13)]

AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
# CIPO-enable sub-sheets ("CIPO B0/B1 Enable for Pn") also belong to a column.
ENABLE_SHEETFILE = "cipo_enable.kicad_sch"
ENABLE_SHEET_RE = re.compile(r"Enable for (P\d+)")
# element keywords that own an (at ...); only text angles are absolute
TEXT_OWNERS = ("fp_text", "property", "gr_text")
OTHER_OWNERS = ("pad", "fp_line", "fp_circle", "fp_arc", "fp_poly",
                "fp_rect", "model", "footprint")


def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def norm_angle(a: float) -> float:
    a %= 360.0
    return a + 360.0 if a < 0 else a


def kicad_rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate an offset by a KiCad footprint orientation.

    KiCad's Y axis points DOWN, and its footprint rotation (RotatePoint) is the
    opposite handedness of a standard math rotation: a positive footprint angle
    rotates counter-clockwise on screen, i.e. R(-angle) in ordinary (Y-up) math.
    KiCad places a footprint child by:  world = footprint_pos + rotate(local, a),
    so the inverse (world -> local) is rotate(world - pos, -a) with this same fn.
    """
    r = math.radians(-deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


def find_footprints(txt: str) -> dict[str, dict]:
    """Return {reference: {span, x, y, a, path, leaf, colinst}}."""
    out: dict[str, dict] = {}
    for m in re.finditer(r'\n\t\(footprint ', txt):
        start = m.start() + 1
        end = txt.find('\n\t(footprint ', m.end())
        if end == -1:
            end = len(txt)
        block = txt[start:end]
        mref = re.search(r'\(property "Reference" "([^"]+)"', block)
        if not mref:
            continue
        at = AT_RE.search(block)
        path = re.search(r'\(path "([^"]*)"', block)
        seg = path.group(1).strip("/").split("/") if path else []
        out[mref.group(1)] = dict(
            span=(start, end),
            x=float(at.group(1)), y=float(at.group(2)), a=float(at.group(3) or 0.0),
            path=path.group(1) if path else "",
            leaf=seg[-1] if seg else None,
            colinst=seg[1] if len(seg) >= 2 else None,
        )
    return out


def enable_instances(sch_path: str) -> dict[str, str]:
    """Map each CIPO-enable sheet-instance UUID -> connector reference.

    Footprints in the PCB don't carry sheet names, so the "... Enable for Pn"
    label (which tells us the connector) is read from the fan-out schematic. The
    sheet's own UUID is the path segment those footprints carry in the PCB.
    """
    try:
        sch = open(sch_path, encoding="utf-8").read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for m in re.finditer(r"\(sheet\b", sch):
        blk = sch[m.start():m.start() + 1600]
        name = re.search(r'\(property "Sheetname" "([^"]*)"', blk)
        sfile = re.search(r'\(property "Sheetfile" "([^"]*)"', blk)
        uu = re.search(r'\(uuid "([^"]+)"\)', blk)             # sheet's own uuid (first)
        if not (name and sfile and uu) or sfile.group(1) != ENABLE_SHEETFILE:
            continue
        mm = ENABLE_SHEET_RE.search(name.group(1))
        if mm:
            out[uu.group(1)] = mm.group(1)
    return out


def owner_before(block: str, pos: int) -> str | None:
    """Which element keyword most-recently opened before char `pos`."""
    best, besti = None, -1
    for kw in TEXT_OWNERS + OTHER_OWNERS:
        i = block.rfind("(" + kw + " ", 0, pos)
        if i > besti:
            besti, best = i, kw
    return best


def rewrite_block(block: str, nx: float, ny: float, nrot: float) -> str:
    """Set the footprint (at) and shift only text-element angles by the delta."""
    fp = AT_RE.search(block)
    orot = float(fp.group(3) or 0.0)
    delta = nrot - orot
    new_fp_at = f"(at {fmt(nx)} {fmt(ny)} {fmt(norm_angle(nrot))})"

    out = [block[:fp.start()], new_fp_at]
    pos = fp.end()
    for m in AT_RE.finditer(block, pos):
        out.append(block[pos:m.start()])
        pos = m.end()
        owner = owner_before(block, m.start())
        if owner in TEXT_OWNERS or owner == "pad":
            # In a placed footprint KiCad stores pad/text ANGLES in the board
            # (absolute) frame, while their x/y stay in the footprint-local
            # frame. So rotate the angle by the footprint's rotation delta to
            # keep the pad/text locked to the footprint (its relative angle,
            # angle - footprint_orientation, is preserved). Positions are local
            # and rotate with the footprint automatically, so they are untouched.
            if m.group(3) is None and abs(delta) < 1e-9:
                out.append(m.group(0))                       # absent, no rotation
            else:
                base = float(m.group(3)) if m.group(3) is not None else 0.0
                out.append(f"(at {m.group(1)} {m.group(2)} {fmt(norm_angle(base + delta))})")
        else:
            out.append(m.group(0))
    out.append(block[pos:])
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default=PCB, help=f"PCB file (default: {PCB})")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    txt = open(args.pcb, encoding="utf-8").read()
    fps = find_footprints(txt)

    missing = [c for c in CONNECTORS if c not in fps]
    if missing:
        sys.exit(f"Missing panel connectors: {missing}")

    conn_leaf = fps[TEMPLATE_COL]["leaf"]

    # Map path-segment UUIDs to a connector:
    #   - each connector's own panel-column sheet-instance (its 2nd path segment)
    #   - each CIPO-enable sheet-instance ("... Enable for Pn"), read from the
    #     fan-out schematic since footprints don't store sheet names
    seg2conn = {fps[c]["colinst"]: c for c in CONNECTORS}
    sch_path = os.path.join(os.path.dirname(os.path.abspath(args.pcb)), "fan_out.kicad_sch")
    enables = enable_instances(sch_path)
    seg2conn.update(enables)
    if enables:
        print(f"CIPO-enable blocks: {len(enables)} instances -> "
              f"{sorted(set(enables.values()), key=lambda p: int(p[1:]))}")
    else:
        print(f"WARNING: no CIPO-enable sheets found (looked in {sch_path}); "
              f"placing panel-column blocks only.")

    # Group every associated footprint (panel-column + CIPO-enable) by connector,
    # keyed by hierarchical leaf UUID (shared across the repeated sub-sheets).
    columns: dict[str, dict[str, str]] = {c: {} for c in CONNECTORS}   # col -> {leaf: ref}
    for ref, f in fps.items():
        conn = next((seg2conn[s] for s in f["path"].strip("/").split("/") if s in seg2conn), None)
        if conn:
            columns[conn][f["leaf"]] = ref

    # template: relative transform of each P2 part (excluding the connector)
    anchor = fps[TEMPLATE_COL]
    template = {}   # leaf -> (rel_x, rel_y, rel_a, template_ref)
    for leaf, ref in columns[TEMPLATE_COL].items():
        if leaf == conn_leaf:
            continue
        f = fps[ref]
        lx, ly = kicad_rotate(f["x"] - anchor["x"], f["y"] - anchor["y"], -anchor["a"])
        template[leaf] = (lx, ly, f["a"] - anchor["a"], ref)
    print(f"Template = column {TEMPLATE_COL} ({fps[TEMPLATE_COL].get('leaf','')[:8]} connector), "
          f"{len(template)} parts relative to {TEMPLATE_COL}.\n")

    edits = []   # (start, end, newtext)
    for col in CONNECTORS:
        if col == TEMPLATE_COL:
            print(f"{col}: template, left unchanged.")
            continue
        cfp = fps[col]                      # this column's connector (fixed anchor)
        members = columns[col]
        placed = missing_here = 0
        samples = []
        for leaf, (lx, ly, da, tref) in template.items():
            ref = members.get(leaf)
            if not ref:
                missing_here += 1
                continue
            gx, gy = kicad_rotate(lx, ly, cfp["a"])
            nx, ny = cfp["x"] + gx, cfp["y"] + gy
            na = norm_angle(cfp["a"] + da)
            s, e = fps[ref]["span"]
            edits.append((s, e, rewrite_block(txt[s:e], nx, ny, na)))
            placed += 1
            if len(samples) < 2:
                samples.append(f"{ref}->({fmt(nx)},{fmt(ny)},{fmt(na)})")
        extra = [members[l] for l in members if l not in template and l != conn_leaf]
        note = f"  UNMATCHED template parts: {missing_here}" if missing_here else ""
        note += f"  extra-not-in-template: {extra}" if extra else ""
        print(f"{col}: connector at ({fmt(cfp['x'])},{fmt(cfp['y'])},{fmt(cfp['a'])}), "
              f"placed {placed} parts. e.g. {', '.join(samples)}{note}")

    # apply edits back-to-front so spans stay valid
    for s, e, newtext in sorted(edits, key=lambda t: t[0], reverse=True):
        txt = txt[:s] + newtext + txt[e:]

    if args.apply:
        shutil.copyfile(args.pcb, args.pcb + ".bak")
        open(args.pcb, "w", encoding="utf-8").write(txt)
        print(f"\nWrote {args.pcb} (backup at {args.pcb}.bak); {len(edits)} footprints moved.")
    else:
        print(f"\nDRY RUN -- no file written ({len(edits)} footprints would move). "
              f"Re-run with --apply to save.")


if __name__ == "__main__":
    main()
