"""Room finder: fill outward from each room label to the surrounding walls.

Closed outlines surround only about a third of the room labels in NFE's pilot drawings, so rooms are found
on a raster of the plotted base copy's wall layers:
  1. draw the wall layers at 2.5 cm per pixel;
  2. close door-sized gaps by treating everything within 0.45 m of a wall as blocked;
  3. label the remaining free space into connected regions;
  4. grow each region back out to the wall faces (growth cannot cross a wall line);
  5. a label's room is the region under it; regions touching the plan edge or larger
     than any plausible room have leaked through an opening and are reported as failures.
Labels that sit in spaces narrower than the door-gap closing (ducts, shower trays) get a
second pass with a 0.2 m closing.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from contourpy import contour_generator
from ezdxf import path as ezpath
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.geometry import Point, Polygon, mapping
from shapely.ops import polylabel

RES_M = 0.025          # raster resolution
DOOR_GAP_M = 0.9       # openings up to this wide are closed
SMALL_GAP_M = 0.2      # second pass for narrow spaces
MAX_ROOM_M2 = 300.0
LABEL_SEARCH_M = 0.6   # how far from the label to look for free space


@dataclass
class Label:
    text: str
    seed: tuple[float, float]
    insert: tuple[float, float]
    height: float
    layer: str
    handles: list[str]
    room_type: str
    service: bool


@dataclass
class Room:
    room_id: str
    name: str
    labels: list[str]
    room_type: str
    service: bool
    polygon: Polygon
    area_m2: float
    label_point: tuple[float, float]
    status: str = "ok"
    issues: list[str] = field(default_factory=list)
    unit: str | None = None
    devices: Counter = field(default_factory=Counter)

    def as_dict(self, to_m: float = 1.0, with_polygon: bool = True) -> dict:
        d = {
            "room_id": self.room_id, "name": self.name, "labels": self.labels, "room_type": self.room_type,
            "service": self.service, "area_m2": round(self.area_m2, 2), "status": self.status, "issues": self.issues,
            "label_point": [round(v, 3) for v in self.label_point], "unit": self.unit,
            "centroid": [round(self.polygon.centroid.x, 3), round(self.polygon.centroid.y, 3)],
            "bbox": [round(v, 3) for v in self.polygon.bounds],
            "devices": dict(self.devices.most_common()),
        }
        if with_polygon:
            ext = [[round(x, 3), round(y, 3)] for x, y in self.polygon.exterior.coords]
            d["polygon"] = ext
            d["holes"] = [[[round(x, 3), round(y, 3)] for x, y in r.coords] for r in self.polygon.interiors]
        return d


@dataclass
class LabelFailure:
    label: str
    seed: tuple[float, float]
    reason: str
    area_m2: float | None = None


class RoomSet:
    def __init__(self, floor, rooms, failures, labels, wall, room_map, origin, res, params, unlabelled):
        self.floor = floor
        self.rooms: list[Room] = rooms
        self.failures: list[LabelFailure] = failures
        self.labels: list[Label] = labels
        self.wall = wall              # bool raster, True = wall line
        self.room_map = room_map      # int raster, 0 = none, k = rooms[k-1]
        self.origin = origin          # (x0, y1): world coords of the top-left pixel corner
        self.res = res                # drawing units per pixel
        self.params = params
        self.unlabelled = unlabelled
        self.other_kind = np.zeros(wall.shape, dtype=np.uint8)  # 1 = unlabelled space, 2 = exterior
        self.other_space = np.zeros(wall.shape, dtype=bool)
        self._near = None
        self._wall_edt = None

    # ------------------------------------------------------------ lookups
    def px(self, x: float, y: float) -> tuple[int, int]:
        return int((self.origin[1] - y) / self.res), int((x - self.origin[0]) / self.res)

    def world(self, r: float, c: float) -> tuple[float, float]:
        return self.origin[0] + (c + 0.5) * self.res, self.origin[1] - (r + 0.5) * self.res

    def inside_raster(self, r: int, c: int) -> bool:
        return 0 <= r < self.room_map.shape[0] and 0 <= c < self.room_map.shape[1]

    def room_at(self, x: float, y: float, max_dist_m: float = 0.6) -> Room | None:
        """The room at a point. Points on a wall line (e.g. wall-mounted symbols) go to the
        nearest space within `max_dist_m`; points in unlabelled spaces belong to no room."""
        r, c = self.px(x, y)
        if not self.inside_raster(r, c):
            return None
        k = int(self.room_map[r, c])
        if k == 0:
            if self.other_space[r, c]:
                return None
            if self._near is None:
                occupied = (self.room_map > 0) | self.other_space
                self._near = ndimage.distance_transform_edt(~occupied, return_indices=True)
            dist, idx = self._near
            if dist[r, c] * self.res * self.floor.to_m > max_dist_m:
                return None
            k = int(self.room_map[idx[0][r, c], idx[1][r, c]])
        return self.rooms[k - 1] if k > 0 else None

    def space_at(self, x: float, y: float) -> str:
        """What is at a point: a room id, 'unlabelled', 'exterior', 'wall' or 'outside'."""
        r, c = self.px(x, y)
        if not self.inside_raster(r, c):
            return "outside"
        if self.room_map[r, c]:
            return self.rooms[int(self.room_map[r, c]) - 1].room_id
        if self.wall[r, c]:
            return "wall"
        return {1: "unlabelled", 2: "exterior"}.get(int(self.other_kind[r, c]), "unassigned")

    def wall_edt(self):
        """Distance (pixels) to, and index of, the nearest wall pixel for every pixel."""
        if self._wall_edt is None:
            self._wall_edt = ndimage.distance_transform_edt(~self.wall, return_indices=True)
        return self._wall_edt

    def get(self, ref: str) -> Room:
        """A room by id (`R07`) or by an unambiguous name (`Lounge`, `Lounge [W]`)."""
        for room in self.rooms:
            if room.room_id.lower() == ref.lower():
                return room
        key = ref.strip().lower()
        matches = [r for r in self.rooms if r.name.lower() == key or f"{r.name} [{r.unit}]".lower() == key]
        if not matches:
            matches = [r for r in self.rooms if key in r.name.lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no room {ref!r}")
        raise KeyError(f"{ref!r} is ambiguous: " + ", ".join(f"{r.room_id} {r.name} [{r.unit}]" for r in matches))

    def assign_symbols(self, symbols) -> None:
        for room in self.rooms:
            room.devices = Counter()
        for s in symbols:
            if not s.on_plan:
                continue
            room = self.room_at(*s.centre)
            s.room_id = room.room_id if room else None
            if room:
                room.devices[s.device_type] += 1

    # ------------------------------------------------------------ reports
    def summary(self) -> dict:
        ok = [r for r in self.rooms if r.status == "ok"]
        return {
            "labels": len(self.labels), "rooms": len(self.rooms), "rooms_ok": len(ok),
            "rooms_suspect": len(self.rooms) - len(ok), "labels_failed": len(self.failures),
            "labels_bounded": len(self.labels) - len(self.failures),
            "unlabelled_enclosed_spaces": len(self.unlabelled), "params": self.params,
        }

    def to_json(self) -> dict:
        to_m = self.floor.to_m
        return {
            "floor_id": self.floor.floor_id, "units": self.floor.units.detected, "summary": self.summary(),
            "rooms": [r.as_dict(to_m) for r in self.rooms],
            "failed_labels": [{"label": f.label, "seed": [round(v, 3) for v in f.seed], "reason": f.reason,
                               "area_m2": f.area_m2} for f in self.failures],
            "unlabelled_enclosed_spaces": self.unlabelled,
        }

    def write(self, path) -> str:
        with open(path, "w") as fh:
            json.dump(self.to_json(), fh, indent=1)
        return str(path)


# ---------------------------------------------------------------- labels
def _plain(e) -> str:
    try:
        return e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
    except Exception:
        return e.dxf.get("text", "")


def _text_lines(e):
    """(text, left-x, baseline-y, height) for each line of a TEXT/MTEXT entity, roughly."""
    if e.dxftype() == "MTEXT":
        h = e.dxf.char_height
        lines = [ln.strip() for ln in _plain(e).split("\n") if ln.strip()]
        x, y = e.dxf.insert.x, e.dxf.insert.y
        ap = e.dxf.get("attachment_point", 1)
        widths = [0.6 * h * len(ln) for ln in lines]
        w = max(widths, default=0.0)
        block_h = 1.67 * h * len(lines)
        col, row = (ap - 1) % 3, (ap - 1) // 3          # 0 left/top .. 2 right/bottom
        left = x - (0.5 * w if col == 1 else w if col == 2 else 0.0)
        top = y + (0.5 * block_h if row == 1 else block_h if row == 2 else 0.0)
        return [(ln, left, top - h - i * 1.67 * h, h) for i, ln in enumerate(lines)]
    h = e.dxf.height
    text = _plain(e).strip()
    x, y = e.dxf.insert.x, e.dxf.insert.y
    halign, valign = e.dxf.get("halign", 0), e.dxf.get("valign", 0)
    w = 0.6 * h * len(text)
    if halign or valign:
        ax, ay = e.dxf.get("align_point", e.dxf.insert)[:2]
        x = ax - (0.5 * w if halign in (1, 4) else w if halign == 2 else 0.0)
        y = ay - (0.5 * h if valign == 2 or halign == 4 else h if valign == 3 else 0.0)
    return [(text, x, y, h)]


def collect_labels(floor) -> list[Label]:
    cat = floor.catalogue
    vp = floor.plan_viewport
    raw = []
    for e in floor.msp.query("TEXT MTEXT"):
        if e.dxf.get("layer") in cat.label_layers and vp.contains(e.dxf.insert.x, e.dxf.insert.y):
            raw.extend((t, x, y, h, e.dxf.handle, e.dxf.layer) for t, x, y, h in _text_lines(e))
    if not raw:  # PDF-imported bases keep room names on their own text layers
        fam = floor.base.plotted
        for e in floor.msp.query("TEXT MTEXT"):
            ly = e.dxf.get("layer", "")
            if ly.upper().startswith(fam + "_") and re.search(r"(?i)text|label|room|zone", ly) \
                    and ly in floor.visible_layers and vp.contains(e.dxf.insert.x, e.dxf.insert.y):
                for t, x, y, h in _text_lines(e):
                    if cat.room_type(t):
                        raw.append((t, x, y, h, e.dxf.handle, ly))
    lines = [ln for ln in raw if re.search(r"[A-Za-z]{2,}", ln[0]) or not cat.not_room_label.search(ln[0])]
    lines.sort(key=lambda ln: -ln[2])
    used = set()
    labels = []
    for i, (t, x, y, h, hd, ly) in enumerate(lines):
        if i in used or cat.not_room_label.search(t):
            continue
        parts, handles, ys = [t], [hd], [y]
        for j in range(i + 1, len(lines)):
            t2, x2, y2, h2, hd2, _ = lines[j]
            if j in used or cat.not_room_label.search(t2):
                continue
            dy = ys[-1] - y2
            if 1.0 * h <= dy <= 2.6 * h and abs(x2 - x) <= 1.5 * h and 0.7 <= h2 / h <= 1.4:
                parts.append(t2); handles.append(hd2); ys.append(y2); used.add(j)
        used.add(i)
        text = " ".join(parts)
        width = max(0.6 * h * len(p) for p in parts)
        seed = (x + 0.5 * width, (ys[0] + ys[-1]) / 2 + 0.5 * h)
        rt = cat.room_type(text)
        labels.append(Label(text=text, seed=seed, insert=(x, y), height=h, layer=ly, handles=handles,
                            room_type=rt.key if rt else "other", service=bool(rt and rt.service)))
    return labels


# ---------------------------------------------------------------- raster
def _outside(e, x0, y0, x1, y1) -> bool:
    """Cheap bounding-box test for the common wall entity types (others are never culled)."""
    t = e.dxftype()
    try:
        if t == "LINE":
            xs, ys = (e.dxf.start.x, e.dxf.end.x), (e.dxf.start.y, e.dxf.end.y)
        elif t == "LWPOLYLINE":
            pts = e.get_points("xy")
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        elif t in ("ARC", "CIRCLE"):
            c, r = e.dxf.center, e.dxf.radius
            xs, ys = (c.x - r, c.x + r), (c.y - r, c.y + r)
        else:
            return False
    except Exception:
        return False
    return not xs or max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1


def _iter_wall_entities(floor, layers: set[str], window, margin: float):
    x0, y0, x1, y1 = window[0] - margin, window[1] - margin, window[2] + margin, window[3] + margin
    for e in floor.msp:
        if e.dxf.get("layer", "0") not in layers or _outside(e, x0, y0, x1, y1):
            continue
        if e.dxftype() == "INSERT":
            try:
                yield from (v for v in e.virtual_entities())
            except Exception:
                continue
            continue
        yield e


def _short_segment(e, max_len: float):
    t = e.dxftype()
    if t == "LINE":
        a, b = (e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)
    elif t == "LWPOLYLINE" and len(e) == 2 and not e.has_arc:
        pts = e.get_points("xy")
        a, b = (pts[0][0], pts[0][1]), (pts[1][0], pts[1][1])
    else:
        return None
    return (a, b) if 0 < math.dist(a, b) <= max_len else None


def dashed_chains(entities, to_m: float) -> set[int]:
    """ids of entities that are dashes of a dashed line (beams or walls above, hidden edges).

    PDF imports turn every dash into its own short segment. Chains of three or more
    collinear, regularly gapped short segments are kept apart from the walls: they still
    split zones (the pilot project's architect marks the kitchen / dining / lounge zones of an
    open-plan space with dashed beam lines) but nothing is ever mounted on them."""
    groups: dict[tuple, list] = {}
    for e in entities:
        seg = _short_segment(e, 0.5 / to_m)
        if seg is None:
            continue
        (x0, y0), (x1, y1) = seg
        ang = math.atan2(y1 - y0, x1 - x0) % math.pi
        nx, ny = -math.sin(ang), math.cos(ang)
        off = (x0 * nx + y0 * ny) * to_m
        key = (round(math.degrees(ang)) % 180, round(off / 0.01))
        ux, uy = math.cos(ang), math.sin(ang)
        p0, p1 = sorted(((x0 * ux + y0 * uy), (x1 * ux + y1 * uy)))
        groups.setdefault(key, []).append((p0, p1, id(e)))
    dashed: set[int] = set()
    for members in groups.values():
        if len(members) < 3:
            continue
        members.sort()
        run = [members[0]]
        for m in members[1:]:
            gap = (m[0] - run[-1][1]) * to_m
            if 0.015 <= gap <= 0.6:
                run.append(m)
            else:
                if len(run) >= 3:
                    dashed |= {r[2] for r in run}
                run = [m]
        if len(run) >= 3:
            dashed |= {r[2] for r in run}
    return dashed


def build_wall_raster(floor, layers: set[str], window, res: float):
    """Rasters of the solid walls and of the dashed (soft) lines, plus the raster origin."""
    margin = 10 * res
    gx0, gy0, gx1, gy1 = window[0] - margin, window[1] - margin, window[2] + margin, window[3] + margin
    nx, ny = int(math.ceil((gx1 - gx0) / res)), int(math.ceil((gy1 - gy0) / res))
    hard_img, soft_img = Image.new("L", (nx, ny), 0), Image.new("L", (nx, ny), 0)
    hard_draw, soft_draw = ImageDraw.Draw(hard_img), ImageDraw.Draw(soft_img)

    def px(p):
        return ((p[0] - gx0) / res, (gy1 - p[1]) / res)

    entities = list(_iter_wall_entities(floor, layers, window, margin))
    dashed = dashed_chains(entities, floor.to_m)
    for e in entities:
        draw = soft_draw if id(e) in dashed else hard_draw
        t = e.dxftype()
        try:
            if t == "LINE":
                draw.line([px(e.dxf.start), px(e.dxf.end)], fill=255, width=1)
            elif t in ("LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"):
                pts = [px(v) for v in ezpath.make_path(e).flattening(res / 2)]
                if len(pts) >= 2:
                    draw.line(pts, fill=255, width=1)
            elif t == "HATCH":
                for p in ezpath.from_hatch(e):
                    pts = [px(v) for v in p.flattening(res / 2)]
                    if len(pts) >= 3:
                        (draw.polygon(pts, fill=255) if e.dxf.solid_fill else draw.line(pts, fill=255, width=1))
            elif t in ("SOLID", "TRACE"):
                v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx3, e.dxf.vtx2]
                draw.polygon([px(p) for p in v], fill=255)
        except Exception:
            continue
    return np.asarray(hard_img) > 0, np.asarray(soft_img) > 0, (gx0, gy1), len(dashed)


def _grow(room_map: np.ndarray, wall: np.ndarray, steps: int) -> np.ndarray:
    """Grow labelled regions pixel by pixel through non-wall pixels (4-neighbour, cannot cross walls)."""
    out = room_map.copy()
    cross = ndimage.generate_binary_structure(2, 1)
    for _ in range(steps):
        dil = ndimage.grey_dilation(out, footprint=cross)
        new = (out == 0) & ~wall & (dil > 0)
        if not new.any():
            break
        out[new] = dil[new]
    return out


def _regions(wall: np.ndarray, gap_px: float):
    blocked = ndimage.distance_transform_edt(~wall) <= gap_px
    comp, n = ndimage.label(~blocked)
    edge = np.unique(np.concatenate([comp[0, :], comp[-1, :], comp[:, 0], comp[:, -1]]))
    return comp, n, set(int(v) for v in edge if v)


def _seed_component(comp: np.ndarray, r: int, c: int, search_px: int) -> int:
    r0, r1 = max(0, r - search_px), min(comp.shape[0], r + search_px + 1)
    c0, c1 = max(0, c - search_px), min(comp.shape[1], c + search_px + 1)
    sub = comp[r0:r1, c0:c1]
    if not sub.any():
        return 0
    rr, cc = np.nonzero(sub)
    k = int(np.argmin((rr + r0 - r) ** 2 + (cc + c0 - c) ** 2))
    return int(sub[rr[k], cc[k]])


def _polygon(mask: np.ndarray, origin, res, min_hole_area: float) -> Polygon | None:
    rows, cols = np.nonzero(mask)
    if rows.size == 0:
        return None
    r0, c0 = rows.min() - 1, cols.min() - 1
    sub = np.zeros((rows.max() - r0 + 2, cols.max() - c0 + 2))
    sub[rows - r0, cols - c0] = 1.0
    rings = []
    for line in contour_generator(z=sub).lines(0.5):
        if len(line) < 4:
            continue
        xy = [(origin[0] + (c0 + x + 0.5) * res, origin[1] - (r0 + y + 0.5) * res) for x, y in line]
        ring = Polygon(xy)
        if ring.is_valid and ring.area > 0:
            rings.append(ring)
        elif ring.area > 0:
            rings.append(ring.buffer(0))
    if not rings:
        return None
    rings.sort(key=lambda p: -p.area)
    outer = rings[0]
    holes = [h.exterior.coords for h in rings[1:] if outer.contains(h) and h.area > min_hole_area]
    poly = Polygon(outer.exterior.coords, holes)
    poly = poly.simplify(res * 0.75, preserve_topology=True)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    return poly


def find_rooms(floor, res_m: float = RES_M, door_gap_m: float = DOOR_GAP_M,
               wall_layers: list[str] | None = None) -> RoomSet:
    to_m = floor.to_m
    res = res_m / to_m
    layers = set(wall_layers or floor.base.wall_layers)
    window = floor.plan_window
    hard, soft, origin, n_dashes = build_wall_raster(floor, layers, window, res)
    wall = hard | soft  # zones are split by walls and by dashed zone lines
    labels = collect_labels(floor)
    px_area_m2 = res_m * res_m
    search_px = int(LABEL_SEARCH_M / res_m)

    passes = []
    for gap_m in (door_gap_m, SMALL_GAP_M):
        comp, n, edge = _regions(wall, gap_m / 2 / res_m)
        sizes = np.bincount(comp.ravel(), minlength=n + 1) * px_area_m2
        passes.append((gap_m, comp, n, edge, sizes))

    # decide each label's region: the door-gap pass first; the narrow pass only rescues
    # labels that found no region, and only if the rescued region is not part of a room
    # the first pass already found (a narrow pass leaks through every door).
    picks: dict[tuple[int, int], list[Label]] = {}
    failures: list[LabelFailure] = []
    pending: list[tuple[Label, int, int, float, str]] = []
    for lab in labels:
        r, c = int((origin[1] - lab.seed[1]) / res), int((lab.seed[0] - origin[0]) / res)
        if not (0 <= r < wall.shape[0] and 0 <= c < wall.shape[1]):
            failures.append(LabelFailure(lab.text, lab.seed, "label outside the raster"))
            continue
        rt = floor.catalogue.room_type(lab.text)
        max_area = max(MAX_ROOM_M2 if rt is None else rt.area_m2[1] * 2.0, 20.0)
        gap_m, comp, n, edge, sizes = passes[0]
        k = _seed_component(comp, r, c, search_px)
        if k == 0:
            pending.append((lab, r, c, max_area, "no free space near the label"))
        elif k in edge:
            failures.append(LabelFailure(lab.text, lab.seed, f"leaked to the plan edge (gap closing {gap_m} m)",
                                         round(float(sizes[k]), 1)))
        elif sizes[k] > max_area:
            failures.append(LabelFailure(
                lab.text, lab.seed, f"region of {sizes[k]:.0f} m2 is too large for a {lab.room_type} "
                                    "(leaked through an opening)", round(float(sizes[k]), 1)))
        else:
            picks.setdefault((0, k), []).append(lab)
    first_pass = np.isin(passes[0][1], [k for (_, k) in picks]) if picks else np.zeros(wall.shape, bool)
    gap_m, comp, n, edge, sizes = passes[1]
    for lab, r, c, max_area, reason in pending:
        k = _seed_component(comp, r, c, search_px)
        if k == 0:
            failures.append(LabelFailure(lab.text, lab.seed, reason))
            continue
        region = comp == k
        overlap = float((region & first_pass).sum()) / max(1, int(region.sum()))
        if k in edge or sizes[k] > max_area or overlap > 0.05:
            failures.append(LabelFailure(lab.text, lab.seed, "no closed space found: too narrow for door-gap "
                                         "closing and leaks without it", round(float(sizes[k]), 1)))
            continue
        picks.setdefault((1, k), []).append(lab)

    # Build the room map: one id per picked region. Every other enclosed space and the
    # exterior grow too, so that a room's growth stops where it meets a neighbour's
    # (mid-doorway) instead of running into unlabelled spaces.
    order = sorted(picks.items(), key=lambda kv: (-round(kv[1][0].seed[1] * to_m), kv[1][0].seed[0]))
    n_rooms = len(order)
    unl, ext = n_rooms + 1, n_rooms + 2
    gap_m, comp1, n1, edge1, sizes1 = passes[0]
    lut = np.zeros(n1 + 1, dtype=np.int32)
    for k in range(1, n1 + 1):
        lut[k] = ext if k in edge1 else (unl if sizes1[k] >= 0.5 else 0)
    for idx, ((pi, k), _) in enumerate(order, start=1):
        if pi == 0:
            lut[k] = idx
    merged = _absorb_unlabelled_zones(hard, comp1, lut, unl, door_gap_m / 2 / res_m, int(0.8 / res_m))
    seed_map = lut[comp1]
    for idx, ((pi, k), _) in enumerate(order, start=1):
        if pi == 1:
            seed_map[(passes[1][1] == k) & (seed_map == 0)] = idx
    grow_px = int(math.ceil(2.5 * door_gap_m / 2 / res_m)) + 2  # 4-neighbour growth needs ~2x to fill corners
    grown = _grow(seed_map, wall, grow_px)
    room_map = np.where(grown <= n_rooms, grown, 0).astype(np.int32)
    other_kind = np.where(grown == unl, 1, np.where(grown == ext, 2, 0)).astype(np.uint8)

    rooms: list[Room] = []
    for idx, ((pi, k), labs) in enumerate(order, start=1):
        mask = room_map == idx
        poly = _polygon(mask, origin, res, min_hole_area=0.1 / (to_m * to_m))
        if poly is None or poly.is_empty:
            for lab in labs:
                failures.append(LabelFailure(lab.text, lab.seed, "region vanished when tracing the outline"))
            continue
        primary = _primary_label(labs)
        names = [primary.text]
        for lab in labs:
            if lab.text not in names:
                names.append(lab.text)
        area = float(mask.sum()) * px_area_m2
        rt = floor.catalogue.room_type(primary.text)
        room = Room(room_id=f"R{idx:02d}", name=" + ".join(names), labels=names, room_type=primary.room_type,
                    service=primary.service, polygon=poly, area_m2=area, label_point=primary.seed)
        if rt:
            lo, hi = rt.area_m2
            if area < lo:
                room.issues.append(f"area {area:.1f} m2 below the plausible {lo}-{hi} m2 for a {rt.key}")
            elif area > hi:
                room.issues.append(f"area {area:.1f} m2 above the plausible {lo}-{hi} m2 for a {rt.key}")
        else:
            room.issues.append("label does not match any known room type")
        if pi == 1:
            room.issues.append(f"bounded only with {SMALL_GAP_M} m gap closing (narrow space)")
        if len(labs) > 1:
            room.issues.append("open-plan region shared by labels: " + ", ".join(lab.text for lab in labs))
        if not poly.contains(Point(primary.seed)) and poly.distance(Point(primary.seed)) > 0.3 / to_m:
            room.issues.append("label lies outside the traced outline")
        room.status = "ok" if not any(i.startswith("area") or "outside" in i for i in room.issues) else "suspect"
        rooms.append(room)

    # renumber densely in case a region vanished
    remap = np.zeros(len(order) + 1, dtype=np.int32)
    for new, room in enumerate(rooms, start=1):
        old = int(room.room_id[1:])
        remap[old] = new
        room.room_id = f"R{new:02d}"
    room_map = remap[room_map]

    # enclosed spaces with no label (first pass), for the engineer's attention
    gap_m, comp, n, edge, sizes = passes[0]
    used = {k for (pi, k) in picks if pi == 0}
    unlabelled = []
    for k in range(1, n + 1):
        if k in used or k in edge or sizes[k] < 2.0 or sizes[k] > MAX_ROOM_M2:
            continue
        rr, cc = np.nonzero(comp == k)
        cx, cy = origin[0] + (cc.mean() + 0.5) * res, origin[1] - (rr.mean() + 0.5) * res
        unlabelled.append({"centre": [round(cx, 2), round(cy, 2)], "core_area_m2": round(float(sizes[k]), 1)})

    params = {"resolution_m": res_m, "door_gap_m": door_gap_m, "narrow_pass_gap_m": SMALL_GAP_M,
              "wall_layers": sorted(layers), "dashed_segments_as_zone_lines": n_dashes,
              "unlabelled_zones_absorbed": merged, "raster_px": list(wall.shape)}
    rs = RoomSet(floor, rooms, failures, labels, hard, room_map, origin, res, params, unlabelled)
    rs.soft = soft
    rs.other_kind = other_kind
    rs.other_space = other_kind > 0
    return rs


def _absorb_unlabelled_zones(hard, comp, lut, unl: int, gap_px: float, beam_px: int) -> int:
    """Give unlabelled zones that are cut off from a labelled zone only by dashed lines to
    that zone (e.g. the strip between a pilot lounge's beam line and the party wall).
    `beam_px` allows for a beam drawn as two dashed edges between the zones."""
    hard_comp, _ = ndimage.label(~(ndimage.distance_transform_edt(~hard) <= gap_px))
    reach = int(2 * gap_px) + beam_px + 3
    merged = 0
    for k in np.nonzero(lut == unl)[0]:
        mask = comp == k
        hc = hard_comp[mask]
        hc = hc[hc > 0]
        if hc.size == 0:
            continue
        home = np.bincount(hc).argmax()
        rr, cc = np.nonzero(mask)
        r0, r1 = max(0, rr.min() - reach), min(comp.shape[0], rr.max() + reach + 1)
        c0, c1 = max(0, cc.min() - reach), min(comp.shape[1], cc.max() + reach + 1)
        near = ndimage.binary_dilation(mask[r0:r1, c0:c1], iterations=reach)
        neigh = comp[r0:r1, c0:c1][near]
        same_hard = hard_comp[r0:r1, c0:c1][near] == home
        ids = lut[neigh[same_hard]]
        ids = ids[(ids > 0) & (ids < unl)]
        if ids.size:
            lut[k] = np.bincount(ids).argmax()
            merged += 1
    return merged


_PRIORITY = ["lounge", "dining", "kitchen", "bedroom", "cinema", "office", "lobby", "corridor", "pantry", "wc",
             "closet", "store", "balcony", "plant", "gym", "bar", "duct", "lift", "stair", "void", "parking", "other"]


def _primary_label(labels: list[Label]) -> Label:
    return min(labels, key=lambda lab: _PRIORITY.index(lab.room_type) if lab.room_type in _PRIORITY else 99)


def room_centre(room: Room) -> tuple[float, float]:
    """A point well inside the room (pole of inaccessibility), for ceiling devices and labels."""
    p = polylabel(room.polygon, tolerance=max(room.polygon.length / 400, 1e-3))
    return p.x, p.y


def polygon_geojson(room: Room) -> dict:
    return mapping(room.polygon)
