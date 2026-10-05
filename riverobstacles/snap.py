"""
snap.py
-------
Snap barrier POINT features onto the nearest reach of the river network.

Uses arcpy.analysis.GenerateNearTable for spatial snapping.  This approach
relies on arcpy's native C++ spatial index rather than loading densified
vertices into Python memory, so it scales to networks with hundreds of
thousands of reaches without exhausting RAM.

The previous vertex-densification algorithm (which caused MemoryError on
large SDE networks) has been replaced.  The public API is unchanged.
"""

from __future__ import annotations

import collections
import math
import os
import tempfile
import uuid
from typing import Dict, List, Optional, Tuple

from .id_utils import normalize_feature_id


def _iter_polyline_vertices(shape) -> List[Tuple[float, float]]:
    vertices: List[Tuple[float, float]] = []

    def _append_point(point) -> None:
        if point is not None:
            vertices.append((point.X, point.Y))

    for part in shape:
        if part is None:
            continue
        if hasattr(part, "X") and hasattr(part, "Y"):
            _append_point(part)
            continue
        try:
            for point in part:
                _append_point(point)
        except TypeError:
            _append_point(part)
    return vertices


def _distance_along_polyline(shape, xy: Tuple[float, float]) -> Optional[float]:
    vertices = _iter_polyline_vertices(shape)
    if len(vertices) < 2:
        return None

    target_x, target_y = xy
    best_offset = None
    best_distance_sq = None
    traversed = 0.0

    for (x1, y1), (x2, y2) in zip(vertices, vertices[1:]):
        dx = x2 - x1
        dy = y2 - y1
        segment_length_sq = dx * dx + dy * dy
        if segment_length_sq == 0:
            continue

        t = ((target_x - x1) * dx + (target_y - y1) * dy) / segment_length_sq
        t = max(0.0, min(1.0, t))
        proj_x = x1 + t * dx
        proj_y = y1 + t * dy
        distance_sq = (target_x - proj_x) ** 2 + (target_y - proj_y) ** 2
        segment_length = math.sqrt(segment_length_sq)
        offset = traversed + t * segment_length

        if best_distance_sq is None or distance_sq < best_distance_sq:
            best_distance_sq = distance_sq
            best_offset = offset

        traversed += segment_length

    return best_offset


