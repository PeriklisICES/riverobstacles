# riverobstacles – ArcGIS Enterprise implementation

Port of the [`riverobstacles`](https://github.com/as-french/riverobstacles) R
package to ArcGIS Python.  Identifies the first impassable barrier upstream
from the tidal limit in a directed, multi-threaded river network stored in an
**ArcGIS Enterprise geodatabase (PostgreSQL)**.

**No Trace Network or Network Analyst licence required.**

---

## How it works

The algorithm mirrors the R package exactly:

| Step | R function | Python module |
|------|-----------|---------------|
| Build directed topology (NEXT / PREVIOUS) from polyline endpoints | `collate_up_down_seg_ids()` | `riverobstacles/topology.py` |
| Snap barrier points to nearest reach vertex | `snap_points_to_lines_geos()` | `riverobstacles/snap.py` |
| BFS from outlet → upstream, stop at barriers | `identify_accessible_reaches()` | `riverobstacles/graph_traversal.py` |
| Write results to GDB / feature service | *(arcpy da cursors)* | `riverobstacles/arcgis_io.py` |
| Orchestrate all steps | *(top-level call)* | `riverobstacles/run_analysis.py` |

### Topology building

Endpoint matching works without any pre-existing topology fields.  The end
point of each polyline is hashed onto a grid and matched to the start point of
the next downstream polyline within a configurable coordinate tolerance
(default 1 mm).  This produces NEXT and PREVIOUS relationships identical to
those that `collate_up_down_seg_ids()` generates in R.

Alternatively, if your geodatabase already stores NEXT / PREVIOUS fields
(e.g., maintained by a hydrology processing workflow), set
`topology_source = "fields"` to skip the geometry-matching step.

### Barrier snapping

Each barrier point is snapped to the nearest densified vertex of any reach
within `snap_distance` map units.  Dense vertices are indexed in a grid
spatial hash – no Advanced licence, no `GenerateNearTable`.

When multiple barriers land on the same reach, the one closest to the
downstream end is selected as the first barrier for that reach.

### BFS traversal

A breadth-first search seeds from every tidal outlet (reach with no
downstream neighbour) and traverses upstream.  When a barrier-carrying reach
is encountered the traversal stops; no further upstream reaches from that
branch are classified as accessible.

---

## Output fields

### River network feature class

| Field | Type | Meaning |
|-------|------|---------|
| `is_accessible` | Short (0/1) | 1 = reachable from sea below all barriers |
| `is_first_barrier_reach` | Short (0/1) | 1 = reach carries the first upstream barrier |
| `tot_up_segments` | Long | Total upstream reach count (optional) |

### Barriers feature class

| Field | Type | Meaning |
|-------|------|---------|
| `is_first_barrier` | Short (0/1) | 1 = this barrier is the first obstacle on its branch |
| `snapped_reach_id` | Text | ID of the reach this barrier was snapped to |

---

## Project layout

```
arcgis_riverobstacles/
├── RiverObstacles.pyt          ArcGIS Python Toolbox (open in ArcGIS Pro)
├── webhook_handler.py          Flask webhook listener
├── config.example.py           Configuration template → copy to config.py
├── requirements.txt            Python dependencies
└── riverobstacles/
    ├── __init__.py
    ├── topology.py             Directed topology from polyline geometry
    ├── snap.py                 Barrier snapping
    ├── graph_traversal.py      BFS + upstream counts
    ├── arcgis_io.py            GDB / REST read-write helpers
    └── run_analysis.py         Top-level orchestration
```

---

## Installation

### 1 – Copy files to the server

Copy the entire `arcgis_riverobstacles/` folder to the ArcGIS Server machine
(or any machine that has ArcGIS Pro installed and can reach the enterprise GDB
via an SDE connection file).

### 2 – Install Flask (webhook mode only)

Open the ArcGIS Python command prompt and run:

```bash
conda install flask waitress -c conda-forge -y
```

### 3 – Create your SDE connection file

In ArcGIS Pro → Catalog pane → Databases → New Database Connection, connect to
your PostgreSQL enterprise geodatabase and save the `.sde` file.

---

## Usage

### Option A – ArcGIS Pro / ArcGIS Toolbox (interactive / ad-hoc)

1. Open ArcGIS Pro.
2. Catalog pane → right-click `RiverObstacles.pyt` → **Open**.
3. Run **Identify First Barriers** with your feature class paths and field
   names.

The tool writes results directly back to the enterprise GDB.

### Option B – Standalone Python script

```python
import sys, os
sys.path.insert(0, r"C:\path\to\arcgis_riverobstacles")

from riverobstacles import run_analysis

run_analysis(
    fc_river_network = r"C:/connections/enterprise.sde/owner.river_network",
    fc_barriers      = r"C:/connections/enterprise.sde/owner.barriers",
    reach_id_field   = "riverID",
    barrier_id_field = "unique_id_row",
    snap_distance    = 50,
    vertex_density   = 10,
    # Only process impassable barriers (optional filter):
    passability_field = "is_passable",
    impassable_value  = False,
    verbose = True,
)
```

### Option C – Publish as a GP Service (call from REST / webhooks)

1. Run the tool once in ArcGIS Pro with your data.
2. In the **History** pane, right-click the result → **Share As →
   Geoprocessing Service**.
3. Publish to your ArcGIS Enterprise portal.
4. The service is now callable via the ArcGIS REST API:

```
POST https://server/arcgis/rest/services/RiverObstacles/GPServer/Identify%20First%20Barriers/submitJob
```

### Option D – Automatic trigger via ArcGIS Enterprise webhook

This is the fully automated option: the analysis reruns every time the
barriers feature service is edited.

#### Step 1 – Configure `config.py`

```bash
cp config.example.py config.py
# Edit config.py with your paths, field names, and webhook secret
```

#### Step 2 – Start the webhook handler

```bash
# Development (local testing only):
python webhook_handler.py

# Production (run as a Windows Service or systemd unit):
waitress-serve --port=5050 webhook_handler:app
```

Make the endpoint reachable from the ArcGIS Enterprise server.  If the
webhook handler is on the same machine as ArcGIS Server, `localhost:5050`
is fine.  Otherwise configure your reverse proxy (IIS / nginx) to forward
requests to it over HTTPS.

#### Step 3 – Register the webhook in ArcGIS Enterprise

1. Open Portal → **Content** → open your barriers feature service item.
2. **Settings** → **Webhooks** → **Add Webhook**.
3. Fill in:
   - **Payload URL**: `https://<your-server>/riverobstacles/webhook`
   - **Secret**: the value you put in `config.py` → `WEBHOOK_SECRET`
   - **Active**: ✔
   - **Events**: `FeaturesCreated`, `FeaturesUpdated`, `FeaturesDeleted`
4. Save.

Every edit to the barriers service now triggers the analysis automatically.
The webhook handler responds with HTTP 200 immediately; the analysis runs in
a background thread and never blocks the webhook delivery.  Concurrent
deliveries are de-bounced (at most one queued re-run at a time).

---

## Performance notes

| Network size | Typical runtime |
|---|---|
| Single catchment, < 5 000 reaches | < 5 s |
| National dataset, 50 000+ reaches | 30–120 s (depending on barrier count and vertex density) |

For large national datasets, set `river_group_field` and `river_group_id` to
restrict processing to the catchment(s) where barriers were edited.  The
webhook payload includes the edited feature IDs; you can extend
`webhook_handler.py` to query the catchment ID of each edited barrier and
pass it to `run_analysis()`.

If you need to process all catchments nightly, wrap `run_analysis()` in a
loop over river-group IDs and schedule with Windows Task Scheduler:

```python
# nightly_refresh.py
import arcpy, sys
sys.path.insert(0, r"C:\path\to\arcgis_riverobstacles")
from riverobstacles import run_analysis

FC_RIV = r"C:/connections/enterprise.sde/owner.river_network"
FC_BAR = r"C:/connections/enterprise.sde/owner.barriers"

# Get all distinct river group IDs
groups = set()
with arcpy.da.SearchCursor(FC_RIV, ["river_group"]) as cur:
    for row in cur:
        groups.add(row[0])

for gid in sorted(groups):
    print(f"Processing river group {gid} …")
    run_analysis(
        fc_river_network    = FC_RIV,
        fc_barriers         = FC_BAR,
        reach_id_field      = "riverID",
        barrier_id_field    = "unique_id_row",
        snap_distance       = 50,
        vertex_density      = 10,
        passability_field   = "is_passable",
        impassable_value    = False,
        river_group_field   = "river_group",
        river_group_id      = gid,
        verbose             = False,
    )
```

---

## Requirements

| Package | Source | Notes |
|---------|--------|-------|
| `arcpy` | ArcGIS Pro / Server | Already in the ArcGIS Python env |
| `flask` | conda-forge | Webhook handler only |
| `waitress` | conda-forge | Production WSGI server (webhook only) |

All geometry operations use `arcpy` built-ins (`SHAPE@`, `densify`).
No Spatial Analyst, Network Analyst, or Advanced licence extensions needed.

---

## Differences from the R package

| Feature | R package | This implementation |
|---------|-----------|---------------------|
| Graph library | `igraph` / `sfnetworks` | Pure Python BFS (no external graph library) |
| Geometry engine | GEOS via `geos` package | `arcpy` built-in geometry |
| Spatial index | GEOS STRtree | Grid hash (O(1) per query) |
| Multi-barrier reach handling | Buffer → split reach | Keep barrier closest to downstream end |
| Distance to sea | `sfnetworks` shortest path | Not implemented (add if needed) |
| Braided networks | Supported | Supported |
