"""Symbol catalogue and room vocabulary, loaded from the YAML files in `data/`.

The catalogue answers two questions:
  * read side: what is the existing block called `SOCKET` (with attribute S=USB)?
  * write side: which source block, attributes and layer does `socket_twin_13a` use?
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

_BOUND_PREFIX = re.compile(r"^.*\$0\$")


def strip_bound_prefix(block_name: str) -> str:
    """'<old job drawing>$0$sh' -> 'sh' (bound-xref prefixes from older jobs)."""
    return _BOUND_PREFIX.sub("", block_name)


@dataclass(frozen=True)
class Classification:
    device_type: str
    category: str
    description: str
    known: bool


@dataclass(frozen=True)
class DeviceType:
    key: str
    category: str
    mount: str
    source_block: str
    layer: str
    attribs: dict[str, str]
    description: str
    height_mm: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.key, "category": self.category, "mount": self.mount,
            "source_block": self.source_block, "layer": self.layer, "attribs": dict(self.attribs),
            "description": self.description, "height_mm": self.height_mm,
        }


@dataclass(frozen=True)
class RoomType:
    key: str
    pattern: re.Pattern
    area_m2: tuple[float, float]
    service: bool


@dataclass
class Catalogue:
    blocks: dict[str, dict]
    device_types: dict[str, DeviceType]
    nfe_layers: dict[str, dict]
    layer_groups: dict[str, list[str]]
    nfe_attributes: list[str]
    room_types: list[RoomType]
    not_room_label: re.Pattern
    label_layers: list[str]
    source: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, catalogue_path: str | Path | None = None, room_types_path: str | Path | None = None) -> "Catalogue":
        cat = _read_yaml(catalogue_path, "catalogue.yaml")
        rooms = _read_yaml(room_types_path, "room_types.yaml")
        device_types = {
            key: DeviceType(
                key=key, category=v["category"], mount=v.get("mount", "wall"), source_block=v["source_block"],
                layer=v["layer"], attribs={str(a): str(b) for a, b in (v.get("attribs") or {}).items()},
                description=v.get("description", key), height_mm=v.get("height_mm"),
            )
            for key, v in cat["device_types"].items()
        }
        for dt in device_types.values():
            if dt.layer not in cat["nfe_layers"]:
                raise ValueError(f"device type {dt.key} writes to {dt.layer!r}, which is not an NFE layer")
        room_types = [
            RoomType(key=k, pattern=re.compile(v["pattern"]), area_m2=tuple(v["area_m2"]), service=bool(v.get("service")))
            for k, v in rooms["room_types"].items()
        ]
        return cls(
            blocks=cat["blocks"], device_types=device_types, nfe_layers=cat["nfe_layers"],
            layer_groups=cat["existing_electrical_layers"], nfe_attributes=list(cat.get("nfe_attributes", [])),
            room_types=room_types, not_room_label=re.compile(rooms["not_room_labels"]),
            label_layers=list(rooms.get("label_layers", ["Text Room"])),
            source={"catalogue": str(catalogue_path or "builtin"), "room_types": str(room_types_path or "builtin")},
        )

    # ------------------------------------------------------------ read side
    def classify(self, block_name: str, attribs: dict[str, str] | None = None, layer: str | None = None) -> Classification:
        """Device type of an existing insert, from its block name (and attributes for variants)."""
        entry = self.blocks.get(block_name) or self.blocks.get(strip_bound_prefix(block_name))
        if entry is not None:
            for var in entry.get("variants", []) or []:
                if attribs and str(attribs.get(var["attrib"], "")).strip().upper() == str(var["value"]).upper():
                    return Classification(var["type"], entry["category"], var.get("description", entry["description"]), True)
            return Classification(entry["type"], entry["category"], entry["description"], True)
        group = self.layer_group(layer) if layer else None
        return Classification("unknown", group or "unknown", f"unknown block {block_name!r}", False)

    def layer_group(self, layer: str | None) -> str | None:
        if not layer:
            return None
        low = layer.lower()
        for group, names in self.layer_groups.items():
            if low in (n.lower() for n in names):
                return group
        return None

    def is_electrical_layer(self, layer: str) -> bool:
        return self.layer_group(layer) is not None

    def all_electrical_layers(self) -> list[str]:
        return [n for names in self.layer_groups.values() for n in names]

    # ----------------------------------------------------------- write side
    def device_type(self, key: str) -> DeviceType:
        try:
            return self.device_types[key]
        except KeyError:
            raise KeyError(f"unknown device type {key!r}; known: {', '.join(sorted(self.device_types))}") from None

    # ---------------------------------------------------------------- rooms
    def room_type(self, label: str) -> RoomType | None:
        for rt in self.room_types:
            if rt.pattern.search(label):
                return rt
        return None


def _read_yaml(path: str | Path | None, builtin_name: str) -> dict:
    if path is not None:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return yaml.safe_load(resources.files("nfe_electrical").joinpath("data", builtin_name).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def default_catalogue() -> Catalogue:
    return Catalogue.load()
