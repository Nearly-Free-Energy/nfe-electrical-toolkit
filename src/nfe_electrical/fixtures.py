"""Furniture the architect drew, found as closed shapes on the base's furniture layers.

PDF-imported bases have no furniture blocks, only loose lines and arcs, so each room's
furniture layers are drawn onto a 2 cm raster and closed shapes are filled. A bed
(with its bedside tables, which the pilot project's architect draws touching it) shows up as a
2-8 m2 roughly rectangular shape. The bedside tables make the outline widest at the head, so
the shape shows which side the head is on even when no wall stands behind it (a headboard,
or only a dashed beam line).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from ezdxf import path as ezpath
from PIL import Image, ImageDraw
from scipy import ndimage

FURNITURE_LAYER = re.compile(r"(?i)furn|\bbed")
SIDE_NORMALS = {"north": (0.0, 1.0), "south": (0.0, -1.0), "east": (1.0, 0.0), "west": (-1.0, 0.0)}
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}
EDGE_BAND_M = 0.3


@dataclass
class Shape:
    bbox: tuple[float, float, float, float]
    centre: tuple[float, float]
    size_m: tuple[float, float]
    fill: float
    edge_fill: dict[str, float] = field(default_factory=dict)  # share of a band along each side that is filled
    edge_span: dict[str, tuple[float, float]] = field(default_factory=dict)  # filled extent in that band, along it

    @property
    def area_m2(self) -> float:
        return self.size_m[0] * self.size_m[1] * self.fill

    def head_side(self) -> str | None:
        """For a bed: the side whose band the outline fills most (bed head plus bedside tables), if it stands out."""
        if len(self.edge_fill) < 4:
            return None
        first, second = sorted(self.edge_fill.values(), reverse=True)[:2]
        return max(self.edge_fill, key=self.edge_fill.get) if first - second >= 0.15 else None

    def axis_point(self, head: str) -> tuple[float, float]:
        """A point on a bed's centre line: the middle of the outline at the foot, where only the mattress is."""
        if OPPOSITE[head] not in self.edge_span:
            return self.centre
        lo, hi = self.edge_span[OPPOSITE[head]]
        return ((lo + hi) / 2, self.centre[1]) if head in ("north", "south") else (self.centre[0], (lo + hi) / 2)


def furniture_shapes(floor, room, res_m: float = 0.02) -> list[Shape]:
    cache = floor.__dict__.setdefault("_furniture_shapes", {})
    if room.room_id in cache:
        return cache[room.room_id]
    to_m = floor.to_m
    res = res_m / to_m
    x0, y0, x1, y1 = room.polygon.bounds
    nx, ny = int((x1 - x0) / res) + 1, int((y1 - y0) / res) + 1
    img = Image.new("L", (nx, ny), 0)
    draw = ImageDraw.Draw(img)
    fam = floor.base.plotted.upper() + "_"
    for e in floor.msp:
        layer = e.dxf.get("layer", "")
        if not (layer.upper().startswith(fam) or floor.base.plotted == "NATIVE") or not FURNITURE_LAYER.search(layer):
            continue
        if e.dxftype() not in ("LWPOLYLINE", "LINE", "ARC", "CIRCLE", "POLYLINE", "ELLIPSE", "SPLINE"):
            continue
        try:
            pts = [((v.x - x0) / res, (y1 - v.y) / res) for v in ezpath.make_path(e).flattening(res / 2)]
        except Exception:
            continue
        if len(pts) >= 2 and any(0 <= p[0] < nx and 0 <= p[1] < ny for p in pts):
            draw.line(pts, fill=255, width=1)
    lines = ndimage.binary_dilation(np.asarray(img) > 0, iterations=1)
    lab, _ = ndimage.label(ndimage.binary_fill_holes(lines))
    band = max(1, int(round(EDGE_BAND_M / res_m)))
    shapes = []
    for k, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        hpx, wpx = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        mask = lab[sl] == k
        fill = float(mask.sum()) / (hpx * wpx)
        bx0, bx1 = x0 + sl[1].start * res, x0 + sl[1].stop * res
        by0, by1 = y1 - sl[0].stop * res, y1 - sl[0].start * res
        edge_fill, edge_span = {}, {}
        # rows run from north to south; the west and east bands are transposed so their rows run along the side
        for side, b in (("north", mask[:band]), ("south", mask[-band:]), ("west", mask[:, :band].T),
                        ("east", mask[:, -band:].T)):
            edge_fill[side] = float(b.mean())
            idx = np.nonzero(b.any(axis=0))[0]
            if idx.size:
                edge_span[side] = ((bx0 + idx.min() * res, bx0 + (idx.max() + 1) * res) if side in ("north", "south")
                                   else (by1 - (idx.max() + 1) * res, by1 - idx.min() * res))
        shapes.append(Shape((bx0, by0, bx1, by1), ((bx0 + bx1) / 2, (by0 + by1) / 2),
                            (wpx * res_m, hpx * res_m), fill, edge_fill, edge_span))
    cache[room.room_id] = shapes
    return shapes


def find_bed(floor, room) -> Shape | None:
    """The largest bed-like shape in the room (bed, possibly with bedside tables)."""
    beds = [s for s in furniture_shapes(floor, room)
            if 1.2 <= min(s.size_m) <= 3.4 and 1.8 <= max(s.size_m) <= 3.6 and s.fill >= 0.55
            and 2.0 <= s.area_m2 <= 9.0]
    return max(beds, key=lambda s: s.area_m2, default=None)
