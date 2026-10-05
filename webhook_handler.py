"""
webhook_handler.py
------------------
Flask micro-service that listens for ArcGIS Enterprise feature-service
webhooks and re-runs the river-obstacle analysis whenever the barriers
dataset is edited.

Architecture
------------

    ArcGIS Enterprise Feature Service (barriers)
        │
        │  POST (JSON payload + HMAC-SHA256 signature)
        ▼
    webhook_handler.py   (Flask on ArcGIS Server / stand-alone VM)
        │
        │  calls run_analysis() via arcpy  – OR –
        │  calls the published GP Service via REST
        ▼
    River network & barriers feature classes updated in Enterprise GDB

Quick start
-----------
1.  Install dependencies (once, in the ArcGIS Python environment):

        conda install flask -c conda-forge

2.  Create config.py (copy from config.example.py) and fill in your paths.

3.  Register the webhook in ArcGIS Enterprise:
    • Portal → Content → (open barriers service) → Settings → Webhooks
    • Payload URL:  https://<your-server>/riverobstacles/webhook
    • Secret:       <choose a strong random string, put it in config.py>
    • Trigger:      FeaturesCreated, FeaturesUpdated, FeaturesDeleted

4.  Run the handler (development):

        python webhook_handler.py

    For production, use a WSGI server (gunicorn / waitress) behind nginx:

        waitress-serve --port=5050 webhook_handler:app

Security notes
--------------
- HMAC-SHA256 signature validation is enforced on every request.
  Requests without a valid signature return 403.
- The webhook is served over HTTPS only (configure your reverse proxy
  to terminate TLS).
- Analysis is run in a background thread so the endpoint returns 200
  immediately and the webhook does not time out.
- Concurrent webhook deliveries are de-bounced: if an analysis is already
  running, the new request is queued and executed once the current one
  finishes (at most one pending job at a time).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from typing import Optional

from flask import Flask, Response, abort, request

# ---------------------------------------------------------------------------
# Configuration – edit config.py (copied from config.example.py)
# ---------------------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

try:
    import config as _cfg
except ImportError:
    # Fallback: read from environment variables
    class _cfg:  # type: ignore[no-redef]
        WEBHOOK_SECRET = os.environ.get("RO_WEBHOOK_SECRET", "")
        FC_RIVER_NETWORK = os.environ.get("RO_FC_RIVER_NETWORK", "")
        FC_BARRIERS = os.environ.get("RO_FC_BARRIERS", "")
        REACH_ID_FIELD = os.environ.get("RO_REACH_ID_FIELD", "riverID")
        BARRIER_ID_FIELD = os.environ.get("RO_BARRIER_ID_FIELD", "unique_id_row")
        SNAP_DISTANCE = float(os.environ.get("RO_SNAP_DISTANCE", "50"))
        SNAP_DISTANCE_METHOD = os.environ.get("RO_SNAP_DISTANCE_METHOD", "PLANAR")
        VERTEX_DENSITY = float(os.environ.get("RO_VERTEX_DENSITY", "10"))
        TOPOLOGY_SOURCE = os.environ.get("RO_TOPOLOGY_SOURCE", "geometry")
        PASSABILITY_FIELD = os.environ.get("RO_PASSABILITY_FIELD", "") or None
        IMPASSABLE_VALUE = os.environ.get("RO_IMPASSABLE_VALUE", "") or None
        LOG_LEVEL = os.environ.get("RO_LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, getattr(_cfg, "LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
)
logger = logging.getLogger("riverobstacles.webhook")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job-queue state (simple threading lock + flag)
# ---------------------------------------------------------------------------
_analysis_lock = threading.Lock()
_pending = threading.Event()   # set when a new request arrives while busy


def _run_analysis_safe() -> None:
    """Run analysis in a background thread; re-run once if a job was queued."""
    from riverobstacles.run_analysis import run_analysis

    while True:
        _pending.clear()
        logger.info("Starting river-obstacle analysis …")
        try:
            summary = run_analysis(
                fc_river_network=_cfg.FC_RIVER_NETWORK,
                fc_barriers=_cfg.FC_BARRIERS,
                reach_id_field=_cfg.REACH_ID_FIELD,
                barrier_id_field=_cfg.BARRIER_ID_FIELD,
                snap_distance=_cfg.SNAP_DISTANCE,
                distance_method=getattr(_cfg, "SNAP_DISTANCE_METHOD", "PLANAR"),
                vertex_density=_cfg.VERTEX_DENSITY,
                topology_source=_cfg.TOPOLOGY_SOURCE,
                passability_field=_cfg.PASSABILITY_FIELD,
                impassable_value=_cfg.IMPASSABLE_VALUE,
                verbose=True,
            )
            logger.info(
                "Analysis complete: %d accessible reaches, %d first barriers  (%.1f s)",
                summary["reaches_accessible"],
                summary["barriers_first"],
                summary["elapsed_s"],
            )
        except Exception:
            logger.exception("Analysis failed with an unhandled exception.")

        with _analysis_lock:
            if not _pending.is_set():
                # No new request arrived while we were running → done
                return
        # A new request arrived – loop and run again


def _dispatch_analysis() -> None:
    """
    Dispatch an analysis job.
    If one is already running, set a flag so it re-runs on completion.
    If none is running, start a new background thread.
    """
    with _analysis_lock:
        if threading.active_count() > 1:
            # A background analysis thread is already running
            _pending.set()
            logger.info("Analysis already running – new job queued.")
            return

    thread = threading.Thread(target=_run_analysis_safe, daemon=True, name="ro-analysis")
    thread.start()


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

def _verify_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
    """
    Verify the HMAC-SHA256 signature sent by ArcGIS Enterprise.

    ArcGIS Enterprise sends the signature in the header
    ``x-esriHook-Signature`` as ``sha256=<hex-digest>``.

    Returns True if valid, False otherwise.
    """
    secret = getattr(_cfg, "WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("WEBHOOK_SECRET is not configured – skipping signature check.")
        return True

    if not signature_header:
        return False

    # Header format: "sha256=<hex>"
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        return False

    expected = hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, parts[1])


@app.route("/riverobstacles/webhook", methods=["POST"])
def webhook() -> Response:
    """
    Receive ArcGIS Enterprise feature-service webhook POST.

    Returns 200 immediately; analysis runs in a background thread.
    """
    payload_bytes = request.get_data()  # raw bytes for HMAC validation

    # ── Signature validation ──────────────────────────────────────────── #
    sig = request.headers.get("x-esriHook-Signature") or \
          request.headers.get("X-esriHook-Signature")
    if not _verify_signature(payload_bytes, sig):
        logger.warning("Webhook signature validation failed – rejecting request.")
        abort(403)

    # ── Parse payload (optional – used for logging / filtering) ──────── #
    try:
        payload = json.loads(payload_bytes)
        change_type = (
            payload.get("info", {}).get("changeType", "unknown")
        )
        layer_id = payload.get("info", {}).get("layerId", "?")
        logger.info(
            "Webhook received: changeType=%s layerId=%s",
            change_type,
            layer_id,
        )
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Could not parse webhook payload JSON – proceeding anyway.")

    # ── Dispatch analysis ─────────────────────────────────────────────── #
    _dispatch_analysis()

    return Response("OK", status=200, mimetype="text/plain")


@app.route("/riverobstacles/health", methods=["GET"])
def health() -> Response:
    """Simple health-check endpoint."""
    return Response(
        json.dumps({"status": "ok", "service": "riverobstacles-webhook"}),
        status=200,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Development server only – use waitress or gunicorn in production
    logger.info("Starting development server on port 5050 …")
    app.run(host="127.0.0.1", port=5050, debug=False)
