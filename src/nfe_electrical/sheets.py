"""Rebuild sheet views (paper-space layouts) from viewport geometry.

LibreDWG brings the viewports of most of the pilot sheets through with `status` and `id`
zeroed, which tells ezdxf they are off. Nothing here trusts those fields: each
viewport's model-space window comes from its paper centre and size, view centre
and view height, and "viewport off" is read from bit 131072 of the flags.

A sheet's main (plan) viewport is the largest live one that shows the drawing. Sheets can
carry a stale copy of the plan viewport, left behind when the plan was moved or redrawn in
other units, that is as large as the real one but looks at empty model space.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from ezdxf.document import Drawing

from .basepick import first_point

VIEWPORT_OFF_FLAG = 131072


@dataclass
class ViewportView:
    handle: str
    layout: str
    paper_centre: tuple[float, float]
    paper_size: tuple[float, float]
    view_centre: tuple[float, float]
    view_height: float
    scale: float  # model units per paper unit
    twist_deg: float
    window: tuple[float, float, float, float]  # model-space x0, y0, x1, y1
    frozen_layers: list[str]
    is_off: bool
    status_zeroed: bool
    role: str = "secondary"  # main | legend | secondary
    shown: int = -1  # model-space entities that start inside the window (-1: not counted)

    @property
    def paper_area(self) -> float:
        return self.paper_size[0] * self.paper_size[1]

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        x0, y0, x1, y1 = self.window
        return x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin

    def to_paper(self, x: float, y: float) -> tuple[float, float]:
        """Model -> paper coordinates (only for untwisted views, which is all the pilot drawings use)."""
        if abs(self.twist_deg) > 1e-9:
            raise ValueError("twisted viewports are not supported yet")
        return (self.paper_centre[0] + (x - self.view_centre[0]) / self.scale,
                self.paper_centre[1] + (y - self.view_centre[1]) / self.scale)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["window"] = [round(v, 3) for v in self.window]
        d["scale"] = round(self.scale, 6)
        return d


@dataclass
class SheetView:
    layout: str
    tab_order: int
    paper_size_mm: tuple[float, float]
    paper_unit_mm: float
    n_entities: int
    viewports: list[ViewportView] = field(default_factory=list)
    discipline: str = "unknown"
    visible_categories: dict[str, int] = field(default_factory=dict)
    limits: tuple[float, float, float, float] | None = None  # the layout's LIMMIN/LIMMAX, paper units
    plot_type: int | None = None  # the layout's plot area: 5 = the layout itself

    @property
    def main(self) -> ViewportView | None:
        for vp in self.viewports:
            if vp.role == "main":
                return vp
        return None

    @property
    def paper_limits(self) -> tuple[float, float, float, float] | None:
        """The layout's limits when they are the paper itself (AutoCAD's default), else None."""
        if not self.limits or self.paper_unit_mm <= 0:
            return None
        w, h = self.paper_size_mm[0] / self.paper_unit_mm, self.paper_size_mm[1] / self.paper_unit_mm
        x0, y0, x1, y1 = self.limits
        if w > 0 and h > 0 and abs(x1 - x0 - w) <= 0.02 * w and abs(y1 - y0 - h) <= 0.02 * h:
            return self.limits
        return None

    def scale_ratio(self, to_m: float) -> float | None:
        """1:N plot scale of the main viewport, once the model units are known."""
        if not self.main:
            return None
        return self.main.scale * to_m * 1000.0 / self.paper_unit_mm

    def as_dict(self) -> dict:
        return {
            "layout": self.layout, "tab_order": self.tab_order, "paper_size_mm": list(self.paper_size_mm),
            "paper_unit_mm": round(self.paper_unit_mm, 4), "n_entities": self.n_entities,
            "discipline": self.discipline, "visible_categories": self.visible_categories,
            "limits": [round(v, 3) for v in self.limits] if self.limits else None, "plot_type": self.plot_type,
            "viewports": [vp.as_dict() for vp in self.viewports],
        }


def _paper_unit_mm(layout) -> float:
    """Size of one paper-space drawing unit in mm, from the layout's plot settings."""
    d = layout.dxf_layout.dxf
    num, den = d.get("scale_numerator", 1.0) or 1.0, d.get("scale_denominator", 1.0) or 1.0
    per_unit = num / den  # paper units per drawing unit
    if d.get("plot_paper_units", 1) == 0:  # inches
        per_unit *= 25.4
    # The pilot sheets are drawn in inches with a 1 : 0.03937 plot scale, i.e. 25.4 mm per unit.
    return per_unit if 0.01 < per_unit < 1000 else 25.4


