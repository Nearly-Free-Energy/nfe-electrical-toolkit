"""Index electrical symbols by block name and *drawn* graphic centre.

About 20-23 of the ~50 electrical blocks in each pilot file draw their graphics far
from their base point (a `spot` inserted at (65.8, 214.7) m draws at (19.9, 119.9) m),
so a symbol's position is always taken from its block geometry pushed through the
insert's transform, never from the insertion point.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.math import Vec3

from .catalogue import Catalogue

FAR_BASEPOINT_RATIO = 5.0  # offset more than 5x the symbol size counts as "far"


@dataclass
class BlockGeometry:
    name: str
    centre: tuple[float, float]  # block units, relative to the block's own origin
    size: tuple[float, float]
    base_point: tuple[float, float]

    @property
    def offset(self) -> float:
        return math.hypot(self.centre[0] - self.base_point[0], self.centre[1] - self.base_point[1])

    @property
    def offset_ratio(self) -> float:
        return self.offset / max(self.size[0], self.size[1], 1e-12)

    @property
    def far_basepoint(self) -> bool:
        return self.offset_ratio > FAR_BASEPOINT_RATIO


@dataclass
class Symbol:
    handle: str
    block: str
    device_type: str
    category: str
    description: str
    layer: str
    insert: tuple[float, float]
    centre: tuple[float, float]
    bbox: tuple[float, float, float, float]
    rotation: float
    xscale: float
    yscale: float
    attribs: dict[str, str]
    known: bool
    offset: float  # insertion point -> drawn centre, drawing units
    on_plan: bool = False
    room_id: str | None = None

    @property
    def mirrored(self) -> bool:
        return (self.xscale < 0) != (self.yscale < 0)

    def as_dict(self, to_m: float = 1.0) -> dict:
        d = asdict(self)
        d["mirrored"] = self.mirrored
        d["offset_m"] = round(self.offset * to_m, 3)
        for k in ("insert", "centre"):
            d[k] = [round(v, 4) for v in d[k]]
        d["bbox"] = [round(v, 4) for v in d["bbox"]]
        return d


class BlockGeometryCache:
    """Geometric extents of block definitions (ATTDEFs ignored), computed once per block."""

    def __init__(self, doc: Drawing):
        self.doc = doc
        self._cache: dict[str, BlockGeometry | None] = {}

    def get(self, name: str) -> BlockGeometry | None:
        if name not in self._cache:
            self._cache[name] = self._compute(name)
        return self._cache[name]

    def _compute(self, name: str) -> BlockGeometry | None:
        blk = self.doc.blocks.get(name)
        if blk is None:
            return None
        ents = [e for e in blk if e.dxftype() != "ATTDEF"]
        ext = bbox.extents(ents, fast=True)
        if not ext.has_data:
            return None
        bp = blk.block.dxf.base_point
        return BlockGeometry(name=name, centre=(ext.center.x, ext.center.y), size=(ext.size.x, ext.size.y),
                             base_point=(bp.x, bp.y))

    def items(self):
        return [(k, v) for k, v in self._cache.items() if v is not None]


def drawn_frame(insert, geom: BlockGeometry) -> tuple[tuple[float, float], tuple[float, float, float, float]]:
    """World centre and bounding box of an insert's drawn geometry."""
    m = insert.matrix44()
    c = m.transform(Vec3(geom.centre[0], geom.centre[1], 0))
    hw, hh = geom.size[0] / 2, geom.size[1] / 2
    corners = [m.transform(Vec3(geom.centre[0] + sx * hw, geom.centre[1] + sy * hh, 0))
               for sx in (-1, 1) for sy in (-1, 1)]
    xs = [p.x for p in corners]
    ys = [p.y for p in corners]
    return (c.x, c.y), (min(xs), min(ys), max(xs), max(ys))


def index_symbols(doc: Drawing, catalogue: Catalogue, geometry: BlockGeometryCache) -> list[Symbol]:
    """Every model-space insert that is a known electrical block or sits on an electrical layer."""
    out: list[Symbol] = []
    for e in doc.modelspace().query("INSERT"):
        name, layer = e.dxf.name, e.dxf.get("layer", "0")
        attribs = {a.dxf.tag: a.dxf.text for a in e.attribs}
        cls = catalogue.classify(name, attribs, layer)
        if not cls.known and not catalogue.is_electrical_layer(layer):
            continue
        geom = geometry.get(name)
        if geom is None:
            continue
        centre, box = drawn_frame(e, geom)
        ins = e.dxf.insert
        out.append(Symbol(
            handle=e.dxf.handle, block=name, device_type=cls.device_type, category=cls.category,
            description=cls.description, layer=layer, insert=(ins.x, ins.y), centre=centre, bbox=box,
            rotation=float(e.dxf.get("rotation", 0.0)), xscale=float(e.dxf.get("xscale", 1.0)),
            yscale=float(e.dxf.get("yscale", 1.0)), attribs=attribs, known=cls.known,
            offset=math.dist((ins.x, ins.y), centre),
        ))
    return out


@dataclass
class BlockAudit:
    blocks: list[dict] = field(default_factory=list)
    far_blocks: int = 0
    far_inserts: int = 0
    total_blocks: int = 0
    total_inserts: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def block_offset_audit(symbols: list[Symbol], geometry: BlockGeometryCache, to_m: float) -> BlockAudit:
    by_block: dict[str, list[Symbol]] = {}
    for s in symbols:
        by_block.setdefault(s.block, []).append(s)
    audit = BlockAudit()
    for name, syms in sorted(by_block.items(), key=lambda kv: -len(kv[1])):
        g = geometry.get(name)
        if g is None:
            continue
        scales = sorted(abs(s.xscale) for s in syms)
        typical = scales[len(scales) // 2]
        row = {
            "block": name, "device_type": syms[0].device_type, "inserts": len(syms),
            "on_plan": sum(s.on_plan for s in syms),
            "size_block_units": [round(g.size[0], 1), round(g.size[1], 1)],
            "centre_block_units": [round(g.centre[0], 1), round(g.centre[1], 1)],
            "offset_ratio": round(g.offset_ratio, 1), "far_basepoint": g.far_basepoint,
            "typical_insert_scale": typical,
            "typical_offset_m": round(sorted(s.offset for s in syms)[len(syms) // 2] * to_m, 2),
            "mirrored_inserts": sum(s.mirrored for s in syms),
            "layers": sorted({s.layer for s in syms}),
        }
        audit.blocks.append(row)
        audit.total_blocks += 1
        audit.total_inserts += len(syms)
        if g.far_basepoint:
            audit.far_blocks += 1
            audit.far_inserts += len(syms)
    return audit
