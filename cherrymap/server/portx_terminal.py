"""PortX cells on the TPM-119 software bridge.

The pin shows the records fused into that cell. It is not a coordinate,
not a fixed PortX phrase, and not filtered by category.
"""

COLOR = "#e36a12"
CATEGORY = "portx"
ORBITAL_ID = "9478e72a99d4559998eb69046357fbe0"
PROVENANCE = f"PortX terminal · orbital {ORBITAL_ID} · software bridge"


def _feeds(feeds: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in feeds:
        name = " ".join(str(raw or "").split())
        if name.startswith("NeuralLink · "):
            name = name.split(" · ", 1)[1].strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= 12:
            break
    return seen


def pin_fields(cycle: int, feeds: list[str]) -> dict[str, str]:
    seen = _feeds(feeds)
    if not seen:
        return {"name": "", "address": "", "category": CATEGORY, "color": COLOR, "provenance": PROVENANCE, "observed_at": ""}
    label = seen[0]
    if len(label) > 36:
        label = label[:35].rstrip() + "…"
    detail = " · ".join(seen[1:])
    if len(detail) > 280:
        detail = detail[:279].rstrip() + "…"
    return {
        "name": label,
        "address": detail,
        "category": CATEGORY,
        "color": COLOR,
        "provenance": PROVENANCE,
        "observed_at": f"fusion cycle {max(1, int(cycle))}",
    }
