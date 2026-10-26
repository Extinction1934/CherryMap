"""Read-only software synchronization checks for a source-extension frame.

These checks establish local pipeline consistency/freshness only. They neither
produce a TPM quote nor establish sensor/hardware attestation.
"""
from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import Any

from .source_gate import EXPECTED_SHA256


def _fresh(timestamp: object, *, now: datetime, maximum_age: float) -> bool:
    if not isinstance(timestamp, str) or len(timestamp) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        age = (now - parsed.astimezone(UTC)).total_seconds()
        return -2 <= age <= maximum_age
    except (ValueError, OverflowError):
        return False


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _nonnegative_int_or_none(value: object) -> int | None:
    """Return a narrowed counter only when it satisfies the public contract."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def verify_software_sync(
    *, source: list[dict[str, Any]], worker: dict[str, Any],
    index: dict[str, Any], nearby: dict[str, Any], fusion: dict[str, Any], poi: dict[str, Any] | None = None,
    runner: dict[str, Any], now: datetime | None = None,
) -> dict[str, Any]:
    checked = now or datetime.now(UTC)
    source_limit = 15.0
    total_in_view = index.get("total_in_view", 0)
    total = int(total_in_view) if _nonnegative_integer(total_in_view) else 0
    named_total = index.get("coverage", {}).get("named_total", 0) if isinstance(index.get("coverage"), dict) else 0
    page_total = int(named_total) if _nonnegative_integer(named_total) else total
    index_limit = max(15.0, (ceil(page_total / 500) + 2) * 5.0) if index.get("mode") in {"places", "overview"} else 15.0
    provider_limit = 90.0
    index_enabled = index.get("status") != "hidden"
    provider_enabled = nearby.get("status") != "hidden"
    checks: dict[str, bool | None] = {
        "source_integrity": worker.get("running") is True and worker.get("status") == "running"
            and runner.get("sha256") == EXPECTED_SHA256 and runner.get("original_sha256") == EXPECTED_SHA256,
        "source_refresh": worker.get("running") is True and worker.get("status") == "running"
            and _fresh(worker.get("last_refresh_at"), now=checked, maximum_age=source_limit),
        "index_refresh": None,
        "index_complete": None,
        "provider_refresh": None,
        "fusion_consistent": False,
    }
    if index_enabled:
        checks["index_refresh"] = index.get("worker_running") is True and index.get("status") == "ready" \
            and _fresh(index.get("refreshed_at"), now=checked, maximum_age=index_limit)
        indexed = index.get("places", [])
        if index.get("mode") == "clusters":
            represented = sum(
                int(record["cluster"]["count"]) for record in indexed
                if isinstance(record, dict) and isinstance(record.get("cluster"), dict)
                and _nonnegative_integer(record["cluster"].get("count"))
            )
            checks["index_complete"] = index.get("status") == "ready" and represented == total and index.get("has_more") is False
        elif index.get("mode") == "overview":
            coverage = index.get("coverage")
            clusters = [
                record for record in indexed
                if isinstance(record, dict) and record.get("origin") == "index-cluster"
                and isinstance(record.get("cluster"), dict)
                and _nonnegative_integer(record["cluster"].get("count"))
            ]
            named = [
                record for record in indexed
                if isinstance(record, dict) and record.get("origin") == "open-index"
            ]
            if not isinstance(coverage, dict):
                checks["index_complete"] = False
            else:
                represented = sum(int(record["cluster"]["count"]) for record in clusters)
                covered: object = coverage.get("represented_records")
                coverage_named_total: object = coverage.get("named_total")
                coverage_named_loaded: object = coverage.get("named_loaded")
                coverage_clusters: object = coverage.get("cluster_count")
                covered_count = _nonnegative_int_or_none(covered)
                named_total_count = _nonnegative_int_or_none(coverage_named_total)
                named_loaded_count = _nonnegative_int_or_none(coverage_named_loaded)
                cluster_count = _nonnegative_int_or_none(coverage_clusters)
                structurally_valid = (
                    covered_count is not None and named_total_count is not None
                    and named_loaded_count is not None and cluster_count is not None
                    and coverage.get("includes_capitals") is True
                )
                complete = structurally_valid and (
                    represented == total == covered_count
                    and len(clusters) == cluster_count
                    and len(named) == named_loaded_count == named_total_count
                    and index.get("loaded_count") == named_loaded_count
                    and index.get("has_more") is False
                )
                # A paged actual-name overview is usable (clusters plus the
                # first 500-name page) but cannot truthfully claim complete
                # synchronization until all matching LOD rows are present.
                checks["index_complete"] = True if complete else None if (
                    structurally_valid and index.get("status") == "ready" and index.get("has_more") is True
                ) else False
        else:
            checks["index_complete"] = index.get("status") == "ready" and index.get("has_more") is False \
                and index.get("loaded_count") == total and len(indexed) == total
    if provider_enabled:
        checks["provider_refresh"] = nearby.get("worker_running") is True and nearby.get("status") == "ready" \
            and _fresh(nearby.get("last_refresh_at"), now=checked, maximum_age=provider_limit)

    if poi is not None:
        checks["poi_available"] = None
        checks["poi_complete"] = None
        if poi.get("status") not in {"hidden", "zoom"}:
            checks["poi_available"] = poi.get("status") == "ready"
            checks["poi_complete"] = None if poi.get("has_more") is True else (
                poi.get("status") == "ready" and poi.get("loaded_count") == poi.get("total_in_view")
                and len(poi.get("places", [])) == poi.get("total_in_view")
            )

    # Exact-ID precedence mirrors the extension; no coordinate-based identity or
    # browser acknowledgement is invented. A provider page is not global coverage.
    collections = (source, index.get("places", []), nearby.get("places", []), (poi or {}).get("places", []))
    expected: dict[str, dict[str, Any]] = {}
    for collection in collections:
        for record in collection:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                expected.setdefault(record["id"], record)
    markers = fusion.get("markers")
    counts = fusion.get("counts")
    if isinstance(markers, list) and isinstance(counts, dict):
        actual: dict[str, dict[str, Any]] = {}
        valid = True
        for record in markers:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or record["id"] in actual:
                valid = False
                break
            actual[record["id"]] = record
        expected_counts = {
            "source": len(source), "index": len(collections[1]), "provider": len(collections[2]), "total": len(expected),
        }
        if poi is not None:
            expected_counts["poi"] = len(collections[3])
        checks["fusion_consistent"] = valid and actual == expected and counts == expected_counts and _fresh(fusion.get("fused_at"), now=checked, maximum_age=15.0)
    waiting = any(layer.get("status") == "loading" for layer in (index, nearby, poi or {})) or (
        index_enabled and checks["index_complete"] is None
    ) or (
        poi is not None and poi.get("status") == "ready" and poi.get("has_more") is True
    )
    verified = not waiting and all(value is not False for value in checks.values())
    return {
        "scope": "software-marker-pipeline",
        "status": "waiting" if waiting else "verified" if verified else "degraded",
        "verified": verified,
        "checked_at": checked.isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "failed_checks": [key for key, value in checks.items() if value is False],
        "maximum_age_seconds": {"source": source_limit, "index": index_limit, "provider": provider_limit},
        "hardware_verified": False,
        "sensor_verified": False,
        "coverage": "available inputs for this frame; not exhaustive provider coverage or browser-render acknowledgement",
    }
