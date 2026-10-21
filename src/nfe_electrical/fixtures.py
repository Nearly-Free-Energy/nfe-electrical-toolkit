"""Furniture the architect drew, found as closed shapes on the base's furniture layers.

PDF-imported bases have no furniture blocks, only loose lines and arcs, so each room's
furniture layers are drawn onto a 2 cm raster and closed shapes are filled. A bed
(with its bedside tables, which the pilot project's architect draws touching it) shows up as a
2-8 m2 roughly rectangular shape; that is enough to tell which wall the bed head is on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from ezdxf import path as ezpath
from PIL import Image, ImageDraw
from scipy import ndimage

FURNITURE_LAYER = re.compile(r"(?i)furn|\bbed")


@dataclass
class Shape:
    bbox: tuple[float, float, float, float]
    centre: tuple[float, float]
    size_m: tuple[float, float]
    fill: float

    @property
    def area_m2(self) -> float:
        return self.size_m[0] * self.size_m[1] * self.fill


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
    shapes = []
    for k, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None:
            continue
        hpx, wpx = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        fill = float((lab[sl] == k).sum()) / (hpx * wpx)
        bx0, bx1 = x0 + sl[1].start * res, x0 + sl[1].stop * res
        by0, by1 = y1 - sl[0].stop * res, y1 - sl[0].start * res
        shapes.append(Shape((bx0, by0, bx1, by1), ((bx0 + bx1) / 2, (by0 + by1) / 2),
                            (wpx * res_m, hpx * res_m), fill))
    cache[room.room_id] = shapes
    return shapes


def find_bed(floor, room) -> Shape | None:
    """The largest bed-like shape in the room (bed, possibly with bedside tables)."""
    beds = [s for s in furniture_shapes(floor, room)
            if 1.2 <= min(s.size_m) <= 3.4 and 1.8 <= max(s.size_m) <= 3.6 and s.fill >= 0.55
            and 2.0 <= s.area_m2 <= 9.0]
    return max(beds, key=lambda s: s.area_m2, default=None)
