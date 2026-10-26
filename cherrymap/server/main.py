"""CherryMap FastAPI entry point. The immutable source gate runs in lifespan."""
from __future__ import annotations

import os
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .index_runtime import GLOBAL_INDEX_QUERY
from .maps_service import MapsService
from .source_gate import SourceGateResult, run_source_gate
from .source_worker import SourceWorkerError, SourceWorkerSupervisor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
PUBLIC_DIR = PROJECT_ROOT / "public"
ASSETS_DIR = DIST_DIR / "assets"


class DirectionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    origin: str = Field(min_length=1, max_length=250)
    destination: str = Field(min_length=1, max_length=250)
    mode: Literal["driving", "walking", "bicycling", "transit"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No socket is bound by the ready app until this succeeds. The immutable
    # original is gated before the fixed original-plus-extension child launches.
    gate_result: SourceGateResult = run_source_gate()
    maps = MapsService()
    supervisor = SourceWorkerSupervisor()
    try:
        await supervisor.start(gate_result)
        # The attached extension registers and makes one bounded first read of
        # this exact query before ready. Asking the supervisor for it here keeps
        # FastAPI bootstrap/default API state on that real global frame, rather
        # than allowing a later source-only heartbeat cache to replace it.
        await supervisor.snapshot_for_query(dict(GLOBAL_INDEX_QUERY))
    except Exception:
        await supervisor.stop()
        raise
    app.state.source_result = gate_result
    app.state.source_supervisor = supervisor
    app.state.maps = maps
    try:
        yield
    finally:
        await supervisor.stop()


app = FastAPI(title="CherryMap", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR, check_dir=False), name="assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(self)"
    # Do not set X-Frame-Options or frame-ancestors: the approved app must not
    # block cross-site iframe embedding.
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") or request.url.path == "/_app/health" else "no-cache"
    return response


def _ready_source(request: Request) -> SourceGateResult:
    result = getattr(request.app.state, "source_result", None)
    if result is None:
        raise HTTPException(status_code=503, detail="source startup gate has not completed")
    return result


def _source_supervisor(request: Request) -> SourceWorkerSupervisor:
    supervisor = getattr(request.app.state, "source_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="marker startup has not completed")
    return supervisor


async def _snapshot(request: Request, query: dict[str, object]) -> dict[str, object]:
    """Proxy a bounded cached child snapshot; private IPC config never returns."""
    supervisor = _source_supervisor(request)
    try:
        return await supervisor.snapshot_for_query(query)
    except SourceWorkerError:
        raise HTTPException(status_code=503, detail="source mapping worker is unavailable") from None


@app.get("/_app/health")
async def health(request: Request) -> JSONResponse:
    _ready_source(request)
    supervisor = _source_supervisor(request)
    if not supervisor.ready:
        return JSONResponse({"status": "unavailable", "worker": {"ready": False}}, status_code=503)
    return JSONResponse({"status": "ready", "worker": {"ready": True}})


@app.get("/api/markers")
async def markers(
    request: Request,
    nearby: bool = Query(False),
    index: bool = Query(False),
    poi: bool | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius: int | None = Query(None),
    south: float | None = Query(None),
    north: float | None = Query(None),
    west: float | None = Query(None),
    east: float | None = Query(None),
    zoom: int | None = Query(None),
) -> JSONResponse:
    _ready_source(request)
    default_global_index = not nearby and not index and poi is not True
    if nearby:
        _validate_nearby_marker_query(lat=lat, lng=lng, radius=radius, zoom=zoom)
    if index or poi is True:
        _validate_index_marker_query(south=south, north=north, west=west, east=east, zoom=zoom)
    if default_global_index:
        payload = await _snapshot(request, dict(GLOBAL_INDEX_QUERY))
    else:
        query: dict[str, object] = {
            "nearby": nearby, "index": index, "lat": lat, "lng": lng, "radius": radius,
            "south": south, "north": north, "west": west, "east": east, "zoom": zoom,
        }
        if poi is not None:
            query["poi"] = poi
        payload = await _snapshot(request, query)
    return JSONResponse(payload)


@app.get("/api/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    source = _ready_source(request)
    maps: MapsService = request.app.state.maps
    snapshot = await _snapshot(request, dict(GLOBAL_INDEX_QUERY))
    return JSONResponse({
        "maps": {"api_base": maps.public_api_base, "browser_key": os.getenv("MANUS_API_BROWSER_KEY", "")},
        "source": source.public_dict(),
        "locations": snapshot["locations"],
        "worker": snapshot["worker"],
        "nearby": snapshot["nearby"],
        "index": snapshot["index"],
        "poi": snapshot["poi"],
        "fusion": snapshot["fusion"],
        "uplinks": {"connected": False, "status": "Not connected"},
    })


def _validate_nearby_marker_query(
    *,
    lat: float | None,
    lng: float | None,
    radius: int | None,
    zoom: int | None,
) -> None:
    """Validate a client viewport before it can enter the bounded worker map."""
    if lat is None or lng is None or radius is None:
        raise HTTPException(status_code=422, detail="nearby requires lat, lng, and radius")
    if (
        isinstance(lat, bool)
        or isinstance(lng, bool)
        or not math.isfinite(lat)
        or not math.isfinite(lng)
        or not -90 <= lat <= 90
        or not -180 <= lng <= 180
    ):
        raise HTTPException(status_code=422, detail="lat and lng must be finite geographic coordinates")
    if isinstance(radius, bool) or not 100 <= radius <= 5000:
        raise HTTPException(status_code=422, detail="radius must be an integer between 100 and 5000")
    if zoom is None or isinstance(zoom, bool) or not math.isfinite(zoom) or zoom < 12:
        raise HTTPException(status_code=422, detail="nearby requires browser zoom of at least 12")


def _validate_index_marker_query(
    *,
    south: float | None,
    north: float | None,
    west: float | None,
    east: float | None,
    zoom: int | None,
) -> None:
    """Validate a GeoNames viewport; west > east intentionally crosses 180°."""
    if None in (south, north, west, east, zoom):
        raise HTTPException(status_code=422, detail="index requires south, north, west, east, and zoom")
    values = (south, north, west, east)
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
        for value in values
    ):
        raise HTTPException(status_code=422, detail="index bounds must be finite geographic coordinates")
    assert south is not None and north is not None and west is not None and east is not None
    if not -90 <= south < north <= 90 or not -180 <= west <= 180 or not -180 <= east <= 180 or west == east:
        raise HTTPException(status_code=422, detail="index bounds are outside valid geographic limits")
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 2 <= zoom <= 20:
        raise HTTPException(status_code=422, detail="index requires integer zoom from 2 through 20")


@app.get("/api/source")
async def source_status(request: Request) -> JSONResponse:
    return JSONResponse(_ready_source(request).public_dict())


@app.get("/api/search")
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=160),
    scope: Literal["world", "nearby"] = Query("world"),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius: int | None = Query(None, ge=100, le=20000),
) -> JSONResponse:
    if scope == "nearby" and (lat is None or lng is None or radius is None):
        raise HTTPException(status_code=422, detail="nearby search requires lat, lng, and radius")
    if scope == "world" and any(value is not None for value in (lat, lng, radius)):
        raise HTTPException(status_code=422, detail="world search does not accept proximity parameters")
    if any(value is not None and (not math.isfinite(float(value))) for value in (lat, lng)):
        raise HTTPException(status_code=422, detail="coordinates must be finite")
    if lat is not None and not -90 <= lat <= 90 or lng is not None and not -180 <= lng <= 180:
        raise HTTPException(status_code=422, detail="coordinates are outside geographic limits")
    _ready_source(request)
    try:
        result = await _source_supervisor(request).search(q, scope=scope, lat=lat, lng=lng, radius=radius)
    except SourceWorkerError:
        raise HTTPException(status_code=503, detail="source mapping worker is unavailable") from None
    return JSONResponse(result)


