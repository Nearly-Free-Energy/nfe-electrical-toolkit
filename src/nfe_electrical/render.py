"""Review renders: the overlay drawn over the base drawing, as PNG or PDF.

The architect's base is drawn light grey, the engineer's existing symbols of the chosen
categories in blue, and everything in the overlay in magenta. Rendering uses ezdxf's
drawing add-on with the matplotlib backend (both open source); nothing is written to a
drawing.
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from ezdxf.addons.drawing import Frontend, RenderContext  # noqa: E402
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration, LineweightPolicy  # noqa: E402
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Polygon as MplPolygon  # noqa: E402

from .symbols import drawn_frame  # noqa: E402

BASE = "#c4c4c4"
EXISTING = "#1f5fbf"
OVERLAY = "#e0008a"
ROOM_EDGE = "#2e7d32"


# ---------------------------------------------------------------- culling
def _box(e, geometry):
    t = e.dxftype()
    try:
        if t == "LINE":
            xs, ys = (e.dxf.start.x, e.dxf.end.x), (e.dxf.start.y, e.dxf.end.y)
        elif t == "LWPOLYLINE":
            pts = e.get_points("xy")
            if not pts:
                return None
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        elif t in ("CIRCLE", "ARC"):
            c, r = e.dxf.center, e.dxf.radius
            return (c.x - r, c.y - r, c.x + r, c.y + r)
        elif t in ("TEXT", "MTEXT"):
            p = e.dxf.insert
            h = (e.dxf.char_height if t == "MTEXT" else e.dxf.height) or 0
            n = len(e.plain_text() if t == "MTEXT" else e.dxf.text)
            return (p.x - h, p.y - 3 * h, p.x + 0.7 * h * n + h, p.y + 3 * h)
        elif t == "INSERT":
            g = geometry.get(e.dxf.name)
            return drawn_frame(e, g)[1] if g else None
        elif t == "HATCH":
            xs, ys = [], []
            for path in e.paths:
                for v in getattr(path, "vertices", []):
                    xs.append(v[0]); ys.append(v[1])
                for edge in getattr(path, "edges", []):
                    for attr in ("start", "end", "center"):
                        q = getattr(edge, attr, None)
                        if q is not None:
                            xs.append(q[0]); ys.append(q[1])
            if not xs:
                return None
        elif t in ("SOLID", "TRACE", "3DFACE"):
            v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx2]
            xs, ys = [p.x for p in v], [p.y for p in v]
        elif t == "DIMENSION":
            pts = [e.dxf.get(k) for k in ("defpoint", "defpoint2", "defpoint3", "text_midpoint") if e.dxf.hasattr(k)]
            xs, ys = [p.x for p in pts], [p.y for p in pts]
        elif t == "ELLIPSE":
            c, mj = e.dxf.center, e.dxf.major_axis
            r = math.hypot(mj.x, mj.y)
            return (c.x - r, c.y - r, c.x + r, c.y + r)
        elif t == "SPLINE":
            pts = list(e.control_points) or list(e.fit_points)
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        elif t == "POINT":
            p = e.dxf.location
            return (p.x, p.y, p.x, p.y)
        else:
            return None
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return None


def _boxes(floor) -> dict:
    cache = getattr(floor, "_render_boxes", None)
    if cache is None:
        cache = {e.dxf.handle: _box(e, floor.geometry) for e in floor.msp}
        floor._render_boxes = cache
    return cache


def _hits(box, win) -> bool:
    return box is None or not (box[2] < win[0] or box[0] > win[2] or box[3] < win[1] or box[1] > win[3])


# ---------------------------------------------------------------- drawing
def _config() -> Configuration:
    return Configuration(background_policy=BackgroundPolicy.WHITE, lineweight_policy=LineweightPolicy.RELATIVE,
                         lineweight_scaling=0.4)


def draw_crop(ax, floor, window, overlay=None, existing_categories=("power", "board"), rooms=True,
              pairs=None, room_ids=None, title=None, highlight_types=None) -> None:
    """Draw one crop onto `ax`: grey base, blue existing symbols, magenta overlay, room outlines."""
    boxes = _boxes(floor)
    elec_layers = {ly.lower() for ly in floor.catalogue.all_electrical_layers()}
    sym = {s.handle: s for s in floor.symbols}
    existing_categories = set(existing_categories or ())
    pad = 0.05 * max(window[2] - window[0], window[3] - window[1])
    win = (window[0] - pad, window[1] - pad, window[2] + pad, window[3] + pad)

    def keep(e) -> bool:
        layer = e.dxf.get("layer", "0")
        s = sym.get(e.dxf.handle)
        if s is not None:
            if s.category not in existing_categories:
                return False
            if highlight_types and s.device_type not in highlight_types:
                return False
            return _hits(s.bbox, win)
        if layer.lower() in elec_layers or layer not in floor.layers_on:
            return False
        if e.dxftype() == "INSERT" and layer == "0" and floor.catalogue.classify(e.dxf.name).known:
            return False
        return _hits(boxes.get(e.dxf.handle), win)

    def base_colour(entity, props):
        props.color = EXISTING if entity.dxf.handle in sym else BASE

    fe = Frontend(RenderContext(floor.doc), MatplotlibBackend(ax), config=_config())
    fe.push_property_override_function(base_colour)
    fe.draw_layout(floor.msp, finalize=False, filter_func=keep)

    if overlay is not None:
        def ov_colour(entity, props):
            props.color = OVERLAY
        fo = Frontend(RenderContext(overlay.doc), MatplotlibBackend(ax), config=_config())
        fo.push_property_override_function(ov_colour)
        fo.draw_layout(overlay.msp, finalize=False)

    if rooms:
        for r in floor.rooms.rooms:
            if room_ids and r.room_id not in room_ids:
                continue
            minx, miny, maxx, maxy = r.polygon.bounds
            if maxx < win[0] or minx > win[2] or maxy < win[1] or miny > win[3]:
                continue
            ax.add_patch(MplPolygon(np.asarray(r.polygon.exterior.coords), closed=True, fill=False,
                                    edgecolor=ROOM_EDGE, lw=0.8, ls=(0, (4, 2)), zorder=5))
            cx, cy = r.polygon.representative_point().coords[0]
            ax.text(cx, cy, f"{r.room_id} {r.name}\n{r.area_m2:.1f} m²", fontsize=6, color=ROOM_EDGE, ha="center",
                    va="center", zorder=6, alpha=0.9)
    if pairs:
        for p in pairs:
            (x0, y0), (x1, y1) = p["existing_at"], p["generated_at"]
            ax.plot([x0, x1], [y0, y1], color="#222222", lw=0.6, zorder=7)
    ax.set_xlim(window[0], window[2])
    ax.set_ylim(window[1], window[3])
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    handles = [Patch(color=BASE, label="architect's base"),
               Patch(color=EXISTING, label="engineer's existing symbols"),
               Patch(color=OVERLAY, label="toolkit overlay (generated)")]
    if rooms:
        handles.append(Line2D([], [], color=ROOM_EDGE, ls="--", label="room found by the toolkit"))
    if pairs:
        handles.append(Line2D([], [], color="#222222", label="existing ↔ generated pair"))
    ax.legend(handles=handles, loc="upper right", fontsize=7, framealpha=0.9)
    if title:
        ax.set_title(title, fontsize=10)


def _figure(window, width_in: float):
    w, h = window[2] - window[0], window[3] - window[1]
    fig = plt.figure(figsize=(width_in, max(3.0, width_in * h / w + 0.6)))
    ax = fig.add_axes([0.01, 0.01, 0.98, 0.93])
    return fig, ax


def render_crop(floor, out_path, window=None, overlay=None, dpi: int = 160, width_in: float = 14.0, **kw) -> str:
    window = window or floor.plan_window
    fig, ax = _figure(window, width_in)
    draw_crop(ax, floor, window, overlay=overlay, **kw)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi, facecolor="white")
    plt.close(fig)
    return str(out)


def room_window(floor, room_ids, margin_m: float = 1.0):
    polys = [floor.rooms.get(r).polygon for r in room_ids]
    x0 = min(p.bounds[0] for p in polys); y0 = min(p.bounds[1] for p in polys)
    x1 = max(p.bounds[2] for p in polys); y1 = max(p.bounds[3] for p in polys)
    m = margin_m / floor.to_m
    return (x0 - m, y0 - m, x1 + m, y1 + m)


def render_rooms(floor, out_path, dpi: int = 130) -> str:
    """Room-finder result for the engineer to confirm: every room found, its id, name and area."""
    rs = floor.rooms
    rm = rs.room_map
    rgb = np.ones(rm.shape + (3,))
    rng = np.random.default_rng(7)
    cols = rng.uniform(0.55, 0.95, (len(rs.rooms) + 1, 3))
    mask = rm > 0
    rgb[mask] = cols[rm[mask]]
    rgb[rs.other_kind == 1] = (0.93, 0.93, 0.93)
    rgb[getattr(rs, "soft", np.zeros_like(rs.wall))] = (0.55, 0.7, 1.0)
    rgb[rs.wall] = (0.25, 0.25, 0.25)
    x0, y1 = rs.origin
    ext = (x0, x0 + rm.shape[1] * rs.res, y1 - rm.shape[0] * rs.res, y1)
    fig = plt.figure(figsize=(rm.shape[1] / 110, rm.shape[0] / 110 + 0.5))
    ax = fig.add_axes([0, 0, 1, 0.95])
    ax.imshow(rgb, extent=ext, interpolation="nearest")
    for r in rs.rooms:
        ax.plot(*r.polygon.exterior.xy, lw=0.7, color="#d32f2f" if r.status != "ok" else "k")
        cx, cy = r.polygon.representative_point().coords[0]
        ax.text(cx, cy, f"{r.room_id} {r.name}\n{r.area_m2:.1f} m²" + (f" [{r.unit}]" if r.unit else ""),
                fontsize=6, ha="center", va="center")
    for f in rs.failures:
        ax.plot(*f.seed, marker="x", color="#d32f2f", ms=7)
        ax.text(f.seed[0], f.seed[1], " " + f.label + " (failed)", color="#d32f2f", fontsize=6, va="center")
    s = rs.summary()
    ax.set_title(f"{floor.floor_id}: {s['rooms']} rooms from {s['labels']} labels; {s['labels_failed']} labels failed "
                 f"(red x); dashed zone lines in blue; unlabelled spaces in light grey", fontsize=9)
    ax.axis("off")
    out = Path(out_path)
    fig.savefig(out, dpi=dpi, facecolor="white")
    plt.close(fig)
    return str(out)


def review_pack(floor, out_pdf, pages: list[dict], overlay=None, width_in: float = 16.5) -> str:
    """Multi-page PDF; each page is a dict of draw_crop() arguments plus 'window' and 'title'."""
    out = Path(out_pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out) as pdf:
        for page in pages:
            page = dict(page)
            window = page.pop("window", None) or floor.plan_window
            fig, ax = _figure(window, width_in)
            draw_crop(ax, floor, window, overlay=page.pop("overlay", overlay), **page)
            pdf.savefig(fig)
            plt.close(fig)
    return str(out)


# ---------------------------------------------------------------- sheets
def default_sheet(floor, overlay=None) -> str:
    """The sheet that shows what the overlay holds (small power for sockets, and so on)."""
    from collections import Counter
    if overlay is not None:
        cats = Counter(floor.catalogue.device_types[d["type"]].category for d in overlay.devices()
                       if d.get("type") in floor.catalogue.device_types)
        if cats:
            want = {"power": "small_power", "board": "small_power", "lighting": "lighting", "switch": "lighting",
                    "fire": "ict_fire", "ict": "ict_fire"}.get(cats.most_common(1)[0][0])
            match = next((s.layout for s in floor.sheets if s.discipline == want and s.main), None)
            if match:
                return match
    return floor.plan_sheet.layout


def plot_sheet(floor, out_path, layout: str | None = None, overlay=None, mode: str = "issue",
               replace_types: set[str] | None = None, dpi: int = 200) -> dict:
    """Plot a paper-space sheet at true size (e.g. A1) with the overlay drawn through its plan viewport.

    mode "issue": the overlay in its NFE layer colours, and the engineer's existing symbols of
    the types the overlay holds are hidden in the rooms the overlay covers, so the sheet shows
    the regenerated layout as it would be issued.
    mode "review": everything kept; the overlay in magenta over the existing drawing.
    Viewport layer freezes, the title block and the legend come from the drawing itself."""
    import ezdxf
    from ezdxf.math import Matrix44
    from matplotlib.patches import Rectangle

    from .sheets import enable_viewports_for_render

    sheet = next((s for s in floor.sheets if s.layout == (layout or default_sheet(floor, overlay))), None)
    if sheet is None or sheet.main is None:
        raise ValueError(f"no sheet {layout!r} with a plan viewport; sheets: {[s.layout for s in floor.sheets]}")
    vp = sheet.main
    enable_viewports_for_render(floor.doc)  # in memory only: LibreDWG zeroes viewport status flags
    ps = floor.doc.paperspace(sheet.layout)

    hidden: set[str] = set()
    covered: set[str] = set()
    if overlay is not None and mode == "issue":
        devices = overlay.devices()
        types = replace_types or {d["type"] for d in devices}
        covered = {d.get("room") for d in devices if d.get("room")}
        hidden = {s.handle for s in floor.symbols if s.device_type in types and s.room_id in covered}

    # ezdxf 1.4.5 works out each viewport's frozen layers but does not apply them to entity
    # visibility, so lighting would leak onto the small power sheet. Apply them here,
    # tracking which viewport is being drawn.
    frozen_by_vp = {v.handle: {n.lower() for n in v.frozen_layers} for v in sheet.viewports}
    current = {"vp": None}

    def base_override(entity, props):
        if entity.dxf.handle in hidden:
            props.is_visible = False
        elif current["vp"] and (props.layer or "").lower() in frozen_by_vp.get(current["vp"], ()):
            props.is_visible = False

    w_mm, h_mm = sheet.paper_size_mm
    unit = sheet.paper_unit_mm
    W, H = w_mm / unit, h_mm / unit
    boxes = [_box(e, floor.geometry) for e in ps.query("INSERT LINE LWPOLYLINE")]
    boxes = [b for b in boxes if b is not None]
    cx = (min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2 if boxes else W / 2
    cy = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2 if boxes else H / 2

    fig = plt.figure(figsize=(w_mm / 25.4, h_mm / 25.4))
    ax = fig.add_axes([0, 0, 1, 1])
    fe = Frontend(RenderContext(floor.doc), MatplotlibBackend(ax), config=_config())
    fe.push_property_override_function(base_override)
    draw_viewport = fe.draw_viewport

    def tracked_draw_viewport(vp_entity):
        current["vp"] = vp_entity.dxf.handle
        try:
            draw_viewport(vp_entity)
        finally:
            current["vp"] = None
    fe.draw_viewport = tracked_draw_viewport
    fe.draw_layout(ps, finalize=False)

    n_overlay = 0
    if overlay is not None:
        overlay.save()
        odoc = ezdxf.readfile(str(overlay.path))  # a private copy: the overlay file itself is not touched
        to_paper = Matrix44.chain(Matrix44.translate(-vp.view_centre[0], -vp.view_centre[1], 0),
                                  Matrix44.scale(1.0 / vp.scale), Matrix44.translate(vp.paper_centre[0],
                                                                                     vp.paper_centre[1], 0))
        for e in odoc.modelspace():
            e.transform(to_paper)
            n_overlay += 1
        before = {id(a) for a in (*ax.patches, *ax.lines, *ax.collections, *ax.texts, *ax.images)}
        fo = Frontend(RenderContext(odoc), MatplotlibBackend(ax), config=_config())
        if mode == "review":
            def ov_colour(entity, props):
                props.color = OVERLAY
            fo.push_property_override_function(ov_colour)
        fo.draw_layout(odoc.modelspace(), finalize=False)
        clip = Rectangle((vp.paper_centre[0] - vp.paper_size[0] / 2, vp.paper_centre[1] - vp.paper_size[1] / 2),
                         vp.paper_size[0], vp.paper_size[1], transform=ax.transData)
        for artist in (*ax.patches, *ax.lines, *ax.collections, *ax.texts, *ax.images):
            if id(artist) not in before:
                artist.set_clip_path(clip)

    ax.set_xlim(cx - W / 2, cx + W / 2)
    ax.set_ylim(cy - H / 2, cy + H / 2)
    ax.set_aspect("equal")
    ax.axis("off")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=dpi, facecolor="white")
    plt.close(fig)
    ratio = sheet.scale_ratio(floor.to_m)
    return {"file": str(out), "layout": sheet.layout, "paper_mm": [w_mm, h_mm], "mode": mode,
            "plan_scale": f"1:{ratio:.0f}" if ratio else None, "overlay_entities": n_overlay,
            "existing_symbols_hidden": len(hidden), "rooms_replaced": sorted(covered)}
