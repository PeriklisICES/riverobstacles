"""
graph_traversal.py
------------------
Breadth-first search (BFS) from every tidal outlet upstream through the
directed river network, stopping at the first impassable barrier encountered
on each branch.

Equivalent to the BFS / graph-traversal step inside R's
identify_accessible_reaches() in the riverobstacles package.

Design
------
The topology dict (from topology.py) encodes a directed acyclic graph where
edges point *upstream* (each node knows its PREVIOUS upstream neighbours).

BFS proceeds downstream→upstream from each outlet:

  accessible reach  →  continue traversal to all PREVIOUS reaches
  barrier reach      →  mark as first_barrier; DO NOT traverse further
                        upstream from this point

Reaches never visited by any BFS tree are inaccessible (blocked upstream of
a first barrier, or in a disconnected component with no reachable outlet).

Braided networks (reaches with multiple upstream or downstream neighbours)
are handled naturally because BFS queues all PREVIOUS neighbours of every
accessible reach.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, FrozenSet, List, Set, Tuple

from .topology import Topology, find_outlets


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def identify_accessible_reaches(
    topology: Topology,
    barrier_reach_map: Dict[str, str],
) -> Tuple[Set[str], Set[str]]:
    """
    Traverse the river network from every outlet, classifying each reach.

    Parameters
    ----------
    topology : Topology
        ``{reach_id: {'next': str, 'previous': [str], ...}}``
        as returned by ``topology.build_topology()``.
    barrier_reach_map : dict
        ``{reach_id: barrier_id}``
        Reaches that carry at least one impassable barrier.  A reach can
        appear at most once (use ``snap.select_first_barrier_per_reach()``
        to reduce multi-barrier reaches before calling this function).

    Returns
    -------
    accessible_ids : set[str]
        Reach IDs that are accessible from the sea (below all barriers).
    first_barrier_reach_ids : set[str]
        Reach IDs that are the first impassable barrier encountered on each
        branch when tracing upstream from the tidal outlet.  On a straight
        (non-splitting) river there is exactly one first barrier.  When the
        network splits at a confluence, each branch independently finds its
        own first barrier.  Barriers upstream of a first barrier are not
        marked – the BFS stops at the first barrier on every branch.

    Notes
    -----
    A reach that *contains* a barrier is classified as a first-barrier reach
    (``is_accessible = False``).  Fish can swim into the downstream portion
    of such a reach but are blocked before exiting it.

    BFS proceeds from every outlet upstream, queuing all upstream neighbours
    of each accessible reach.  The first barrier encountered on each branch
    is recorded and the traversal stops there – nothing above it is marked.
    This guarantees at most one first barrier per uninterrupted upstream path.
    """
    outlets = find_outlets(topology)

    accessible: Set[str] = set()
    first_barrier_reach_ids: Set[str] = set()
    visited: Set[str] = set()

    queue: deque[str] = deque()

    # Seed the BFS with all outlets
    for outlet_id in outlets:
        if outlet_id in visited:
            continue
        visited.add(outlet_id)
        if outlet_id in barrier_reach_map:
            first_barrier_reach_ids.add(outlet_id)
            # Do not queue – nothing upstream is accessible
        else:
            accessible.add(outlet_id)
            queue.append(outlet_id)

    # BFS upstream – stop at the first barrier on each branch
    while queue:
        current = queue.popleft()
        for upstream_id in topology[current].get("previous", []):
            if upstream_id in visited:
                continue
            visited.add(upstream_id)
            if upstream_id in barrier_reach_map:
                first_barrier_reach_ids.add(upstream_id)
                # Stop: do not continue past this barrier
            else:
                accessible.add(upstream_id)
                queue.append(upstream_id)

    return accessible, first_barrier_reach_ids


# ---------------------------------------------------------------------------
# Derive which *barrier IDs* are first barriers
# ---------------------------------------------------------------------------

def get_first_barrier_ids(
    barrier_reach_map: Dict[str, str],
    first_barrier_reach_ids: Set[str],
) -> List[str]:
    """
    Translate first-barrier *reach* IDs back to *barrier* IDs.

    Parameters
    ----------
    barrier_reach_map : dict
        Output of ``snap.select_first_barrier_per_reach()`` –
        ``{reach_id: barrier_id}`` with at most one barrier per reach.
    first_barrier_reach_ids : set[str]
        Reaches classified as first-barrier by ``identify_accessible_reaches``.

    Returns
    -------
    list[str]
        Barrier IDs selected as the first barrier on each first-barrier reach.
    """
    return [
        barrier_reach_map[reach_id]
        for reach_id in first_barrier_reach_ids
        if reach_id in barrier_reach_map
    ]


# ---------------------------------------------------------------------------
# Aggregate upstream reach count (mirrors R's TOT_UP_SEGMENTS)
# ---------------------------------------------------------------------------

def compute_upstream_counts(topology: Topology) -> Dict[str, int]:
    """
    Return ``{reach_id: number_of_upstream_reaches}`` for every reach.

    Uses a topological sort (iterative Kahn's algorithm) to avoid recursion
    limits on large networks.  Handles braided (non-simple dendritic)
    networks correctly.

    This mirrors R's ``aggregate_upstream_attribute()`` → ``TOT_UP_SEGMENTS``
    column and is useful for proportional-width river mapping.
    """
    # in-degree: number of upstream reaches not yet processed
    in_degree: Dict[str, int] = {rid: len(d["previous"]) for rid, d in topology.items()}

    # upstream_count[rid] = cumulative upstream reaches reachable from rid
    upstream_count: Dict[str, int] = {rid: 0 for rid in topology}

    # Start with headwaters (in_degree == 0)
    queue: deque[str] = deque(rid for rid, deg in in_degree.items() if deg == 0)

    while queue:
        current = queue.popleft()
        downstream_ids = topology[current].get("next", [])
        for nxt in downstream_ids:
            if nxt not in topology:
                continue

            # Propagate: downstream reach gains all of current's upstream count + 1
            upstream_count[nxt] += upstream_count[current] + 1
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    # Each reach counts itself
    return {rid: cnt + 1 for rid, cnt in upstream_count.items()}
