#!/usr/bin/env python3
"""
_build_precise_coastline.py

One-off generator, not part of the regular pipeline -- run by hand whenever
ireland_counties.json or ireland_coastline.json change. Writes
ireland_coastline_precise.json: the SAME rings as ireland_coastline.json's
closed (landmass) rings, but with detail spliced in from
ireland_counties.json wherever available. One continuous line per
landmass -- not a second, independently-drawn line -- so there's no
ghosting where the two datasets disagree by a pixel or two, and no
orphan dots from fragments that don't connect to anything.

Only for DRAWING (see B_ireland_radar_greyscale.py's
load_precise_coastline()) -- ireland_coastline.json itself still drives the
land mask that keeps ship trails off land.

WHY THIS EXISTS
    ireland_counties.json digitizes each county's own boundary far more
    finely than ireland_coastline.json digitizes the coastline as a whole
    (confirmed against a real capture: ~11x the vertex density in the
    Dublin Bay area alone).

METHOD
    1. Candidate detail: every edge in ireland_counties.json's rings is
       counted. An edge shared by two adjacent counties (an inland
       administrative border, OR a shared river/estuary boundary -- there's
       no attribute in this data to tell those apart) appears exactly
       twice; an edge on the true exterior (facing the sea, or -- since
       this dataset only covers the Republic's 26 counties -- facing
       Northern Ireland) appears exactly once. An earlier version of this
       script used ONLY singleton edges, which excluded real coastline
       wherever a county boundary follows a river inland to the sea (e.g.
       the Boyne estuary) -- confirmed by an actual render showing exactly
       that gap. This version doesn't try to classify doubled edges at
       all; it sidesteps the question in step 2 instead.
    2. Singleton edges are chained into polylines (open chains between two
       county-border junctions, or closed loops for small islands bordering
       no other county). Each chain's two endpoints are matched against
       ireland_coastline.json's own closed rings by nearest point ON the
       ring's path (not nearest vertex -- a real junction rarely lands
       exactly on an existing coarse vertex). Confirmed against a real
       capture: median match distance is 0km (many chains' endpoints sit
       exactly on the existing path) and 90% are within ~1km --
       MAX_MATCH_DISTANCE_KM (2) sits just above that with margin.
    3. A chain matched on BOTH ends to the same base ring gets SPLICED in:
       the base ring's own points between those two matched positions are
       replaced by the chain's own (finer) points, oriented to match the
       base ring's own point order. This is what avoids drawing two
       independent, slightly-offset lines for the same stretch of coast --
       there's only ever one ring, one line. A chain that doesn't match
       both ends closely enough (an island the base ring doesn't touch, a
       stray fragment, or -- confirmed against a real capture -- the
       Northern Ireland border, which sits 100km+ from anything in
       ireland_coastline.json) is simply left out, rather than drawn as an
       orphan fragment.
    4. Overlapping matches on the same ring (rare; from the handful of
       degree-4 junctions where two chains meet at one point) keep
       whichever matched more precisely and drop the other.

Run:   python3 _build_precise_coastline.py
Needs: numpy
"""

import json
import os
from collections import Counter, defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
COUNTIES_PATH = os.path.join(HERE, "ireland_counties.json")
COASTLINE_PATH = os.path.join(HERE, "ireland_coastline.json")
OUT_PATH = os.path.join(HERE, "ireland_coastline_precise.json")

COORD_PRECISION = 6          # decimal places for matching shared vertices between county rings
MAX_MATCH_DISTANCE_KM = 2    # see METHOD step 2 above -- 90% of real chains match within ~1km

# A real detail chain is a short LOCAL stretch of coast -- confirmed against
# a real capture, even a long one (77 points) only spans ~10 ring-index
# units (measured circularly -- see _circular_span, which is what makes
# this cap meaningful: a chain sitting near a closed ring's start/end seam
# is adjacent there, not "nearly the whole ring apart", so a real one no
# longer gets wrongly rejected or wrongly treated as huge). This still
# guards against a genuinely bad match slipping through some other way.
MAX_SPAN_INDEX = 100


def _key(point, prec=COORD_PRECISION):
    return (round(point[0], prec), round(point[1], prec))


def _extract_singleton_edges(rings):
    """Edges appearing in exactly one ring -- the true exterior boundary of
    the unioned polygons (see METHOD step 1)."""
    edge_counts = Counter()
    for ring in rings:
        for i in range(len(ring) - 1):
            a, b = _key(ring[i]), _key(ring[i + 1])
            edge_counts[(a, b) if a <= b else (b, a)] += 1
    return {edge for edge, count in edge_counts.items() if count == 1}


