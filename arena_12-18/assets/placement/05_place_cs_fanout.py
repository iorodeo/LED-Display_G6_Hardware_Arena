#!/usr/bin/env python3
"""Place + route the CS fan-out groups relative to their pair's midpoint connector.

Each block of four CS signals fans one Teensy chip-select out to a panel PAIR
(Pn, Pn+6) and is anchored on that pair's MIDPOINT connector P(n+3):
    CS_00..03 -> P1,P7 -> P4   (template, placed by hand)
    CS_04..07 -> P2,P8 -> P5
    CS_08..11 -> P3,P9 -> P6
    CS_12..15 -> P4,P10 -> P7
    CS_16..19 -> P5,P11 -> P8
    CS_20..23 -> P6,P12 -> P9

The CS_00..03 group (its current placement + routing) is the TEMPLATE, measured
relative to P4. Each other pair-group is reproduced relative to its own midpoint
connector with the same rigid rotate+translate 02_/03_ use (so the six groups fan
around the ring following the connector angles). Then:
  1. clear the old routing on the target groups (keeping the template's),
  2. place each target CS_(4j+k) part at template CS_k's offset from P4, applied
       relative to P(4+j),
  3. route each target group by replicating the template group's tracks through
       the same transform, nets remapped by connectivity (like 03_).
The Teensy CS series resistors are left untouched.

Default is a DRY RUN. Pass --apply to write (a .bak copy is made first).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
import uuid
from collections import defaultdict

PCB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "arena_12-18.kicad_pcb"))

GRID = 0.254
TEMPLATE_SIGNALS = [0, 1, 2, 3]    # CS_00..03 = the P1/P7 group, the template
TEMPLATE_ANCHOR = 4                # midpoint connector P4; pair j is anchored on P(4+j)
NPAIRS = 6                         # 6 pairs (j = 0..5); j = 0 is the template
CAP_MARGIN = 6.0                   # mm around a CS group when capturing / clearing routing
POWER_NETS = {"GND", "+3.3V", "+5V", "+5VP", "+3V3", "GNDA"}

AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
COORD_RE = re.compile(r"\((start|end|mid|at) (-?[\d.]+) (-?[\d.]+)((?: -?[\d.]+)?)\)")
TEXT_OWNERS = ("fp_text", "property", "gr_text")
OTHER_OWNERS = ("pad", "fp_line", "fp_circle", "fp_arc", "fp_poly", "fp_rect", "model", "footprint")


def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def snap(v: float, origin: float = 0.0) -> float:
    """Snap to the 0.254 mm grid anchored at `origin` (the local grid origin)."""
    return origin + round((v - origin) / GRID) * GRID


def norm_angle(a: float) -> float:
    a %= 360.0
    return a + 360.0 if a < 0 else a


def krot(x: float, y: float, deg: float):
    r = math.radians(-deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


def owner_before(block: str, pos: int):
    best, bi = None, -1
    for kw in TEXT_OWNERS + OTHER_OWNERS:
        i = block.rfind("(" + kw + " ", 0, pos)
        if i > bi:
            bi, best = i, kw
    return best


def rewrite_fp(block: str, nx: float, ny: float, nrot: float) -> str:
    """Set a footprint (at) and shift pad/text absolute angles by the delta."""
    fp = AT_RE.search(block)
    delta = nrot - float(fp.group(3) or 0.0)
    out = [block[:fp.start()], f"(at {fmt(nx)} {fmt(ny)} {fmt(norm_angle(nrot))})"]
    pos = fp.end()
    for m in AT_RE.finditer(block, pos):
        out.append(block[pos:m.start()])
        pos = m.end()
        ow = owner_before(block, m.start())
        if ow in TEXT_OWNERS or ow == "pad":
            if m.group(3) is None and abs(delta) < 1e-9:
                out.append(m.group(0))
            else:
                base = float(m.group(3)) if m.group(3) is not None else 0.0
                out.append(f"(at {m.group(1)} {m.group(2)} {fmt(norm_angle(base + delta))})")
        else:
            out.append(m.group(0))
    out.append(block[pos:])
    return "".join(out)


def parse(txt):
    """footprints (ref -> {span,x,y,a,leaf,cs,pads}) and tracks (list)."""
    sch = open(os.path.join(os.path.dirname(os.path.abspath(ARGS.pcb)),
                            "fan_out.kicad_sch"), encoding="utf-8").read()
    cs_by_uuid = {}
    for m in re.finditer(r"\(sheet\b", sch):
        blk = sch[m.start():m.start() + 1600]
        nm = re.search(r'\(property "Sheetname" "([^"]*)"', blk)
        uu = re.search(r'\(uuid "([^"]+)"\)', blk)
        if nm and uu:
            mm = re.search(r"CS_(\d+) .*fan ?out", nm.group(1), re.I)
            if mm:
                cs_by_uuid[uu.group(1)] = int(mm.group(1))

    fps = {}
    for m in re.finditer(r"\n\t\(footprint ", txt):
        s = m.start() + 1
        e = txt.find("\n\t(footprint ", m.end())
        b = txt[s:(e if e != -1 else len(txt))]
        ref = re.search(r'\(property "Reference" "([^"]+)"', b)
        if not ref:
            continue
        at = AT_RE.search(b)
        fx, fy, fa = float(at.group(1)), float(at.group(2)), float(at.group(3) or 0.0)
        path = re.search(r'\(path "([^"]*)"', b)
        seg = path.group(1).strip("/").split("/") if path else []
        pads = []
        for pm in re.finditer(r'\(pad "([^"]+)"[^\n]*\n\s*\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\).*?\(net "([^"]*)"\)', b, re.S):
            gx, gy = krot(float(pm.group(2)), float(pm.group(3)), fa)
            pads.append((pm.group(1), fx + gx, fy + gy, pm.group(5)))
        fps[ref.group(1)] = dict(span=(s, e if e != -1 else len(txt)), x=fx, y=fy, a=fa,
                                 leaf=seg[-1] if seg else None,
                                 cs=next((cs_by_uuid[x] for x in seg if x in cs_by_uuid), None),
                                 pads=pads)

    tracks = []
    for m in re.finditer(r"\n\t\((?:segment|via|arc)\b", txt):
        s = m.start() + 1
        e = txt.find("\n\t)", m.end()) + len("\n\t)")
        blk = txt[s:e]
        net = re.search(r'\(net "([^"]*)"\)', blk)
        pts = [(float(a), float(b)) for _, a, b, _ in COORD_RE.findall(blk)]
        tracks.append(dict(s=s, e=e, text=blk, net=net.group(1) if net else None, pts=pts))
    return fps, tracks


def dedup_tracks(txt):
    """Collapse exactly-coincident copper (same type, net, layer, rounded coords,
    width/size). Keeps re-runs idempotent: the power stubs a group's routing carries
    are replicated every run and remapped to the shared power net (so deletion by
    signal net can't remove them); dedup drops the redundant copies. Returns
    (new_txt, removed)."""
    seen, dels = set(), []
    for m in re.finditer(r"\n\t\((segment|via|arc)\b", txt):
        s = m.start() + 1
        e = txt.find("\n\t)", m.end()) + len("\n\t)")
        blk = txt[s:e]
        mnet = re.search(r'\(net "([^"]*)"\)', blk)
        pts = tuple(sorted((round(float(a), 3), round(float(b), 3))
                           for _, a, b, _ in COORD_RE.findall(blk)))
        mlay = re.search(r'\(layers?\s+([^)]*)\)', blk)
        mw = re.search(r'\(width ([\d.]+)\)', blk) or re.search(r'\(size ([\d.]+)\)', blk)
        key = (m.group(1), mnet.group(1) if mnet else "", pts,
               mlay.group(1).strip() if mlay else "", mw.group(1) if mw else "")
        if key in seen:
            dels.append((s - 1, e))
        else:
            seen.add(key)
    for s0, e in sorted(dels, reverse=True):
        txt = txt[:s0] + txt[e:]
    return txt, len(dels)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default=PCB)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ARGS = ap.parse_args()

    txt = open(ARGS.pcb, encoding="utf-8").read()
    fps, tracks = parse(txt)

    # ---- per-CS-group membership + pad nets -------------------------------
    csgrp = defaultdict(dict)        # cs -> {leaf: ref}
    colpads = defaultdict(dict)      # cs -> {(leaf,pad): net}
    for ref, f in fps.items():
        if f["cs"] is not None:
            csgrp[f["cs"]][f["leaf"]] = ref
            for pn, x, y, net in f["pads"]:
                colpads[f["cs"]][(f["leaf"], pn)] = net

    # ---- template: CS_00..03 group measured relative to connector P4 -------
    p4 = fps[f"P{TEMPLATE_ANCHOR}"]
    template = {}                    # (cs_k, leaf) -> (rel_x, rel_y, rel_a)  in P4's frame
    for k in TEMPLATE_SIGNALS:
        for leaf, ref in csgrp.get(k, {}).items():
            f = fps[ref]
            lx, ly = krot(f["x"] - p4["x"], f["y"] - p4["y"], -p4["a"])
            template[(k, leaf)] = (lx, ly, f["a"] - p4["a"])

    # net -> (template cs, leaf, pad); lets any template-group net be remapped to
    # the same pad's net in the target sub-signal (power nets are kept as-is).
    net_src = {}
    for k in TEMPLATE_SIGNALS:
        for (leaf, pad), net in colpads.get(k, {}).items():
            if net:
                net_src.setdefault(net, (k, leaf, pad))
    def remap(j, net):
        if net is None or net in POWER_NETS:
            return net
        src = net_src.get(net)
        if not src:
            return None
        k, leaf, pad = src
        return colpads.get(4 * j + k, {}).get((leaf, pad))

    # routing template = the CS_00..03 group's own tracks. A track belongs to it if
    # it carries one of the group's signal nets (unique per CS, taken net-wide) or
    # it is power (GND/+3.3V) touching a group pad -- so we get the group's routing
    # and its power stubs, but not a neighbour's trace that merely passes nearby.
    tmpl_pads = [(x, y) for k in TEMPLATE_SIGNALS for leaf, ref in csgrp.get(k, {}).items()
                 for _, x, y, _ in fps[ref]["pads"]]
    def touches(pts):
        return any(min((math.hypot(px - gx, py - gy) for gx, gy in tmpl_pads), default=1e9) < 0.4
                   for px, py in pts)
    # Internal wiring nets (Net-(R..-Pad1), unique + fully local to the group) are
    # taken net-wide; power (GND/+3.3V) only where it touches a group pad (the
    # decoupling stubs). The long inter-block signals (PAN.CS_*, TNY.CS_*) run off
    # to the panels/Teensy and are NOT replicated -- route those separately.
    def local_net(net):
        return net in net_src and net.startswith("Net-(")
    tmpl_tracks = [t for t in tracks if t["pts"] and (
        local_net(t["net"]) or (t["net"] in POWER_NETS and touches(t["pts"])))]
    print(f"template CS_00..03 @ P{TEMPLATE_ANCHOR}: {len(template)} parts, "
          f"routing template = {len(tmpl_tracks)} tracks "
          f"on {sorted(set(t['net'] for t in tmpl_tracks) - POWER_NETS)}")

    edits = []          # (start, end, newtext)  -- footprint rewrites + track deletions
    additions = []      # new track blocks

    # ---- 1) clear old routing on the target groups (keep the template's) ---
    # only their INTERNAL wiring (Net-(R..-Pad1)) -- the same nets we re-route --
    # so a neighbour's copper and the long PAN.CS_*/TNY.CS_* signals are left alone.
    target_nets = {net for j in range(1, NPAIRS) for k in range(4)
                   for net in colpads.get(4 * j + k, {}).values()
                   if net and net.startswith("Net-(")}
    deleted = 0
    for t in tracks:
        if t["net"] in target_nets:
            edits.append((t["s"] - 1, t["e"], ""))
            deleted += 1

    # ---- 2) place + 3) route each target pair relative to its midpoint conn -
    def make_tp(conn):
        def tp(x, y):
            lx, ly = krot(x - p4["x"], y - p4["y"], -p4["a"])
            gx, gy = krot(lx, ly, conn["a"])
            return conn["x"] + gx, conn["y"] + gy
        return tp

    placed = routed = skipped = 0
    for j in range(1, NPAIRS):
        conn = fps[f"P{TEMPLATE_ANCHOR + j}"]
        tp = make_tp(conn)
        for (k, leaf), (lx, ly, da) in template.items():
            ref = csgrp.get(4 * j + k, {}).get(leaf)
            if not ref:
                continue
            gx, gy = krot(lx, ly, conn["a"])
            s, e = fps[ref]["span"]
            edits.append((s, e, rewrite_fp(txt[s:e], conn["x"] + gx, conn["y"] + gy,
                                           norm_angle(conn["a"] + da))))
            placed += 1
        for t in tmpl_tracks:
            newnet = remap(j, t["net"])
            if t["net"] is not None and newnet is None:
                skipped += 1
                continue
            blk = COORD_RE.sub(lambda m: f"({m.group(1)} "
                               + " ".join(fmt(v) for v in tp(float(m.group(2)), float(m.group(3))))
                               + f"{m.group(4)})", t["text"])
            if newnet is not None:
                blk = re.sub(r'\(net "[^"]*"\)', f'(net "{newnet}")', blk, count=1)
            blk = re.sub(r'\(uuid "[^"]*"\)', f'(uuid "{uuid.uuid4()}")', blk, count=1)
            additions.append(blk)
            routed += 1

    print(f"CS groups: deleted {deleted} old tracks, placed {placed} footprints in "
          f"{NPAIRS - 1} pairs (P5..P{TEMPLATE_ANCHOR + NPAIRS - 1}), routed {routed} tracks "
          f"({skipped} skipped).")

    for s, e, newtext in sorted(edits, key=lambda t: t[0], reverse=True):
        txt = txt[:s] + newtext + txt[e:]
    idx = txt.rstrip().rfind(")")
    txt = txt[:idx] + "\n".join(additions) + "\n" + txt[idx:]

    txt, ndup = dedup_tracks(txt)
    if ndup:
        print(f"Dedup: removed {ndup} exactly-coincident (stacked) tracks.")

    if ARGS.apply:
        shutil.copyfile(ARGS.pcb, ARGS.pcb + ".bak")
        open(ARGS.pcb, "w", encoding="utf-8").write(txt)
        print(f"\nWrote {ARGS.pcb} (backup at {ARGS.pcb}.bak).")
    else:
        print(f"\nDRY RUN -- no file written. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
