"""
arcgis_io.py
------------
Read from and write results to an ArcGIS Enterprise geodatabase or feature
service.

Two access modes are supported:

1. **arcpy mode** (default for server-side scripts / GP tools):
   Uses arcpy.da cursors to read and update the enterprise geodatabase
   directly.  Requires ArcGIS Pro / ArcGIS Server Python environment.

2. **REST mode** (optional, for remote scripts):
   Uses the ``arcgis`` Python API to read / update layers through the
   feature-service REST endpoint.  Useful when the script runs on a machine
   that does not have arcpy installed.  Pass ``use_rest=True`` and supply a
   ``GIS`` object (from ``arcgis.gis import GIS``).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Set

from .id_utils import normalize_feature_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _edit_and_run(workspace: str, callback) -> None:
    """
    Open an explicit edit session with startOperation/stopOperation and run
    *callback* inside it.

    Tries a small set of Editor mode combinations (with_undo and
    multiuser_mode) to handle differences between file geodatabases and
    enterprise/versioned workspaces.
    """
    import arcpy

    ws_type = ""
    try:
        ws_type = arcpy.Describe(workspace).workspaceType
    except Exception:
        pass

    # Remote databases are typically versioned; file geodatabases are not.
    # Some enterprise/file-GDB combinations still behave differently, so we
    # try a small set of modes when read-only/runtime edit-mode errors occur.
    preferred_multiuser = ws_type == "RemoteDatabase"
    edit_modes = [
        (False, preferred_multiuser),
        (True, preferred_multiuser),
    ]
    if preferred_multiuser:
        edit_modes.extend([(False, False), (True, False)])
    else:
        edit_modes.extend([(False, True), (True, True)])

    last_error = None
    for with_undo, multiuser_mode in edit_modes:
        editor = arcpy.da.Editor(workspace)
        try:
            editor.startEditing(with_undo, multiuser_mode)
            editor.startOperation()
            callback()
            editor.stopOperation()
            editor.stopEditing(True)
            return
        except Exception as ex:
            last_error = ex
            try:
                editor.stopOperation()
            except Exception:
                pass
            try:
                editor.stopEditing(False)
            except Exception:
                pass

            msg = str(ex).lower()
            retriable = (
                "read only" in msg
                or "outside an edit session" in msg
                or "cannot be updated" in msg
                or "cannot update the table" in msg
            )
            if not retriable:
                raise

    if last_error is not None:
        raise last_error


def _resolve_fc_and_workspace(fc: str):
    """
    Resolve a potentially ambiguous FC reference (layer name or dotted SDE
    path) to ``(fc_for_cursor, workspace_path)`` suitable for use with
    ``arcpy.da.Editor`` and ``arcpy.da.UpdateCursor``.

    Key challenge: for SDE layers added to ArcGIS Pro, ``arcpy.Describe``
    resolves the layer to a *local read-only replica* in a file GDB, while
    ``arcpy.da.UpdateCursor`` uses the original string to hit the *writable
    SDE database*.  Passing the replica path to both cursor and Editor causes
    an apparent mismatch: the Editor opens on the read-only GDB, the cursor
    tries to write to SDE (which has no active edit session) → "read only".

    Resolution strategy:
    1.  Resolve ``catalogPath`` as usual.
    2.  If the resolved workspace is a *.gdb* (local) **and** the original
        ``fc`` string contains ``.sde`` (SDE reference), try
        ``arcpy.Describe(sde_name).catalogPath`` to obtain the *full* path
        to the SDE connection file on disk.
    3.  When the SDE path is found, return ``(fc, sde_workspace)`` so that
        the cursor uses the SDE path (writable) and the Editor opens on the
        same SDE workspace.
    4.  Otherwise fall back to the catalog path + its containing workspace.
    """
    import arcpy
    import os
    import re

    try:
        cat = arcpy.Describe(fc).catalogPath
    except Exception:
        cat = fc  # fallback: use original string as-is

    # Walk up from the feature class to the containing .gdb / .sde / .mdb
    candidate = os.path.dirname(cat)
    while candidate:
        lower = candidate.lower()
        if lower.endswith(('.gdb', '.sde', '.mdb')):
            # If the catalog resolved to a local GDB but the original fc
            # string references an SDE connection, try to find the full path
            # to the SDE connection file so the Editor and cursor both target
            # the writable SDE database rather than the local read-only copy.
            if lower.endswith('.gdb'):
                sde_match = re.search(r'^(.*?\.sde)(?:[/\\.]|$)', fc, re.IGNORECASE)
                if sde_match:
                    sde_ref = sde_match.group(1)   # e.g. "dbtrutta.sde"
                    try:
                        sde_workspace = arcpy.Describe(sde_ref).catalogPath
                        # Return the original fc (cursor will hit SDE) and
                        # the resolved SDE path (Editor workspace).
                        return fc, sde_workspace
                    except Exception:
                        pass   # SDE not resolvable; fall through to GDB path
            return cat, candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:       # reached the filesystem root
            break
        candidate = parent

    return cat, os.path.dirname(cat)  # last-resort fallback


def get_editor_tracking_state(fc: str) -> Optional[Dict]:
    """Return editor tracking settings for *fc* if enabled, otherwise None."""
    import arcpy

    try:
        desc = arcpy.Describe(fc)
    except Exception:
        return None

    if not bool(getattr(desc, "editorTrackingEnabled", False)):
        return None

    def _pick(*names, default=None):
        for name in names:
            value = getattr(desc, name, None)
            if value not in (None, ""):
                return value
        return default

    return {
        "enabled": True,
        "creator_field": _pick("creatorFieldName", "creatorField", default=None),
        "creation_date_field": _pick("creationDateFieldName", "createdAtFieldName", "creationDateField", default=None),
        "editor_field": _pick("editorFieldName", "editorField", default=None),
        "edit_date_field": _pick("editDateFieldName", "editedAtFieldName", "editDateField", default=None),
        "record_dates_in_utc": bool(_pick("isTimeInUTC", "recordDatesInUTC", default=True)),
    }


def disable_editor_tracking(fc: str, log_callback=None) -> Optional[Dict]:
    """Disable editor tracking on *fc* and return prior settings if enabled."""
    import arcpy

    state = get_editor_tracking_state(fc)
    if not state:
        if log_callback:
            log_callback(f"    Editor tracking already disabled on {fc}")
        return None

    arcpy.management.DisableEditorTracking(fc)
    if log_callback:
        log_callback(f"    Disabled editor tracking on {fc}")
    return state


def restore_editor_tracking(fc: str, state: Optional[Dict], log_callback=None) -> None:
    """Restore editor tracking on *fc* from a state returned by get_editor_tracking_state()."""
    import arcpy

    if not state or not state.get("enabled"):
        return

    creator_field = state.get("creator_field")
    creation_date_field = state.get("creation_date_field")
    editor_field = state.get("editor_field")
    edit_date_field = state.get("edit_date_field")

    if not all([creator_field, creation_date_field, editor_field, edit_date_field]):
        raise RuntimeError(
            f"Cannot restore editor tracking on {fc}: missing one or more field names in saved state."
        )

    record_dates_in = "UTC" if state.get("record_dates_in_utc", True) else "LOCAL_TIME"
    arcpy.management.EnableEditorTracking(
        fc,
        creator_field,
        creation_date_field,
        editor_field,
        edit_date_field,
        "NO_ADD_FIELDS",
        record_dates_in,
    )
    if log_callback:
        log_callback(f"    Restored editor tracking on {fc}")


# ---------------------------------------------------------------------------
# Field management helpers
# ---------------------------------------------------------------------------

def _make_unique_field_name(fc: str, preferred_name: str) -> str:
    import arcpy

    existing = {f.name.lower() for f in arcpy.ListFields(fc)}
    if preferred_name.lower() not in existing:
        return preferred_name

    base = preferred_name[:54]
    i = 1
    while True:
        candidate = f"{base}_{i}"
        if candidate.lower() not in existing:
            return candidate
        i += 1


def ensure_long_field_compatible(
    fc: str,
    field_name: str,
    field_alias: str = "Total Upstream Segments",
) -> str:
    """
    Ensure a LONG-compatible field exists for upstream counts.

    Returns the field name that should be used for writing counts. If the
    existing field is incompatible (e.g. SHORT/TEXT), a replacement LONG field
    is created and returned.
    """
    import arcpy

    fields = {f.name.lower(): f for f in arcpy.ListFields(fc)}
    field = fields.get(field_name.lower())

    if field is None:
        arcpy.management.AddField(
            in_table=fc,
            field_name=field_name,
            field_type="LONG",
            field_alias=field_alias,
        )
        return field_name

    # ArcPy reports LONG as Integer.
    if field.type == "Integer":
        return field.name

    # Try to preserve the original field name by renaming the incompatible one.
    backup_name = _make_unique_field_name(fc, f"{field_name}_old")
    try:
        arcpy.management.AlterField(
            in_table=fc,
            field=field.name,
            new_field_name=backup_name,
        )
        arcpy.management.AddField(
            in_table=fc,
            field_name=field_name,
            field_type="LONG",
            field_alias=field_alias,
        )
        return field_name
    except Exception:
        # If rename is blocked by permissions/locks, create a fallback LONG field.
        fallback_name = _make_unique_field_name(fc, f"{field_name}_long")
        arcpy.management.AddField(
            in_table=fc,
            field_name=fallback_name,
            field_type="LONG",
            field_alias=f"{field_alias} (Long)",
        )
        return fallback_name

def ensure_fields(
    fc: str,
    field_specs: List[Dict],
) -> None:
    """
    Add fields to *fc* if they do not already exist.

    Parameters
    ----------
    fc : str
        Path to the feature class.
    field_specs : list of dict
        Each dict: ``{'name': str, 'type': str, 'alias': str, 'length': int}``
        where *type* is an arcpy field type string
        (``'SHORT'``, ``'LONG'``, ``'TEXT'``, ``'DOUBLE'``, …).
    """
    import arcpy

    existing = {f.name.lower() for f in arcpy.ListFields(fc)}
    for spec in field_specs:
        if spec["name"].lower() not in existing:
            kwargs = {
                "in_table": fc,
                "field_name": spec["name"],
                "field_type": spec.get("type", "SHORT"),
                "field_alias": spec.get("alias", spec["name"]),
            }
            if "length" in spec:
                kwargs["field_length"] = spec["length"]
            arcpy.management.AddField(**kwargs)


# ---------------------------------------------------------------------------
# River network updates
# ---------------------------------------------------------------------------

def update_river_network_results(
    fc_river_network: str,
    reach_id_field: str,
    accessible_ids: Set[str],
    first_barrier_reach_ids: Set[str],
    upstream_counts: Optional[Dict[str, int]] = None,
    accessibility_field: str = "is_accessible",
    first_barrier_reach_field: str = "is_first_barrier_reach",
    upstream_count_field: str = "tot_up_segments",
    accessible_update_field: str = "is_accessible_update",
    progress_every: int = 50000,
    max_write_seconds: Optional[float] = 10800,
    log_callback=None,
    edit_workspace: Optional[str] = None,
    _cursor_only: bool = False,
    _resolved_fc: Optional[str] = None,
) -> int:
    """
    Write accessibility results back to the river network feature class.

    Parameters
    ----------
    fc_river_network : str
        Path to the polyline feature class.
    reach_id_field : str
        Name of the unique reach identifier field.
    accessible_ids : set[str]
        Reach IDs classified as accessible from sea.
    first_barrier_reach_ids : set[str]
        Reach IDs where the first barrier sits.
    upstream_counts : dict | None
        Optional ``{reach_id: int}`` – total upstream segment count per reach.
        If supplied, written to *upstream_count_field*.
    accessibility_field : str
        Short integer field: 1 = accessible, 0 = not accessible.
    first_barrier_reach_field : str
        Short integer field: 1 = first barrier reach, 0 = not.
    upstream_count_field : str
        Long integer field for upstream segment count.
    accessible_update_field : str
        Short integer field written by the upstream-trace step:
        1 = reach is accessible from the tidal outlet tracing upstream and
        stopping at every impassable barrier (passable barriers are
        transparent to the trace), 0 = not accessible.

    Returns
    -------
    int
        Number of rows updated.
    """
    import arcpy
    import os
    import re

    if _cursor_only and _resolved_fc:
        # Called from run_analysis inside a combined edit session; skip resolve,
        # ensure_fields, and editor management – just run the cursor.
        actual_fc = _resolved_fc
        workspace = None
    elif edit_workspace:
        fc_name = re.split(r'[./\\]', fc_river_network)[-1]
        actual_fc = os.path.join(edit_workspace, fc_name)
        workspace = edit_workspace
    else:
        actual_fc, workspace = _resolve_fc_and_workspace(fc_river_network)

    if not _cursor_only:
        field_specs = [
            {"name": accessibility_field, "type": "SHORT", "alias": "Is Accessible"},
            {"name": first_barrier_reach_field, "type": "SHORT",
             "alias": "Is First Barrier Reach"},
            {"name": accessible_update_field, "type": "SHORT",
             "alias": "Is Accessible (Upstream Trace)"},
        ]
        if upstream_counts is not None:
            field_specs.append(
                {"name": upstream_count_field, "type": "LONG",
                 "alias": "Total Upstream Segments"}
            )
        ensure_fields(actual_fc, field_specs)

    base_fields = [
        reach_id_field,
        accessibility_field,
        first_barrier_reach_field,
        accessible_update_field,
    ]
    count = 0

    # arcpy.da.Editor context manager only calls startEditing(), not
    # startOperation().  Feature classes that participate in a topology,
    # network dataset, or replica require an explicit startOperation() before
    # any row updates are accepted.  Use the lower-level API directly.
    ws_type = ""
    try:
        ws_type = arcpy.Describe(workspace).workspaceType if workspace else ""
    except Exception:
        pass
    needs_editor = ws_type == "RemoteDatabase" or bool(edit_workspace)

    def _run_cursor(include_upstream_counts: bool) -> None:
        nonlocal count
        update_fields = list(base_fields)
        if include_upstream_counts:
            update_fields.append(upstream_count_field)

        count = 0
        scanned = 0
        t_start = time.time()

        def _emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)
            else:
                arcpy.AddMessage(msg)

        with arcpy.da.UpdateCursor(actual_fc, update_fields) as cur:
            for row in cur:
                rid = normalize_feature_id(row[0])
                new_accessible = 1 if rid in accessible_ids else 0
                new_first = 1 if rid in first_barrier_reach_ids else 0
                new_accessible_update = 1 if rid in accessible_ids else 0

                changed = (
                    row[1] != new_accessible
                    or row[2] != new_first
                    or row[3] != new_accessible_update
                )

                row[1] = new_accessible
                row[2] = new_first
                row[3] = new_accessible_update

                if include_upstream_counts:
                    new_up_count = int(upstream_counts.get(rid, 0)) if upstream_counts else 0
                    if row[4] != new_up_count:
                        changed = True
                    row[4] = new_up_count

                if changed:
                    cur.updateRow(row)
                    count += 1

                scanned += 1

                if progress_every and scanned % progress_every == 0:
                    _emit(
                        f"    River write progress: scanned {scanned:,}, updated {count:,} rows in {time.time() - t_start:.1f} s"
                    )

                if max_write_seconds and (time.time() - t_start) > max_write_seconds:
                    raise RuntimeError(
                        "River network write timed out after "
                        f"{max_write_seconds:.0f} s (scanned {scanned:,}, updated {count:,} rows)."
                    )

        _emit(
            f"    River write complete: scanned {scanned:,}, updated {count:,} rows in {time.time() - t_start:.1f} s"
        )

    def _run_with_fallback() -> None:
        if upstream_counts is not None:
            try:
                _run_cursor(True)
            except RuntimeError as ex:
                msg = str(ex).lower()
                if "incompatible with the field type" in msg and upstream_count_field.lower() in msg:
                    arcpy.AddWarning(
                        f"Field '{upstream_count_field}' is incompatible with computed upstream counts. "
                        "Proceeding without writing upstream counts."
                    )
                    _run_cursor(False)
                else:
                    raise
        else:
            _run_cursor(False)

    if _cursor_only:
        # Editor is managed by the caller (combined session); just run the cursor.
        _run_with_fallback()
        return count

    _edit_and_run(workspace, _run_with_fallback)
    return count


# ---------------------------------------------------------------------------
# Barrier updates
# ---------------------------------------------------------------------------

def update_barrier_results(
    fc_barriers: str,
    barrier_id_field: str,
    first_barrier_ids: List[str],
    snapped_reach_map: Dict[str, str],
    first_barrier_field: str = "is_first_barrier",
    snapped_reach_field: str = "snapped_reach_id",
    progress_every: int = 50000,
    max_write_seconds: Optional[float] = 10800,
    log_callback=None,
    edit_workspace: Optional[str] = None,
    _cursor_only: bool = False,
    _resolved_fc: Optional[str] = None,
) -> int:
    """
    Write first-barrier flags back to the barriers feature class.

    Parameters
    ----------
    fc_barriers : str
        Path to the barriers point feature class.
    barrier_id_field : str
        Unique barrier identifier field.
    first_barrier_ids : list[str]
        Barrier IDs classified as first barriers.
    snapped_reach_map : dict
        ``{barrier_id: reach_id}`` for all snapped barriers.
    first_barrier_field : str
        Short integer field: 1 = first barrier, 0 = not / not snapped.
    snapped_reach_field : str
        Text field recording the reach ID each barrier was snapped to.

    Returns
    -------
    int
        Number of rows updated.
    """
    import arcpy
    import os
    import re

    if _cursor_only and _resolved_fc:
        actual_fc = _resolved_fc
        workspace = None
    elif edit_workspace:
        fc_name = re.split(r'[./\\]', fc_barriers)[-1]
        actual_fc = os.path.join(edit_workspace, fc_name)
        workspace = edit_workspace
    else:
        actual_fc, workspace = _resolve_fc_and_workspace(fc_barriers)

    if not _cursor_only:
        ensure_fields(
            actual_fc,
            [
                {"name": first_barrier_field, "type": "SHORT",
                 "alias": "Is First Barrier"},
                {"name": snapped_reach_field, "type": "TEXT",
                 "alias": "Snapped Reach ID", "length": 64},
            ],
        )

    first_set = {normalize_feature_id(b) for b in first_barrier_ids}

    count = 0

    def _run_cursor():
        nonlocal count
        count = 0
        scanned = 0
        t_start = time.time()

        def _emit(msg: str) -> None:
            if log_callback:
                log_callback(msg)
            else:
                arcpy.AddMessage(msg)

        with arcpy.da.UpdateCursor(
            actual_fc, [barrier_id_field, first_barrier_field, snapped_reach_field]
        ) as cur:
            try:
                for row in cur:
                    bid = normalize_feature_id(row[0])
                    new_first = 1 if bid in first_set else 0
                    new_snapped_reach = snapped_reach_map.get(bid, "")
                    old_snapped_reach = row[2] if row[2] is not None else ""

                    if row[1] != new_first or old_snapped_reach != new_snapped_reach:
                        row[1] = new_first
                        row[2] = new_snapped_reach
                        cur.updateRow(row)
                        count += 1

                    scanned += 1

                    if progress_every and scanned % progress_every == 0:
                        _emit(
                            f"    Barrier write progress: scanned {scanned:,}, updated {count:,} rows in {time.time() - t_start:.1f} s"
                        )

                    if max_write_seconds and (time.time() - t_start) > max_write_seconds:
                        raise RuntimeError(
                            "Barrier write timed out after "
                            f"{max_write_seconds:.0f} s (scanned {scanned:,}, updated {count:,} rows)."
                        )
            finally:
                del cur

        _emit(
            f"    Barrier write complete: scanned {scanned:,}, updated {count:,} rows in {time.time() - t_start:.1f} s"
        )

    if _cursor_only:
        _run_cursor()
        return count

    _edit_and_run(workspace, _run_cursor)
    return count


# ---------------------------------------------------------------------------
# REST mode helpers (arcgis Python API)
# ---------------------------------------------------------------------------

def update_layer_via_rest(
    layer,                     # arcgis.features.FeatureLayer
    oid_field: str,
    accessible_ids: Set[str],
    first_barrier_reach_ids: Set[str],
    reach_id_field: str,
    accessibility_field: str = "is_accessible",
    first_barrier_reach_field: str = "is_first_barrier_reach",
    chunk_size: int = 500,
) -> dict:
    """
    Update river network accessibility results via ArcGIS REST API
    (no arcpy required).

    Parameters
    ----------
    layer : arcgis.features.FeatureLayer
        The published river-network feature layer.
    oid_field : str
        Name of the ObjectID field in the layer.
    accessible_ids : set[str]
        Reach IDs that are accessible.
    first_barrier_reach_ids : set[str]
        Reach IDs that are first-barrier reaches.
    reach_id_field : str
        Field name for the user-defined reach identifier (not OID).
    chunk_size : int
        Number of features to update per REST call.

    Returns
    -------
    dict
        REST API response summary.
    """
    from arcgis.features import Feature

    # Query all features to get OID → reach_id mapping
    result = layer.query(out_fields=[oid_field, reach_id_field], return_geometry=False)
    features = result.features

    updates = []
    for feat in features:
        rid = normalize_feature_id(feat.attributes.get(reach_id_field, ""))
        is_acc = 1 if rid in accessible_ids else 0
        is_first = 1 if rid in first_barrier_reach_ids else 0
        oid = feat.attributes[oid_field]
        updates.append(
            Feature(
                attributes={
                    oid_field: oid,
                    accessibility_field: is_acc,
                    first_barrier_reach_field: is_first,
                }
            )
        )

    # Apply edits in chunks to avoid request size limits
    responses = []
    for i in range(0, len(updates), chunk_size):
        chunk = updates[i: i + chunk_size]
        resp = layer.edit_features(updates=chunk)
        responses.append(resp)

    success = sum(
        len(r.get("updateResults", [])) for r in responses
        if isinstance(r, dict)
    )
    return {"features_updated": success, "chunks": len(responses)}
