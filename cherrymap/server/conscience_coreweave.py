"""TPM-119 AI conscience pins on the fusion diaphragm.

Each pin is a fused cell of its own. It is not another channel's title
and it is not a repeated Conscience label.
"""

COLOR = "#ca8a04"
CATEGORY = "conscience"
PROVENANCE = "TPM-119 AI · Conscience Interconnect CoreWeave · software bridge"


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
    if len(seen) < 2:
        return {"name": "", "address": "", "category": CATEGORY, "color": COLOR, "provenance": PROVENANCE, "observed_at": ""}
    label = seen[1] if len(seen) >= 4 else f"{seen[0]} · {seen[2]}" if len(seen) == 3 else f"{seen[1]} / {seen[0]}"
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
