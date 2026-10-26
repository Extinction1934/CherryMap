"""Anaconda proximity feeds on the software-bridge lattice.

The title is the records fused into that lattice cell. It is not the word
Anaconda, not a coordinate, and not a NeuralLink label. The map title stays
short so a far view does not draw a line of names.
"""

COLOR = "#1a4fbf"
CATEGORY = "anaconda"
PROVENANCE = "Anaconda AI · software bridge lattice"


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


def pin_fields(cycle: int, feeds: list[str]) -> dict[str, str]:
    seen = _feeds(feeds)
    if not seen:
        return {"name": "", "address": "", "category": CATEGORY, "color": COLOR, "provenance": PROVENANCE, "observed_at": ""}
    label = seen[0] if len(seen) == 1 or len(seen[0]) > 18 else f"{seen[0]} · {seen[1]}"
    if len(label) > 32:
        label = label[:31].rstrip() + "…"
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
