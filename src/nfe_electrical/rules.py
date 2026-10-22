"""Apply a small YAML rules file (a draft of the NFE playbook) to the rooms of a floor.

    name: BS 7671 - apartment type A - small power
    standard: BS 7671                    # the standard the rule set is written for
    applies_to: {units: [W, E]}          # mirrored units found on the floor, or {rooms: [R07, R24]}
    defaults: {door_clearance: 0.3, corner_clearance: 0.3, min_spacing: 0.8, avoid_facing: [balcony]}
    rooms:
      bedroom:
        - {type: socket_twin_usb, count: 2, place: pair, wall: longest, spacing: 2.4, why: bedside pair}

`count` is a number or {per_m2: 8, min: 2, max: 6}. `place` is a placement strategy
(perimeter, wall, pair, corners, beside_door, centre); the other keys tune it.

Each job records its governing standard (BS 7671 by default). A rule set may only be
applied to a job governed by the standard it was written for.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .placement import STRATEGIES, place_device

DEFAULT_STANDARD = "BS 7671"
KNOWN_STANDARDS = {"BS 7671": "BS 7671 Requirements for Electrical Installations (IET Wiring Regulations)",
                   "IEC 60364": "IEC 60364 Low-voltage electrical installations",
                   "US EAS 811-1:2014": "US EAS 811-1:2014, the East African code gazetted in Uganda"}


def normalise_standard(value: str | None) -> str:
    """'bs7671' or 'BS-7671' -> 'BS 7671'; unknown names are kept as given (stripped)."""
    raw = (value or DEFAULT_STANDARD).strip()
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    for name in KNOWN_STANDARDS:
        if re.sub(r"[^a-z0-9]", "", name.lower()) == key or (key.startswith("bs7671") and name == "BS 7671"):
            return name
    return raw


_RULE_KEYS = {"type", "count", "place", "wall", "spacing", "door_clearance", "corner_clearance", "min_spacing",
              "avoid_facing", "why"}


@dataclass
class RuleSet:
    name: str
    standard: str
    version: str
    applies_to: dict
    defaults: dict
    rooms: dict[str, list[dict]]
    path: str = ""
    notes: list[str] = field(default_factory=list)


def load_rules(path: str | Path, catalogue) -> RuleSet:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not data.get("standard"):
        raise ValueError(f"{Path(path).name} does not say which standard it is written for (add `standard:`)")
    rs = RuleSet(name=str(data.get("name", Path(path).stem)), standard=normalise_standard(data["standard"]),
                 version=str(data.get("version", "")),
                 applies_to=data.get("applies_to") or {}, defaults=data.get("defaults") or {},
                 rooms=data.get("rooms") or {}, path=str(path), notes=list(data.get("notes") or []))
    problems = []
    for room_type, rules in rs.rooms.items():
        for r in rules or []:
            unknown = set(r) - _RULE_KEYS
            if unknown:
                problems.append(f"{room_type}: unknown keys {sorted(unknown)}")
            if r.get("type") not in catalogue.device_types:
                problems.append(f"{room_type}: unknown device type {r.get('type')!r}")
            if r.get("place", "perimeter") not in STRATEGIES:
                problems.append(f"{room_type}: unknown placement {r.get('place')!r}")
    if problems:
        raise ValueError("rules file problems: " + "; ".join(problems))
    return rs


def resolve_count(count, area_m2: float) -> int:
    if isinstance(count, dict):
        n = math.ceil(area_m2 / float(count["per_m2"])) if count.get("per_m2") else int(count.get("min", 1))
        return max(int(count.get("min", 0)), min(int(count.get("max", 99)), n))
    return int(count)


def target_rooms(floor, rules: RuleSet, units: list[str] | None = None, rooms: list[str] | None = None):
    want_rooms = rooms or rules.applies_to.get("rooms")
    if want_rooms:
        return [floor.rooms.get(r) for r in want_rooms]
    want_units = units or rules.applies_to.get("units")
    out = [r for r in floor.rooms.rooms if r.status != "failed" and not r.service]
    if want_units:
        out = [r for r in out if r.unit in want_units]
    return out


def scope_warnings(floor, rules: RuleSet, units: list[str] | None = None, rooms: list[str] | None = None) -> list[str]:
    """What a units scope leaves out: rooms the rule set has rules for that sit in no wanted unit
    (e.g. the core between two mirrored apartments), or every room on a floor with no mirrored units."""
    if rooms or rules.applies_to.get("rooms"):
        return []
    want_units = units or rules.applies_to.get("units")
    if not want_units:
        return []
    ruled = [r for r in floor.rooms.rooms if r.status != "failed" and not r.service and rules.rooms.get(r.room_type)]
    left_out = [r for r in ruled if r.unit not in want_units]
    if not left_out:
        return []
    scope = ", ".join(want_units)
    if floor.mirror is None:
        return [f"the rules apply to units {scope}, but no mirrored units were found on this floor: "
                f"{len(left_out)} room(s) with rules got nothing"]
    return [f"{len(left_out)} room(s) with rules sit outside units {scope} and got nothing: "
            + ", ".join(f"{r.room_id} {r.name} [{r.unit}]" for r in left_out)]


def apply_rules(floor, overlay, rules: RuleSet, units: list[str] | None = None, rooms: list[str] | None = None,
                dry_run: bool = False) -> dict:
    """Apply a rule set to the floor's rooms. Every rule runs on an in-memory copy of the overlay, so a
    dry run reports exactly what a write would place (each rule sees what the earlier ones proposed), and
    a write is all or nothing: the overlay changes, and is saved once, only if every rule succeeded."""
    job_standard = normalise_standard(getattr(floor, "standard", None))
    if rules.standard != job_standard:
        raise ValueError(f"rule set {rules.name!r} is written for {rules.standard}, but job {floor.floor_id} is "
                         f"governed by {job_standard}")
    t0 = time.perf_counter()
    report = {"rules": rules.name, "standard": rules.standard, "version": rules.version, "dry_run": dry_run,
              "rooms": [], "warnings": scope_warnings(floor, rules, units, rooms)}
    work = overlay.copy()
    for room in target_rooms(floor, rules, units, rooms):
        room_rules = rules.rooms.get(room.room_type)
        if room_rules is None:
            continue
        entry = {"room": room.room_id, "name": room.name, "unit": room.unit, "type": room.room_type,
                 "area_m2": round(room.area_m2, 1), "placed": []}
        for rule in room_rules or []:
            spec = {**rules.defaults, **{k: v for k, v in rule.items() if k not in ("type", "place", "count", "why")}}
            spec["strategy"] = rule.get("place", "perimeter")
            spec["count"] = resolve_count(rule.get("count", 1), room.area_m2)
            if spec["count"] <= 0:
                continue
            res = place_device(floor, work, room.room_id, rule["type"], spec)
            items = [{k: v for k, v in i.items() if k != "handle"} for i in res["placed"]] if dry_run else res["placed"]
            entry["placed"].append({"type": rule["type"], "why": rule.get("why", ""), "rule": res["rule"],
                                    "requested": res["requested"], "placed": len(items), "items": items})
            if res.get("warning"):
                report["warnings"].append(res["warning"])
        report["rooms"].append(entry)
    report["devices_placed"] = sum(p["placed"] for r in report["rooms"] for p in r["placed"])
    if not dry_run and report["devices_placed"]:
        overlay.adopt(work)
    report["seconds"] = round(time.perf_counter() - t0, 2)
    return report


def preview_rules(floor, overlay, rules: RuleSet, units: list[str] | None = None,
                  rooms: list[str] | None = None) -> tuple:
    """Apply a rule set to an in-memory copy of `overlay` that is never saved, so it can be rendered and
    shown before anything is written. Returns the copy and the apply_rules report."""
    preview = overlay.copy()
    return preview, apply_rules(floor, preview, rules, units=units, rooms=rooms)
