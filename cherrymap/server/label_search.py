"""Read-only label search executed inside the source-extension process."""
from __future__ import annotations

import math
import unicodedata
from datetime import UTC, datetime
from typing import Any

from .geo_index import GeoIndexRepository
from .poi_runtime import PoiRepository, POIRuntime, shared_memory_poi_repository


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold().strip()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p = math.pi / 180
    a = math.sin((lat2-lat1)*p/2)**2 + math.cos(lat1*p)*math.cos(lat2*p)*math.sin((lng2-lng1)*p/2)**2
    return 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _like(q: str) -> str:
    return "%" + q.replace("=", "==").replace("%", "=%").replace("_", "=_") + "%"


def _scope_sql(lat: float | None, lng: float | None, radius: int | None) -> tuple[str, list[object]]:
    if lat is None or lng is None or radius is None:
        return "", []
    dlat = radius / 111195.0
    dlng = radius / (111195.0 * max(0.000001, math.cos(math.radians(lat))))
    sql = " AND latitude BETWEEN %s AND %s"
    args: list[object] = [max(-90, lat-dlat), min(90, lat+dlat)]
    if dlng < 180:
        west, east = (lng-dlng+180) % 360-180, (lng+dlng+180) % 360-180
        sql += " AND (longitude >= %s OR longitude <= %s)" if west > east else " AND longitude BETWEEN %s AND %s"
        args += [west, east]
    return sql, args


class LabelSearch:
    """Search current source records, stored geographic/POI data and real businesses."""
    def __init__(self, maps_service: Any, marker: Any, nearby: Any, *, geo_repo: Any = None, poi_repo: Any = None) -> None:
        self.maps, self.marker, self.nearby = maps_service, marker, nearby
        self.geo = geo_repo or GeoIndexRepository()
        self.poi = poi_repo or PoiRepository()

    @staticmethod
    def validate(q: object, lat: object = None, lng: object = None, radius: object = None) -> tuple[str, float | None, float | None, int | None]:
        if not isinstance(q, str) or any(ord(c) < 32 for c in q):
            raise ValueError("query must be between 1 and 160 printable characters")
        text = " ".join(q.split())
        if not 1 <= len(text) <= 160:
            raise ValueError("query must be between 1 and 160 printable characters")
        if any(x is not None for x in (lat, lng, radius)):
            if isinstance(lat, bool) or isinstance(lng, bool) or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180 or isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 20000:
                raise ValueError("scope coordinates and radius are invalid")
            return text, float(lat), float(lng), radius
        return text, None, None, None

    def _db(self, q: str, lat: float | None, lng: float | None, radius: int | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        scope, params = _scope_sql(lat, lng, radius)
        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            connection = self.geo._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT geoname_id,name,latitude,longitude,country_code,feature_code,population,modification_date FROM geo_places WHERE (name LIKE %s ESCAPE '=' OR CAST(geoname_id AS CHAR) = %s)" + scope + " ORDER BY population DESC, geoname_id ASC LIMIT 30", (_like(q), q.removeprefix("geonames:"), *params))
                    for row in cursor.fetchall():
                        name = str(row[1])
                        rows.append({"id": f"geonames:{row[0]}", "name": name, "lat": row[2], "lng": row[3], "origin": "open-index", "country_code": row[4], "population": int(row[6]), "overview": False, "category": row[5], "data_modified_at": str(row[7]), "refreshed_at": stamp, "marker": {"title": name, "label": name, "color": "#467da6", "popup": {"heading": name, "provenance": "GeoNames cities500 · CC BY 4.0 · database label search"}}})
            finally:
                connection.close()
        except Exception:
            pass  # Source matches remain available when the managed index is down.
        memory = shared_memory_poi_repository()
        if memory.engaged:
            try:
                rows.extend(POIRuntime._row_marker(row) for row in memory.search_rows(q, lat, lng, radius))
            except Exception:
                pass
        else:
            try:
                connection = self.poi._connect()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT osm_key,name,latitude,longitude,category,address,tag_fields_json,data_modified_at,retrieved_at FROM open_poi_places WHERE (name LIKE %s ESCAPE '=' OR osm_key LIKE %s ESCAPE '=')" + scope + " ORDER BY name ASC LIMIT 30", (_like(q), _like(q), *params))
                        rows.extend(POIRuntime._row_marker(row) for row in cursor.fetchall())
                finally:
                    connection.close()
            except Exception:
                pass
        return rows

    def search(self, q: object, *, lat: object = None, lng: object = None, radius: object = None, scope: str = "world") -> dict[str, object]:
        from .mapping_extension import _safe_record
        text, la, lo, rad = self.validate(q, lat, lng, radius)
        if scope not in {"world", "nearby"} or (scope == "world" and la is not None) or (scope == "nearby" and la is None):
            raise ValueError("search scope is invalid")
        folded = _fold(text)
        originals = self.marker.locations()
        known = {str(record.get("id")) for record in originals if isinstance(record, dict)}
        matches: list[dict[str, Any]] = []
        warnings: list[str] = []

        def accept(raw: object, family: str, *, matching: bool = False) -> None:
            record = _safe_record(raw, family=family, known_source_ids=known)
            if record is None:
                return
            if matching and folded not in _fold(str(record["name"])) and folded not in _fold(str(record["id"])):
                return
            if la is not None and lo is not None and rad is not None and _distance_m(la, lo, float(record["lat"]), float(record["lng"])) > rad:
                return
            matches.append(record)

        for raw in originals:
            accept(raw, "source", matching=True)
        for raw in self._db(text, la, lo, rad):
            accept(raw, "poi" if raw.get("origin") == "open-poi" else "index", matching=True)
        for raw in self.nearby.selected_places():
            accept(raw, "provider", matching=True)
        exact_source = any(r["origin"] == "source" and folded in {_fold(str(r["name"])), _fold(str(r["id"]))} for r in matches)
        if not exact_source:
            try:
                for raw in self.maps.text_search_for_worker(text, la, lo, rad):
                    name = str(raw.get("name", ""))
                    provider = {**raw, "origin": "provider-worker", "refreshed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "position_origin": "provider-nearby-worker", "marker": {"title": name, "label": name, "color": "#356d63", "popup": {"heading": name, "provenance": "Managed map provider · source-process place label search"}}}
                    accept(provider, "provider")
                    if matches and matches[-1].get("id") == provider.get("id"):
                        self.nearby._remember_provider_place(matches[-1])
            except Exception:
                warnings.append("Provider search is temporarily unavailable; available source/index matches are still shown.")
        unique: dict[str, dict[str, Any]] = {}
        for record in matches:
            unique.setdefault(str(record["id"]), record)
        result = list(unique.values())
        result.sort(key=lambda r: (0 if folded in {_fold(str(r["name"])), _fold(str(r["id"]))} else 1, 0 if r["origin"] == "source" else 1, _distance_m(la, lo, float(r["lat"]), float(r["lng"])) if la is not None and lo is not None else 0))
        result = result[:20]
        return {"results": result, "query": text, "scope": scope, "count": len(result), "warnings": warnings}
