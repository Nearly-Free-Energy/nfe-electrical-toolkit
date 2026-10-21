"""Compare generated devices with the engineer's existing layout, room by room.

Counts are compared per device type. Positions are paired one-to-one within each room
by minimum total distance (Hungarian assignment); a pair counts as a match if the two
symbols are within `match_m` of each other.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter

import numpy as np
from scipy.optimize import linear_sum_assignment

SOCKET_TYPES = {"socket_twin_13a", "socket_twin_usb", "socket_single_13a", "socket_twin_high_level",
                "socket_twin_weatherproof"}


def _pairs(a: list[tuple], b: list[tuple]):
    if not a or not b:
        return []
    cost = np.array([[math.dist(p, q) for q in b] for p in a])
    ri, ci = linear_sum_assignment(cost)
    return [(int(i), int(j), float(cost[i, j])) for i, j in zip(ri, ci)]


def compare_devices(floor, overlay, room_ids: list[str] | None = None, types: set[str] | None = None,
                    match_m: float = 1.0) -> dict:
    types = types or SOCKET_TYPES
    to_m = floor.to_m
    rooms = [floor.rooms.get(r) for r in room_ids] if room_ids else [r for r in floor.rooms.rooms if not r.service]
    generated = [d for d in overlay.devices() if d.get("type") in types]
    per_room, all_d, matched_total = [], [], 0
    tot_existing = tot_generated = 0
    for room in rooms:
        ex = [s for s in floor.symbols if s.on_plan and s.room_id == room.room_id and s.device_type in types]
        gen = [d for d in generated if d.get("room") == room.room_id]
        if not ex and not gen:
            continue
        pairs = _pairs([s.centre for s in ex], [tuple(d["centre"]) for d in gen])
        dists = [d * to_m for _, _, d in pairs]
        matched = [(i, j, d) for (i, j, d), dm in zip(pairs, dists) if dm <= match_m]
        same_type = sum(1 for i, j, _ in matched if ex[i].device_type == gen[j]["type"])
        rec = {
            "room": room.room_id, "name": room.name, "unit": room.unit, "room_type": room.room_type,
            "existing": dict(Counter(s.device_type for s in ex)), "generated": dict(Counter(d["type"] for d in gen)),
            "existing_total": len(ex), "generated_total": len(gen),
            "matched_within_m": {f"{t}": sum(1 for dm in dists if dm <= t) for t in (0.5, 1.0, 2.0)},
            "matched": len(matched), "matched_same_type": same_type,
            "pair_distances_m": [round(dm, 2) for dm in sorted(dists)],
            "pairs": [{"existing": ex[i].handle, "existing_type": ex[i].device_type,
                       "existing_at": [round(v, 3) for v in ex[i].centre], "generated": gen[j]["handle"],
                       "generated_type": gen[j]["type"], "generated_at": gen[j]["centre"],
                       "distance_m": round(d * to_m, 2)} for i, j, d in pairs],
            "unmatched_existing": [{"handle": ex[i].handle, "type": ex[i].device_type,
                                    "at": [round(v, 3) for v in ex[i].centre]}
                                   for i in range(len(ex)) if i not in {p[0] for p in matched}],
            "unmatched_generated": [{"handle": gen[j]["handle"], "type": gen[j]["type"], "at": gen[j]["centre"]}
                                    for j in range(len(gen)) if j not in {p[1] for p in matched}],
        }
        per_room.append(rec)
        all_d += dists
        matched_total += len(matched)
        tot_existing += len(ex)
        tot_generated += len(gen)
    by_unit: dict[str, dict] = {}
    for rec in per_room:
        u = by_unit.setdefault(rec["unit"] or "-", {"existing": 0, "generated": 0, "matched": 0, "rooms": 0,
                                                    "rooms_count_equal": 0})
        u["existing"] += rec["existing_total"]
        u["generated"] += rec["generated_total"]
        u["matched"] += rec["matched"]
        u["rooms"] += 1
        u["rooms_count_equal"] += int(rec["existing_total"] == rec["generated_total"])
    return {
        "types": sorted(types), "match_radius_m": match_m, "rooms": per_room, "by_unit": by_unit,
        "totals": {"existing": tot_existing, "generated": tot_generated, "matched": matched_total,
                   "rooms_compared": len(per_room),
                   "rooms_with_equal_counts": sum(1 for r in per_room if r["existing_total"] == r["generated_total"]),
                   "median_pair_distance_m": round(statistics.median(all_d), 2) if all_d else None,
                   "pairs_within_0.5m": sum(1 for d in all_d if d <= 0.5),
                   "pairs_within_1.0m": sum(1 for d in all_d if d <= 1.0),
                   "pairs_within_2.0m": sum(1 for d in all_d if d <= 2.0)},
        "not_in_any_room": [{"handle": s.handle, "type": s.device_type, "at": [round(v, 3) for v in s.centre]}
                            for s in floor.symbols if s.on_plan and s.room_id is None and s.device_type in types],
    }


def mirror_consistency(floor, types: set[str] | None = None, tol_m: float = 0.3) -> dict | None:
    """How exactly the engineer's own layout repeats across the mirrored units."""
    m = floor.mirror
    if m is None:
        return None
    types = types or SOCKET_TYPES
    rows = []
    hit = total = 0
    for p in m.pairs:
        a = [s for s in floor.symbols if s.on_plan and s.room_id == p["a"] and s.device_type in types]
        b = [s for s in floor.symbols if s.on_plan and s.room_id == p["b"] and s.device_type in types]
        pairs = _pairs([m.mirror(*s.centre) for s in a], [s.centre for s in b])
        ok = sum(1 for _, _, d in pairs if d * floor.to_m <= tol_m)
        rows.append({"rooms": [p["a"], p["b"]], "name": p["name_a"], "counts": [len(a), len(b)], "mirrored_within_tol": ok})
        hit += ok
        total += max(len(a), len(b))
    return {"axis": m.axis, "axis_value": round(m.value, 3), "tolerance_m": tol_m, "rooms": rows,
            "symbols_mirrored": hit, "symbols_total": total}


