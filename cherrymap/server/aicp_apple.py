"""TPM-119 AICP pins from the Apple source worker.

The identity is read from the immutable Apple source. The pin is a fused
cell of its own, not another channel's title and not a repeated AICP label.
"""

from __future__ import annotations

import re
from pathlib import Path

COLOR = "#0369a1"
CATEGORY = "aicp"
_SOURCE = Path(__file__).resolve().parents[1] / "source" / "CherryPro_Z4_portable.py"


def _identity() -> str:
    version = "Apple Intelligence Conscience Processor v2.0"
    apple = "Apple Inc."
    try:
        with _SOURCE.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(24000)
        found = re.search(r'AICP_VERSION = "([^"]+)"', head)
        if found:
            version = found.group(1)[:80]
        found = re.search(r'APPLE_INC_IDENTIFIER = "([^"]+)"', head)
        if found:
            apple = found.group(1)[:40]
    except OSError:
        pass
    return f"TPM-119 AICP · {apple} source worker · {version} · Conscience Interconnect CoreWeave"


PROVENANCE = _identity()


def _feeds(feeds: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in feeds:
        name = " ".join(str(raw or "").split())
        if name.startswith("NeuralLink · "):
            name = name.split(" · ", 1)[1].strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= 8:
            break
    return seen


def _label(seen: list[str]) -> str:
    if len(seen) >= 5:
        return seen[3]
    if len(seen) == 4:
        return f"{seen[3]} / {seen[1]}"
    if len(seen) == 3:
        return f"{seen[2]} / {seen[0]}"
    return f"{seen[0]} / {seen[1]}"


def pin_fields(cycle: int, feeds: list[str]) -> dict[str, str]:
    seen = _feeds(feeds)
    if len(seen) < 2:
        return {"name": "", "address": "", "category": CATEGORY, "color": COLOR, "provenance": PROVENANCE, "observed_at": ""}
    label = _label(seen)
    if len(label) > 36:
        label = label[:35].rstrip() + "…"
    detail = " · ".join(seen)
    if len(detail) > 280:
        detail = detail[:279].rstrip() + "…"
    return {
        "name": label,
        "address": detail if detail != label else "",
        "category": CATEGORY,
        "color": COLOR,
        "provenance": PROVENANCE,
        "observed_at": f"fusion cycle {max(1, int(cycle))}",
    }