@app.get("/api/nearby")
def nearby(
    request: Request,
    lat: float = Query(),
    lng: float = Query(),
    radius: int = Query(ge=100, le=5000),
) -> JSONResponse:
    """Fetch one bounded provider page without blocking the async event loop."""
    maps: MapsService = request.app.state.maps
    return JSONResponse(maps.nearby(lat, lng, radius, request))


@app.get("/api/place")
async def place(request: Request, place_id: str = Query(min_length=1, max_length=180, pattern=r"^[A-Za-z0-9_-]{1,180}$")) -> JSONResponse:
    """Resolve one validated business/place ID from a supported basemap POI click.

    This route neither accepts URLs nor contacts Maps itself. The source child
    validates the ID, maintains a five-minute cache, and owns the fixed proxy request.
    """
    _ready_source(request)
    try:
        reply = await _source_supervisor(request).place_details(place_id)
    except SourceWorkerError:
        raise HTTPException(status_code=404, detail="selected provider place is unavailable") from None
    return JSONResponse(reply)


@app.post("/api/directions")
async def directions(request: Request, payload: DirectionsBody) -> JSONResponse:
    if any(ord(char) < 32 for char in payload.origin + payload.destination):
        raise HTTPException(status_code=422, detail="origin and destination must contain printable characters")
    maps: MapsService = request.app.state.maps
    return JSONResponse(maps.directions(payload.origin, payload.destination, payload.mode, request))


@app.get("/manus-routes.json")
async def routes_manifest() -> FileResponse:
    path = PUBLIC_DIR / "manus-routes.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="route manifest is unavailable")
    return FileResponse(path, media_type="application/json")


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    path = PUBLIC_DIR / "favicon.svg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="favicon is unavailable")
    return FileResponse(path, media_type="image/svg+xml")


@app.get("/{requested_path:path}")
async def frontend(requested_path: str) -> FileResponse:
    clean = requested_path.lstrip("/")
    if clean == "api" or clean.startswith("api/") or clean.startswith(("source", "logs", "config", ".env")):
        raise HTTPException(status_code=404, detail="not found")
    index = DIST_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="frontend build is unavailable")
    return FileResponse(index, media_type="text/html")


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    uvicorn.run("server.main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
