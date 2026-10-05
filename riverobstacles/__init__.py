# riverobstacles/__init__.py
"""
riverobstacles_arcgis
=====================
ArcGIS implementation of the riverobstacles R package.

Identifies the first impassable barrier upstream from the tidal limit in a
directed river network, without requiring ArcGIS Trace Network or Network
Analyst.

Modules
-------
topology        – Build directed graph topology from polyline geometry.
snap            – Snap barrier points onto the nearest network reach.
graph_traversal – BFS from outlet(s) to classify accessible reaches.
arcgis_io       – Read / write results to Enterprise GDB or feature services.
run_analysis    – High-level orchestration entry point.
"""

from .topology import build_topology, build_topology_from_fields
from .snap import snap_barriers_to_network, select_first_barrier_per_reach
from .graph_traversal import identify_accessible_reaches, get_first_barrier_ids
from .run_analysis import run_analysis

__all__ = [
    "build_topology",
    "build_topology_from_fields",
    "snap_barriers_to_network",
    "select_first_barrier_per_reach",
    "identify_accessible_reaches",
    "get_first_barrier_ids",
    "run_analysis",
]
