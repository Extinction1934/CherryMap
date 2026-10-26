"""Server-side, fixed-path access to the managed Maps proxy."""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
from collections import OrderedDict, deque
from html.parser import HTMLParser
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from fastapi import HTTPException, Request as FastAPIRequest


class _InstructionText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def strip_html_instruction(value: object) -> str:
    parser = _InstructionText()
    parser.feed(str(value or ""))
    parser.close()
    return html.unescape(parser.text())


def decode_polyline(encoded: str) -> list[dict[str, float]]:
    """Decode a Google encoded polyline into the public coordinate contract."""
    points: list[dict[str, float]] = []
    index = lat = lng = 0
    try:
        while index < len(encoded):
            for coordinate in range(2):
                shift = result = 0
                while True:
                    if index >= len(encoded):
                        raise ValueError("truncated")
                    byte = ord(encoded[index]) - 63
                    index += 1
                    result |= (byte & 0x1F) << shift
                    shift += 5
                    if byte < 0x20:
                        break
                    if shift > 30:
                        raise ValueError("invalid")
                delta = ~(result >> 1) if result & 1 else result >> 1
                if coordinate == 0:
                    lat += delta
                else:
                    lng += delta
            points.append({"lat": lat / 1e5, "lng": lng / 1e5})
    except (TypeError, ValueError):
        raise HTTPException(status_code=502, detail="maps provider returned an invalid route geometry") from None
    return points


