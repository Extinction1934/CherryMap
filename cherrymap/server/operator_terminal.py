"""Operator feeds on the TPM-119 software bridge.

Each pin takes a record from its own cell, not the lead record already used
by the other channels, and not a repeated Operator title.
"""

COLOR = "#be185d"
CATEGORY = "operator"
PROVENANCE = "Operator feed · TPM-119 software bridge"


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
    label = seen[-1]
    if len(label) > 36:
        label = label[:35].rstrip() + "…"
    detail = " · ".join(name for name in seen if name != seen[-1])
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
