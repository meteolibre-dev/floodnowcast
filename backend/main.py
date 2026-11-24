"""
FloodNowcast Europe — FastAPI backend.

Serves:
  - /                              -> Interactive MapLibre GL JS frontend
  - /api/health                    -> Health check
  - /api/basins/geometry/{level}   -> Cached simplified HydroBASINS GeoJSON (lev 7, 8, 9)
  - /api/flood/summary             -> Live summary + active/alert basins from GCS
  - /api/flood/basins              -> Live metrics for all basins at a given level
"""

import io
import json
import os
import time
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from google.cloud import storage

REF_BUCKET = os.environ.get("FLOOD_REF_BUCKET", "generalinfos")
REF_PREFIX = os.environ.get("FLOOD_REF_PREFIX", "floodnowcasting/reference")
LIVE_BUCKET = os.environ.get("FLOOD_LIVE_BUCKET", "inference_result_meteolibre_forecast")
LIVE_PREFIX = os.environ.get("FLOOD_LIVE_PREFIX", "last_available")
CACHE_DIR = os.environ.get("FLOOD_CACHE_DIR", "/tmp/floodnowcast_web_cache")
LIVE_TTL_SEC = int(os.environ.get("FLOOD_LIVE_TTL_SEC", "60"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="FloodNowcast Europe API", version="1.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_gcs_client: Optional[storage.Client] = None
_geom_cache: Dict[int, bytes] = {}
_live_cache: Dict[str, Any] = {
    "fetched_at": 0.0,
    "summary": None,
    "basins_df": None,
    "vigicrues_at": 0.0,
    "vigicrues_bytes": None,
}


def get_gcs() -> storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = storage.Client()
    return _gcs_client


def _refresh_live_if_needed(force: bool = False) -> None:
    now = time.monotonic()
    if (
        not force
        and _live_cache["summary"] is not None
        and (now - _live_cache["fetched_at"]) < LIVE_TTL_SEC
    ):
        return

    client = get_gcs()
    bucket = client.bucket(LIVE_BUCKET)

    summary_blob = bucket.blob(f"{LIVE_PREFIX}/flood_basins_summary.json")
    parquet_blob = bucket.blob(f"{LIVE_PREFIX}/flood_basins.parquet")

    if not summary_blob.exists() or not parquet_blob.exists():
        raise HTTPException(
            status_code=503,
            detail="Live flood_basins data not yet published in GCS. Run publish_last_available.py first.",
        )

    summary = json.loads(summary_blob.download_as_text())
    pq_bytes = parquet_blob.download_as_bytes()
    df = pd.read_parquet(io.BytesIO(pq_bytes))

    _live_cache["summary"] = summary
    _live_cache["basins_df"] = df
    _live_cache["fetched_at"] = now


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "floodnowcast-europe"}


@app.get("/api/basins/geometry/{level}")
def get_basins_geometry(level: int) -> Response:
    if level not in (7, 8, 9):
        raise HTTPException(status_code=400, detail="level must be 7, 8, or 9")

    if level not in _geom_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fname = f"hybas_eu_lev{level:02d}_simplified.geojson"
        local_path = os.path.join(CACHE_DIR, fname)
        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            client = get_gcs()
            blob = client.bucket(REF_BUCKET).blob(f"{REF_PREFIX}/{fname}")
            if not blob.exists():
                raise HTTPException(status_code=404, detail=f"Missing reference geometry {fname}")
            blob.download_to_filename(local_path)
        with open(local_path, "rb") as f:
            _geom_cache[level] = f.read()

    return Response(
        content=_geom_cache[level],
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/flood/summary")
def get_flood_summary() -> JSONResponse:
    _refresh_live_if_needed()
    return JSONResponse(
        content=_live_cache["summary"],
        headers={"Cache-Control": f"public, max-age={LIVE_TTL_SEC}"},
    )


@app.get("/api/flood/basins")
def get_flood_basins(
    level: int = Query(8, ge=7, le=9),
    min_ratio: float = Query(0.0, ge=0.0),
) -> JSONResponse:
    _refresh_live_if_needed()
    df: pd.DataFrame = _live_cache["basins_df"]
    sub = df[df["level"] == level]
    if min_ratio > 0.0:
        sub = sub[sub["max_ratio"] >= min_ratio]

    cols = [
        "hybas_id",
        "response_time_h",
        "est_return_period_y",
        "obs_1h_mm",
        "obs_3h_mm",
        "obs_12h_mm",
        "fc_1h_mm",
        "fc_3h_mm",
        "peak_1h_mm",
        "peak_3h_mm",
        "total_6h_mm",
        "total_12h_mm",
        "rl10_areal_1h",
        "rl10_areal_3h",
        "rl10_areal_6h",
        "rl10_areal_12h",
        "ratio_1h",
        "ratio_3h",
        "ratio_6h",
        "ratio_12h",
        "max_ratio",
        "alert_level",
    ]
    sub_clean = sub[cols].astype(object).where(pd.notna(sub[cols]), None)
    records = sub_clean.to_dict(orient="records")
    # Also return compact map keyed by hybas_id for fast O(1) MapLibre feature-state join
    by_id = {
        str(r["hybas_id"]): r for r in records
    }
    return JSONResponse(
        content={
            "level": level,
            "count": len(records),
            "obs_latest_ts": _live_cache["summary"].get("obs_latest_ts"),
            "forecast_run_time": _live_cache["summary"].get("forecast_run_time"),
            "basins": by_id,
        },
        headers={"Cache-Control": f"public, max-age={LIVE_TTL_SEC}"},
    )


@app.get("/api/vigicrues")
def get_vigicrues() -> Response:
    now = time.monotonic()
    if _live_cache["vigicrues_bytes"] is not None and (now - _live_cache["vigicrues_at"]) < 120:
        return Response(
            content=_live_cache["vigicrues_bytes"],
            media_type="application/geo+json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    client = get_gcs()
    blob = client.bucket(LIVE_BUCKET).blob(f"{LIVE_PREFIX}/vigicrues.geojson")
    if not blob.exists():
        raise HTTPException(status_code=503, detail="Vigicrues GeoJSON not yet published in GCS.")
    raw = blob.download_as_bytes()
    _live_cache["vigicrues_bytes"] = raw
    _live_cache["vigicrues_at"] = now
    return Response(
        content=raw,
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=120"},
    )
