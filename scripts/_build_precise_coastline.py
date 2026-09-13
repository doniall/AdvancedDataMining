#!/usr/bin/env python3
"""
_build_precise_coastline.py

One-off generator, not part of the regular pipeline -- run by hand whenever
ireland_counties.json or ireland_coastline.json change. Writes
ireland_coastline_precise.json, a higher-resolution coastline derived from
ireland_counties.json for use as the DRAWN coastline (see
B_ireland_radar_greyscale.py's load_precise_coastline()) -- NOT as a
replacement for ireland_coastline.json's role in the land mask that keeps
ship trails off land, which needs genuinely closed landmass rings and stays
on the original data.

WHY THIS EXISTS
    ireland_counties.json digitizes each county's own boundary far more
    finely than ireland_coastline.json digitizes the coastline as a whole
    (confirmed against a real capture: ~11x the vertex density in the
    Dublin Bay area alone) -- because it's tracing each county's real
    boundary in detail, whereas ireland_coastline.json is a single
    lower-resolution trace of the whole coast. Wherever a county's own
    edge faces the sea, that finer digitization IS the coastline; it's just
    never been extracted on its own before.

METHOD
    1. Every edge in ireland_counties.json's rings is counted. An edge
       shared by two adjacent counties (an inland administrative border)
       appears exactly twice; an edge on the true exterior (facing the sea,
       or -- since this dataset only covers the Republic's 26 counties --
       facing Northern Ireland) appears exactly once. Confirmed against a
       real capture: every edge is either count 1 or count 2, no
       three-way ties, so this split is unambiguous.
    2. Singleton edges are chained at shared endpoints into polylines (open
       chains between two county-border junctions) and, where a chain
       closes on itself with no junction at all (a small island bordering
       no other county), simple loops.
    3. Northern Ireland problem: with no NI county on the other side, the
       land border with NI also shows up as singleton edges -- there's
       nothing in the edge-count step to tell it apart from real coast.
       Confirmed against a real capture: the chains it produces are
       obviously distinct by shape, not just in principle -- the 3 largest
       chains (527-1072 points, all in the 54-55N Donegal/NI-border
       latitude band) sit 100km+ from the nearest point on the existing
       (trusted) ireland_coastline.json at their farthest point, while
       every other chain -- real coast, including small islands -- stays
       within ~6.5km of it. MAX_DIST_TO_REFERENCE_KM (15) sits with wide
       margin in that gap and discards the NI-border chains cleanly.

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

COORD_PRECISION = 6           # decimal places for matching shared vertices between rings
MAX_DIST_TO_REFERENCE_KM = 15  # see METHOD step 3 above -- real chains: ~6.5km max; NI border: 100km+


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
    whatever's left over as closed loops (islands with no junction at all).
    Coordinates here are the rounded lookup keys, not the original
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
            lookup.setdefault(_key(point), point)
    return lookup


def _max_dist_to_reference_km(chain_points, reference_points, sample=15):
    """Farthest any point on this chain sits from its nearest point on the
    reference coastline -- see METHOD step 3. Degrees->km via a flat
    equirectangular approximation; plenty accurate for a 15km cutoff with a
    100km-wide margin either side."""
    pts = np.array(chain_points)
    if len(pts) > sample:
        pts = pts[np.linspace(0, len(pts) - 1, sample).astype(int)]
    d = np.sqrt(((pts[:, None, :] - reference_points[None, :, :]) ** 2).sum(-1))
    return float(d.min(axis=1).max()) * 111.0


def main():
    with open(COUNTIES_PATH) as f:
        counties = [np.array(r, float) for r in json.load(f)]
    with open(COASTLINE_PATH) as f:
        coastline = json.load(f)

    singleton_edges = _extract_singleton_edges(counties)
    chains_keyed = _chain_edges(singleton_edges)
    point_for_key = _original_points(counties)
    chains = [[list(point_for_key[k]) for k in chain] for chain in chains_keyed]

    reference_points = np.array([p for ring in coastline for p in ring])

    kept = [c for c in chains if len(c) >= 2
            and _max_dist_to_reference_km(c, reference_points) <= MAX_DIST_TO_REFERENCE_KM]
    dropped = len(chains) - len(kept)

    with open(OUT_PATH, "w") as f:
        json.dump(kept, f, separators=(",", ":"))

    print(f"{len(counties)} county rings -> {len(singleton_edges)} singleton edges -> "
          f"{len(chains)} chains -> kept {len(kept)} ({dropped} dropped as inland, "
          f"almost certainly the NI border), {sum(len(c) for c in kept)} total points")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
