"""Find mirrored apartment units: the pilot floors repeat one apartment type either side of a core.

The mirror axis is estimated from pairs of same-type rooms with matching areas; rooms
on either side become units ("W"/"E" for a vertical axis, "S"/"N" for a horizontal one)
and rooms straddling the axis (lift, foyer) form the "core". A floor only counts as
mirrored when at least three pairs of rooms other than service spaces (ducts, lifts,
stairs) mirror each other: a single apartment spanning a floor can still have
mirrored façade ducts.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field


@dataclass
class MirrorUnits:
    axis: str                      # "x" (vertical mirror line x = value) or "y"
    value: float
    sides: tuple[str, str]         # unit names on the low and high side
    pairs: list[dict] = field(default_factory=list)
    units: dict[str, list[str]] = field(default_factory=dict)
    score: int = 0

    def mirror(self, x: float, y: float) -> tuple[float, float]:
        return (2 * self.value - x, y) if self.axis == "x" else (x, 2 * self.value - y)

    def partner(self, room_id: str) -> str | None:
        for p in self.pairs:
            if p["a"] == room_id:
                return p["b"]
            if p["b"] == room_id:
                return p["a"]
        return None

    def as_dict(self) -> dict:
        return asdict(self)


def detect_mirrored_units(rooms, to_m: float, tol_m: float = 0.75, area_tol: float = 0.15,
                          min_pairs: int = 3) -> MirrorUnits | None:
    tol = tol_m / to_m
    best, best_key = None, (0, 0)
    for axis in ("x", "y"):
        k, j = (0, 1) if axis == "x" else (1, 0)
        cands = []
        for i, a in enumerate(rooms):
            for b in rooms[i + 1:]:
                if a.room_type != b.room_type:
                    continue
                ca, cb = a.polygon.centroid.coords[0], b.polygon.centroid.coords[0]
                if abs(ca[j] - cb[j]) > tol or abs(ca[k] - cb[k]) < 2 * tol:
                    continue
                if abs(a.area_m2 - b.area_m2) > area_tol * max(a.area_m2, b.area_m2):
                    continue
                cands.append(((ca[k] + cb[k]) / 2, a, b))
        if len(cands) < 2:
            continue
        value = statistics.median(c[0] for c in cands)
        # keep the best-mirrored partner for each room
        scored = []
        for mid, a, b in cands:
            ca, cb = a.polygon.centroid.coords[0], b.polygon.centroid.coords[0]
            mx = (2 * value - ca[0], ca[1]) if axis == "x" else (ca[0], 2 * value - ca[1])
            err = ((mx[0] - cb[0]) ** 2 + (mx[1] - cb[1]) ** 2) ** 0.5
            if err <= tol:
                scored.append((err, a, b))
        scored.sort(key=lambda s: s[0])
        used, pairs = set(), []
        for err, a, b in scored:
            if a.room_id in used or b.room_id in used:
                continue
            used |= {a.room_id, b.room_id}
            lo, hi = (a, b) if a.polygon.centroid.coords[0][k] < b.polygon.centroid.coords[0][k] else (b, a)
            pairs.append({"a": lo.room_id, "b": hi.room_id, "type": a.room_type, "name_a": lo.name, "name_b": hi.name,
                          "service": a.service, "mirror_error_m": round(err * to_m, 2),
                          "area_m2": [round(lo.area_m2, 1), round(hi.area_m2, 1)]})
        key = (sum(1 for p in pairs if not p["service"]), len(pairs))
        if best is None or key > best_key:
            sides = ("W", "E") if axis == "x" else ("S", "N")
            best, best_key = MirrorUnits(axis=axis, value=value, sides=sides, pairs=pairs, score=len(pairs)), key
    if best is None or best_key[0] < min_pairs:
        return None
    k = 0 if best.axis == "x" else 1
    units = {best.sides[0]: [], best.sides[1]: [], "core": []}
    for r in rooms:
        minx, miny, maxx, maxy = r.polygon.bounds
        lo, hi = (minx, maxx) if k == 0 else (miny, maxy)
        if lo < best.value - tol / 2 < best.value + tol / 2 < hi or abs((lo + hi) / 2 - best.value) < tol:
            side = "core"
        else:
            side = best.sides[0] if (lo + hi) / 2 < best.value else best.sides[1]
        r.unit = side
        units[side].append(r.room_id)
    best.units = units
    return best
