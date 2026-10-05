# config.example.py
# ------------------
# Copy this file to  config.py  and fill in your values.
# config.py is imported by webhook_handler.py.
# Keep config.py out of version control (add it to .gitignore).

# ── Webhook security ─────────────────────────────────────────────────────── #
# Must match the secret you register in ArcGIS Enterprise
# (Portal → service Settings → Webhooks).
# Generate with:  python -c "import secrets; print(secrets.token_hex(32))"
WEBHOOK_SECRET = "REPLACE_WITH_YOUR_STRONG_RANDOM_SECRET"

# ── Feature class paths ──────────────────────────────────────────────────── #
# Full SDE connection path OR local file GDB path.
# Examples:
#   r"C:/connections/my_enterprise.sde/schema.river_network"
#   r"C:/data/rivers.gdb/river_network"

FC_RIVER_NETWORK = r"C:/connections/my_enterprise.sde/owner.river_network"
FC_BARRIERS      = r"C:/connections/my_enterprise.sde/owner.barriers"

# ── Field names ──────────────────────────────────────────────────────────── #
REACH_ID_FIELD   = "riverID"
BARRIER_ID_FIELD = "unique_id_row"

# ── Snapping parameters ──────────────────────────────────────────────────── #
SNAP_DISTANCE   = 50.0   # map units (metres if projected CRS)
SNAP_DISTANCE_METHOD = "PLANAR"  # "PLANAR" or "GEODESIC"
VERTEX_DENSITY  = 10.0   # smaller → more accurate, slower

# ── Topology source ──────────────────────────────────────────────────────── #
# "geometry" – derive NEXT/PREVIOUS from polyline endpoint matching (default)
# "fields"   – read from existing NEXT / PREVIOUS attribute fields
TOPOLOGY_SOURCE = "geometry"

# Only used when TOPOLOGY_SOURCE = "fields":
NEXT_FIELD     = "NEXT"
PREVIOUS_FIELD = "PREVIOUS"   # set to None if the field does not exist

# ── Passability filter (optional) ────────────────────────────────────────── #
# Set PASSABILITY_FIELD to the name of a field on the barriers feature class
# that encodes whether a barrier can be passed by fish.
# Set IMPASSABLE_VALUE to the value that means "impassable".
# Leave both as None to process ALL barriers (no filter).
#
# Examples:
#   PASSABILITY_FIELD = "is_passable"  ;  IMPASSABLE_VALUE = False
#   PASSABILITY_FIELD = "type"         ;  IMPASSABLE_VALUE = "dam"
PASSABILITY_FIELD = None       # e.g. "is_passable"
IMPASSABLE_VALUE  = None       # e.g. False

# ── Logging ──────────────────────────────────────────────────────────────── #
LOG_LEVEL = "INFO"   # DEBUG | INFO | WARNING | ERROR