def _chain_edges(singleton_edges):
    """Walk singleton edges into polylines (METHOD step 2): open chains
    starting from a junction (degree != 2) out to the next junction, then
    whatever's left over as closed loops (islands with no junction at
    all). Coordinates here are the rounded lookup keys, not the original
    full-precision points -- _original_points() below recovers those."""
    adjacency = defaultdict(set)
    for a, b in singleton_edges:
        adjacency[a].add(b)
        adjacency[b].add(a)

    visited = set()

    def walk(start, nxt):
        chain = [start, nxt]
        prev, cur = start, nxt
        while True:
            edge = (prev, cur) if prev <= cur else (cur, prev)
            visited.add(edge)
            options = [n for n in adjacency[cur]
                       if ((cur, n) if cur <= n else (n, cur)) not in visited]
            if len(adjacency[cur]) != 2 or not options:
                break
            prev, cur = cur, options[0]
            chain.append(cur)
        return chain

    chains = []
    junctions = [v for v, neighbours in adjacency.items() if len(neighbours) != 2]
    for v in junctions:
        for n in list(adjacency[v]):
            edge = (v, n) if v <= n else (n, v)
            if edge not in visited:
                chains.append(walk(v, n))

    remaining = defaultdict(set)
    for a, b in singleton_edges - visited:
        remaining[a].add(b)
        remaining[b].add(a)
    loop_seen = set()
    for start in list(remaining):
        if start in loop_seen or not remaining[start]:
            continue
        prev, cur = start, next(iter(remaining[start]))
        loop = [start, cur]
        loop_seen.add(start)
        while cur != start:
            loop_seen.add(cur)
            nxt = [n for n in remaining[cur] if n != prev]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            loop.append(cur)
        chains.append(loop)

    return chains


def _original_points(rings):
    """Rounded key -> one real (full-precision) point that had that key, so
    chained output keeps original coordinate precision instead of the
    rounded lookup keys used for matching."""
    lookup = {}
    for ring in rings:
        for point in ring:
            lookup.setdefault(_key(point), point.tolist())
    return lookup


def _nearest_on_ring(point, ring):
    """Closest point to `point` anywhere ON ring's path (not just its
    vertices) -- returns (distance_km, arc_position), where arc_position is
    a continuous "how far along the ring" measure (segment index + fraction
    into that segment) used to place a chain's splice point precisely
    between two existing ring vertices, not just at the nearest one."""
    p = np.array(point)
    a, b = ring[:-1], ring[1:]
    ab = b - a
    ab2 = (ab ** 2).sum(1)
    ab2[ab2 == 0] = 1e-12
    t = np.clip(((p - a) * ab).sum(1) / ab2, 0, 1)
    proj = a + t[:, None] * ab
    d = np.sqrt(((proj - p) ** 2).sum(1))
    i = int(d.argmin())
    return float(d[i]) * 111.0, i + float(t[i])


def _circular_span(pos0, pos1, ring_len_segments):
    """A closed ring's start and end index are the same geographic point,
    so the short path between two arc positions can either go the plain
    way (small pos to large pos) or wrap past that seam -- whichever is
    shorter. Confirmed against real data: a chain sitting near the seam
    (e.g. one that happened to fall almost exactly at Rogerstown Estuary)
    computed a ~2083-unit non-circular span on a 2087-point ring, when the
    real (wrapped) distance was ~3 units -- treating that as "far apart"
    caused it to be either wrongly discarded or, worse, wrongly treated as
    the single largest span and left to consume almost everything else in
    an early version of this script. Returns (span, wraps, start_pos,
    end_pos) -- for a wrapping span, start_pos is the position near the
    ring's end and end_pos the position near its start, i.e. start_pos >
    end_pos (the reverse of the non-wrapping case), which _splice uses to
    tell the two apart."""
    raw = abs(pos1 - pos0)
    wrapped = ring_len_segments - raw
    if wrapped < raw:
        return wrapped, True, max(pos0, pos1), min(pos0, pos1)
    return raw, False, min(pos0, pos1), max(pos0, pos1)


def _splice(ring, normal_matches, wrap_match):
    """ring: one closed base ring (numpy array, ring[0] == ring[-1]).
    normal_matches: [(start_pos, end_pos, chain_points), ...], start_pos <
    end_pos, already resolved to non-overlapping splices strictly between
    wrap_match's two positions (see main()). wrap_match: (start_pos,
    end_pos, chain_points) with start_pos > end_pos (see _circular_span),
    or None if no chain matched across this ring's seam. Returns a new
    point list, still closed (first point == last point) -- not
    necessarily starting at the same point the original ring did, which
    doesn't matter for drawing (PIL just walks the sequence)."""
    normal_matches = sorted(normal_matches, key=lambda m: m[0])

    if wrap_match is None:
        out = []
        cursor = 0.0
        for start_pos, end_pos, chain_points in normal_matches:
            i, j = int(np.ceil(cursor)), int(np.floor(start_pos))
            out.extend(ring[i:j + 1].tolist())
            out.extend(chain_points)
            cursor = end_pos
        out.extend(ring[int(np.ceil(cursor)):].tolist())
        return out

    # start right after the wrap chain's low (near-ring-start) end, walk
    # the middle of the ring forward applying normal splices, then close
    # by appending the wrap chain itself -- its first point lands back
    # near where this output started, closing the ring
    w_start, w_end, w_points = wrap_match
    out = []
    cursor = w_end
    for start_pos, end_pos, chain_points in normal_matches:
        i, j = int(np.ceil(cursor)), int(np.floor(start_pos))
        out.extend(ring[i:j + 1].tolist())
        out.extend(chain_points)
        cursor = end_pos
    out.extend(ring[int(np.ceil(cursor)):int(np.floor(w_start)) + 1].tolist())
    out.extend(w_points)
    return out


