"""
run_analysis.py
---------------
Top-level orchestration function that wires together topology building,
barrier snapping, BFS traversal, and result writing.

Call ``run_analysis()`` from:
  - the ArcGIS Python Toolbox  (RiverObstacles.pyt)
  - a scheduled ArcGIS Notebook
  - a webhook handler (webhook_handler.py)
  - any standalone Python script

Example
-------
::

    from riverobstacles import run_analysis

    run_analysis(
        fc_river_network = r"C:/data/rivers.gdb/river_network",
        fc_barriers      = r"C:/data/rivers.gdb/barriers",
        reach_id_field   = "riverID",
        barrier_id_field = "unique_id_row",
        snap_distance    = 50,
        vertex_density   = 10,
        topology_tolerance = 0.001,
        verbose          = True,
    )
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def run_analysis(
    fc_river_network: str,
    fc_barriers: str,
    reach_id_field: str = "riverID",
    barrier_id_field: str = "unique_id_row",
    snap_distance: float = 50.0,
    distance_method: str = "PLANAR",
    vertex_density: float = 10.0,
    topology_tolerance: float = 0.001,
    # Topology source: 'geometry' (default) or 'fields'
    topology_source: str = "geometry",
    next_field: str = "NEXT",
    previous_field: Optional[str] = "PREVIOUS",
    # Optional passability filter applied to fc_barriers
    passability_field: Optional[str] = None,
    impassable_value: Optional[object] = None,
    temp_workspace: Optional[str] = None,
    disable_editor_tracking: bool = False,
    # Optional river-group filter (processes one catchment subset)
    river_group_field: Optional[str] = None,
    river_group_id: Optional[object] = None,
    # Output field names
    accessibility_field: str = "is_accessible",
    first_barrier_reach_field: str = "is_first_barrier_reach",
    first_barrier_field: str = "is_first_barrier",
    snapped_reach_field: str = "snapped_reach_id",
    upstream_count_field: str = "tot_up_segments",
    accessible_update_field: str = "is_accessible_update",
    write_upstream_counts: bool = False,
    enable_cursor_fallback: bool = False,
    edit_workspace: Optional[str] = None,
    debug_sample_n: int = 0,
    verbose: bool = True,
) -> Dict:
    """
    Identify the first impassable barrier upstream from the tidal limit.

    Reads from an ArcGIS Enterprise geodatabase (or any local geodatabase),
    runs the BFS graph traversal, and writes results back to both the river
    network and barriers feature classes.

    Parameters
    ----------
    fc_river_network : str
        Full path to the river network polyline feature class.
    fc_barriers : str
        Full path to the barriers point feature class.
    reach_id_field : str
        Unique reach identifier field in *fc_river_network*.
    barrier_id_field : str
        Unique barrier identifier field in *fc_barriers*.
    snap_distance : float
        Maximum distance (map units) for snapping barriers to reaches.
    distance_method : str
        Distance calculation method for snapping. Use ``'PLANAR'`` or
        ``'GEODESIC'``.
    vertex_density : float
        Maximum spacing between densified vertices when snapping (map units).
        Smaller → more accurate, slower.
    topology_tolerance : float
        Coordinate tolerance for matching polyline endpoints (map units).
        Only used when *topology_source* = ``'geometry'``.
    topology_source : str
        ``'geometry'`` – build topology by matching polyline endpoints
        (works on any network, no pre-existing attribute fields needed).
        ``'fields'``   – read topology from existing NEXT / PREVIOUS fields.
    next_field : str
        Downstream reach ID field (used when *topology_source* = ``'fields'``).
    previous_field : str | None
        Upstream reach IDs field (space-separated string).
        Optional; PREVIOUS is derived from NEXT if not supplied.
    passability_field : str | None
        Field on *fc_barriers* that indicates barrier passability.
        When supplied, only barriers where the field value matches
        *impassable_value* are processed.
    impassable_value : object | None
        The attribute value marking a barrier as impassable.
        Example: ``False`` for a boolean field, ``0`` for an integer field.
        Pass ``None`` to filter on falsy values (0, False, None, '').
    river_group_field : str | None
        If the network feature class covers multiple catchments, supply the
        field name for the river-group / catchment identifier to restrict
        processing to a single catchment.
    river_group_id : object | None
        The river-group value to filter on.
    accessibility_field : str
        Output field on *fc_river_network* – 1 = accessible, 0 = not.
    first_barrier_reach_field : str
        Output field on *fc_river_network* – 1 = reach contains a first barrier.
    first_barrier_field : str
        Output field on *fc_barriers* – 1 = first barrier, 0 = not / unsnapped.
    snapped_reach_field : str
        Output field on *fc_barriers* – records which reach the barrier was
        snapped to.
    upstream_count_field : str
        Output field on *fc_river_network* – total upstream segment count
        (only written when *write_upstream_counts* is True).
    write_upstream_counts : bool
        If True, compute and write ``tot_up_segments`` to the river network.
    verbose : bool
        If True, emit progress messages via logging and arcpy.AddMessage.

    Returns
    -------
    dict
        Summary statistics:
        ``{'reaches_total', 'reaches_accessible', 'reaches_first_barrier',
           'barriers_total', 'barriers_snapped', 'barriers_first', 'elapsed_s'}``
    """
    import arcpy

    t0 = time.time()

    def _log(msg: str) -> None:
        if verbose:
            logger.info(msg)
            arcpy.AddMessage(msg)

    def _warn(msg: str) -> None:
        logger.warning(msg)
        arcpy.AddWarning(msg)

    # ------------------------------------------------------------------ #
    # Log input parameters for diagnostics                                #
    # ------------------------------------------------------------------ #
    _log("=" * 60)
    _log("River Obstacles – Identify First Barriers")
    _log("=" * 60)
    _log(f"  River network FC : {fc_river_network}")
    _log(f"  Barriers FC      : {fc_barriers}")
    _log(f"  Reach ID field   : {reach_id_field}")
    _log(f"  Barrier ID field : {barrier_id_field}")
    _log(f"  Topology source  : {topology_source}")
    _log(f"  Snap distance    : {snap_distance} map units")
    _log(f"  Distance method  : {distance_method}")
    _log(f"  Vertex density   : {vertex_density} map units")
    _log(f"  Topo tolerance   : {topology_tolerance} map units")
    if passability_field:
        _log(f"  Passability field: {passability_field} (impassable = {impassable_value!r})")
    if temp_workspace:
        _log(f"  Temp workspace   : {temp_workspace}")
    _log(f"  Editor tracking  : {'temporarily disable during write' if disable_editor_tracking else 'leave enabled'}")
    if river_group_field:
        _log(f"  River group      : {river_group_field} = {river_group_id!r}")
    _log(f"  Cursor fallback  : {'enabled' if enable_cursor_fallback else 'disabled'}")
    _log("-" * 60)

    # ------------------------------------------------------------------ #
    # 0.  Optionally restrict network to a single river group             #
    # ------------------------------------------------------------------ #
    target_fc_riv = fc_river_network
    target_fc_bar = fc_barriers
    tmp_layers: List[str] = []

    if river_group_field and river_group_id is not None:
        _log(f"Filtering river network to {river_group_field} = {river_group_id} …")
        riv_lyr = "riv_lyr_ro"
        bar_lyr = "bar_lyr_ro"
        where = f"{river_group_field} = {river_group_id}"
        arcpy.management.MakeFeatureLayer(fc_river_network, riv_lyr, where)
        arcpy.management.MakeFeatureLayer(fc_barriers, bar_lyr)
        # Clip barriers to river network bounding box + snap_distance
        method_norm = (distance_method or "PLANAR").upper()
        overlap_type = (
            "WITHIN_A_DISTANCE_GEODESIC"
            if method_norm == "GEODESIC"
            else "WITHIN_A_DISTANCE"
        )
        group_search_distance = (
            f"{float(snap_distance) + 1000.0} Meters"
            if method_norm == "GEODESIC"
            else snap_distance + 1000
        )
        arcpy.management.SelectLayerByLocation(
            bar_lyr, overlap_type, riv_lyr, group_search_distance
        )
        target_fc_riv = riv_lyr
        target_fc_bar = bar_lyr
        tmp_layers = [riv_lyr, bar_lyr]
        n_riv_filtered = int(arcpy.management.GetCount(riv_lyr).getOutput(0))
        n_bar_filtered = int(arcpy.management.GetCount(bar_lyr).getOutput(0))
        _log(f"    → {n_riv_filtered:,} river reaches and {n_bar_filtered:,} barriers selected for group {river_group_id!r}.")
        if n_riv_filtered == 0:
            _warn("    WARNING: River group filter returned 0 reaches – check river_group_field / river_group_id.")

    if debug_sample_n:
        _warn(f"    DEBUG MODE: topology limited to first {debug_sample_n:,} reaches. Results will be partial.")

    # ------------------------------------------------------------------ #
    # 1.  Build topology                                                  #
    # ------------------------------------------------------------------ #
    _log("(1/5) Building network topology …")
    _t1 = time.time()
    from .topology import build_topology, build_topology_from_fields

    if topology_source == "fields":
        topology = build_topology_from_fields(
            target_fc_riv,
            reach_id_field=reach_id_field,
            next_field=next_field,
            previous_field=previous_field,
            max_features=debug_sample_n,
        )
    else:
        topology = build_topology(
            target_fc_riv,
            reach_id_field=reach_id_field,
            tolerance=topology_tolerance,
            max_features=debug_sample_n,
        )

    _log(f"    → {len(topology):,} reaches loaded in {time.time()-_t1:.1f} s.")
    if len(topology) == 0:
        _warn("    WARNING: Topology is empty – no reaches were read. Check the river network FC and reach_id_field.")
    from .topology import find_outlets
    _outlets = find_outlets(topology)
    if topology_source == "geometry" and len(topology) > 0:
        outlet_ratio = len(_outlets) / float(len(topology))
        if outlet_ratio > 0.9:
            # Retry geometry matching with progressively coarser tolerances.
            best_topology = topology
            best_outlets = _outlets
            best_tol = topology_tolerance
            for factor in (10.0, 100.0, 1000.0):
                cand_tol = topology_tolerance * factor
                cand_topology = build_topology(
                    target_fc_riv,
                    reach_id_field=reach_id_field,
                    tolerance=cand_tol,
                    max_features=debug_sample_n,
                )
                cand_outlets = find_outlets(cand_topology)
                if len(cand_outlets) < len(best_outlets):
                    best_topology = cand_topology
                    best_outlets = cand_outlets
                    best_tol = cand_tol
                if len(cand_topology) > 0 and (len(cand_outlets) / float(len(cand_topology))) <= 0.9:
                    break

            if len(best_outlets) < len(_outlets):
                topology = best_topology
                _outlets = best_outlets
                _warn(
                    "    WARNING: Geometry topology looked disconnected at the initial "
                    f"tolerance ({topology_tolerance}). Retried with coarser tolerance "
                    f"{best_tol} and reduced outlets to {len(_outlets):,}/{len(topology):,}."
                )

            outlet_ratio = len(_outlets) / float(len(topology))

        if outlet_ratio > 0.9:
            field_names = {f.name.lower() for f in arcpy.ListFields(target_fc_riv)}
            has_next_field = next_field.lower() in field_names
            if has_next_field:
                _warn(
                    "    WARNING: Geometry topology appears mostly disconnected "
                    f"({len(_outlets):,}/{len(topology):,} outlets). "
                    "Retrying with NEXT/PREVIOUS fields."
                )
                topology = build_topology_from_fields(
                    target_fc_riv,
                    reach_id_field=reach_id_field,
                    next_field=next_field,
                    previous_field=previous_field,
                    max_features=debug_sample_n,
                )
                _outlets = find_outlets(topology)
                _log(
                    "    → Fallback topology: "
                    f"{len(topology):,} reaches, {len(_outlets):,} outlets "
                    "(field-based)."
                )
            else:
                _warn(
                    "    WARNING: Geometry topology appears mostly disconnected "
                    f"({len(_outlets):,}/{len(topology):,} outlets) and NEXT field "
                    f"'{next_field}' was not found for fallback."
                )
    _log(f"    → {len(_outlets):,} outlet reach(es) detected (reaches with no downstream neighbour).")
    if len(_outlets) == 0:
        _warn("    WARNING: No outlets found – the network may be a closed loop or all reaches have a downstream link.")

    # ------------------------------------------------------------------ #
    # 2.  Snap barriers to the nearest reach                              #
    # ------------------------------------------------------------------ #
    _log(
        f"(2/5) Snapping barriers to network "
        f"(snap_distance={snap_distance}, method={distance_method}) …"
    )
    _t2 = time.time()
    from .snap import snap_barriers_to_network, select_first_barrier_per_reach

    snapped = snap_barriers_to_network(
        fc_barriers=target_fc_bar,
        fc_river_network=target_fc_riv,
        barrier_id_field=barrier_id_field,
        reach_id_field=reach_id_field,
        snap_distance=snap_distance,
        distance_method=distance_method,
        vertex_density=vertex_density,
        passability_field=passability_field,
        impassable_value=impassable_value,
        temp_workspace=temp_workspace,
        enable_cursor_fallback=enable_cursor_fallback,
    )

    n_barriers_total = int(arcpy.management.GetCount(target_fc_bar).getOutput(0))
    _log(f"    → {len(snapped):,} of {n_barriers_total:,} barriers snapped in {time.time()-_t2:.1f} s.")
    if n_barriers_total == 0:
        _warn("    WARNING: Barriers feature class contains 0 features (after any passability filter).")
    elif len(snapped) == 0:
        _warn(f"    WARNING: No barriers were snapped within {snap_distance} map units. "
              "Results will show all reaches as accessible with no first barriers.")
    elif passability_field and len(snapped) < n_barriers_total:
        _log(f"    → {n_barriers_total - len(snapped):,} barriers excluded by passability filter ({passability_field} != {impassable_value!r}).")

    # ------------------------------------------------------------------ #
    # 3.  Reduce to one barrier per reach                                 #
    # ------------------------------------------------------------------ #
    _log("(3/5) Selecting first (lowest) barrier per reach …")
    _t3 = time.time()
    barrier_reach_map: Dict[str, str] = select_first_barrier_per_reach(
        snapped,
        topology,
        fc_barriers=target_fc_bar,
        fc_river_network=target_fc_riv,
        barrier_id_field=barrier_id_field,
        reach_id_field=reach_id_field,
    )

    _log(f"    → {len(barrier_reach_map):,} reaches carry a first barrier in {time.time()-_t3:.1f} s.")

    # Build reverse map: {barrier_id: reach_id} for all snapped barriers
    snapped_reach_map: Dict[str, str] = {s["barrier_id"]: s["reach_id"] for s in snapped}

    # ------------------------------------------------------------------ #
    # 4.  BFS traversal                                                   #
    # ------------------------------------------------------------------ #
    _log(f"(4/5) Traversing graph from {len(_outlets):,} outlet(s) to first barriers (BFS) …")
    _t4 = time.time()
    from .graph_traversal import (
        identify_accessible_reaches as _bfs,
        get_first_barrier_ids,
        compute_upstream_counts,
    )

    accessible_ids, first_barrier_reach_ids = _bfs(topology, barrier_reach_map)
    first_barrier_ids = get_first_barrier_ids(barrier_reach_map, first_barrier_reach_ids)

    _log(
        f"    → {len(accessible_ids):,} accessible reaches, "
        f"{len(first_barrier_reach_ids):,} first-barrier reaches "
        f"in {time.time()-_t4:.1f} s."
    )
    if len(accessible_ids) == 0:
        _warn("    WARNING: BFS found 0 accessible reaches. Check that outlets exist and topology is correct.")
    n_above_barrier = len(topology) - len(accessible_ids) - len(first_barrier_reach_ids)
    if n_above_barrier > 0:
        _log(f"    → {n_above_barrier:,} reaches are above a barrier (not accessible, not a first-barrier reach).")

    upstream_counts: Optional[Dict[str, int]] = None
    if write_upstream_counts:
        _log("    Computing upstream segment counts …")
        upstream_counts = compute_upstream_counts(topology)

    # ------------------------------------------------------------------ #
    # 5.  Write results                                                   #
    # ------------------------------------------------------------------ #
    _log("(5/5) Writing results to geodatabase …")
    _t5 = time.time()

    # Clean up temporary filtered layers BEFORE opening the edit session.
    # Layer locks held by filtered layers can interfere with writes to large
    # datasets (837k+ rows).  Releasing them upfront ensures the write doesn't
    # fail with "Workspace or data source is read only".
    for lyr in tmp_layers:
        arcpy.management.Delete(lyr)

    from .arcgis_io import update_river_network_results, update_barrier_results

    # Pre-flight: verify both workspaces are writable before touching them.
    # File geodatabases on OneDrive/SharePoint can be locked read-only while
    # a sync is in progress.  Checking upfront gives a clear error message
    # instead of a mid-write RuntimeError.
    import os
    import re
    def _check_writable(fc: str, label: str) -> None:
        # Resolve the workspace path.  For SDE, extract the .sde connection
        # from the fc string directly; for file GDB, walk up via catalogPath.
        sde_match = re.search(r'^(.*?\.sde)(?:[/\\.]|$)', fc, re.IGNORECASE)
        if sde_match:
            ws = sde_match.group(1)
            # SDE workspaces are network connections; skip OS-level check.
            _log(f"    SDE workspace     : {ws}")
            return
        ws = arcpy.Describe(fc).catalogPath
        while ws and not ws.lower().endswith((".gdb", ".sde")):
            ws = os.path.dirname(ws)
        if ws and os.path.exists(ws) and not os.access(ws, os.W_OK):
            raise RuntimeError(
                f"{label} workspace is read-only: {ws}\n"
                "Common causes:\n"
                "  • OneDrive / SharePoint is syncing the geodatabase — "
                "wait for sync to complete, or pause OneDrive and retry.\n"
                "  • The .gdb folder or its .lock files are marked read-only "
                "by another process.\n"
                "  • The layer was added to ArcGIS Pro in read-only mode.\n"
                "  • You do not have write permission on this path."
            )
        _log(f"    Workspace writable: {ws}")

    _check_writable(fc_river_network, "River network")
    _check_writable(fc_barriers, "Barriers")

    from .arcgis_io import (
        _resolve_fc_and_workspace,
        _edit_and_run,
        ensure_fields,
        ensure_long_field_compatible,
        disable_editor_tracking as _disable_editor_tracking,
        restore_editor_tracking as _restore_editor_tracking,
    )
    actual_riv_fc, riv_workspace = _resolve_fc_and_workspace(fc_river_network)
    actual_bar_fc, bar_workspace = _resolve_fc_and_workspace(fc_barriers)
    _log(f"    River network → {actual_riv_fc}  (workspace: {riv_workspace})")
    _log(f"    Barriers      → {actual_bar_fc}  (workspace: {bar_workspace})")

    if edit_workspace:
        import os as _os, re as _re
        _log(f"    Edit workspace    : {edit_workspace} (user-provided)")
        actual_riv_fc = _os.path.join(edit_workspace, _re.split(r'[./\\]', fc_river_network)[-1])
        actual_bar_fc = _os.path.join(edit_workspace, _re.split(r'[./\\]', fc_barriers)[-1])
        riv_workspace = bar_workspace = edit_workspace
        _log(f"    River network FC for cursor: {actual_riv_fc}")
        _log(f"    Barriers FC for cursor    : {actual_bar_fc}")

    # Ensure fields BEFORE any edit session (schema changes cannot be inside one).
    # IMPORTANT: call ensure_fields on the *original* fc paths (fc_river_network /
    # fc_barriers) so that the arcpy.management.AddField child results reference
    # the project's own SDE layers.  If the packaging analyser only sees the
    # resolved local-GDB path it cannot find a matching project data source and
    # raises "00068: … broken project data source: in_table".
    # When the resolved write-target path differs (offline-replica local GDB),
    # also add the fields there so cursor writes succeed without a sync step.
    riv_field_specs = [
        {"name": accessibility_field, "type": "SHORT", "alias": "Is Accessible"},
        {"name": first_barrier_reach_field, "type": "SHORT", "alias": "Is First Barrier Reach"},
        {"name": accessible_update_field, "type": "SHORT", "alias": "Is Accessible (Upstream Trace)"},
    ]
    if upstream_counts is not None:
        riv_field_specs.append({"name": upstream_count_field, "type": "LONG", "alias": "Total Upstream Segments"})
    bar_field_specs = [
        {"name": first_barrier_field, "type": "SHORT", "alias": "Is First Barrier"},
        {"name": snapped_reach_field, "type": "TEXT", "alias": "Snapped Reach ID", "length": 64},
    ]
    ensure_fields(fc_river_network, riv_field_specs)
    ensure_fields(fc_barriers, bar_field_specs)
    # Secondary call for the offline-replica / edit_workspace case where the
    # write target is a different path from the original layer.
    def _norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p))
    if _norm(actual_riv_fc) != _norm(fc_river_network):
        ensure_fields(actual_riv_fc, riv_field_specs)
    if _norm(actual_bar_fc) != _norm(fc_barriers):
        ensure_fields(actual_bar_fc, bar_field_specs)

    effective_upstream_count_field = upstream_count_field
    if upstream_counts is not None:
        # Ensure write target has a LONG-compatible field for potentially large
        # upstream counts. This can migrate/alias the configured field name.
        effective_upstream_count_field = ensure_long_field_compatible(
            actual_riv_fc,
            upstream_count_field,
            "Total Upstream Segments",
        )
        if _norm(actual_riv_fc) != _norm(fc_river_network):
            ensure_long_field_compatible(
                fc_river_network,
                upstream_count_field,
                "Total Upstream Segments",
            )
        if effective_upstream_count_field != upstream_count_field:
            _warn(
                f"    WARNING: Field '{upstream_count_field}' is not LONG-compatible on the write target. "
                f"Using '{effective_upstream_count_field}' for upstream counts."
            )

    # Run cursor operations. If both FCs are in the same workspace, keep a
    # single session; if they are in different workspaces (common with temp
    # .sde layer connections), each workspace needs its own edit session.
    counts = {"riv": 0, "bar": 0}

    tracking_states = {}
    if disable_editor_tracking:
        _log("    Disabling editor tracking on write targets …")
        for label, target in (("river", actual_riv_fc), ("barrier", actual_bar_fc)):
            tracking_states[label] = _disable_editor_tracking(target, log_callback=_log)
        _log("    Editor tracking disabled; proceeding with writes.")

    def _write_river_only():
        counts["riv"] = update_river_network_results(
            fc_river_network=fc_river_network,
            reach_id_field=reach_id_field,
            accessible_ids=accessible_ids,
            first_barrier_reach_ids=first_barrier_reach_ids,
            upstream_counts=upstream_counts,
            accessibility_field=accessibility_field,
            first_barrier_reach_field=first_barrier_reach_field,
            upstream_count_field=effective_upstream_count_field,
            accessible_update_field=accessible_update_field,
            edit_workspace=edit_workspace,
            _cursor_only=True,
            _resolved_fc=actual_riv_fc,
        )

    def _write_barrier_only():
        counts["bar"] = update_barrier_results(
            fc_barriers=fc_barriers,
            barrier_id_field=barrier_id_field,
            first_barrier_ids=first_barrier_ids,
            snapped_reach_map=snapped_reach_map,
            first_barrier_field=first_barrier_field,
            snapped_reach_field=snapped_reach_field,
            edit_workspace=edit_workspace,
            _cursor_only=True,
            _resolved_fc=actual_bar_fc,
        )

    def _do_all_writes():
        _write_river_only()
        _write_barrier_only()

    # Always use a single combined edit session for both writes.
    # The "try without editor first" pattern is avoided deliberately: a failed
    # cursor attempt (which happens when the GDB requires an edit session) can
    # leave a partial lock that causes the subsequent _edit_and_run to also
    # fail with "Workspace or data source is read only".  Going straight to
    # _edit_and_run is safe for all workspace types – for plain file GDBs that
    # don't require a session it is harmless overhead.
    if _norm(riv_workspace) == _norm(bar_workspace):
        _log(f"    Opening combined edit session on {riv_workspace}")
        try:
            _edit_and_run(riv_workspace, _do_all_writes)
        finally:
            if disable_editor_tracking:
                _log("    Restoring editor tracking on write targets …")
                try:
                    _restore_editor_tracking(actual_riv_fc, tracking_states.get("river"), log_callback=_log)
                except Exception as ex:
                    _warn(f"    WARNING: Failed to restore editor tracking on river target: {ex}")
                try:
                    _restore_editor_tracking(actual_bar_fc, tracking_states.get("barrier"), log_callback=_log)
                except Exception as ex:
                    _warn(f"    WARNING: Failed to restore editor tracking on barrier target: {ex}")
    else:
        try:
            _log(f"    Opening river edit session on {riv_workspace}")
            _edit_and_run(riv_workspace, _write_river_only)
            _log(f"    Opening barrier edit session on {bar_workspace}")
            _edit_and_run(bar_workspace, _write_barrier_only)
        finally:
            if disable_editor_tracking:
                _log("    Restoring editor tracking on write targets …")
                try:
                    _restore_editor_tracking(actual_riv_fc, tracking_states.get("river"), log_callback=_log)
                except Exception as ex:
                    _warn(f"    WARNING: Failed to restore editor tracking on river target: {ex}")
                try:
                    _restore_editor_tracking(actual_bar_fc, tracking_states.get("barrier"), log_callback=_log)
                except Exception as ex:
                    _warn(f"    WARNING: Failed to restore editor tracking on barrier target: {ex}")

    n_riv_updated = counts["riv"]
    n_bar_updated = counts["bar"]
    _log(
        f"    → {n_riv_updated:,} river reaches written (fields: "
        f"{accessibility_field}, {first_barrier_reach_field}, {accessible_update_field}"
        + (f", {effective_upstream_count_field}" if upstream_counts is not None else "")
        + ")."
    )
    _log(f"    → {n_bar_updated:,} barriers written.")

    _log(f"    Step 5 completed in {time.time()-_t5:.1f} s.")

    elapsed = round(time.time() - t0, 2)
    _log("-" * 60)
    _log(f"Done! Total elapsed: {elapsed} s")

    return {
        "reaches_total": len(topology),
        "reaches_accessible": len(accessible_ids),
        "reaches_first_barrier": len(first_barrier_reach_ids),
        "barriers_total": n_barriers_total,
        "barriers_snapped": len(snapped),
        "barriers_first": len(first_barrier_ids),
        "elapsed_s": elapsed,
    }