def snap_barriers_to_network(
    fc_barriers: str,
    fc_river_network: str,
    barrier_id_field: str,
    reach_id_field: str = "riverID",
    snap_distance: float = 50.0,
    distance_method: str = "PLANAR",
    vertex_density: float = 10.0,   # kept for API compatibility; no longer used
    passability_field: Optional[str] = None,
    impassable_value: Optional[object] = None,
    temp_workspace: Optional[str] = None,
    enable_cursor_fallback: bool = False,
) -> List[Dict]:
    """
    Snap barrier points to the nearest river-network reach within *snap_distance*.

    Parameters
    ----------
    fc_barriers : str
        Path to the barriers point feature class.
    fc_river_network : str
        Path to the river network polyline feature class.
    barrier_id_field : str
        Unique barrier identifier field.
    reach_id_field : str
        Unique reach identifier field.
    snap_distance : float
        Maximum distance (map units) to consider a barrier "on" a reach.
    distance_method : str
        Distance method for proximity operations. Must be ``'PLANAR'`` or
        ``'GEODESIC'``.
    vertex_density : float
        No longer used (kept for backwards-compatible call sites).
    passability_field : str | None
        If supplied, only barriers where this field equals *impassable_value*
        (or is falsy when *impassable_value* is None) are processed.
    impassable_value : object | None
        The attribute value that marks a barrier as impassable.

    Returns
    -------
    list of dict
        Each element: ``{'barrier_id': str, 'reach_id': str, 'distance': float}``.
        Sorted by barrier_id then distance.
    """
    import arcpy

    distance_method_norm = (distance_method or "PLANAR").upper()
    if distance_method_norm not in {"PLANAR", "GEODESIC"}:
        raise ValueError(
            "distance_method must be either 'PLANAR' or 'GEODESIC' "
            f"(got {distance_method!r})"
        )

    # ArcPy treats bare numeric radii as map units. For GEODESIC, enforce
    # explicit meters to avoid accidental degree-based thresholds in GCS data.
    geodesic_radius = f"{float(snap_distance)} Meters"
    near_search_radius = geodesic_radius if distance_method_norm == "GEODESIC" else snap_distance

    def _resolve_fc_gdb(path: str) -> Optional[str]:
        """Return containing .gdb path for a dataset, if any."""
        p = os.path.normpath(path)
        lower = p.lower()
        idx = lower.find(".gdb")
        if idx == -1:
            return None
        return p[: idx + 4]

    def _ensure_writable_gdb(candidate_gdb: str) -> Optional[str]:
        """Create/validate a writable file geodatabase and return path or None."""
        if not candidate_gdb:
            return None
        try:
            gdb_dir = os.path.dirname(candidate_gdb)
            gdb_name = os.path.basename(candidate_gdb)
            if gdb_dir and not os.path.isdir(gdb_dir):
                os.makedirs(gdb_dir, exist_ok=True)
            if not arcpy.Exists(candidate_gdb):
                if gdb_name.lower().endswith(".gdb"):
                    arcpy.management.CreateFileGDB(gdb_dir, gdb_name)
                else:
                    return None

            # Validate by creating/deleting a tiny table.
            test_table = os.path.join(candidate_gdb, f"_ro_test_{uuid.uuid4().hex[:8]}")
            if arcpy.Exists(test_table):
                arcpy.management.Delete(test_table)
            arcpy.management.CreateTable(candidate_gdb, os.path.basename(test_table))
            if arcpy.Exists(test_table):
                arcpy.management.Delete(test_table)
            return candidate_gdb
        except Exception:
            return None

    def _safe_temp_scratch_gdb() -> str:
        """Return a writable scratch GDB, preferring source dataset workspace."""
        user_temp_gdb = None
        if temp_workspace:
            tw = os.path.normpath(temp_workspace)
            if tw.lower().endswith(".gdb"):
                user_temp_gdb = tw
            else:
                user_temp_gdb = os.path.join(tw, "riverobstacles_scratch.gdb")

        src_gdb = _resolve_fc_gdb(fc_barriers)
        scratch_gdb_env = arcpy.env.scratchGDB
        scratch_folder = getattr(arcpy.env, "scratchFolder", None)

        candidates = [
            user_temp_gdb,
            src_gdb,
            scratch_gdb_env,
            os.path.join(scratch_folder, "riverobstacles_scratch.gdb") if scratch_folder else None,
            os.path.join(tempfile.gettempdir(), "riverobstacles_scratch.gdb"),
        ]

        for candidate in candidates:
            if not candidate:
                continue
            validated = _ensure_writable_gdb(candidate)
            if validated:
                return validated

        raise RuntimeError(
            "Unable to resolve a writable scratch geodatabase. "
            f"Checked candidates: {candidates}"
        )

    def _gp_error_text(ex: Exception) -> str:
        """Return exception text plus ArcPy geoprocessing messages, if any."""
        gp_messages = ""
        try:
            gp_messages = (arcpy.GetMessages(2) or arcpy.GetMessages() or "").strip()
        except Exception:
            gp_messages = ""
        if gp_messages:
            return f"{ex}\nArcPy GP messages: {gp_messages}"
        return str(ex)

    def _safe_count(path: str) -> str:
        try:
            return str(int(arcpy.management.GetCount(path).getOutput(0)))
        except Exception:
            return "unknown"

    def _safe_layer_meta(path: str) -> str:
        try:
            desc = arcpy.Describe(path)
            shape_type = getattr(desc, "shapeType", "unknown")
            sr = getattr(desc, "spatialReference", None)
            sr_name = getattr(sr, "name", "unknown") if sr is not None else "unknown"
            sr_code = getattr(sr, "factoryCode", "unknown") if sr is not None else "unknown"
            return f"shapeType={shape_type}, spatialRef={sr_name} (WKID={sr_code})"
        except Exception:
            return "shapeType=unknown, spatialRef=unknown"

    # ------------------------------------------------------------------ #
    # 1.  Read barriers – collect OIDs, IDs, and passability flags        #
    # ------------------------------------------------------------------ #
    bar_oid_field = arcpy.Describe(fc_barriers).OIDFieldName
    bar_fields = [bar_oid_field, barrier_id_field]
    if passability_field:
        bar_fields.append(passability_field)

    impassable_oids: set = set()
    bar_oid_to_id: Dict[int, str] = {}

    with arcpy.da.SearchCursor(fc_barriers, bar_fields) as cur:
        for row in cur:
            oid = row[0]
            bid = normalize_feature_id(row[1])
            bar_oid_to_id[oid] = bid
            if passability_field:
                pval = row[2]
                if impassable_value is not None:
                    if pval == impassable_value:
                        impassable_oids.add(oid)
                else:
                    if not pval:          # falsy = impassable
                        impassable_oids.add(oid)
            else:
                impassable_oids.add(oid)  # no filter → all are impassable

    if not impassable_oids:
        return []

    # ------------------------------------------------------------------ #
    # 2.  Build OID → reach_id lookup for river network                   #
    # ------------------------------------------------------------------ #
    riv_oid_field = arcpy.Describe(fc_river_network).OIDFieldName
    riv_oid_to_id: Dict[int, str] = {}
    with arcpy.da.SearchCursor(fc_river_network, [riv_oid_field, reach_id_field]) as cur:
        for row in cur:
            riv_oid_to_id[row[0]] = normalize_feature_id(row[1])

    # ------------------------------------------------------------------ #
    # 3.  Generate near table                                              #
    #     arcpy uses its C++ spatial index – memory usage is O(1) in      #
    #     Python regardless of how many reaches or barriers exist.        #
    # ------------------------------------------------------------------ #
    scratch_gdb = _safe_temp_scratch_gdb()
    near_table = os.path.join(scratch_gdb, f"near_table_ro_snap_{uuid.uuid4().hex[:8]}")
    if arcpy.Exists(near_table):
        arcpy.management.Delete(near_table)

    def _copy_features_checked(in_fc: str, out_fc: str) -> None:
        """CopyFeatures with explicit existence check and rich failure detail."""
        try:
            arcpy.management.CopyFeatures(in_fc, out_fc)
        except Exception as ex:
            raise RuntimeError(
                f"CopyFeatures failed: in='{in_fc}' out='{out_fc}' err={_gp_error_text(ex)}"
            )
        if not arcpy.Exists(out_fc):
            raise RuntimeError(
                f"CopyFeatures did not create expected output: '{out_fc}'"
            )

    def _run_generate_near(in_features, near_features, out_table):
        arcpy.analysis.GenerateNearTable(
            in_features=in_features,
            near_features=near_features,
            out_table=out_table,
            search_radius=near_search_radius,
            location="NO_LOCATION",
            angle="NO_ANGLE",
            closest="CLOSEST",
            closest_count=1,
            method=distance_method_norm,
        )

    def _read_generate_near_table(table_path: str) -> List[Dict]:
        near_results: List[Dict] = []
        with arcpy.da.SearchCursor(table_path, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cur:
            for row in cur:
                bar_oid = row[0]
                riv_oid = row[1]
                dist = row[2]
                if riv_oid == -1:               # no match within snap_distance
                    continue
                if bar_oid not in impassable_oids:
                    continue                    # passable barrier – skip
                bid = bar_oid_to_id.get(bar_oid)
                rid = riv_oid_to_id.get(riv_oid)
                if bid is not None and rid is not None:
                    near_results.append(
                        {
                            "barrier_id": bid,
                            "reach_id": rid,
                            "distance": round(dist, 4),
                        }
                    )
        return near_results

    def _run_chunked_local_near(
        local_barriers: str,
        local_network: str,
        impassable_barrier_ids: set,
        chunk_size: int = 25000,
    ) -> List[Dict]:
        """
        Run proximity on local copies in chunks to avoid large monolithic GP calls.
        """
        local_results: List[Dict] = []

        local_bar_oid = arcpy.Describe(local_barriers).OIDFieldName
        local_riv_oid = arcpy.Describe(local_network).OIDFieldName

        local_bar_oid_to_id: Dict[int, str] = {}
        oid_values: List[int] = []
        with arcpy.da.SearchCursor(local_barriers, [local_bar_oid, barrier_id_field]) as cur:
            for row in cur:
                oid = row[0]
                local_bar_oid_to_id[oid] = normalize_feature_id(row[1])
                oid_values.append(oid)

        local_riv_oid_to_id: Dict[int, str] = {}
        with arcpy.da.SearchCursor(local_network, [local_riv_oid, reach_id_field]) as cur:
            for row in cur:
                local_riv_oid_to_id[row[0]] = normalize_feature_id(row[1])

        if not oid_values:
            return local_results

        oid_values.sort()
        layer_name = f"bar_lyr_chunk_{uuid.uuid4().hex[:8]}"
        where_field = arcpy.AddFieldDelimiters(local_barriers, local_bar_oid)

        chunk_errors: List[str] = []
        network_layer = f"riv_lyr_chunk_{uuid.uuid4().hex[:8]}"

        try:
            arcpy.management.MakeFeatureLayer(local_barriers, layer_name)
            arcpy.management.MakeFeatureLayer(local_network, network_layer)
            for idx in range(0, len(oid_values), chunk_size):
                lo = oid_values[idx]
                hi = oid_values[min(idx + chunk_size - 1, len(oid_values) - 1)]
                where = f"{where_field} >= {lo} AND {where_field} <= {hi}"

                arcpy.management.SelectLayerByAttribute(layer_name, "NEW_SELECTION", where)
                if int(arcpy.management.GetCount(layer_name).getOutput(0)) == 0:
                    continue

                # Subset the network to features near the current barrier chunk.
                overlap_type = (
                    "WITHIN_A_DISTANCE_GEODESIC"
                    if distance_method_norm == "GEODESIC"
                    else "WITHIN_A_DISTANCE"
                )
                arcpy.management.SelectLayerByLocation(
                    in_layer=network_layer,
                    overlap_type=overlap_type,
                    select_features=layer_name,
                    search_distance=near_search_radius,
                    selection_type="NEW_SELECTION",
                )
                if int(arcpy.management.GetCount(network_layer).getOutput(0)) == 0:
                    continue

                chunk_network_fc = os.path.join(
                    scratch_gdb, f"riv_chunk_{idx // chunk_size}_{uuid.uuid4().hex[:6]}"
                )
                if arcpy.Exists(chunk_network_fc):
                    arcpy.management.Delete(chunk_network_fc)
                arcpy.management.CopyFeatures(network_layer, chunk_network_fc)

                chunk_near = os.path.join(scratch_gdb, f"near_chunk_{idx // chunk_size}_{uuid.uuid4().hex[:6]}")
                try:
                    _run_generate_near(layer_name, chunk_network_fc, chunk_near)
                    with arcpy.da.SearchCursor(chunk_near, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cur:
                        for row in cur:
                            bid = local_bar_oid_to_id.get(row[0])
                            rid = local_riv_oid_to_id.get(row[1])
                            dist = row[2]
                            if bid is None or rid is None or row[1] == -1:
                                continue
                            if bid not in impassable_barrier_ids:
                                continue
                            local_results.append(
                                {
                                    "barrier_id": bid,
                                    "reach_id": rid,
                                    "distance": round(dist, 4),
                                }
                            )
                except Exception as ex_chunk:
                    # Secondary chunk fallback: run Near on copied chunk features.
                    chunk_fc = os.path.join(scratch_gdb, f"bar_chunk_{idx // chunk_size}_{uuid.uuid4().hex[:6]}")
                    try:
                        _copy_features_checked(layer_name, chunk_fc)
                        arcpy.analysis.Near(
                            in_features=chunk_fc,
                            near_features=chunk_network_fc,
                            search_radius=near_search_radius,
                            location="NO_LOCATION",
                            angle="NO_ANGLE",
                            method=distance_method_norm,
                        )
                        with arcpy.da.SearchCursor(chunk_fc, [barrier_id_field, "NEAR_FID", "NEAR_DIST"]) as cur:
                            for row in cur:
                                bid = normalize_feature_id(row[0])
                                riv_oid = row[1]
                                dist = row[2]
                                if bid not in impassable_barrier_ids:
                                    continue
                                if riv_oid == -1:
                                    continue
                                rid = local_riv_oid_to_id.get(riv_oid)
                                if rid is not None:
                                    local_results.append(
                                        {
                                            "barrier_id": bid,
                                            "reach_id": rid,
                                            "distance": round(dist, 4),
                                        }
                                    )
                    except Exception as ex_chunk_near:
                        chunk_errors.append(
                            f"chunk {idx // chunk_size}: GNT=({_gp_error_text(ex_chunk)}), Near=({_gp_error_text(ex_chunk_near)})"
                        )
                    finally:
                        if arcpy.Exists(chunk_fc):
                            arcpy.management.Delete(chunk_fc)
                finally:
                    if arcpy.Exists(chunk_near):
                        arcpy.management.Delete(chunk_near)
                    if arcpy.Exists(chunk_network_fc):
                        arcpy.management.Delete(chunk_network_fc)
        finally:
            if arcpy.Exists(layer_name):
                arcpy.management.Delete(layer_name)
            if arcpy.Exists(network_layer):
                arcpy.management.Delete(network_layer)

        if chunk_errors and not local_results:
            raise RuntimeError(
                "Chunked local snapping failed for all chunks. "
                f"First chunk errors: {chunk_errors[:3]}"
            )

        return local_results

    def _run_cursor_spatial_fallback(impassable_barrier_ids: set) -> List[Dict]:
        """
        Last-resort fallback that avoids ArcGIS proximity GP tools entirely.

        Uses a barrier-by-barrier spatial-filter cursor query and geometry
        distance checks to find the closest reach within snap_distance.
        """
        cursor_results: List[Dict] = []
        processed = 0

        with arcpy.da.SearchCursor(fc_barriers, [barrier_id_field, "SHAPE@"]) as bcur:
            for b_row in bcur:
                bid = normalize_feature_id(b_row[0])
                if bid not in impassable_barrier_ids:
                    continue

                b_geom = b_row[1]
                if b_geom is None:
                    continue

                processed += 1
                if processed % 10000 == 0:
                    try:
                        arcpy.AddMessage(
                            f"    [fallback] cursor snap processed {processed:,} impassable barriers..."
                        )
                    except Exception:
                        pass

                search_geom = b_geom.buffer(snap_distance)
                best_dist = None
                best_rid = None

                with arcpy.da.SearchCursor(
                    fc_river_network,
                    [reach_id_field, "SHAPE@"],
                    spatial_filter=search_geom,
                    spatial_relationship="INTERSECTS",
                ) as rcur:
                    for r_row in rcur:
                        rid = normalize_feature_id(r_row[0])
                        r_geom = r_row[1]
                        if r_geom is None:
                            continue

                        dist = b_geom.distanceTo(r_geom)
                        if dist is None or dist > snap_distance:
                            continue
                        if best_dist is None or dist < best_dist:
                            best_dist = dist
                            best_rid = rid

                if best_rid is not None and best_dist is not None:
                    cursor_results.append(
                        {
                            "barrier_id": bid,
                            "reach_id": best_rid,
                            "distance": round(float(best_dist), 4),
                        }
                    )

        return cursor_results

    results: List[Dict] = []
    direct_err: Optional[str] = None
    layer_err: Optional[str] = None

    try:
        _run_generate_near(fc_barriers, fc_river_network, near_table)
        results = _read_generate_near_table(near_table)
    except Exception as ex:
        direct_err = _gp_error_text(ex)
        # Some enterprise workspaces can fail when GenerateNearTable targets
        # direct SDE paths with an in_memory output. Retry via feature layers
        # and a scratch GDB table.
        if arcpy.Exists(near_table):
            arcpy.management.Delete(near_table)

        in_lyr = "bar_lyr_ro_near"
        near_lyr = "riv_lyr_ro_near"
        fallback_near_table = os.path.join(scratch_gdb, "near_table_ro_snap")
        if arcpy.Exists(fallback_near_table):
            arcpy.management.Delete(fallback_near_table)

        try:
            arcpy.management.MakeFeatureLayer(fc_barriers, in_lyr)
            arcpy.management.MakeFeatureLayer(fc_river_network, near_lyr)
            _run_generate_near(in_lyr, near_lyr, fallback_near_table)
            results = _read_generate_near_table(fallback_near_table)
        except Exception as ex_layer:
            layer_err = _gp_error_text(ex_layer)
        finally:
            for lyr in (in_lyr, near_lyr):
                if arcpy.Exists(lyr):
                    arcpy.management.Delete(lyr)
            if arcpy.Exists(fallback_near_table):
                arcpy.management.Delete(fallback_near_table)

        if layer_err is not None or not results:
            # If both GenerateNearTable attempts failed in enterprise SDE,
            # fall back to the Near tool on a scratch copy of barriers.
            near_call_err: Optional[str] = None
            try:
                impassable_barrier_ids = {
                    bar_oid_to_id[oid] for oid in impassable_oids if oid in bar_oid_to_id
                }
                temp_barriers = os.path.join(scratch_gdb, f"barriers_near_{uuid.uuid4().hex[:8]}")
                if arcpy.Exists(temp_barriers):
                    arcpy.management.Delete(temp_barriers)
                try:
                    _copy_features_checked(fc_barriers, temp_barriers)
                    try:
                        arcpy.analysis.Near(
                            in_features=temp_barriers,
                            near_features=fc_river_network,
                            search_radius=near_search_radius,
                            location="NO_LOCATION",
                            angle="NO_ANGLE",
                            method=distance_method_norm,
                        )
                    except Exception as near_ex:
                        # Capture GP messages immediately, before cleanup calls
                        # overwrite ArcPy's message buffer.
                        near_call_err = _gp_error_text(near_ex)
                        raise
                    with arcpy.da.SearchCursor(
                        temp_barriers, [barrier_id_field, "NEAR_FID", "NEAR_DIST"]
                    ) as cur:
                        for row in cur:
                            bid = normalize_feature_id(row[0])
                            riv_oid = row[1]
                            dist = row[2]
                            if bid not in impassable_barrier_ids:
                                continue
                            if riv_oid == -1:
                                continue
                            rid = riv_oid_to_id.get(riv_oid)
                            if rid is not None:
                                results.append(
                                    {
                                        "barrier_id": bid,
                                        "reach_id": rid,
                                        "distance": round(dist, 4),
                                    }
                                )
                finally:
                    if arcpy.Exists(temp_barriers):
                        arcpy.management.Delete(temp_barriers)
            except Exception as ex2:
                near_err = near_call_err or _gp_error_text(ex2)

                # Final fallback: copy both barriers and network to local scratch
                # workspace and run GenerateNearTable entirely on local data.
                local_copy_err: Optional[str] = None
                try:
                    local_barriers = os.path.join(
                        scratch_gdb, f"barriers_local_{uuid.uuid4().hex[:8]}"
                    )
                    local_network = os.path.join(
                        scratch_gdb, f"network_local_{uuid.uuid4().hex[:8]}"
                    )
                    local_near_table = os.path.join(
                        scratch_gdb, f"near_local_{uuid.uuid4().hex[:8]}"
                    )

                    for p in (local_barriers, local_network, local_near_table):
                        if arcpy.Exists(p):
                            arcpy.management.Delete(p)

                    try:
                        _copy_features_checked(fc_barriers, local_barriers)
                        _copy_features_checked(fc_river_network, local_network)
                        used_chunked_local = False
                        try:
                            _run_generate_near(local_barriers, local_network, local_near_table)
                        except Exception as ex_local_near:
                            # Capture GP failure details before cleanup operations
                            # overwrite ArcPy's message buffer.
                            local_copy_err = _gp_error_text(ex_local_near)
                            # Monolithic local run can fail on very large datasets;
                            # retry with chunked local processing.
                            chunked_results = _run_chunked_local_near(
                                local_barriers=local_barriers,
                                local_network=local_network,
                                impassable_barrier_ids=impassable_barrier_ids,
                            )
                            if chunked_results:
                                results.extend(chunked_results)
                                used_chunked_local = True
                            else:
                                raise

                        if not used_chunked_local:
                            local_bar_oid = arcpy.Describe(local_barriers).OIDFieldName
                            local_riv_oid = arcpy.Describe(local_network).OIDFieldName

                            local_bar_oid_to_id: Dict[int, str] = {}
                            with arcpy.da.SearchCursor(
                                local_barriers, [local_bar_oid, barrier_id_field]
                            ) as cur:
                                for row in cur:
                                    local_bar_oid_to_id[row[0]] = normalize_feature_id(row[1])

                            local_riv_oid_to_id: Dict[int, str] = {}
                            with arcpy.da.SearchCursor(
                                local_network, [local_riv_oid, reach_id_field]
                            ) as cur:
                                for row in cur:
                                    local_riv_oid_to_id[row[0]] = normalize_feature_id(row[1])

                            with arcpy.da.SearchCursor(
                                local_near_table, ["IN_FID", "NEAR_FID", "NEAR_DIST"]
                            ) as cur:
                                for row in cur:
                                    bid = local_bar_oid_to_id.get(row[0])
                                    rid = local_riv_oid_to_id.get(row[1])
                                    dist = row[2]
                                    if bid is None or rid is None or row[1] == -1:
                                        continue
                                    if bid not in impassable_barrier_ids:
                                        continue
                                    results.append(
                                        {
                                            "barrier_id": bid,
                                            "reach_id": rid,
                                            "distance": round(dist, 4),
                                        }
                                    )
                    finally:
                        for p in (local_near_table, local_barriers, local_network):
                            if arcpy.Exists(p):
                                arcpy.management.Delete(p)

                except Exception as ex_local:
                    if local_copy_err is None:
                        local_copy_err = _gp_error_text(ex_local)

                if not results:
                    cursor_fallback_err: Optional[str] = None
                    if enable_cursor_fallback:
                        if distance_method_norm == "GEODESIC":
                            arcpy.AddWarning(
                                "Cursor fallback uses geometry distanceTo/buffer and may "
                                "not exactly match geodesic Near calculations."
                            )
                        try:
                            arcpy.AddMessage(
                                "    [fallback] Attempting cursor-based snapping (no Near/GenerateNearTable)..."
                            )
                        except Exception:
                            pass

                        try:
                            cursor_results = _run_cursor_spatial_fallback(impassable_barrier_ids)
                            if cursor_results:
                                results.extend(cursor_results)
                        except Exception as ex_cursor:
                            cursor_fallback_err = _gp_error_text(ex_cursor)
                    else:
                        cursor_fallback_err = (
                            "disabled (set enable_cursor_fallback=True to allow this very slow fallback)"
                        )

                if not results:
                    msg = (
                        "Snapping failed after multiple fallbacks: "
                        f"direct GenerateNearTable error=({direct_err}), "
                        f"layer fallback error=({layer_err}), "
                        f"Near fallback error=({near_err}), "
                        f"local-copy GenerateNearTable error=({local_copy_err}), "
                        f"cursor spatial fallback error=({cursor_fallback_err}). "
                        "Context: "
                        f"barriers='{fc_barriers}' (count={_safe_count(fc_barriers)}), "
                        f"network='{fc_river_network}' (count={_safe_count(fc_river_network)}), "
                        f"barriers_meta=({_safe_layer_meta(fc_barriers)}), "
                        f"network_meta=({_safe_layer_meta(fc_river_network)}), "
                        f"scratchGDB='{scratch_gdb}', "
                        f"snap_distance={snap_distance}, "
                        f"distance_method={distance_method_norm}, "
                        f"effective_search_radius={near_search_radius!r}."
                    )
                    raise RuntimeError(msg) from ex2

    # ------------------------------------------------------------------ #
    # 4.  Read near table, filter to impassable barriers                  #
    # ------------------------------------------------------------------ #
    if arcpy.Exists(near_table):
        arcpy.management.Delete(near_table)

    results.sort(key=lambda r: (r["barrier_id"], r["distance"]))
    return results


# ---------------------------------------------------------------------------
# Reduce to one barrier per reach (lowest / nearest to downstream end)
# ---------------------------------------------------------------------------

def select_first_barrier_per_reach(
    snapped: List[Dict],
    topology: dict,
    fc_barriers: str,
    fc_river_network: str,
    barrier_id_field: str,
    reach_id_field: str = "riverID",
) -> Dict[str, str]:
    """
    When multiple barriers snap to the same reach, keep the one that is
    closest to the *downstream* end of that reach (i.e., the first barrier
    a fish would encounter swimming upstream).

    Returns
    -------
    dict
        ``{reach_id: barrier_id}``  – at most one barrier per reach.
    """
    import arcpy

    # Group snapped barriers by reach
    per_reach: Dict[str, List[Dict]] = collections.defaultdict(list)
    for snap in snapped:
        per_reach[snap["reach_id"]].append(snap)

    # For reaches with only one barrier, keep it directly
    result: Dict[str, str] = {}
    multi_reach_ids = []

    for rid, snaps in per_reach.items():
        if len(snaps) == 1:
            result[rid] = snaps[0]["barrier_id"]
        else:
            multi_reach_ids.append(rid)

    if not multi_reach_ids:
        return result

    # For multi-barrier reaches: project each barrier point onto the reach
    # polyline and keep the downstream-most one, i.e. the first barrier a fish
    # encounters while moving upstream from the outlet.
    multi_barrier_ids = {
        snap["barrier_id"]
        for rid in multi_reach_ids
        for snap in per_reach[rid]
    }
    barrier_xy = snap_barriers_with_coords(fc_barriers, barrier_id_field)
    reach_shapes: Dict[str, object] = {}
    with arcpy.da.SearchCursor(fc_river_network, [reach_id_field, "SHAPE@"]) as cur:
        for row in cur:
            rid = normalize_feature_id(row[0])
            if rid in multi_reach_ids and row[1] is not None:
                reach_shapes[rid] = row[1]

    for rid in multi_reach_ids:
        snaps = per_reach[rid]
        shape = reach_shapes.get(rid)
        if shape is None:
            snaps_sorted = sorted(snaps, key=lambda s: s["distance"])
            result[rid] = snaps_sorted[0]["barrier_id"]
            continue

        best_barrier_id = None
        best_downstream_distance = None
        for snap in snaps:
            bid = snap["barrier_id"]
            if bid not in multi_barrier_ids:
                continue
            xy = barrier_xy.get(bid)
            if xy is None:
                continue
            distance_from_start = _distance_along_polyline(shape, xy)
            if distance_from_start is None:
                continue
            distance_from_downstream_end = abs(shape.length - distance_from_start)
            if (
                best_downstream_distance is None
                or distance_from_downstream_end < best_downstream_distance
            ):
                best_downstream_distance = distance_from_downstream_end
                best_barrier_id = bid

        if best_barrier_id is None:
            snaps_sorted = sorted(snaps, key=lambda s: s["distance"])
            result[rid] = snaps_sorted[0]["barrier_id"]
        else:
            result[rid] = best_barrier_id

    return result


def snap_barriers_with_coords(
    fc_barriers: str,
    barrier_id_field: str,
) -> Dict[str, Tuple[float, float]]:
    """Return {barrier_id: (x, y)} for all barriers."""
    import arcpy

    coords: Dict[str, Tuple[float, float]] = {}
    with arcpy.da.SearchCursor(fc_barriers, [barrier_id_field, "SHAPE@XY"]) as cur:
        for row in cur:
            if row[1]:
                coords[normalize_feature_id(row[0])] = (row[1][0], row[1][1])
    return coords