def comparison_markdown(cmp: dict, mirror: dict | None = None, title: str = "Generated vs existing") -> str:
    t = cmp["totals"]
    lines = [f"## {title}", "",
             f"Device types compared: {', '.join(cmp['types'])}. A generated symbol 'matches' an existing one "
             f"if they pair up within {cmp['match_radius_m']} m.", "",
             "| Room | Unit | Existing | Generated | Matched | Pair distances (m) |", "|---|---|---|---|---|---|"]
    for r in cmp["rooms"]:
        lines.append(f"| {r['room']} {r['name']} | {r['unit']} | {r['existing_total']} | {r['generated_total']} | "
                     f"{r['matched']} | {', '.join(str(d) for d in r['pair_distances_m']) or '-'} |")
    lines += ["", f"**Totals:** {t['existing']} existing, {t['generated']} generated, {t['matched']} matched within "
              f"{cmp['match_radius_m']} m; rooms with equal counts {t['rooms_with_equal_counts']} of "
              f"{t['rooms_compared']}; median pair distance {t['median_pair_distance_m']} m; pairs within "
              f"0.5/1/2 m: {t['pairs_within_0.5m']}/{t['pairs_within_1.0m']}/{t['pairs_within_2.0m']}.", ""]
    if cmp["not_in_any_room"]:
        lines.append(f"{len(cmp['not_in_any_room'])} existing symbols of these types sit in no labelled room "
                     "(unlabelled spaces such as bathrooms and corridors) and are not compared.")
        lines.append("")
    if mirror:
        lines.append(f"Engineer's own mirror consistency: {mirror['symbols_mirrored']} of {mirror['symbols_total']} "
                     f"symbols in mirrored room pairs repeat within {mirror['tolerance_m']} m of the mirror image.")
        lines.append("")
    return "\n".join(lines)