def main():
    with open(COUNTIES_PATH) as f:
        counties = [np.array(r, float) for r in json.load(f)]
    with open(COASTLINE_PATH) as f:
        coastline_raw = json.load(f)
    coastline = [np.array(r, float) for r in coastline_raw]
    closed_ring_idx = [i for i, r in enumerate(coastline) if len(r) > 2 and np.array_equal(r[0], r[-1])]

    singleton_edges = _extract_singleton_edges(counties)
    chains_keyed = _chain_edges(singleton_edges)
    point_for_key = _original_points(counties)
    chains = [[point_for_key[k] for k in chain] for chain in chains_keyed]

    # match each chain's two endpoints against every closed base ring;
    # keep only chains matching the SAME ring within tolerance on both ends
    per_ring_matches = defaultdict(list)  # ring_idx -> [(start_pos, end_pos, points, wraps), ...]
    matched, dropped = 0, 0
    for chain in chains:
        if len(chain) < 2:
            continue
        best = None  # (total_dist, ring_idx, start_pos, end_pos, oriented_points, wraps)
        for ring_idx in closed_ring_idx:
            ring = coastline[ring_idx]
            d0, pos0 = _nearest_on_ring(chain[0], ring)
            d1, pos1 = _nearest_on_ring(chain[-1], ring)
            if d0 > MAX_MATCH_DISTANCE_KM or d1 > MAX_MATCH_DISTANCE_KM:
                continue
            span, wraps, start_pos, end_pos = _circular_span(pos0, pos1, len(ring) - 1)
            if span > MAX_SPAN_INDEX:  # see MAX_SPAN_INDEX docstring above
                continue
            # orientation: non-wrap wants start_pos<end_pos order (pos0<=pos1 already
            # true from _circular_span), wrap wants the high-to-low seam-crossing order
            if wraps:
                points = chain if pos0 > pos1 else list(reversed(chain))
            else:
                points = chain if pos0 <= pos1 else list(reversed(chain))
            total = d0 + d1
            if best is None or total < best[0]:
                best = (total, ring_idx, start_pos, end_pos, points, wraps)
        if best is None:
            dropped += 1
            continue
        matched += 1
        _, ring_idx, start_pos, end_pos, points, wraps = best
        per_ring_matches[ring_idx].append((start_pos, end_pos, points, wraps))

    # per ring: pick at most one wrap match (best by total distance -- extremely
    # rare to have two chains both needing to cross the same seam), drop any
    # normal match that would overlap it, then drop overlapping NORMAL matches,
    # keeping whichever chain is longer (a real coastal stretch) when two claim
    # the same span
    out_rings = list(coastline_raw)
    for ring_idx, all_matches in per_ring_matches.items():
        wrap_candidates = [m for m in all_matches if m[3]]
        normal = [m[:3] for m in all_matches if not m[3]]
        wrap_match = None
        if wrap_candidates:
            start_pos, end_pos, points, _ = wrap_candidates[0]
            wrap_match = (start_pos, end_pos, points)
            # a wrap match's "used" range is [0, end_pos] union [start_pos, ring_len] --
            # anything a normal match claims outside (end_pos, start_pos) would overlap it
            normal = [m for m in normal if m[0] > wrap_match[1] and m[1] < wrap_match[0]]

        normal.sort(key=lambda m: m[0])
        kept = []
        for span in normal:
            if kept and span[0] < kept[-1][1]:
                if (span[1] - span[0]) > (kept[-1][1] - kept[-1][0]):
                    kept[-1] = span
                continue
            kept.append(span)
        out_rings[ring_idx] = _splice(coastline[ring_idx], kept, wrap_match)

    with open(OUT_PATH, "w") as f:
        json.dump(out_rings, f, separators=(",", ":"))

    print(f"{len(counties)} county rings -> {len(singleton_edges)} singleton edges -> "
          f"{len(chains)} chains -> {matched} spliced into {len(per_ring_matches)} base ring(s), "
          f"{dropped} dropped (no matching base ring within {MAX_MATCH_DISTANCE_KM}km on both ends)")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
