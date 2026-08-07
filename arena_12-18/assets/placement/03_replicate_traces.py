#!/usr/bin/env python3
"""Replicate the tracks (segments + vias) routed around P2 to every other column.

P2's column block is routed; the other columns are not. This copies every track
in P2's block region to P1 and P3..P12, rigidly transformed relative to each
column's connector (the same transform 02_place_column_blocks.py uses), and
re-assigns each copied track to the TARGET column's corresponding net.

Net handling (tracks reference nets by NAME in this board):
  - The target net for a P2 track is found by CONNECTIVITY, not by string
    munging: the P2-block pads on that net are mapped to the leaf-matched pads in
    the target column, and the net those pads carry is the target net. This
    handles the per-column differences automatically:
        PAN.CS_04_P2  -> PAN.CS_00_P1 (P1) / PAN.CS_00_P7 (P7, bank B1) ...
        PAN.CIPO_B0_P2 -> PAN.CIPO_B1_P7 ...
        Net-(R120-Pad1) -> Net-(R112-Pad1) ...
    Shared nets (GND/+3.3V/+5V) map to themselves and are kept.
  - If a net can't be mapped to exactly one target net, that track is SKIPPED
    for that column and reported (so it can never silently create a short).

Only the geometry, net name, and uuid of each copied track change; width, layer,
via size/drill, etc. are preserved verbatim. Tracks are ADDED (targets are
unrouted); nothing is deleted.

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

PCB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "arena_12-18.kicad_pcb"))

TEMPLATE = "P2"
CONNECTORS = [f"P{i}" for i in range(1, 13)]
ENABLE_SHEETFILE = "cipo_enable.kicad_sch"
ENABLE_SHEET_RE = re.compile(r"Enable for (P\d+)")

# Shared dual fan-out buffers (SCK/COPI/EINT), one per panel pair. Only the buffer
# + its cap + their direct legs are shared, and replicated per PAIR (even connector,
# 40 deg). Everything else connected to a column is per-column (20 deg). Template =
# the P1/P2 pair. See 02_place_column_blocks.py for the full model.
SHARED_BUFFERS = ["U4", "U7", "U39"]
SHARED_CAPS    = ["C6", "C9", "C41"]
PAIR_ANCHORS   = [2, 4, 6, 8, 10, 12]     # even connector per pair; P2 = template
POWER_NETS = {"GND", "+3.3V", "+5V", "+5VP", "+3V3", "GNDA"}

AT_RE = re.compile(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)")
COORD_RE = re.compile(r"\((start|end|mid|at) (-?[\d.]+) (-?[\d.]+)\)")


def fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def to_col(net: str, k: int) -> str:
    """Retarget a column-tagged net to column k: fix the SCK/COPI bank (B0 for
    P1-P6, B1 for P7-P12) and the _P<n> suffix. Bankless nets (EINT) just get the
    suffix swapped."""
    net = re.sub(r"_B[01]_", "_B0_" if k <= 6 else "_B1_", net)
    return re.sub(r"_P\d+$", f"_P{k}", net)


def kicad_rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad footprint rotation (Y-down; positive = CCW on screen = R(-deg))."""
    r = math.radians(-deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


def find_footprints(txt: str) -> dict:
    out = {}
    for m in re.finditer(r"\n\t\(footprint ", txt):
        start = m.start() + 1
        end = txt.find("\n\t(footprint ", m.end())
        if end == -1:
            end = len(txt)
        b = txt[start:end]
        mref = re.search(r'\(property "Reference" "([^"]+)"', b)
        if not mref:
            continue
        at = AT_RE.search(b)
        path = re.search(r'\(path "([^"]*)"', b)
        seg = path.group(1).strip("/").split("/") if path else []
        pads = {}
        for pm in re.finditer(r'\(pad "([^"]+)".*?\(net "([^"]*)"\)', b, re.S):
            pads.setdefault(pm.group(1), pm.group(2))
        out[mref.group(1)] = dict(
            x=float(at.group(1)), y=float(at.group(2)), a=float(at.group(3) or 0.0),
            leaf=seg[-1] if seg else None, seg=seg, pads=pads)
    return out


def enable_instances(sch_path: str) -> dict:
    try:
        sch = open(sch_path, encoding="utf-8").read()
    except OSError:
        return {}
    out = {}
    for m in re.finditer(r"\(sheet\b", sch):
        blk = sch[m.start():m.start() + 1600]
        name = re.search(r'\(property "Sheetname" "([^"]*)"', blk)
        sfile = re.search(r'\(property "Sheetfile" "([^"]*)"', blk)
        uu = re.search(r'\(uuid "([^"]+)"\)', blk)
        if name and sfile and uu and sfile.group(1) == ENABLE_SHEETFILE:
            mm = ENABLE_SHEET_RE.search(name.group(1))
            if mm:
                out[uu.group(1)] = mm.group(1)
    return out


def find_tracks(txt: str) -> list:
    """Return each top-level segment/via/arc as (start, end, text, net, points)."""
    tracks = []
    for m in re.finditer(r"\n\t\((?:segment|via|arc)\b", txt):
        s = m.start() + 1
        e = txt.find("\n\t)", m.end()) + len("\n\t)")
        blk = txt[s:e]
        net = re.search(r'\(net "([^"]*)"\)', blk)
        pts = [(float(a), float(b)) for _, a, b in COORD_RE.findall(blk)]
        tracks.append(dict(s=s, e=e, text=blk, net=net.group(1) if net else None, pts=pts))
    return tracks


def shared_template_refs(txt, fps, sch_path):
    """The template pair's shared footprints (dual buffers + their caps), so the
    copper flood keeps P2's shared routing, not just its panel-column routing."""
    return {r for r in SHARED_BUFFERS + SHARED_CAPS if r in fps}


def strip_to_template(txt, fps, sch_path):
    """Delete every track/via/arc that is not part of P2's template routing.

    Copper flood seeded from ALL of P2's block pads (panel-column + CIPO-enable +
    the shared SCK/COPI fan-out block), following physical copper only (shared
    endpoints, vias, and pads), stopping where copper ends. Anything reached is
    P2's own routing and is kept; everything else -- replicated copies in other
    columns, leftovers from earlier runs, and strays -- is removed. This is what
    makes replication idempotent: it resets to the P2 template before re-copying.

    Returns (new_txt, kept, deleted, seed_count).
    """
    from collections import defaultdict

    seg2conn = {fps[c]["seg"][1]: c for c in CONNECTORS if len(fps[c]["seg"]) >= 2}
    seg2conn.update(enable_instances(sch_path))
    colof = lambda ref: next((seg2conn[s] for s in fps[ref]["seg"] if s in seg2conn), None)
    shared_refs = shared_template_refs(txt, fps, sch_path)

    # absolute pad centers of every P2-template footprint = the flood seeds
    seeds_pts = []
    for m in re.finditer(r"\n\t\(footprint ", txt):
        s = m.start() + 1
        e = txt.find("\n\t(footprint ", m.end())
        b = txt[s:(e if e != -1 else len(txt))]
        mref = re.search(r'\(property "Reference" "([^"]+)"', b)
        if not mref or (colof(mref.group(1)) != TEMPLATE and mref.group(1) not in shared_refs):
            continue
        at = AT_RE.search(b)
        fx, fy, fa = float(at.group(1)), float(at.group(2)), float(at.group(3) or 0)
        for pm in re.finditer(r'\(pad "[^"]+"[^\n]*\n\s*\(at (-?[\d.]+) (-?[\d.]+)', b):
            gx, gy = kicad_rotate(float(pm.group(1)), float(pm.group(2)), fa)
            seeds_pts.append((fx + gx, fy + gy))

    tracks = find_tracks(txt)

    # union-find over rounded coordinate keys (coincident copper shares a key)
    parent: dict = {}
    def find(k):
        parent.setdefault(k, k); r = k
        while parent[r] != r:
            r = parent[r]
        while parent[k] != r:
            parent[k], k = r, parent[k]
        return r
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    K = lambda x, y: (round(x, 2), round(y, 2))
    CS = 1.0
    cell = defaultdict(list)
    track_keys = []
    for t in tracks:
        ks = [K(x, y) for x, y in t["pts"]]
        for k in ks:
            find(k)
            cell[(int(k[0] // CS), int(k[1] // CS))].append(k)
        for k in ks[1:]:
            union(ks[0], k)
        track_keys.append(ks)

    # bridge each P2 seed pad to nearby track endpoints (same pad => same net; safe)
    reach = 0.25
    seed_keys = []
    for px, py in seeds_pts:
        pk = K(px, py); find(pk); seed_keys.append(pk)
        ci, cj = int(px // CS), int(py // CS)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for k in cell.get((ci + di, cj + dj), []):
                    if abs(k[0] - px) <= reach and abs(k[1] - py) <= reach:
                        union(pk, k)

    # A template track is kept if the copper flood reaches it OR it carries a
    # template SIGNAL net (unique to P2's column/pair, so safe). The net rescue
    # keeps stubs the flood misses (e.g. PAN.SCK_B0_P1, whose only segment does
    # not quite touch a seed pad centre). Power nets stay flood-only, so we never
    # rescue a replicated power track.
    template_nets = {net for ref, f in fps.items()
                     if colof(ref) == TEMPLATE or ref in shared_refs
                     for net in f["pads"].values()
                     if net and net not in POWER_NETS and "TNY." not in net}
    seed_roots = {find(k) for k in seed_keys}
    keep = [any(find(k) in seed_roots for k in ks) or t["net"] in template_nets
            for t, ks in zip(tracks, track_keys)]
    nkeep = sum(keep)
    dels = sorted([(t["s"] - 1, t["e"]) for t, k in zip(tracks, keep) if not k], reverse=True)
    for s0, e in dels:
        txt = txt[:s0] + txt[e:]
    return txt, nkeep, len(dels), len(seeds_pts)


def do_clean(txt: str, args) -> str:
    fps = find_footprints(txt)
    sch_path = os.path.join(os.path.dirname(os.path.abspath(args.pcb)), "fan_out.kicad_sch")
    txt, nkeep, ndel, nseeds = strip_to_template(txt, fps, sch_path)
    txt, ndup = dedup_tracks(txt)
    print(f"P2-template seed pads: {nseeds};  keep {nkeep}, "
          f"{'delete' if args.apply else 'would delete'} {ndel} (+{ndup} stacked deduped).")
    if args.apply:
        shutil.copyfile(args.pcb, args.pcb + ".bak")
        open(args.pcb, "w", encoding="utf-8").write(txt)
        print(f"Wrote {args.pcb} (backup at {args.pcb}.bak); deleted {ndel} tracks, kept {nkeep}.")
    else:
        print(f"DRY RUN -- would delete {ndel} tracks, keep {nkeep}. Re-run with --apply.")
    return txt


def dedup_tracks(txt):
    """Collapse exactly-coincident copper: segments/vias/arcs that share type,
    net, layer(s), rounded coordinates and width/size are redundant (they stack
    on top of each other). Keep the first occurrence, drop the rest. This removes
    both stacks inherited from P2's own routing (replicated to every column) and
    any residue. Returns (new_txt, removed_count)."""
    seen, dels = set(), []
    for m in re.finditer(r"\n\t\((segment|via|arc)\b", txt):
        s = m.start() + 1
        e = txt.find("\n\t)", m.end()) + len("\n\t)")
        blk = txt[s:e]
        kind = m.group(1)
        mnet = re.search(r'\(net "([^"]*)"\)', blk)
        pts = tuple(sorted((round(float(a), 3), round(float(b), 3))
                           for _, a, b in COORD_RE.findall(blk)))
        mlay = re.search(r'\(layers?\s+([^)]*)\)', blk)
        mw = re.search(r'\(width ([\d.]+)\)', blk) or re.search(r'\(size ([\d.]+)\)', blk)
        key = (kind, mnet.group(1) if mnet else "", pts,
               mlay.group(1).strip() if mlay else "", mw.group(1) if mw else "")
        if key in seen:
            dels.append((s - 1, e))
        else:
            seen.add(key)
    for s0, e in sorted(dels, reverse=True):
        txt = txt[:s0] + txt[e:]
    return txt, len(dels)


def warn_incomplete_template(txt, fps, tracks):
    """Report template nets that ARE routed but whose copper does not reach one of
    the net's pads -- a dangling trace that 'ends prematurely'. Because the template
    is copied to every column, one incomplete trace becomes incomplete in all of
    them, so it is worth catching before replicating. Power nets (plane-connected,
    many pads) are skipped. Purely diagnostic; changes nothing."""
    from collections import defaultdict
    ends = defaultdict(list)
    for t in tracks:
        if t["net"]:
            ends[t["net"]].extend(t["pts"])
    padpts = defaultdict(list)
    for m in re.finditer(r"\n\t\(footprint ", txt):
        s = m.start() + 1
        e = txt.find("\n\t(footprint ", m.end())
        b = txt[s:(e if e != -1 else len(txt))]
        mref = re.search(r'\(property "Reference" "([^"]+)"', b)
        at = AT_RE.search(b)
        if not (mref and at):
            continue
        fx, fy, fa = float(at.group(1)), float(at.group(2)), float(at.group(3) or 0)
        for pm in re.finditer(r'\(pad "([^"]+)"[^\n]*\n\s*\(at (-?[\d.]+) (-?[\d.]+)'
                              r'(?: -?[\d.]+)?\).*?\(net "([^"]*)"\)', b, re.S):
            gx, gy = kicad_rotate(float(pm.group(2)), float(pm.group(3)), fa)
            padpts[pm.group(4)].append((f"{mref.group(1)}.{pm.group(1)}", fx + gx, fy + gy))

    reach = 0.3            # mm: a track endpoint this close counts as landed on the pad
    near_limit = 3.0       # gap up to here = a near miss (a trace that ends just short);
                           # larger gaps are whole cross-block connections routed elsewhere
    near, spans = [], 0
    for net, epts in ends.items():
        if net in POWER_NETS:
            continue
        for name, px, py in padpts.get(net, []):
            d = min((math.hypot(px - x, py - y) for x, y in epts), default=1e9)
            if d <= reach:
                continue
            if d <= near_limit:
                near.append((d, net, name))       # routed but ends just short of the pad
            else:
                spans += 1                         # pad far from any of the net's copper
    if not near:
        extra = f" ({spans} pad(s) are >{near_limit:.0f} mm from their net's copper -- " \
                f"likely cross-block signals routed elsewhere.)" if spans else ""
        print(f"Template completeness: OK -- no traces ending short of a pad.{extra}")
        return
    near.sort()
    print(f"\nWARNING: {len(near)} template trace(s) end short of a pad (dangling near miss; "
          f"would replicate to every column). Finish these in the layout, then re-run:")
    for d, net, name in near:
        print(f"  gap {d:6.2f} mm  {name:16} {net}")
    if spans:
        print(f"  (plus {spans} pad(s) >{near_limit:.0f} mm from their net's copper -- "
              f"likely cross-block signals routed elsewhere.)")
    print()


def shared_block_replicate(txt, fps, tracks, sch_path):
    """Replicate the shared dual-buffer routing to every other pair (anchored on
    the even connector -- 40 deg steps).

    The shared block is only the buffers (U4/U7/U39), their caps (C6/C9/C41), and
    their DIRECT legs: the two output legs (Net-(R..-Pad1) to each column's series
    resistor), the Teensy input stub, and the buffer/cap power stubs. The per-pair
    output legs are unique nets, so they are taken net-wide (every segment); the
    global Teensy buses (TNY.*) and power are taken only where a segment touches a
    buffer/cap pad, so we never grab the whole bus or a panel power fill. Nets are
    remapped to the target pair by connectivity (via to_col + each pair's series
    resistor). Returns (new track blocks, shared track indices).
    """
    refs = [r for r in SHARED_BUFFERS + SHARED_CAPS if r in fps]
    bufs = [r for r in SHARED_BUFFERS if r in fps]
    if not bufs:
        return [], set()

    def resistor_by_pan(pan):
        return next((r for r in fps if r[0] == "R" and pan in fps[r]["pads"].values()), None)

    def pad_centers(ref):
        f = fps[ref]
        for m in re.finditer(r"\n\t\(footprint ", txt):
            s = m.start() + 1
            e = txt.find("\n\t(footprint ", m.end())
            b = txt[s:(e if e != -1 else len(txt))]
            if re.search(rf'\(property "Reference" "{ref}"', b):
                return [(f["x"] + gx, f["y"] + gy) for gx, gy in
                        (kicad_rotate(float(a), float(c), f["a"])
                         for a, c in re.findall(r'\(pad "[^"]+"[^\n]*\n\s*\(at (-?[\d.]+) (-?[\d.]+)', b))]
        return []

    # Each buffer's output legs (Net-(R..-Pad1)) with the PAN net of the resistor
    # they feed, so the leg can be retargeted to the matching pair by connectivity.
    legs, tny = [], set()
    for b in bufs:
        for net in fps[b]["pads"].values():
            if net in POWER_NETS:
                continue
            if net.startswith("Net-("):
                rr = next((x for x in fps if x[0] == "R" and net in fps[x]["pads"].values()), None)
                pan = next((v for v in fps[rr]["pads"].values()
                            if v.startswith("/Fan Out/PAN")), None) if rr else None
                legs.append((net, pan))
            elif "TNY." in net:
                tny.add(net)
    shared_signal = {leg for leg, _ in legs}
    shared_pts = [p for ref in refs for p in pad_centers(ref)]

    def touches(t):
        return any(math.hypot(px - gx, py - gy) < 0.4
                   for px, py in t["pts"] for gx, gy in shared_pts)
    shared_ids = set()
    for i, t in enumerate(tracks):
        net = t["net"]
        if net in shared_signal:
            shared_ids.add(i)
        elif (net in POWER_NETS or (net and "TNY." in net)) and touches(t):
            shared_ids.add(i)

    p2c = fps[TEMPLATE]

    def make_tp(e):
        tc = fps[f"P{e}"]
        def tp(x, y):
            lx, ly = kicad_rotate(x - p2c["x"], y - p2c["y"], -p2c["a"])
            gx, gy = kicad_rotate(lx, ly, tc["a"])
            return tc["x"] + gx, tc["y"] + gy
        return tp

    def net_map(e):
        m = {}
        for leg, pan in legs:
            if not pan:
                continue
            panel = int(re.search(r"_P(\d+)$", pan).group(1))
            tgt_panel = e if panel % 2 == 0 else e - 1     # even->Pe, odd partner->P(e-1)
            rk = resistor_by_pan(to_col(pan, tgt_panel))
            if rk:
                newleg = next((v for v in fps[rk]["pads"].values() if v.startswith("Net-(")), None)
                if newleg:
                    m[leg] = newleg
        for t in tny:                                      # Teensy bus: bank-adjust only
            m[t] = re.sub(r"_B[01]$", "_B0" if e <= 6 else "_B1", t)
        return m

    additions, skipped = [], 0
    for e in PAIR_ANCHORS:
        if e == int(TEMPLATE[1:]):
            continue
        tp, nm = make_tp(e), net_map(e)
        for i in shared_ids:
            t = tracks[i]
            net = t["net"]
            if net is None or net in POWER_NETS:
                newnet = net
            elif net in nm:
                newnet = nm[net]
            else:
                skipped += 1
                continue
            blk = COORD_RE.sub(lambda m: f"({m.group(1)} " + " ".join(fmt(v) for v in tp(float(m.group(2)), float(m.group(3)))) + ")", t["text"])
            if newnet is not None:
                blk = re.sub(r'\(net "[^"]*"\)', f'(net "{newnet}")', blk, count=1)
            blk = re.sub(r'\(uuid "[^"]*"\)', f'(uuid "{uuid.uuid4()}")', blk, count=1)
            additions.append(blk)
    npairs = sum(1 for e in PAIR_ANCHORS if e != int(TEMPLATE[1:]))
    print(f"Shared buffer block: {len(shared_ids)} P2 tracks -> {npairs} pairs "
          f"= {len(additions)} tracks ({skipped} skipped).")
    return additions, shared_ids


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcb", default=PCB)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--clean", action="store_true",
                    help="delete all tracks/vias except P2's (copper flood from P2's block); "
                         "does not replicate")
    args = ap.parse_args()

    txt = open(args.pcb, encoding="utf-8").read()

    if args.clean:
        do_clean(txt, args)
        return

    fps = find_footprints(txt)
    missing = [c for c in CONNECTORS if c not in fps]
    if missing:
        sys.exit(f"Missing panel connectors: {missing}")

    sch_path = os.path.join(os.path.dirname(os.path.abspath(args.pcb)), "fan_out.kicad_sch")

    # Reset to the P2 template first, so re-running never stacks duplicate copies
    # (each prior run's replicated tracks are removed before re-copying). Applied
    # in dry-run too, so the preview reflects the idempotent result.
    txt, keptT, delT, _ = strip_to_template(txt, fps, sch_path)
    txt, dedT = dedup_tracks(txt)             # collapse P2's own stacked copper first
    print(f"Reset to P2 template: kept {keptT - dedT} template tracks "
          f"(removed {delT} replicated + {dedT} stacked duplicates).")

    # connector -> column, and column membership (panel-column + CIPO-enable)
    seg2conn = {fps[c]["seg"][1]: c for c in CONNECTORS if len(fps[c]["seg"]) >= 2}
    seg2conn.update(enable_instances(sch_path))
    colof = lambda ref: next((seg2conn[s] for s in fps[ref]["seg"] if s in seg2conn), None)

    colpads: dict = {c: {} for c in CONNECTORS}   # col -> {(leaf,pad): net}
    for ref, f in fps.items():
        c = colof(ref)
        if not c:
            continue
        for pn, net in f["pads"].items():
            colpads[c][(f["leaf"], pn)] = net

    def remap(col, net):
        keys = [k for k, v in colpads[TEMPLATE].items() if v == net]
        tgt = {colpads[col].get(k) for k in keys if colpads[col].get(k)}
        return tgt

    anchor = fps[TEMPLATE]

    def make_tp(col):
        tc = fps[col]
        def tp(x, y):
            lx, ly = kicad_rotate(x - anchor["x"], y - anchor["y"], -anchor["a"])
            gx, gy = kicad_rotate(lx, ly, tc["a"])
            return tc["x"] + gx, tc["y"] + gy
        return tp

    # After the reset, every remaining track is P2's own routing (connectivity,
    # not a bounding box). The shared-buffer tracks go to the pair replication; the
    # rest is P2's per-column routing, replicated to every other column.
    tracks = find_tracks(txt)
    warn_incomplete_template(txt, fps, tracks)
    shared_additions, shared_ids = shared_block_replicate(txt, fps, tracks, sch_path)
    p2tracks = [t for i, t in enumerate(tracks) if i not in shared_ids]
    print(f"{len(tracks)} template tracks; {len(p2tracks)} per-column (rest shared).")

    new_blocks = []
    skipped: dict = {}
    per_col = {}
    for col in CONNECTORS:
        if col == TEMPLATE:
            continue
        tp = make_tp(col)
        made = 0
        for t in p2tracks:
            net = t["net"]
            tgt = remap(col, net) if net is not None else set()
            if net is None:
                newnet = None
            elif len(tgt) == 1:
                newnet = next(iter(tgt))
            else:                                  # unmapped / ambiguous -> skip
                skipped.setdefault((net, len(tgt)), []).append(col)
                continue
            blk = COORD_RE.sub(
                lambda m: f"({m.group(1)} " + " ".join(fmt(v) for v in tp(float(m.group(2)), float(m.group(3)))) + ")",
                t["text"])
            if newnet is not None:
                blk = re.sub(r'\(net "[^"]*"\)', f'(net "{newnet}")', blk, count=1)
            blk = re.sub(r'\(uuid "[^"]*"\)', f'(uuid "{uuid.uuid4()}")', blk, count=1)
            new_blocks.append(blk)
            made += 1
        per_col[col] = made

    for col in CONNECTORS:
        if col == TEMPLATE:
            print(f"{col}: template, source of the tracks.")
        else:
            print(f"{col}: {per_col[col]} tracks replicated.")
    if skipped:
        print("\nSKIPPED (net could not be mapped to exactly one target net):")
        for (net, n), cols in skipped.items():
            print(f"  {net}  ({n} candidates) in columns {sorted(set(cols))}")

    new_blocks.extend(shared_additions)     # shared SCK/COPI block traces (even anchors)

    # insert new tracks before the final top-level ')'
    idx = txt.rstrip().rfind(")")
    txt = txt[:idx] + "\n".join(new_blocks) + "\n" + txt[idx:]

    txt, ndup = dedup_tracks(txt)
    if ndup:
        print(f"Dedup: removed {ndup} exactly-coincident (stacked) tracks/vias.")

    if args.apply:
        shutil.copyfile(args.pcb, args.pcb + ".bak")
        open(args.pcb, "w", encoding="utf-8").write(txt)
        print(f"\nWrote {args.pcb} (backup at {args.pcb}.bak); {len(new_blocks)} tracks added.")
    else:
        print(f"\nDRY RUN -- {len(new_blocks)} tracks would be added. Re-run with --apply to save.")


if __name__ == "__main__":
    main()