def _is_overall(vp) -> bool:
    d = vp.dxf
    return (abs(d.view_height - d.height) <= 1e-6 * max(1.0, abs(d.height))
            and math.dist((d.view_center_point.x, d.view_center_point.y), (d.center.x, d.center.y)) < 1e-3)


def _model_points(doc: Drawing) -> np.ndarray:
    """First point of every model-space entity, for counting what each viewport shows."""
    pts = [p for p in (first_point(e) for e in doc.modelspace()) if p is not None]
    return np.array(pts, dtype=float).reshape(-1, 2)


def rebuild_sheet_views(doc: Drawing) -> list[SheetView]:
    pts = _model_points(doc)
    sheets: list[SheetView] = []
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        d = layout.dxf_layout.dxf
        lo, hi = d.get("limmin"), d.get("limmax")
        limits = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])) if lo is not None and hi is not None else None
        sheet = SheetView(
            layout=layout.name, tab_order=int(d.get("taborder", 0)),
            paper_size_mm=(float(d.get("paper_width", 0.0)), float(d.get("paper_height", 0.0))),
            paper_unit_mm=_paper_unit_mm(layout), n_entities=len(layout), limits=limits,
            plot_type=d.get("plot_type"),
        )
        for vp in layout.viewports():
            v = vp.dxf
            if _is_overall(vp) or v.width <= 0 or v.height <= 0 or v.view_height <= 0:
                continue
            scale = v.view_height / v.height
            twist = float(v.get("view_twist_angle", 0.0) or 0.0)
            half_w, half_h = v.width * scale / 2, v.view_height / 2
            cx, cy = v.view_center_point.x, v.view_center_point.y
            if abs(twist) > 1e-9:  # bounding box of the rotated window
                a = math.radians(twist)
                half_w, half_h = (abs(half_w * math.cos(a)) + abs(half_h * math.sin(a)),
                                  abs(half_w * math.sin(a)) + abs(half_h * math.cos(a)))
            try:
                frozen = sorted(vp.frozen_layers)
            except Exception:
                frozen = []
            sheet.viewports.append(ViewportView(
                handle=v.handle, layout=layout.name, paper_centre=(v.center.x, v.center.y),
                paper_size=(v.width, v.height), view_centre=(cx, cy), view_height=v.view_height, scale=scale,
                twist_deg=twist, window=(cx - half_w, cy - half_h, cx + half_w, cy + half_h), frozen_layers=frozen,
                is_off=bool(int(v.get("flags", 0)) & VIEWPORT_OFF_FLAG), status_zeroed=int(v.get("status", 0)) == 0,
            ))
        live = [vp for vp in sheet.viewports if not vp.is_off]
        if live:
            for vp in live:
                x0, y0, x1, y1 = vp.window
                vp.shown = int(((pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)).sum())
            # the largest viewport among those that show the drawing; paper area only breaks the tie
            top = max(vp.shown for vp in live)
            max(live, key=lambda vp: (vp.shown >= 0.1 * top, vp.paper_area)).role = "main"
        sheets.append(sheet)
    sheets.sort(key=lambda s: (s.tab_order, s.layout))
    return sheets


def enable_viewports_for_render(doc: Drawing) -> int:
    """In memory only: give zeroed viewports a non-zero status so ezdxf's renderer draws them."""
    n = 0
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for k, vp in enumerate(layout.viewports()):
            v = vp.dxf
            if _is_overall(vp) or int(v.get("flags", 0)) & VIEWPORT_OFF_FLAG:
                continue
            if int(v.get("status", 0)) <= 0:
                v.status = 2 + k
                v.id = 100 + k
                n += 1
    return n
