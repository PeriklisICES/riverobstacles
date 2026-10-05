"""
topology.py
-----------
Build directed graph topology (NEXT / PREVIOUS reach links) from polyline
endpoint geometry.

Equivalent to R's  collate_up_down_seg_ids()  in the riverobstacles package.

Algorithm
---------
1.  Read the start (upstream) and end (downstream) point of every reach.
2.  Index all *start* points in a grid so endpoint-matching is O(1).
3.  For each reach, hash its *end* point and look up which reach's *start*
    point shares the same location → that reach is the downstream neighbour
    (NEXT).
4.  Invert the NEXT relationships to build PREVIOUS lists.

The function returns a plain dict so it can be used without any ArcGIS import
(useful for unit-testing with synthetic data).
"""

from __future__ import annotations

import collections
import math
from typing import Dict, List, Optional, Tuple

from .id_utils import normalize_feature_id

# ---------------------------------------------------------------------------
# Public type alias
# ---------------------------------------------------------------------------
# {reach_id: {'next': [str, ...], 'previous': [str, ...], 'start': (x, y), 'end': (x, y)}}
Topology = Dict[str, dict]


def build_topology(
    fc_river_network: str,
    reach_id_field: str = "riverID",
    tolerance: float = 0.001,
    max_features: int = 0,
) -> Topology:
    """
    Build upstream / downstream topology for every reach in *fc_river_network*.

    Parameters
    ----------
    fc_river_network : str
        Full path to the polyline feature class (geodatabase or SDE connection).
    reach_id_field : str
        Name of the field that uniquely identifies each reach.
    tolerance : float
        Coordinate snap tolerance in map units.  Two endpoints are considered
        coincident when  round(coord / tolerance)  produces the same integer.
        For projected CRS in metres, 0.001 (= 1 mm) is a safe default.
    max_features : int
        If > 0, stop after loading this many reaches.  Intended for quick
        debug runs only – the resulting topology will be a partial network.

    Returns
    -------
    dict
        Keys are string reach IDs.  Each value is a dict with keys:
        ``'next'``      – list[str], IDs of immediately downstream reaches.
                  Empty list means outlet.
        ``'previous'``  – list[str], IDs of all immediately upstream reaches.
        ``'start'``     – (x, y) tuple of the upstream endpoint.
        ``'end'``       – (x, y) tuple of the downstream endpoint.
    """
    import arcpy  # imported here so the module is importable without arcpy

    reach_data: Topology = {}

    with arcpy.da.SearchCursor(fc_river_network, [reach_id_field, "SHAPE@"]) as cur:
        for row in cur:
            rid = normalize_feature_id(row[0])
            geom = row[1]
            if geom is None:
                continue
            first = geom.firstPoint
            last = geom.lastPoint
            reach_data[rid] = {
                "start": (first.X, first.Y),
                "end": (last.X, last.Y),
                "next": [],
                "previous": [],
            }
            if max_features and len(reach_data) >= max_features:
                break

    _assign_next_previous(reach_data, tolerance)
    return reach_data


def build_topology_from_fields(
    fc_river_network: str,
    reach_id_field: str = "riverID",
    next_field: str = "NEXT",
    previous_field: Optional[str] = "PREVIOUS",
    max_features: int = 0,
) -> Topology:
    """
    Build topology by reading pre-existing NEXT / PREVIOUS attribute fields.

    Use this when the geodatabase already stores downstream / upstream IDs
    (e.g. maintained by a network dataset or a prior processing step).

    Parameters
    ----------
    fc_river_network : str
        Path to the polyline feature class.
    reach_id_field, next_field, previous_field : str
        Field names.  *previous_field* is optional; if None or absent the
        PREVIOUS lists are derived from the NEXT relationships.
    """
    import arcpy

    fields = [reach_id_field, next_field, "SHAPE@"]
    has_prev_field = (
        previous_field is not None
        and previous_field in [f.name for f in arcpy.ListFields(fc_river_network)]
    )
    if has_prev_field:
        fields.insert(2, previous_field)

    reach_data: Topology = {}

    with arcpy.da.SearchCursor(fc_river_network, fields) as cur:
        for row in cur:
            rid = normalize_feature_id(row[0])
            nxt = normalize_feature_id(row[1])
            if has_prev_field:
                prev_raw = str(row[2]).strip() if row[2] else ""
                prev = [normalize_feature_id(p) for p in prev_raw.split() if normalize_feature_id(p)] if prev_raw else []
                geom = row[3]
            else:
                prev = []
                geom = row[2]

            first = geom.firstPoint if geom else None
            last = geom.lastPoint if geom else None

            reach_data[rid] = {
                "start": (first.X, first.Y) if first else (0.0, 0.0),
                "end": (last.X, last.Y) if last else (0.0, 0.0),
                "next": [nxt] if nxt else [],
                "previous": prev,
            }
            if max_features and len(reach_data) >= max_features:
                break

    # Rebuild PREVIOUS from NEXT if not stored
    if not has_prev_field:
        for rid, data in reach_data.items():
            for nxt in data["next"]:
                if nxt in reach_data:
                    reach_data[nxt]["previous"].append(rid)

    return reach_data


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _assign_next_previous(reach_data: Topology, tolerance: float) -> None:
    """Mutates *reach_data* in-place, filling 'next' and 'previous'."""
    inv_tol = 1.0 / tolerance

    def _make_index(point_key: str) -> Dict[Tuple[int, int], List[str]]:
        idx: Dict[Tuple[int, int], List[str]] = collections.defaultdict(list)
        for rid, data in reach_data.items():
            px, py = data[point_key]
            idx[(_round(px, inv_tol), _round(py, inv_tol))].append(rid)
        return idx

    def _build_next(source_key: str, target_key: str) -> Dict[str, List[str]]:
        target_index = _make_index(target_key)
        nxt_map: Dict[str, List[str]] = {}
        for rid, data in reach_data.items():
            px, py = data[source_key]
            key = (_round(px, inv_tol), _round(py, inv_tol))
            candidates = [r for r in target_index.get(key, []) if r != rid]
            nxt_map[rid] = candidates
        return nxt_map

    # Some datasets are digitized outlet→headwater while others are
    # headwater→outlet. Evaluate both and keep the orientation that produces
    # the larger number of connected downstream links.
    next_end_to_start = _build_next("end", "start")
    next_start_to_end = _build_next("start", "end")
    links_end_to_start = sum(len(v) for v in next_end_to_start.values())
    links_start_to_end = sum(len(v) for v in next_start_to_end.values())

    chosen_next = (
        next_start_to_end
        if links_start_to_end > links_end_to_start
        else next_end_to_start
    )

    # Reset and rebuild from the chosen orientation.
    for data in reach_data.values():
        data["next"] = []
        data["previous"] = []

    for rid, nxt in chosen_next.items():
        reach_data[rid]["next"] = nxt

    for rid, data in reach_data.items():
        for nxt in data["next"]:
            if nxt in reach_data:
                reach_data[nxt]["previous"].append(rid)


def _round(coord: float, inv_tol: float) -> int:
    return int(math.floor(coord * inv_tol + 0.5))


# ---------------------------------------------------------------------------
# Convenience: find outlets and headwaters
# ---------------------------------------------------------------------------

def find_outlets(topology: Topology) -> List[str]:
    """Return IDs of reaches that have no downstream neighbour (tidal outlets)."""
    return [rid for rid, d in topology.items() if not d["next"]]


def find_headwaters(topology: Topology) -> List[str]:
    """Return IDs of reaches that have no upstream neighbours."""
    return [rid for rid, d in topology.items() if not d["previous"]]
