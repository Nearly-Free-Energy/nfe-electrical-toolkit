"""Pick the plotted architectural base from the copies piled up in one model space.

Each base in the pilot drawings mixes native layers with 2-11 generations of AutoCAD PDF-import
layers (`PDF_`, `PDF2_` ... `PDF11_`) and `New...` re-imports. Superseded copies sit
30-80 m away or overlap the plotted one. A copy is a "family" of layers sharing a
prefix; the plotted base is the family with the most visible wall geometry inside
the plan viewport's window.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ezdxf.document import Drawing

from .catalogue import Catalogue

WALL_LAYER = re.compile(r"(?i)wall|bearing|door|window|column|glaz|curtain|railing|lift|stair|partition")
NOT_WALL_LAYER = re.compile(r"(?i)grid|marker|dimension|text|annotation|label")
_PDF = re.compile(r"(?i)^(PDF\d*)_")
_NEW = re.compile(r"(?i)^(\(new\)|new)(?=[A-Z0-9 (_-])")


def layer_family(layer: str) -> str:
    m = _PDF.match(layer)
    if m:
        return m.group(1).upper()
    if "$0$" in layer:
        return "BOUND"
    if _NEW.match(layer):
        return "NEW"
    return "NATIVE"


def first_point(e) -> tuple[float, float] | None:
    t = e.dxftype()
    try:
        if t in ("TEXT", "MTEXT", "INSERT"):
            p = e.dxf.insert
        elif t == "LINE":
            p = e.dxf.start
        elif t == "LWPOLYLINE":
            if len(e) == 0:
                return None
            q = e[0]
            return (q[0], q[1])
        elif t in ("CIRCLE", "ARC", "ELLIPSE"):
            p = e.dxf.center
        elif t in ("SOLID", "TRACE", "3DFACE"):
            p = e.dxf.vtx0
        elif t == "HATCH":
            for path in e.paths:
                verts = getattr(path, "vertices", None)
                if verts:
                    return (verts[0][0], verts[0][1])
                for edge in getattr(path, "edges", []):
                    q = getattr(edge, "start", None) or getattr(edge, "center", None)
                    if q is not None:
                        return (q[0], q[1])
            return None
        elif t == "POINT":
            p = e.dxf.location
        else:
            return None
    except Exception:
        return None
    return (p[0], p[1])


@dataclass
class BaseCopy:
    family: str
    n_entities: int
    n_in_window: int
    n_wall_in_window: int
    n_wall_visible_in_window: int
    centre: tuple[float, float] | None
    offset_from_plotted: tuple[float, float] | None = None
    layers: list[str] = field(default_factory=list)


@dataclass
class BaseReport:
    plotted: str
    wall_layers: list[str]
    copies: list[BaseCopy]
    overlapping_copies: list[str]
    notes: list[str]

    def as_dict(self) -> dict:
        d = asdict(self)
        for c in d["copies"]:
            c["layers"] = sorted(c["layers"])[:80]
        return d


def pick_base(doc: Drawing, catalogue: Catalogue, window: tuple[float, float, float, float],
              visible_layers: set[str]) -> BaseReport:
    x0, y0, x1, y1 = window
    skip = {ly.lower() for ly in catalogue.all_electrical_layers()} | {ly.lower() for ly in catalogue.nfe_layers}
    stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "in": 0, "wall_in": 0, "wall_vis_in": 0,
                                                  "pts": [], "layers": set(), "wall_layers": set()})
    for e in doc.modelspace():
        layer = e.dxf.get("layer", "0")
        if layer.lower() in skip:
            continue
        fam = layer_family(layer)
        st = stats[fam]
        st["n"] += 1
        st["layers"].add(layer)
        p = first_point(e)
        if p is None:
            continue
        is_wall = bool(WALL_LAYER.search(layer)) and not NOT_WALL_LAYER.search(layer)
        if is_wall and len(st["pts"]) < 20000:
            st["pts"].append(p)
        if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
            st["in"] += 1
            if is_wall:
                st["wall_in"] += 1
                if layer in visible_layers:
                    st["wall_vis_in"] += 1
                    st["wall_layers"].add(layer)

    copies = []
    for fam, st in stats.items():
        pts = st["pts"]
        centre = (float(statistics.median(p[0] for p in pts)), float(statistics.median(p[1] for p in pts))) if pts else None
        copies.append(BaseCopy(family=fam, n_entities=st["n"], n_in_window=st["in"], n_wall_in_window=st["wall_in"],
                               n_wall_visible_in_window=st["wall_vis_in"], centre=centre, layers=sorted(st["layers"])))
    copies.sort(key=lambda c: -c.n_wall_visible_in_window)
    plotted = copies[0] if copies and copies[0].n_wall_visible_in_window > 0 else None
    notes = []
    if plotted is None:
        return BaseReport(plotted="none", wall_layers=[], copies=copies, overlapping_copies=[],
                          notes=["no visible wall geometry inside the plan window"])
    for c in copies:
        if c.centre and plotted.centre:
            c.offset_from_plotted = (round(c.centre[0] - plotted.centre[0], 2), round(c.centre[1] - plotted.centre[1], 2))
    overlapping = [c.family for c in copies[1:]
                   if c.n_wall_visible_in_window >= max(20, 0.05 * plotted.n_wall_visible_in_window)]
    for c in copies[1:]:
        if c.n_wall_in_window == 0 and c.n_entities > 100:
            notes.append(f"{c.family} copy ({c.n_entities} entities) lies entirely outside the plotted window")
    for fam in overlapping:
        notes.append(f"{fam} copy overlaps the plotted window and is visible: superseded walls may show on the sheets")
    wall_layers = sorted(stats[plotted.family]["wall_layers"])
    return BaseReport(plotted=plotted.family, wall_layers=wall_layers, copies=copies,
                      overlapping_copies=overlapping, notes=notes)
