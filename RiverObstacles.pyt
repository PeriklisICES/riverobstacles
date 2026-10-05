# -*- coding: utf-8 -*-
"""
RiverObstacles.pyt
------------------
ArcGIS Python Toolbox.

Publish this toolbox as a Geoprocessing Service on ArcGIS Enterprise to make
the analysis available as a REST endpoint that can be called programmatically
(e.g., from a webhook receiver or a Field Maps / Experience Builder workflow).

Usage
-----
1.  Open ArcGIS Pro → Catalog pane → right-click this .pyt file → Open.
2.  Run the "Identify First Barriers" tool with your parameters.
3.  To publish: right-click the result in the History pane → Share As → 
    Geoprocessing Service.
"""

import os
import sys
import hashlib

# Make the riverobstacles package importable from the toolbox location
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)


class Toolbox:
    def __init__(self):
        self.label = "River Obstacles"
        self.alias = "riverobstacles"
        self.tools = [IdentifyFirstBarriers]


class IdentifyFirstBarriers:
    """
    Identify accessible river reaches and the first impassable barrier
    upstream from each tidal outlet.

    The tool writes two output fields to the river network feature class
    (``is_accessible``, ``is_first_barrier_reach``) and two fields to the
    barriers feature class (``is_first_barrier``, ``snapped_reach_id``).
    """

    def __init__(self):
        self.label = "Identify First Barriers"
        self.description = (
            "Identifies accessible river reaches and the first impassable "
            "barrier upstream from the tidal limit in a multi-threaded "
            "digital river network stored in an Enterprise geodatabase.\n\n"
            "No Trace Network or Network Analyst licence required."
        )
        self.canRunInBackground = True

    # ------------------------------------------------------------------
    def _runtime_code_version(self):
        """Build a lightweight fingerprint from local toolbox/package source files."""
        entries = []

        # Include the toolbox itself and all module files under riverobstacles/.
        paths = [os.path.abspath(__file__)]
        pkg_dir = os.path.join(_here, "riverobstacles")
        if os.path.isdir(pkg_dir):
            for name in sorted(os.listdir(pkg_dir)):
                if name.lower().endswith(".py"):
                    paths.append(os.path.join(pkg_dir, name))

        for p in paths:
            try:
                st = os.stat(p)
                rel = os.path.relpath(p, _here).replace("\\", "/")
                entries.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                # File might be transiently unavailable; ignore it.
                continue

        payload = "|".join(entries)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
        return digest

    # ------------------------------------------------------------------
    def getParameterInfo(self):
        import arcpy

        params = []

        # 0 – River network
        p = arcpy.Parameter(
            displayName="River Network (polyline)",
            name="river_network",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        p.filter.list = ["Polyline"]
        params.append(p)

        # 1 – Reach ID field
        p = arcpy.Parameter(
            displayName="Reach ID Field",
            name="reach_id_field",
            datatype="Field",
            parameterType="Required",
            direction="Input",
        )
        p.parameterDependencies = ["river_network"]
        p.value = "riverID"
        params.append(p)

        # 2 – Barriers
        p = arcpy.Parameter(
            displayName="Barriers (point)",
            name="barriers",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input",
        )
        p.filter.list = ["Point"]
        params.append(p)

        # 3 – Barrier ID field
        p = arcpy.Parameter(
            displayName="Barrier ID Field",
            name="barrier_id_field",
            datatype="Field",
            parameterType="Required",
            direction="Input",
        )
        p.parameterDependencies = ["barriers"]
        p.value = "unique_id_row"
        params.append(p)

        # 4 – Passability field (optional)
        p = arcpy.Parameter(
            displayName="Passability Field (optional – filter to impassable only)",
            name="passability_field",
            datatype="Field",
            parameterType="Optional",
            direction="Input",
        )
        p.parameterDependencies = ["barriers"]
        params.append(p)

        # 5 – Impassable value
        p = arcpy.Parameter(
            displayName="Impassable Field Value (e.g. 0, False, or a category string)",
            name="impassable_value",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        p.value = "0"
        params.append(p)

        # 6 – Snap distance
        p = arcpy.Parameter(
            displayName="Barrier Snap Distance",
            name="snap_distance",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p.value = 50.0
        params.append(p)

        # 7 – Distance method
        p = arcpy.Parameter(
            displayName="Snap Distance Method",
            name="distance_method",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p.filter.type = "ValueList"
        p.filter.list = ["PLANAR", "GEODESIC"]
        p.value = "PLANAR"
        params.append(p)

        # 8 – Vertex density
        p = arcpy.Parameter(
            displayName="Vertex Density – max spacing for snapping (map units)",
            name="vertex_density",
            datatype="GPDouble",
            parameterType="Required",
            direction="Input",
        )
        p.value = 10.0
        params.append(p)

        # 9 – Topology source
        p = arcpy.Parameter(
            displayName="Topology Source",
            name="topology_source",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        p.filter.type = "ValueList"
        p.filter.list = [
            "geometry – derive from polyline endpoints (recommended)",
            "fields   – read from existing NEXT / PREVIOUS attribute fields",
        ]
        p.value = "geometry – derive from polyline endpoints (recommended)"
        params.append(p)

        # 10 – NEXT field (only used when topology_source = 'fields')
        p = arcpy.Parameter(
            displayName="Downstream Reach ID Field (NEXT) – if using field topology",
            name="next_field",
            datatype="Field",
            parameterType="Optional",
            direction="Input",
        )
        p.parameterDependencies = ["river_network"]
        p.value = "NEXT"
        params.append(p)

        # 11 – River group field (optional)
        p = arcpy.Parameter(
            displayName="River Group / Catchment Field (optional – process one catchment)",
            name="river_group_field",
            datatype="Field",
            parameterType="Optional",
            direction="Input",
        )
        p.parameterDependencies = ["river_network"]
        params.append(p)

        # 12 – River group ID value
        p = arcpy.Parameter(
            displayName="River Group ID Value",
            name="river_group_id",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        params.append(p)

        # 13 – Topology tolerance
        p = arcpy.Parameter(
            displayName="Topology Coordinate Tolerance (map units)",
            name="topology_tolerance",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
        )
        p.value = 0.001
        params.append(p)

        # 14 – Write upstream counts
        p = arcpy.Parameter(
            displayName="Write upstream segment count (tot_up_segments) to river network?",
            name="write_upstream_counts",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p.value = False
        params.append(p)

        # 15 – Accessibility output field (river network)
        p = arcpy.Parameter(
            displayName="Accessibility Output Field (river network)",
            name="accessibility_field",
            datatype="Field",
            parameterType="Required",
            direction="Input",
        )
        p.parameterDependencies = ["river_network"]
        p.filter.list = ["Short"]
        params.append(p)

        # 16 – First-barrier output field (barriers)
        p = arcpy.Parameter(
            displayName="First Barrier Output Field (barriers)",
            name="first_barrier_field",
            datatype="Field",
            parameterType="Required",
            direction="Input",
        )
        p.parameterDependencies = ["barriers"]
        p.filter.list = ["Short"]
        params.append(p)

        # 17 – Temporary workspace for intermediate datasets
        p = arcpy.Parameter(
            displayName="Temporary Workspace for Intermediate Data (.gdb)",
            name="temp_workspace",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input",
        )
        p.filter.list = ["LocalDatabase"]
        params.append(p)

        # 18 – Temporarily disable editor tracking during write
        p = arcpy.Parameter(
            displayName="Temporarily disable editor tracking during write?",
            name="disable_editor_tracking",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p.value = False
        params.append(p)

        # 19 – Allow slow cursor fallback (only if GP Near tools fail)
        p = arcpy.Parameter(
            displayName="Allow very slow cursor fallback if Near tools fail?",
            name="enable_cursor_fallback",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        p.value = False
        params.append(p)

        # 20 – SDE edit workspace (optional override)
        p = arcpy.Parameter(
            displayName="Edit Session Workspace (full path to .sde connection file, if needed)",
            name="edit_workspace",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input",
        )
        p.filter.list = ["RemoteDatabase", "LocalDatabase"]
        params.append(p)

        # 21 – Debug: limit to first N reaches
        p = arcpy.Parameter(
            displayName="Debug: limit to first N reaches (0 = process all)",
            name="debug_sample_n",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
        )
        p.value = 0
        params.append(p)

        # 22 – Derived output: updated river network
        # Declared so ArcGIS can track the modified feature class when
        # packaging the result as a Geoprocessing Service / package.
        p = arcpy.Parameter(
            displayName="Updated River Network",
            name="out_river_network",
            datatype="GPFeatureLayer",
            parameterType="Derived",
            direction="Output",
        )
        p.parameterDependencies = ["river_network"]
        p.schema.clone = True
        params.append(p)

        # 23 – Derived output: updated barriers
        p = arcpy.Parameter(
            displayName="Updated Barriers",
            name="out_barriers",
            datatype="GPFeatureLayer",
            parameterType="Derived",
            direction="Output",
        )
        p.parameterDependencies = ["barriers"]
        p.schema.clone = True
        params.append(p)

        return params

    # ------------------------------------------------------------------
    def updateParameters(self, parameters):
        """Grey out NEXT field when topology source is 'geometry'."""
        src = parameters[9].valueAsText or ""
        parameters[10].enabled = "fields" in src.lower()
        # Grey out river_group_id when no group field is set
        parameters[12].enabled = bool(parameters[11].valueAsText)

    # ------------------------------------------------------------------
    def updateMessages(self, parameters):
        import arcpy

        river_fc = parameters[0].valueAsText
        accessibility_field = parameters[15].valueAsText
        barriers_fc = parameters[2].valueAsText
        first_barrier_field = parameters[16].valueAsText

        if river_fc and accessibility_field:
            try:
                fld = next((f for f in arcpy.ListFields(river_fc) if f.name == accessibility_field), None)
                if fld is None:
                    parameters[15].setErrorMessage("Selected field does not exist in the river network layer.")
                elif fld.type.lower() != "smallinteger":
                    parameters[15].setErrorMessage("Selected field must be a Short Integer field.")
            except Exception:
                pass

        if not barriers_fc or not first_barrier_field:
            return

        try:
            fld = next((f for f in arcpy.ListFields(barriers_fc) if f.name == first_barrier_field), None)
            if fld is None:
                parameters[16].setErrorMessage("Selected field does not exist in the barriers layer.")
            elif fld.type.lower() != "smallinteger":
                parameters[16].setErrorMessage("Selected field must be a Short Integer field.")
        except Exception:
            # Let ArcGIS surface the underlying dataset issue.
            pass

    # ------------------------------------------------------------------
    def execute(self, parameters, messages):
        import arcpy
        import sys

        def _resolve_fc_parameter(param):
            """Return a stable feature-class path for GPFeatureLayer input."""
            try:
                if param.value is not None:
                    return arcpy.Describe(param.value).catalogPath
            except Exception:
                pass
            txt = param.valueAsText
            try:
                return arcpy.Describe(txt).catalogPath
            except Exception:
                return txt

        # ArcGIS Pro keeps Python modules loaded in memory for the lifetime of
        # the session.  Without this purge, edits to .py files in the package
        # are silently ignored until Pro is restarted.  Removing the cached
        # entries forces Python to reimport the package fresh on every tool run.
        for _key in list(sys.modules.keys()):
            if _key.startswith("riverobstacles"):
                del sys.modules[_key]

        from riverobstacles.run_analysis import run_analysis

        code_version = self._runtime_code_version()
        arcpy.AddMessage(f"Code version: {code_version}")

        # Parse parameters
        fc_riv = _resolve_fc_parameter(parameters[0])
        reach_id = parameters[1].valueAsText
        fc_bar = _resolve_fc_parameter(parameters[2])
        barrier_id = parameters[3].valueAsText
        pass_field = parameters[4].valueAsText or None
        impass_raw = parameters[5].valueAsText
        snap_dist = float(parameters[6].value)
        dist_method_raw = parameters[7].valueAsText
        vert_dens = float(parameters[8].value)
        topo_src_raw = parameters[9].valueAsText
        next_field = parameters[10].valueAsText or "NEXT"
        rg_field = parameters[11].valueAsText or None
        rg_id_raw = parameters[12].valueAsText or None
        topo_tol = float(parameters[13].value or 0.001)
        write_up = bool(parameters[14].value)
        accessibility_field = parameters[15].valueAsText
        first_barrier_field = parameters[16].valueAsText
        temp_ws = parameters[17].valueAsText or None
        disable_editor_tracking = bool(parameters[18].value)
        enable_cursor_fallback = bool(parameters[19].value)
        edit_ws = parameters[20].valueAsText or None
        debug_n = int(parameters[21].value or 0)

        # Normalise topology source
        topo_src = "fields" if "fields" in (topo_src_raw or "").lower() else "geometry"
        dist_method = "GEODESIC" if "geodesic" in (dist_method_raw or "").lower() else "PLANAR"

        # Parse impassable value
        impass_val = None
        if pass_field and impass_raw:
            v = impass_raw.strip()
            if v.lower() in ("false", "0", "no"):
                impass_val = False
            elif v.lower() in ("true", "1", "yes"):
                impass_val = True
            else:
                # Try numeric, fall back to string
                try:
                    impass_val = int(v)
                except ValueError:
                    try:
                        impass_val = float(v)
                    except ValueError:
                        impass_val = v

        # Parse river group id
        rg_id = None
        if rg_field and rg_id_raw:
            try:
                rg_id = int(rg_id_raw)
            except ValueError:
                rg_id = rg_id_raw

        summary = run_analysis(
            fc_river_network=fc_riv,
            fc_barriers=fc_bar,
            reach_id_field=reach_id,
            barrier_id_field=barrier_id,
            snap_distance=snap_dist,
            distance_method=dist_method,
            vertex_density=vert_dens,
            topology_tolerance=topo_tol,
            topology_source=topo_src,
            next_field=next_field,
            passability_field=pass_field,
            impassable_value=impass_val,
            temp_workspace=temp_ws,
            disable_editor_tracking=disable_editor_tracking,
            river_group_field=rg_field,
            river_group_id=rg_id,
            accessibility_field=accessibility_field,
            first_barrier_field=first_barrier_field,
            write_upstream_counts=write_up,
            enable_cursor_fallback=enable_cursor_fallback,
            edit_workspace=edit_ws,
            debug_sample_n=debug_n,
            verbose=True,
        )

        arcpy.AddMessage("─" * 60)
        arcpy.AddMessage("Summary")
        arcpy.AddMessage(f"  Reaches total:         {summary['reaches_total']:,}")
        arcpy.AddMessage(f"  Accessible reaches:    {summary['reaches_accessible']:,}")
        arcpy.AddMessage(f"  First-barrier reaches: {summary['reaches_first_barrier']:,}")
        arcpy.AddMessage(f"  Barriers total:        {summary['barriers_total']:,}")
        arcpy.AddMessage(f"  Barriers snapped:      {summary['barriers_snapped']:,}")
        arcpy.AddMessage(f"  First barriers:        {summary['barriers_first']:,}")
        arcpy.AddMessage(f"  Elapsed:               {summary['elapsed_s']} s")
        arcpy.AddMessage("─" * 60)

        # Set derived outputs so ArcGIS records the modified feature classes
        # in the result — required for geoprocessing packaging (error 00068).
        parameters[22].value = parameters[0].value
        parameters[23].value = parameters[2].value
