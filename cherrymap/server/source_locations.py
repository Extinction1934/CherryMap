"""Build a minimal, non-executing coordinate index from the immutable source."""
from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DMS_RE = re.compile(
    r"^\s*([NSEW])\s+(\d{1,3})\s+(\d{1,2})\s+(\d{1,2}(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_LABEL_FIELDS = ("name", "observatory", "military_station", "id")


class SourceLocationError(ValueError):
    """The approved, literal location lookup is unavailable or malformed."""


@dataclass(frozen=True)
class SourceLocation:
    id: str
    name: str
    lat: float
    lng: float
    source_line: int
    origin: str = "source"

    def public_dict(self) -> dict[str, object]:
        """Return browser-safe data plus a fully derived, non-executable marker definition."""
        return {
            "id": self.id,
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "source_line": self.source_line,
            "origin": self.origin,
            "marker": {
                "title": self.name,
                "label": self.name,
                "color": "#b92d49",
                "popup": {
                    "heading": self.name,
                    "provenance": "Static Python source location — not live telemetry",
                    "source_line": self.source_line,
                },
            },
        }


def _literal_value(node: ast.AST) -> object | None:
    """Evaluate one AST literal only; never evaluate the enclosing source record."""
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return None


def _literal_string(node: ast.AST) -> str | None:
    value = _literal_value(node)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 160 or any(ord(char) < 32 for char in value):
        return None
    return value


def _dict_fields(node: ast.Dict) -> dict[str, ast.AST]:
    """Return only literal string-keyed fields from one dictionary node."""
    fields: dict[str, ast.AST] = {}
    for key_node, value_node in zip(node.keys, node.values):
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            fields[key_node.value] = value_node
    return fields


def _label(fields: dict[str, ast.AST]) -> str | None:
    for field in _LABEL_FIELDS:
        candidate = fields.get(field)
        if candidate is not None:
            value = _literal_string(candidate)
            if value is not None:
                return value
    return None


def _coordinate(node: ast.AST, *, latitude: bool) -> float:
    """Read one literal decimal or DMS coordinate and enforce geographic bounds."""
    value = _literal_value(node)
    if isinstance(value, bool):
        raise SourceLocationError("source coordinate must not be boolean")

    if isinstance(value, (int, float)):
        coordinate = float(value)
    elif isinstance(value, str):
        match = _DMS_RE.fullmatch(value)
        if match is None:
            raise SourceLocationError("source coordinate is not a safe literal coordinate")
        direction, degrees_text, minutes_text, seconds_text = match.groups()
        direction = direction.upper()
        if (latitude and direction not in {"N", "S"}) or (not latitude and direction not in {"E", "W"}):
            raise SourceLocationError("source DMS coordinate has an invalid hemisphere")
        degrees = int(degrees_text)
        minutes = int(minutes_text)
        seconds = float(seconds_text)
        if minutes >= 60 or seconds >= 60:
            raise SourceLocationError("source DMS coordinate has an invalid component")
        coordinate = degrees + minutes / 60 + seconds / 3600
        if direction in {"S", "W"}:
            coordinate = -coordinate
    else:
        raise SourceLocationError("source coordinate is not a safe literal coordinate")

    maximum = 90 if latitude else 180
    if not math.isfinite(coordinate) or not -maximum <= coordinate <= maximum:
        raise SourceLocationError("source coordinate is outside geographic bounds")
    return coordinate


def _parent_dict(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> tuple[ast.Dict, str] | None:
    """Find the immediate dict field which owns ``node``, without traversing source values."""
    parent = parents.get(node)
    if not isinstance(parent, ast.Dict):
        return None
    for key_node, value_node in zip(parent.keys, parent.values):
        if value_node is node and isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            return parent, key_node.value
    return None


def _stable_identifier(name: str, lat: float, lng: float) -> str:
    material = f"{name}\0{lat:.10f}\0{lng:.10f}".encode("utf-8")
    return f"SOURCE_{hashlib.sha256(material).hexdigest()[:20].upper()}"


def extract_approved_locations(source_path: Path) -> list[SourceLocation]:
    """Index labeled literal coordinates without importing or executing the source.

    Every ``ast.Dict`` with literal ``lat``/``lon`` (or ``lng``) fields is
    considered.  A record is published only when its own dictionary, or the
    immediately enclosing ``coordinates`` record, supplies a safe literal
    ``name``, ``observatory``, ``military_station``, or ``id`` label.  This
    deliberately avoids evaluating computed parents and exposes no other fields.
    """
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise SourceLocationError("source could not be parsed") from exc

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    locations: list[SourceLocation] = []
    for node in sorted((item for item in ast.walk(tree) if isinstance(item, ast.Dict)), key=lambda item: item.lineno):
        fields = _dict_fields(node)
        longitude_node = fields.get("lon", fields.get("lng"))
        latitude_node = fields.get("lat")
        if latitude_node is None or longitude_node is None:
            continue

        name = _label(fields)
        identifier: str | None = _literal_string(fields["id"]) if "id" in fields else None
        parent_field = _parent_dict(node, parents)
        if name is None and parent_field is not None and parent_field[1] == "coordinates":
            name = _label(_dict_fields(parent_field[0]))
        if identifier is None and parent_field is not None:
            outer_key = parent_field[1]
            if _ID_RE.fullmatch(outer_key):
                identifier = outer_key
        if name is None:
            # Coordinates without an approved literal label are not public records.
            continue

        lat = _coordinate(latitude_node, latitude=True)
        lng = _coordinate(longitude_node, latitude=False)
        locations.append(SourceLocation(identifier or _stable_identifier(name, lat, lng), name, lat, lng, node.lineno))

    # Repeated literals are common in generated source.  Preserve the last source
    # occurrence for each label/coordinate pair, then present a source-order index.
    latest: dict[tuple[str, float, float], SourceLocation] = {}
    for location in locations:
        latest[(location.name, location.lat, location.lng)] = location
    return sorted(latest.values(), key=lambda location: location.source_line)


def count_disabled_main_guards(source_path: Path) -> int:
    """Count disabled ``False and __name__`` entry guards without executing source."""
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise SourceLocationError("approved source could not be parsed") from exc

    def is_main_compare(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "__name__"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == "__main__"
        )

    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.BoolOp) or not isinstance(node.test.op, ast.And):
            continue
        values = node.test.values
        if any(isinstance(value, ast.Constant) and value.value is False for value in values) and any(is_main_compare(value) for value in values):
            total += 1
    return total
