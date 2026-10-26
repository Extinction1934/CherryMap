"""Explicit importer for the user-approved GeoNames cities500 archive.

Usage: ``python -m server.import_geonames data_downloads/cities500.zip``.
It is deliberately not run by server startup: the managed database remains the
durable source of imported data and restarts never download or overwrite it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .geo_index import GeoIndexError, GeoIndexRepository, dataset_version_for_archive, stream_cities500

BATCH_SIZE = 5000
PROGRESS_EVERY = 10_000


def import_archive(path: Path, *, repository: GeoIndexRepository | None = None) -> dict[str, object]:
    """Stream one archive into the additive index and return measured metadata."""
    archive = Path(path)
    if not archive.is_file():
        raise GeoIndexError("GeoNames archive is unavailable")
    repo = repository or GeoIndexRepository()
    version, fingerprint = dataset_version_for_archive(archive)
    started = time.monotonic()
    repo.ensure_schema()
    existing = repo.import_metadata()
    if (
        existing is not None
        and existing.get("status") == "complete"
        and existing.get("source_sha256") == fingerprint
    ):
        return {
            "status": "already_complete",
            "version": existing["version"],
            "record_count": int(existing["record_count"]),
            "country_count": int(existing["country_count"]),
            "imported_at": existing.get("imported_at"),
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    repo.begin_import(version=version, fingerprint=fingerprint)
    records_read = 0
    batch = []
    try:
        for record in stream_cities500(archive):
            batch.append(record)
            records_read += 1
            if len(batch) >= BATCH_SIZE:
                repo.insert_places(batch, version=version)
                batch.clear()
            if records_read % PROGRESS_EVERY == 0:
                # Deliberately brief: no database URL, credentials, or raw row data.
                print(f"GeoNames import progress: {records_read} rows", flush=True)
        if batch:
            repo.insert_places(batch, version=version)
        record_count, country_count, imported_at = repo.complete_import(version=version)
    except Exception:
        try:
            repo.fail_import()
        except Exception:
            pass
        raise
    return {
        "status": "complete",
        "version": version,
        "record_count": record_count,
        "country_count": country_count,
        "imported_at": imported_at,
        "rows_read": records_read,
        "duration_seconds": round(time.monotonic() - started, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the GeoNames cities500 archive into CherryMap's own index")
    parser.add_argument("archive", type=Path, help="path to cities500.zip")
    arguments = parser.parse_args(argv)
    try:
        result = import_archive(arguments.archive)
    except GeoIndexError as exc:
        print(f"GeoNames import failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("GeoNames import failed: unexpected import error", file=sys.stderr)
        return 1
    print(
        "GeoNames import {status}: {record_count} places across {country_count} countries "
        "(version {version}, {duration_seconds}s)".format(**result),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