def _api_origin(value: str) -> str:
    """Use only scheme/host in public config; never surface a private URL path/query."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class MapsService:
    """Bounded cache/rate limiter and non-redirecting fixed Maps endpoints."""

    CACHE_TTL_SECONDS = 300.0
    CACHE_LIMIT = 128
    RATE_WINDOW_SECONDS = 60.0
    RATE_LIMIT = 30
    CLIENT_LIMIT = 512
    # The nearby runtime has at most eight active views and a 60-second
    # refresh cadence.  Keep a separate bounded server-side quota because it
    # deliberately has no user Request, headers, or browser credentials.
    WORKER_RATE_LIMIT = 30
    TIMEOUT_SECONDS = 8.0

    def __init__(self, api_url: str | None = None, api_key: str | None = None) -> None:
        self._api_url = (api_url if api_url is not None else os.getenv("MANUS_API_URL", "")).strip().rstrip("/")
        self._api_key = api_key if api_key is not None else os.getenv("MANUS_API_KEY", "")
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._worker_requests: deque[float] = deque()
        self._lock = Lock()

    @property
    def public_api_base(self) -> str:
        return _api_origin(self._api_url)

    @property
    def provider_configured(self) -> bool:
        return bool(self._api_url and self._api_key)

    def _check_client_rate(self, request: FastAPIRequest) -> None:
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self._lock:
            history = self._requests.pop(client, deque())
            while history and now - history[0] > self.RATE_WINDOW_SECONDS:
                history.popleft()
            if len(history) >= self.RATE_LIMIT:
                self._requests[client] = history
                raise HTTPException(status_code=429, detail="maps request rate limit exceeded")
            history.append(now)
            self._requests[client] = history
            while len(self._requests) > self.CLIENT_LIMIT:
                self._requests.popitem(last=False)

    def _check_worker_rate(self) -> None:
        """Bound internal worker provider calls without tying them to a client."""
        now = time.monotonic()
        with self._lock:
            while self._worker_requests and now - self._worker_requests[0] > self.RATE_WINDOW_SECONDS:
                self._worker_requests.popleft()
            if len(self._worker_requests) >= self.WORKER_RATE_LIMIT:
                raise HTTPException(status_code=429, detail="maps worker request limit exceeded")
            self._worker_requests.append(now)

    def _cache_get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry is None:
                return None
            expires, result = entry
            if expires <= now:
                return None
            self._cache[key] = entry
            return result

    def _cache_put(self, key: str, result: Any) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic() + self.CACHE_TTL_SECONDS, result)
            self._cache.move_to_end(key)
            while len(self._cache) > self.CACHE_LIMIT:
                self._cache.popitem(last=False)

    def _provider_json(self, path: str, parameters: dict[str, str]) -> dict[str, Any]:
        if not self._api_url or not self._api_key:
            raise HTTPException(status_code=503, detail="maps provider is not configured")
        # The path is supplied only by this module, never by the HTTP client.
        query = urlencode({**parameters, "key": self._api_key})
        request_url = f"{self._api_url}{path}?{query}"
        request = Request(request_url, headers={"Accept": "application/json", "User-Agent": "CherryMap/1.0"})
        try:
            with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:  # nosec B310: fixed managed origin/path
                payload = response.read(2 * 1024 * 1024)
        except HTTPError as exc:
            if exc.code in {400, 401, 403}:
                raise HTTPException(status_code=502, detail="maps provider rejected the request") from None
            raise HTTPException(status_code=502, detail="maps provider is unavailable") from None
        except (URLError, TimeoutError, OSError):
            raise HTTPException(status_code=502, detail="maps provider is unavailable") from None
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=502, detail="maps provider returned invalid data") from None
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="maps provider returned invalid data")
        return parsed

    @staticmethod
    def _require_ok(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status == "OK":
            return
        if status == "ZERO_RESULTS":
            return
        if isinstance(status, str) and status:
            raise HTTPException(status_code=502, detail=f"maps provider returned {status}")
        raise HTTPException(status_code=502, detail="maps provider returned an invalid response")

    def geocode(self, query: str, request: FastAPIRequest) -> list[dict[str, object]]:
        self._check_client_rate(request)
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 160 or any(ord(char) < 32 for char in normalized):
            raise HTTPException(status_code=422, detail="query must be between 1 and 160 printable characters")
        cache_key = normalized.casefold()
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        payload = self._provider_json("/v1/maps/proxy/maps/api/geocode/json", {"address": normalized})
        self._require_ok(payload)
        if payload.get("status") == "ZERO_RESULTS":
            result: list[dict[str, object]] = []
        else:
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                raise HTTPException(status_code=502, detail="maps provider returned invalid geocoding data")
            result = []
            for item in raw_results[:10]:
                if not isinstance(item, dict):
                    continue
                geometry = item.get("geometry")
                location = geometry.get("location") if isinstance(geometry, dict) else None
                formatted = item.get("formatted_address")
                if not isinstance(location, dict) or not isinstance(formatted, str):
                    continue
                lat, lng = location.get("lat"), location.get("lng")
                if isinstance(lat, bool) or isinstance(lng, bool) or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
                    continue
                place_id = item.get("place_id")
                identifier = place_id if isinstance(place_id, str) and place_id else hashlib.sha256(formatted.encode()).hexdigest()[:20]
                name = formatted
                components = item.get("address_components")
                if isinstance(components, list) and components and isinstance(components[0], dict) and isinstance(components[0].get("long_name"), str):
                    name = components[0]["long_name"]
                result.append({"id": identifier, "name": name, "address": formatted, "lat": float(lat), "lng": float(lng), "origin": "provider"})
        self._cache_put(cache_key, result)
        return result

    @staticmethod
    def _nearby_text(value: object, *, maximum: int) -> str | None:
        """Accept bounded printable provider display text, never provider metadata."""
        if not isinstance(value, str):
            return None
        normalized = " ".join(value.split())
        if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
            return None
        return normalized

    @staticmethod
    def _nearby_coordinate(value: object, *, maximum: float) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        coordinate = float(value)
        if not math.isfinite(coordinate) or not -maximum <= coordinate <= maximum:
            return None
        return coordinate

    @staticmethod
    def _nearby_category(value: object) -> str | None:
        """Limit a provider type to a short display token rather than arbitrary text."""
        if not isinstance(value, str):
            return None
        category = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", category):
            return None
        return category

    @staticmethod
    def _validate_nearby(lat: object, lng: object, radius: object) -> tuple[float, float, int]:
        if (
            isinstance(lat, bool)
            or isinstance(lng, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lng, (int, float))
            or not math.isfinite(lat)
            or not math.isfinite(lng)
            or not -90 <= lat <= 90
            or not -180 <= lng <= 180
        ):
            raise HTTPException(status_code=422, detail="lat and lng must be finite geographic coordinates")
        if isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 5000:
            raise HTTPException(status_code=422, detail="radius must be an integer between 100 and 5000")
        return float(lat), float(lng), radius

    def nearby(self, lat: float, lng: float, radius: int, request: FastAPIRequest) -> dict[str, object]:
        """Return at most one real nearby-search provider page in a narrow contract."""
        self._check_client_rate(request)
        checked_lat, checked_lng, checked_radius = self._validate_nearby(lat, lng, radius)
        return self._nearby_fetch(checked_lat, checked_lng, checked_radius, force_refresh=False, worker=False)

    def nearby_for_worker(
        self,
        lat: float,
        lng: float,
        radius: int,
        *,
        force_refresh: bool = True,
    ) -> dict[str, object]:
        """Fetch a safe nearby page for the app-owned worker only.

        This internal method deliberately accepts no ``Request`` and therefore
        cannot retain client headers, identity, or browser credentials.  The
        normal public ``nearby`` endpoint keeps its client rate check.
        """
        checked_lat, checked_lng, checked_radius = self._validate_nearby(lat, lng, radius)
        return self._nearby_fetch(checked_lat, checked_lng, checked_radius, force_refresh=force_refresh, worker=True)

    def place_details_for_worker(self, place_id: str) -> dict[str, object] | None:
        """Read one selected provider place through a fixed proxy path only.

        The child runtime accepts validated basemap place IDs and retains only
        short-lived selected/recent public details. No browser URL, fields expression, headers, or credentials
        are accepted across the IPC boundary.
        """
        if not isinstance(place_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", place_id):
            raise HTTPException(status_code=422, detail="place id is invalid")
        cache_key = f"place-details:{place_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        self._check_worker_rate()
        payload = self._provider_json(
            "/v1/maps/proxy/maps/api/place/details/json",
            {"place_id": place_id, "fields": "name,place_id,geometry,formatted_address,types,url,business_status"},
        )
        self._require_ok(payload)
        if payload.get("status") == "ZERO_RESULTS":
            self._cache_put(cache_key, None)
            return None
        raw = payload.get("result")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=502, detail="maps provider returned invalid place details")
        geometry = raw.get("geometry")
        location = geometry.get("location") if isinstance(geometry, dict) else None
        lat = self._nearby_coordinate(location.get("lat") if isinstance(location, dict) else None, maximum=90)
        lng = self._nearby_coordinate(location.get("lng") if isinstance(location, dict) else None, maximum=180)
        name = self._nearby_text(raw.get("name"), maximum=200)
        address = self._nearby_text(raw.get("formatted_address"), maximum=300)
        returned_id = self._nearby_text(raw.get("place_id"), maximum=180)
        if lat is None or lng is None or name is None or address is None or returned_id != place_id:
            raise HTTPException(status_code=502, detail="maps provider returned invalid place details")
        result: dict[str, object] = {"id": place_id, "place_id": place_id, "name": name, "address": address, "lat": lat, "lng": lng, "origin": "provider"}
        generic_types = {"establishment", "point_of_interest", "premise", "geocode", "political", "locality", "sublocality", "neighborhood", "route", "street_address", "plus_code"}
        types = raw.get("types")
        if isinstance(types, list):
            for item_type in types:
                category = self._nearby_category(item_type)
                if category is not None and category.casefold() not in generic_types:
                    result["category"] = category
                    break
        raw_url = raw.get("url")
        if isinstance(raw_url, str):
            parsed = urlsplit(raw_url)
            if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
                # Provider URLs may contain a key-like tracking query. Preserve
                # only the HTTPS origin and path needed for a safe display link.
                result["url"] = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        business_status = self._nearby_category(raw.get("business_status"))
        if business_status is not None:
            result["business_status"] = business_status
        self._cache_put(cache_key, result)
        return result

    def _nearby_fetch(
        self,
        lat: float,
        lng: float,
        radius: int,
        *,
        force_refresh: bool,
        worker: bool,
    ) -> dict[str, object]:
        """Use only the fixed nearby endpoint, optionally bypassing its TTL cache."""

        # Query-center quantization reduces duplicate provider calls but provider
        # coordinates themselves are retained exactly as returned.
        cache_key = f"nearby:{lat:.4f}:{lng:.4f}:{radius}"
        if not force_refresh:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        if worker:
            self._check_worker_rate()

        payload = self._provider_json(
            "/v1/maps/proxy/maps/api/place/nearbysearch/json",
            {"location": f"{lat:.6f},{lng:.6f}", "radius": str(radius)},
        )
        self._require_ok(payload)
        raw_results = payload.get("results")
        if payload.get("status") == "ZERO_RESULTS":
            result: dict[str, object] = {"results": [], "has_more": False}
            self._cache_put(cache_key, result)
            return result
        if not isinstance(raw_results, list):
            raise HTTPException(status_code=502, detail="maps provider returned invalid nearby-place data")

        result_limit = 60 if worker else 20
        page_token = payload.get("next_page_token")
        seen_tokens: set[str] = set()
        if worker:
            # Each token comes from the provider, never from a browser URL. Up
            # to three real pages broaden coverage without a global census claim.
            raw_results = list(raw_results[:20])
            for _ in range(2):
                if not isinstance(page_token, str) or not page_token or len(page_token) > 2048 or page_token in seen_tokens:
                    break
                seen_tokens.add(page_token)
                time.sleep(2.0)  # Provider next-page tokens need activation time.
                try:
                    self._check_worker_rate()
                    page = self._provider_json("/v1/maps/proxy/maps/api/place/nearbysearch/json", {"pagetoken": page_token})
                    if page.get("status") == "INVALID_REQUEST":
                        time.sleep(1.0)
                        self._check_worker_rate()
                        page = self._provider_json("/v1/maps/proxy/maps/api/place/nearbysearch/json", {"pagetoken": page_token})
                    self._require_ok(page)
                    rows = page.get("results")
                    if not isinstance(rows, list): break
                    raw_results.extend(rows[:20])
                    page_token = page.get("next_page_token")
                except HTTPException:
                    # Retain the already-retrieved real pages and expose more.
                    break

        places: list[dict[str, object]] = []
        generic_types = {
            "establishment", "point_of_interest", "premise", "geocode", "political",
            "locality", "sublocality", "neighborhood", "route", "street_address", "plus_code",
        }
        for item in raw_results[:result_limit]:
            if not isinstance(item, dict):
                continue
            item_types = item.get("types")
            if worker and isinstance(item_types, list) and any(kind in {"locality", "political", "administrative_area_level_1", "administrative_area_level_2"} for kind in item_types):
                continue
            geometry = item.get("geometry")
            location = geometry.get("location") if isinstance(geometry, dict) else None
            lat_value = self._nearby_coordinate(location.get("lat") if isinstance(location, dict) else None, maximum=90)
            lng_value = self._nearby_coordinate(location.get("lng") if isinstance(location, dict) else None, maximum=180)
            name = self._nearby_text(item.get("name"), maximum=200)
            address = self._nearby_text(item.get("vicinity"), maximum=300)
            if address is None:
                address = self._nearby_text(item.get("formatted_address"), maximum=300)
            if lat_value is None or lng_value is None or name is None or address is None:
                continue

            place_id = self._nearby_text(item.get("place_id"), maximum=255)
            identifier = place_id or hashlib.sha256(
                f"{name}\0{address}\0{lat_value:.10f}\0{lng_value:.10f}".encode("utf-8")
            ).hexdigest()[:20]
            place: dict[str, object] = {
                "id": identifier,
                "name": name,
                "address": address,
                "lat": lat_value,
                "lng": lng_value,
                "origin": "provider",
            }
            if place_id is not None:
                place["place_id"] = place_id
            types = item.get("types")
            if isinstance(types, list):
                for item_type in types:
                    category = self._nearby_category(item_type)
                    if category is not None and category.casefold() not in generic_types:
                        place["category"] = category
                        break
            places.append(place)

        result = {
            "results": places,
            # Source workers may return three real pages; public nearby remains
            # one page. Neither contract implies exhaustive worldwide coverage.
            "has_more": (
                len(raw_results) > result_limit
                or (isinstance(page_token, str) and bool(page_token.strip()))
            ),
        }
        self._cache_put(cache_key, result)
        return result

    def text_search_for_worker(self, query: str, lat: float | None = None, lng: float | None = None, radius: int | None = None) -> list[dict[str, object]]:
        """Search the fixed managed text-search endpoint for the source child."""
        query = self._nearby_text(query, maximum=160)
        if query is None:
            raise HTTPException(status_code=422, detail="search query is invalid")
        params: dict[str, str] = {"query": query}
        if lat is not None and lng is not None and radius is not None:
            params.update({"location": f"{float(lat):.7f},{float(lng):.7f}", "radius": str(int(radius))})
        cache_key = "text-search:" + query.casefold() + ":" + ":".join(str(params.get(k, "")) for k in ("location", "radius"))
        cached = self._cache_get(cache_key)
        if cached is not None:
            return list(cached)
        self._check_worker_rate()
        payload = self._provider_json("/v1/maps/proxy/maps/api/place/textsearch/json", params)
        self._require_ok(payload)
        rows = payload.get("results")
        if not isinstance(rows, list):
            return []
        output: list[dict[str, object]] = []
        for item in rows[:20]:
            if not isinstance(item, dict):
                continue
            geometry = item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
            location = geometry.get("location") if isinstance(geometry, dict) else {}
            lat_value = self._nearby_coordinate(location.get("lat") if isinstance(location, dict) else None, maximum=90)
            lng_value = self._nearby_coordinate(location.get("lng") if isinstance(location, dict) else None, maximum=180)
            name = self._nearby_text(item.get("name"), maximum=255)
            address = self._nearby_text(item.get("formatted_address") or item.get("vicinity"), maximum=300)
            if lat_value is None or lng_value is None or name is None or address is None:
                continue
            place_id = self._nearby_text(item.get("place_id"), maximum=180)
            identifier = place_id or hashlib.sha256(f"{name}\0{address}\0{lat_value}\0{lng_value}".encode()).hexdigest()[:20]
            record: dict[str, object] = {"id": identifier, "name": name, "address": address, "lat": lat_value, "lng": lng_value}
            if place_id is not None:
                record["place_id"] = place_id
            types = item.get("types")
            if isinstance(types, list):
                for kind in types:
                    category = self._nearby_category(kind)
                    if category and category not in {"establishment", "point_of_interest", "premise"}:
                        record["category"] = category
                        break
            output.append(record)
        self._cache_put(cache_key, output)
        return output

    def directions(self, origin: str, destination: str, mode: str, request: FastAPIRequest) -> dict[str, object]:
        self._check_client_rate(request)
        payload = self._provider_json(
            "/v1/maps/proxy/maps/api/directions/json",
            {"origin": origin, "destination": destination, "mode": mode},
        )
        self._require_ok(payload)
        if payload.get("status") == "ZERO_RESULTS":
            raise HTTPException(status_code=404, detail="maps provider returned ZERO_RESULTS")
        routes = payload.get("routes")
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            raise HTTPException(status_code=502, detail="maps provider returned no route")
        route = routes[0]
        polyline = route.get("overview_polyline")
        encoded = polyline.get("points") if isinstance(polyline, dict) else None
        legs = route.get("legs")
        if not isinstance(encoded, str) or not isinstance(legs, list) or not legs or not isinstance(legs[0], dict):
            raise HTTPException(status_code=502, detail="maps provider returned incomplete route data")
        leg = legs[0]
        distance = leg.get("distance")
        duration = leg.get("duration")
        if not isinstance(distance, dict) or not isinstance(duration, dict) or not isinstance(distance.get("text"), str) or not isinstance(duration.get("text"), str):
            raise HTTPException(status_code=502, detail="maps provider returned incomplete route data")
        raw_steps = leg.get("steps")
        if not isinstance(raw_steps, list):
            raise HTTPException(status_code=502, detail="maps provider returned incomplete route data")
        steps: list[dict[str, str]] = []
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            step_distance, step_duration = step.get("distance"), step.get("duration")
            if not isinstance(step_distance, dict) or not isinstance(step_duration, dict):
                continue
            if not isinstance(step_distance.get("text"), str) or not isinstance(step_duration.get("text"), str):
                continue
            steps.append({
                "instruction": strip_html_instruction(step.get("html_instructions")),
                "distance": step_distance["text"],
                "duration": step_duration["text"],
            })
        return {"distance": distance["text"], "duration": duration["text"], "points": decode_polyline(encoded), "steps": steps}
