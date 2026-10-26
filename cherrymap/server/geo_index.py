"""GeoNames cities500 parsing and additive MySQL access for CherryMap's own index.

This module contains no HTTP routes and never contacts an upstream provider.  The
only data source is the explicitly supplied GeoNames archive handled by the
import command.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlsplit
from zipfile import BadZipFile, ZipFile

DATASET_KEY = "geonames-cities500"
DATASET_NAME = "GeoNames cities500"
DATASET_SOURCE_URL = "https://download.geonames.org/export/dump/cities500.zip"
DATASET_LICENSE = "CC BY 4.0"
ARCHIVE_MEMBER = "cities500.txt"
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_NAME_LENGTH = 255
MAX_COUNTRY_LENGTH = 8
MAX_FEATURE_LENGTH = 32


class GeoIndexError(RuntimeError):
    """A fixed, public-safe error category for index storage and input."""


class GeoNamesRecordError(GeoIndexError):
    """A cities500 row did not meet the required geographic record invariants."""


@dataclass(frozen=True)
class GeoNamesPlace:
    geoname_id: int
    name: str
    latitude: float
    longitude: float
    country_code: str
    feature_code: str
    population: int
    modification_date: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dataset_version_for_archive(path: Path) -> tuple[str, str]:
    """Return a deterministic version based on the whole supplied archive."""
    fingerprint = file_sha256(path)
    return f"cities500-{fingerprint[:16]}", fingerprint


def _clean_required(value: str, *, field: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise GeoNamesRecordError(f"invalid {field}")
    return value


def _integer(value: str, *, field: str, minimum: int | None = None) -> int:
    if not re.fullmatch(r"-?[0-9]+", value):
        raise GeoNamesRecordError(f"invalid {field}")
    try:
        result = int(value)
    except ValueError as exc:
        raise GeoNamesRecordError(f"invalid {field}") from exc
    if minimum is not None and result < minimum:
        raise GeoNamesRecordError(f"invalid {field}")
    return result


def _coordinate(value: str, *, latitude: bool) -> float:
    try:
        coordinate = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeoNamesRecordError("invalid coordinate") from exc
    maximum = 90.0 if latitude else 180.0
    if not math.isfinite(coordinate) or not -maximum <= coordinate <= maximum:
        raise GeoNamesRecordError("invalid coordinate")
    return coordinate


def parse_geonames_tsv(line: str) -> GeoNamesPlace:
    """Validate one GeoNames dump line using its documented 19-field layout."""
    fields = line.rstrip("\r\n").split("\t")
    if len(fields) != 19:
        raise GeoNamesRecordError("invalid field count")
    geoname_id = _integer(fields[0], field="geoname id", minimum=1)
    name = _clean_required(fields[1], field="name", maximum=MAX_NAME_LENGTH)
    latitude = _coordinate(fields[4], latitude=True)
    longitude = _coordinate(fields[5], latitude=False)
    feature_code = _clean_required(fields[7], field="feature code", maximum=MAX_FEATURE_LENGTH)
    country_code = _clean_required(fields[8], field="country code", maximum=MAX_COUNTRY_LENGTH)
    # The current GeoNames country field is normally two letters.  Keep the
    # validation compatible with its documented exceptional territorial codes
    # while rejecting arbitrary text.
    if not re.fullmatch(r"[A-Za-z]{2,8}", country_code):
        raise GeoNamesRecordError("invalid country code")
    population = _integer(fields[14], field="population", minimum=0)
    modification_date = fields[18].strip()
    try:
        # Preserve the documented YYYY-MM-DD representation after validation.
        from datetime import date

        date.fromisoformat(modification_date)
    except ValueError as exc:
        raise GeoNamesRecordError("invalid modification date") from exc
    return GeoNamesPlace(
        geoname_id=geoname_id,
        name=name,
        latitude=latitude,
        longitude=longitude,
        country_code=country_code.upper(),
        feature_code=feature_code,
        population=population,
        modification_date=modification_date,
    )


def stream_cities500(path: Path) -> Iterator[GeoNamesPlace]:
    """Yield validated archive rows without extraction or a full in-memory copy."""
    archive_path = Path(path)
    try:
        with ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != ARCHIVE_MEMBER:
                raise GeoIndexError("GeoNames archive layout is invalid")
            member = members[0]
            if member.file_size <= 0 or member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise GeoIndexError("GeoNames archive is outside the supported size")
            with archive.open(member, "r") as raw:
                import io

                with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
                    for line_number, line in enumerate(text, start=1):
                        try:
                            yield parse_geonames_tsv(line)
                        except GeoNamesRecordError as exc:
                            raise GeoNamesRecordError(f"invalid GeoNames record at line {line_number}") from exc
    except (OSError, BadZipFile) as exc:
        raise GeoIndexError("GeoNames archive cannot be read") from exc


def mysql_connect_from_env():
    """Create a TLS-aware PyMySQL connection from a managed ``DATABASE_URL``.

    Credentials are intentionally never included in raised messages or logs.
    The managed URL's ``ssl`` option is carried into PyMySQL's ``ssl`` mapping,
    which makes the driver create a TLS context for values such as ``true``.
    """
    raw_url = os.getenv("DATABASE_URL", "")
    try:
        parsed = urlsplit(raw_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        database = unquote(parsed.path.lstrip("/"))
        if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not database:
            raise ValueError("invalid managed database URL")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if not username:
            raise ValueError("invalid managed database URL")
    except Exception as exc:
        raise GeoIndexError("managed database configuration is unavailable") from exc

    options: dict[str, object] = {
        "host": parsed.hostname,
        "port": int(parsed.port or 3306),
        "user": username,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": 15,
        "write_timeout": 30,
    }
    ssl_value = (query.get("ssl") or [""])[0].strip()
    if ssl_value and ssl_value.lower() not in {"0", "false", "off", "disable", "disabled"}:
        # PyMySQL accepts a mapping here.  Retaining the URL option under the
        # ``ssl`` key forces TLS for the common ``?ssl=true`` managed URL while
        # avoiding an invented CA path.
        options["ssl"] = {"ssl": ssl_value}
    try:
        import pymysql

        return pymysql.connect(**options)
    except Exception as exc:
        raise GeoIndexError("managed database connection is unavailable") from exc


class GeoIndexRepository:
    """Small synchronous repository; callers run it in bounded worker threads."""

    def __init__(self, connect=mysql_connect_from_env) -> None:
        self._connect = connect

    def ensure_schema(self) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS geo_imports (
                        dataset_key VARCHAR(64) NOT NULL PRIMARY KEY,
                        dataset_name VARCHAR(128) NOT NULL,
                        dataset_version VARCHAR(96) NOT NULL,
                        source_sha256 CHAR(64) NOT NULL,
                        source_url VARCHAR(255) NOT NULL,
                        license_name VARCHAR(64) NOT NULL,
                        status VARCHAR(16) NOT NULL,
                        record_count BIGINT NOT NULL DEFAULT 0,
                        country_count INT NOT NULL DEFAULT 0,
                        imported_at VARCHAR(40) NULL,
                        updated_at VARCHAR(40) NOT NULL,
                        error_code VARCHAR(96) NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS geo_places (
                        geoname_id BIGINT NOT NULL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        latitude DOUBLE NOT NULL,
                        longitude DOUBLE NOT NULL,
                        country_code VARCHAR(8) NOT NULL,
                        feature_code VARCHAR(32) NOT NULL,
                        population BIGINT NOT NULL,
                        modification_date DATE NOT NULL,
                        dataset_version VARCHAR(96) NOT NULL,
                        INDEX geo_places_lat_lon (latitude, longitude),
                        INDEX geo_places_version (dataset_version)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise GeoIndexError("managed database schema is unavailable") from exc
        finally:
            connection.close()

    def import_metadata(self) -> dict[str, object] | None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT dataset_version, source_sha256, status, record_count, country_count, imported_at "
                    "FROM geo_imports WHERE dataset_key = %s",
                    (DATASET_KEY,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return {
                "version": str(row[0]),
                "source_sha256": str(row[1]),
                "status": str(row[2]),
                "record_count": int(row[3]),
                "country_count": int(row[4]),
                "imported_at": row[5],
            }
        except Exception as exc:
            raise GeoIndexError("managed database index metadata is unavailable") from exc
        finally:
            connection.close()

    def begin_import(self, *, version: str, fingerprint: str) -> None:
        now = utc_now()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO geo_imports
                    (dataset_key, dataset_name, dataset_version, source_sha256, source_url, license_name,
                     status, record_count, country_count, imported_at, updated_at, error_code)
                    VALUES (%s, %s, %s, %s, %s, %s, 'importing', 0, 0, NULL, %s, NULL)
                    ON DUPLICATE KEY UPDATE
                    dataset_name = VALUES(dataset_name), dataset_version = VALUES(dataset_version),
                    source_sha256 = VALUES(source_sha256), source_url = VALUES(source_url),
                    license_name = VALUES(license_name), status = 'importing', updated_at = VALUES(updated_at),
                    error_code = NULL
                    """,
                    (DATASET_KEY, DATASET_NAME, version, fingerprint, DATASET_SOURCE_URL, DATASET_LICENSE, now),
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise GeoIndexError("managed database import metadata cannot be recorded") from exc
        finally:
            connection.close()

    def insert_places(self, places: list[GeoNamesPlace], *, version: str) -> None:
        if not places:
            return
        connection = self._connect()
        try:
            values = [
                (
                    place.geoname_id,
                    place.name,
                    place.latitude,
                    place.longitude,
                    place.country_code,
                    place.feature_code,
                    place.population,
                    place.modification_date,
                    version,
                )
                for place in places
            ]
            with connection.cursor() as cursor:
                # INSERT IGNORE makes a restarted import additive/idempotent and
                # deliberately does not overwrite a record already present.
                cursor.executemany(
                    """
                    INSERT IGNORE INTO geo_places
                    (geoname_id, name, latitude, longitude, country_code, feature_code,
                     population, modification_date, dataset_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise GeoIndexError("managed database place batch cannot be recorded") from exc
        finally:
            connection.close()

    def complete_import(self, *, version: str) -> tuple[int, int, str]:
        completed_at = utc_now()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT country_code) FROM geo_places WHERE dataset_version = %s",
                    (version,),
                )
                count_row = cursor.fetchone()
                record_count = int(count_row[0])
                country_count = int(count_row[1])
                cursor.execute(
                    """
                    UPDATE geo_imports SET status = 'complete', record_count = %s, country_count = %s,
                    imported_at = %s, updated_at = %s, error_code = NULL WHERE dataset_key = %s
                    """,
                    (record_count, country_count, completed_at, completed_at, DATASET_KEY),
                )
            connection.commit()
            return record_count, country_count, completed_at
        except Exception as exc:
            connection.rollback()
            raise GeoIndexError("managed database completion metadata cannot be recorded") from exc
        finally:
            connection.close()

    def fail_import(self) -> None:
        """Mark metadata safely without persisting implementation exception text."""
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE geo_imports SET status = 'error', updated_at = %s, error_code = 'import_failed' "
                    "WHERE dataset_key = %s",
                    (utc_now(), DATASET_KEY),
                )
            connection.commit()
        except Exception:
            connection.rollback()
        finally:
            connection.close()

    def dataset_metadata(self) -> dict[str, object]:
        """Read only complete imports; incomplete data is never made queryable."""
        metadata = self.import_metadata()
        if metadata is None or metadata["status"] != "complete":
            return {
                "available": False,
                "dataset": public_dataset_metadata(metadata),
                "message": "GeoNames index is not imported",
            }
        return {"available": True, "dataset": public_dataset_metadata(metadata), "message": None}

    def cluster_rows(self, *, south: float, north: float, west: float, east: float, cell_size: float) -> list[tuple]:
        where, parameters = _viewport_sql(south=south, north=north, west=west, east=east)
        # The grid is global rather than viewport-relative, so IDs and bounds
        # remain stable while panning through an otherwise unchanged cell size.
        sql = (
            "SELECT FLOOR((latitude + 90.0) / %s) AS lat_cell, "
            "FLOOR((longitude + 180.0) / %s) AS lng_cell, COUNT(*) AS place_count, "
            "AVG(latitude) AS mean_latitude, AVG(longitude) AS mean_longitude "
            "FROM geo_places WHERE " + where + " GROUP BY lat_cell, lng_cell"
        )
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (cell_size, cell_size, *parameters))
                return list(cursor.fetchall())
        except Exception as exc:
            raise GeoIndexError("managed database cluster query is unavailable") from exc
        finally:
            connection.close()

    def count_places(self, *, south: float, north: float, west: float, east: float) -> int:
        where, parameters = _viewport_sql(south=south, north=north, west=west, east=east)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM geo_places WHERE " + where, parameters)
                row = cursor.fetchone()
                return int(row[0])
        except Exception as exc:
            raise GeoIndexError("managed database place count is unavailable") from exc
        finally:
            connection.close()

    def overview_count(
        self, *, south: float, north: float, west: float, east: float, min_population: int
    ) -> int:
        """Count native overview placemarks selected by documented GeoNames LOD.

        This is deliberately separate from ``count_places``: cluster coverage
        represents every row in a viewport, while this count/page pair selects
        only high-population places plus PPLC country capitals for named map
        labels.  It is read-only, parameterized, and keyset-paged by the caller.
        """
        where, parameters = _viewport_sql(south=south, north=north, west=west, east=east)
        sql = (
            "SELECT COUNT(*) FROM geo_places WHERE " + where
            + " AND (population >= %s OR feature_code = %s)"
        )
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (*parameters, int(min_population), "PPLC"))
                row = cursor.fetchone()
                return int(row[0])
        except Exception as exc:
            raise GeoIndexError("managed database overview count query is unavailable") from exc
        finally:
            connection.close()

    def overview_page(
        self,
        *,
        south: float,
        north: float,
        west: float,
        east: float,
        min_population: int,
        after_id: int,
        limit: int,
    ) -> list[tuple]:
        """Read one native named-overview page without a provider-sized cap."""
        where, parameters = _viewport_sql(south=south, north=north, west=west, east=east)
        sql = (
            "SELECT geoname_id, name, latitude, longitude, country_code, feature_code, population, modification_date "
            "FROM geo_places WHERE " + where
            + " AND (population >= %s OR feature_code = %s) AND geoname_id > %s "
            "ORDER BY geoname_id ASC LIMIT %s"
        )
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (*parameters, int(min_population), "PPLC", int(after_id), int(limit)))
                return list(cursor.fetchall())
        except Exception as exc:
            raise GeoIndexError("managed database overview place query is unavailable") from exc
        finally:
            connection.close()

    def place_page(
        self, *, south: float, north: float, west: float, east: float, after_id: int, limit: int
    ) -> list[tuple]:
        where, parameters = _viewport_sql(south=south, north=north, west=west, east=east)
        sql = (
            "SELECT geoname_id, name, latitude, longitude, country_code, feature_code, population, modification_date "
            "FROM geo_places WHERE " + where + " AND geoname_id > %s "
            "ORDER BY geoname_id ASC LIMIT %s"
        )
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (*parameters, int(after_id), int(limit)))
                return list(cursor.fetchall())
        except Exception as exc:
            raise GeoIndexError("managed database place query is unavailable") from exc
        finally:
            connection.close()


def public_dataset_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
    """The fixed attribution object used even before/after an unavailable import."""
    return {
        "name": DATASET_NAME,
        "record_count": int(metadata["record_count"]) if metadata else 0,
        "imported_at": metadata.get("imported_at") if metadata else None,
        "source_url": DATASET_SOURCE_URL,
        "license": DATASET_LICENSE,
        "version": str(metadata["version"]) if metadata else "unimported",
    }


def _viewport_sql(*, south: float, north: float, west: float, east: float) -> tuple[str, tuple[float, ...]]:
    """Return a bounded predicate that intentionally permits an antimeridian view."""
    if west <= east:
        return "latitude >= %s AND latitude <= %s AND longitude >= %s AND longitude <= %s", (south, north, west, east)
    return "latitude >= %s AND latitude <= %s AND (longitude >= %s OR longitude <= %s)", (south, north, west, east)
