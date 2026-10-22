"""Placement tools: `place_device(room, type, rule)` and a simple circuit-run tool.

Both compute geometry deterministically and write only to the overlay. A language
model never supplies coordinates: it names a room, a device type and a rule, and can
ask for a dry run to review the proposal before anything is written.

Wall-mounted symbols are put against the room's wall-backed boundary, away from door
openings and corners, rotated so their back faces the wall. Which side of each symbol
is its "back", and how far its drawn centre sits from the wall face, is learned from
the engineer's existing inserts of the same block on the floor.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass

from shapely.geometry import Point
from shapely.geometry.polygon import orient

from .overlay import Overlay
from .rooms import Room, room_centre


# ======================================================================= walls
@dataclass
class WallSample:
    x: float
    y: float
    nx: float  # unit normal pointing out of the room, into the wall
    ny: float
    s: float   # arc length along the boundary (drawing units)
    edge: int
    backed: bool
    wall_dist: float  # boundary point -> wall face, drawing units
    facing: str = ""  # what is on the other side of the wall: a room type, 'exterior', 'unlabelled' or 


@dataclass
class WallRun:
    edge: int
    start: float
    end: float
    nx: float
    ny: float
    backed: bool

    @property
    def length(self) -> float:
        return self.end - self.start


class RoomWalls:
    """The boundary of one room, sampled every few cm and classified as wall-backed or open."""

    def __init__(self, floor, room: Room, step_m: float = 0.05, reach_m: float = 0.3, beyond_m: float = 0.8):
        rs = floor.rooms
        self.floor, self.room = floor, room
        to_m = floor.to_m
        poly = orient(room.polygon.simplify(0.04 / to_m, preserve_topology=True), sign=1.0)
        ring = list(poly.exterior.coords)
        self.corners = [(x, y) for x, y in ring[:-1]]
        step, reach = step_m / to_m, reach_m / to_m
        res = rs.res
        samples, s_acc = [], 0.0
        for i in range(len(ring) - 1):
            (x0, y0), (x1, y1) = ring[i], ring[i + 1]
            L = math.hypot(x1 - x0, y1 - y0)
            if L <= 1e-9:
                continue
            dx, dy = (x1 - x0) / L, (y1 - y0) / L
            nx, ny = _snap_normal(dy, -dx)  # CCW ring: interior on the left, outward normal on the right
            n = max(1, int(L / step))
            for k in range(n):
                t = (k + 0.5) * L / n
                px, py = x0 + dx * t, y0 + dy * t
                hit = None
                for j in range(int(reach / res) + 2):
                    qx, qy = px + nx * j * res, py + ny * j * res
                    r, c = rs.px(qx, qy)
                    if rs.inside_raster(r, c) and rs.wall[r, c]:
                        hit = j * res
                        break
                facing = self._beyond(rs, px, py, nx, ny, hit, beyond_m / to_m) if hit is not None else ""
                samples.append(WallSample(px, py, nx, ny, s_acc + t, i, hit is not None, hit or 0.0, facing))
            s_acc += L
        self.samples = samples
        self.perimeter = s_acc
        self.runs = self._runs()

    def _beyond(self, rs, px, py, nx, ny, hit, limit) -> str:
        """Walk through the wall from the hit point and report the first space on the other side."""
        j0 = int(hit / rs.res)
        for j in range(j0, j0 + int(limit / rs.res) + 1):
            r, c = rs.px(px + nx * j * rs.res, py + ny * j * rs.res)
            if not rs.inside_raster(r, c):
                return "exterior"
            if rs.wall[r, c]:
                continue
            k = int(rs.room_map[r, c])
            if k and k != int(rs.room_map[rs.px(px, py)]):
                return rs.rooms[k - 1].room_type
            kind = int(rs.other_kind[r, c])
            if kind:
                return "unlabelled" if kind == 1 else "exterior"
        return ""

    def _runs(self) -> list[WallRun]:
        runs: list[WallRun] = []
        for smp in self.samples:
            last = runs[-1] if runs else None
            if last and last.edge == smp.edge and last.backed == smp.backed:
                last.end = smp.s
            else:
                runs.append(WallRun(smp.edge, smp.s, smp.s, smp.nx, smp.ny, smp.backed))
        return runs

    @property
    def openings(self) -> list[WallRun]:
        """Unbacked stretches long enough to be a door or an open side (>= 0.5 m)."""
        return [r for r in self.runs if not r.backed and r.length * self.floor.to_m >= 0.45]

    def usable(self, door_clearance_m: float, corner_clearance_m: float, avoid_facing=()) -> list[WallSample]:
        to_m = self.floor.to_m
        dc, cc = door_clearance_m / to_m, corner_clearance_m / to_m
        out = []
        for smp in self.samples:
            if not smp.backed or smp.facing in avoid_facing:
                continue
            if any(_arc_dist(smp.s, o, self.perimeter) < dc for o in self.openings):
                continue
            if any(math.hypot(smp.x - cx, smp.y - cy) < cc for cx, cy in self.corners
                   if _is_real_corner(self.corners, (cx, cy))):
                continue
            out.append(smp)
        return out

    def wall_segments(self, door_clearance_m: float, corner_clearance_m: float,
                      avoid_facing=()) -> list[list[WallSample]]:
        """Contiguous usable stretches, one list per straight wall."""
        segs: list[list[WallSample]] = []
        step = self.samples[1].s - self.samples[0].s if len(self.samples) > 1 else 0
        for smp in self.usable(door_clearance_m, corner_clearance_m, avoid_facing):
            if segs and segs[-1][-1].edge == smp.edge and smp.s - segs[-1][-1].s <= 1.5 * step + 1e-9:
                segs[-1].append(smp)
            else:
                segs.append([smp])
        return segs

    def summary(self) -> dict:
        to_m = self.floor.to_m
        step = self.perimeter / max(1, len(self.samples))
        facing = {}
        for smp in self.samples:
            if smp.backed:
                facing[smp.facing or "?"] = facing.get(smp.facing or "?", 0.0) + step * to_m
        return {"perimeter_m": round(self.perimeter * to_m, 2),
                "wall_backed_m": round(sum(r.length for r in self.runs if r.backed) * to_m, 2),
                "wall_backed_by_facing_m": {k: round(v, 2) for k, v in sorted(facing.items())},
                "openings": [{"length_m": round(o.length * to_m, 2), "edge": o.edge} for o in self.openings]}


def _snap_normal(nx: float, ny: float, tol_deg: float = 2.0) -> tuple[float, float]:
    """Raster-traced walls are a fraction of a degree off square; snap those to exact quarter turns."""
    ang = math.degrees(math.atan2(ny, nx))
    q = round(ang / 90.0) * 90.0
    if abs(ang - q) <= tol_deg:
        a = math.radians(q)
        return round(math.cos(a)), round(math.sin(a))
    return nx, ny


def _arc_dist(s: float, run: WallRun, perimeter: float) -> float:
    if run.start <= s <= run.end:
        return 0.0
    d = min(abs(s - run.start), abs(s - run.end))
    return min(d, perimeter - d)


def _is_real_corner(corners, c, min_turn_deg: float = 30.0) -> bool:
    i = corners.index(c)
    a, b = corners[i - 1], corners[(i + 1) % len(corners)]
    v1 = (c[0] - a[0], c[1] - a[1])
    v2 = (b[0] - c[0], b[1] - c[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return False
    cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cosang)) >= min_turn_deg


# ======================================================================= house style
@dataclass
class WallStyle:
    block: str
    back_angle_local: float   # direction (block coordinates) that faces the wall, degrees
    face_offset_m: float      # wall face -> drawn centre
    samples: int
    spread_deg: float
    learned: bool

    def rotation_for(self, nx: float, ny: float) -> float:
        return (math.degrees(math.atan2(ny, nx)) - self.back_angle_local) % 360


def learn_wall_style(floor, block: str, max_dist_m: float = 0.6) -> WallStyle:
    """How the engineers mount `block` on walls in this drawing: which side faces the wall and how far off it."""
    block = floor.resolve_block(block) or block
    cache = floor.__dict__.setdefault("_wall_styles", {})
    if block in cache:
        return cache[block]
    rs = floor.rooms
    dist, (ri, ci) = rs.wall_edt()
    angles, offsets = [], []
    for s in floor.symbols:
        if s.block != block or not s.on_plan:
            continue
        r, c = rs.px(*s.centre)
        if not rs.inside_raster(r, c):
            continue
        d = dist[r, c] * rs.res
        if d * floor.to_m > max_dist_m:
            continue
        wx, wy = rs.world(ri[r, c], ci[r, c])
        vx, vy = wx - s.centre[0], wy - s.centre[1]
        if math.hypot(vx, vy) < 1e-9:
            continue
        a = math.radians(-s.rotation)
        lx, ly = vx * math.cos(a) - vy * math.sin(a), vx * math.sin(a) + vy * math.cos(a)
        lx, ly = lx / s.xscale, ly / s.yscale  # undo scale and mirroring
        angles.append(math.atan2(ly, lx))
        offsets.append(d * floor.to_m)
    if angles:
        # snap to the nearest quarter turn: symbols are drawn square to their own axes
        mean = math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles))
        snapped = round(math.degrees(mean) / 90.0) * 90.0
        spread = statistics.pstdev([math.degrees(math.atan2(math.sin(a - mean), math.cos(a - mean))) for a in angles])
        style = WallStyle(block, snapped % 360, statistics.median(offsets), len(angles), round(spread, 1), True)
    else:
        style = WallStyle(block, 270.0, 0.15, len(angles), 0.0, False)
    cache[block] = style
    return style


# ======================================================================= rules
STRATEGIES = ("perimeter", "wall", "pair", "corners", "beside_door", "centre", "at")


def parse_rule(rule) -> dict:
    """'perimeter:3', 'wall:longest:2', 'pair:longest', 'beside_door', 'centre', 'at:12.3,110.5' or a dict."""
    if isinstance(rule, dict):
        spec = dict(rule)
    else:
        parts = str(rule).strip().split(":")
        spec = {"strategy": parts[0]}
        rest = parts[1:]
        if spec["strategy"] == "at" and rest:
            x, y = (float(v) for v in rest[0].split(","))
            spec.update(x=x, y=y)
        else:
            for p in rest:
                if re.fullmatch(r"\d+", p):
                    spec["count"] = int(p)
                elif p:
                    spec["wall"] = p
    if spec.get("strategy") not in STRATEGIES:
        raise ValueError(f"unknown strategy {spec.get('strategy')!r}; use one of {', '.join(STRATEGIES)}")
    spec.setdefault("count", 2 if spec["strategy"] == "pair" else 1)
    spec.setdefault("door_clearance", 0.30)
    spec.setdefault("corner_clearance", 0.30)
    spec.setdefault("min_spacing", 0.80)
    spec.setdefault("wall", "longest")
    spec.setdefault("spacing", 2.0)
    spec.setdefault("avoid_facing", ["balcony"])
    if not _valid_wall(spec["wall"]):
        raise ValueError(f"unknown wall {spec['wall']!r}; use one of {', '.join(WALL_CHOICES)} or facing_<room type>")
    if isinstance(spec["avoid_facing"], str):
        spec["avoid_facing"] = [v.strip() for v in spec["avoid_facing"].split(",") if v.strip()]
    return spec


def rule_text(spec: dict) -> str:
    keys = ("strategy", "count", "wall", "spacing")
    return ";".join(f"{k}={'|'.join(spec[k]) if isinstance(spec[k], list) else spec[k]}" for k in keys if k in spec)


def _segment_length(seg) -> float:
    return seg[-1].s - seg[0].s if len(seg) > 1 else 0.0


@dataclass
class WallLine:
    """All usable samples on one straight wall, even where a door or corner splits it."""
    samples: list
    nx: float
    ny: float
    step: float

    @property
    def length(self) -> float:
        return len(self.samples) * self.step

    def t(self, x: float, y: float) -> float:
        return -x * self.ny + y * self.nx

    def facing_share(self, kind: str) -> float:
        return sum(1 for smp in self.samples if smp.facing == kind) / max(1, len(self.samples))


def wall_lines(segs, to_m: float) -> list[WallLine]:
    lines: list[WallLine] = []
    for seg in segs:
        nx, ny = seg[0].nx, seg[0].ny
        off = seg[0].x * nx + seg[0].y * ny
        step = (seg[-1].s - seg[0].s) / (len(seg) - 1) if len(seg) > 1 else 0.05 / to_m
        home = next((ln for ln in lines if ln.nx * nx + ln.ny * ny > 0.98
                     and abs(ln.samples[0].x * nx + ln.samples[0].y * ny - off) < 0.15 / to_m), None)
        if home:
            home.samples += seg
        else:
            lines.append(WallLine(list(seg), nx, ny, step))
    for ln in lines:
        ln.samples.sort(key=lambda smp: ln.t(smp.x, smp.y))
    return lines


WALL_CHOICES = ("longest", "shortest", "long_sides", "bed_head", "media_wall", "opposite_door",
                "north", "south", "east", "west")


def _valid_wall(how) -> bool:
    if isinstance(how, (list, tuple)):
        return bool(how) and all(_valid_wall(h) for h in how)
    return how in WALL_CHOICES or bool(re.fullmatch(r"facing_[a-z_]+", how))


def _pick_walls_ordered(floor, room, walls, lines, how):
    """Try each wall choice in turn (a single choice falls back to the longest wall)."""
    choices = list(how) if isinstance(how, (list, tuple)) else [how, "longest"]
    tried = []
    for choice in choices:
        chosen, anchor, why = _pick_walls(floor, room, walls, lines, choice)
        if chosen:
            if tried:
                why += " (" + "; ".join(tried) + ")"
            return chosen, anchor, why
        tried.append(why)
    return [], None, "; ".join(tried)


def _pick_walls(floor, room, walls: RoomWalls, lines: list[WallLine], how: str):
    """The wall(s) a rule means, plus an optional anchor point to centre devices on.

    Returns (list of wall lines, anchor (x, y) or None, note)."""
    if not lines:
        return [], None, "no usable wall"
    longest = max(lines, key=lambda ln: ln.length)
    to_m = floor.to_m
    if how == "bed_head":
        from .fixtures import SIDE_NORMALS, find_bed
        bed = find_bed(floor, room)
        if bed is None:
            return [], None, "no bed found in the furniture layers"
        bx0, by0, bx1, by1 = bed.bbox

        def gap(ln):  # distance from the bed's outline to the wall
            return min(math.hypot(max(bx0 - smp.x, 0, smp.x - bx1), max(by0 - smp.y, 0, smp.y - by1))
                       for smp in ln.samples)
        size = f"bed {bed.size_m[0]:.1f} x {bed.size_m[1]:.1f} m incl. bedside units"
        cands = [ln for ln in lines if ln.length * to_m >= 0.6] or lines
        head = bed.head_side()
        if head is not None:  # the bedside tables show which side is the head
            hx, hy = SIDE_NORMALS[head]
            behind = [ln for ln in cands if ln.nx * hx + ln.ny * hy > 0.9 and gap(ln) * to_m <= 0.5]
            if behind:
                return [min(behind, key=gap)], bed.centre, f"the bed head wall ({size})"
            # no wall right behind the head (a headboard, or only a dashed beam line): mount at the bed head itself
            return [_bed_head_line(floor, bed, head)], bed.axis_point(head), \
                f"the bed head ({size}; no wall right behind it)"
        return [min(cands, key=gap)], bed.centre, f"the bed head wall ({size})"
    if how == "media_wall":
        from .fixtures import furniture_shapes
        units = [u for u in furniture_shapes(floor, room) if 0.25 <= min(u.size_m) <= 0.7 and max(u.size_m) >= 1.2
                 and u.fill >= 0.8]
        best = None
        for ln in lines:
            for u in units:
                along = (u.size_m[0] if abs(ln.nx) < 0.5 else u.size_m[1])  # unit's length along this wall
                ux0, uy0, ux1, uy1 = u.bbox
                d = min(math.hypot(max(ux0 - smp.x, 0, smp.x - ux1), max(uy0 - smp.y, 0, smp.y - uy1))
                        for smp in ln.samples)
                if d * to_m <= 0.8 and along >= 1.2 and (best is None or along > best[0]):
                    best = (along, ln, u)
        if best is None:
            return [], None, "no media unit found in the furniture layers"
        return [best[1]], best[2].centre, f"the media wall (a {best[0]:.1f} m unit stands against it)"
    if how == "long_sides":
        ranked = sorted(lines, key=lambda ln: ln.length, reverse=True)
        first = ranked[0]
        other = next((ln for ln in ranked[1:] if ln.nx * first.nx + ln.ny * first.ny < -0.9), None)
        return ([first, other] if other else [first]), None, "the two long side walls"
    if how.startswith("facing_"):
        kind = how[len("facing_"):]
        cands = [ln for ln in lines if ln.facing_share(kind) >= 0.5]
        if not cands:
            return [], None, f"no wall backing onto a {kind}"
        return [max(cands, key=lambda ln: ln.length)], None, f"the wall backing onto the {kind}"
    door = max(walls.openings, key=lambda o: o.length, default=None)
    if how in ("opposite_door", "facing_door") and door is not None:
        ln = max(lines, key=lambda ln: -(ln.nx * door.nx + ln.ny * door.ny) + 1e-3 * ln.length)
        return [ln], None, "the wall opposite the main opening"
    if how == "shortest":
        return [min(lines, key=lambda ln: ln.length)], None, "the shortest wall"
    if how in ("north", "south", "east", "west"):
        want = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}[how]
        ln = max(lines, key=lambda ln: ln.nx * want[0] + ln.ny * want[1] + 1e-3 * ln.length)
        return [ln], None, f"the {how} wall"
    return [longest], None, "the longest wall"


def _bed_head_line(floor, bed, head: str) -> WallLine:
    """A stand-in wall along a bed's head edge, for sockets mounted at the bed head itself."""
    from .fixtures import SIDE_NORMALS
    nx, ny = SIDE_NORMALS[head]
    bx0, by0, bx1, by1 = bed.bbox
    lo, hi = bed.edge_span.get(head, (bx0, bx1) if head in ("north", "south") else (by0, by1))
    step = 0.05 / floor.to_m
    n = max(2, int((hi - lo) / step) + 1)
    samples = []
    for i in range(n):
        u = lo + (hi - lo) * i / (n - 1)
        if head in ("north", "south"):
            x, y = u, (by1 if head == "north" else by0)
        else:
            x, y = (bx1 if head == "east" else bx0), u
        samples.append(WallSample(x, y, nx, ny, u - lo, -1, True, 0.0, "bed"))
    line = WallLine(samples, nx, ny, step)
    line.samples.sort(key=lambda smp: line.t(smp.x, smp.y))
    return line


def _spread(points, count: int, min_spacing: float, key=None):
    """Pick `count` samples spread evenly over the usable length of a list of samples.

    `key` gives each sample's position along the wall (arc length by default); jumps
    across doors or other gaps do not count as usable length."""
    if not points or count <= 0:
        return []
    key = key or (lambda smp: smp.s)
    step = min((abs(key(b) - key(a)) for a, b in zip(points, points[1:]) if abs(key(b) - key(a)) > 1e-12),
               default=0.0)
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        jump = abs(key(b) - key(a))
        lengths.append(lengths[-1] + (jump if jump <= 3 * step else step))
    total = lengths[-1]
    chosen = []
    for k in range(count):
        target = (k + 0.5) * total / count if total > 0 else 0.0
        idx = min(range(len(points)), key=lambda i: abs(lengths[i] - target))
        if all(math.hypot(points[idx].x - c.x, points[idx].y - c.y) >= min_spacing for c in chosen):
            chosen.append(points[idx])
    return chosen


def propose(floor, room: Room, dt, spec: dict, existing: list[tuple[float, float]] | None = None) -> list[dict]:
    """Target points and rotations for `spec`, without writing anything."""
    to_m = floor.to_m
    strategy, count = spec["strategy"], int(spec["count"])
    min_sp = spec["min_spacing"] / to_m
    if dt.mount == "ceiling" or strategy == "centre":
        if strategy == "at":
            return [{"target": (spec["x"], spec["y"]), "rotation": 0.0, "note": "explicit point"}]
        cx, cy = room_centre(room)
        return [{"target": (cx, cy), "rotation": 0.0, "note": "room centre (pole of inaccessibility)"}]
    walls = RoomWalls(floor, room)
    style = learn_wall_style(floor, dt.source_block)
    segs = walls.wall_segments(spec["door_clearance"], spec["corner_clearance"], tuple(spec["avoid_facing"]))
    usable = [s for seg in segs for s in seg]
    if existing:
        usable = [s for s in usable if all(math.hypot(s.x - ex, s.y - ey) >= min_sp for ex, ey in existing)]
        segs = [[s for s in seg if s in usable] for seg in segs]
        segs = [seg for seg in segs if seg]
    picks: list[WallSample] = []
    note = ""
    if strategy == "perimeter":
        picks = _spread(usable, count, min_sp)
        note = "spread evenly along the wall-backed perimeter"
    elif strategy in ("wall", "pair"):
        lines = wall_lines(segs, to_m)
        chosen, anchor, why = _pick_walls_ordered(floor, room, walls, lines, spec["wall"])
        if chosen and strategy == "pair":
            ln = chosen[0]
            ts = [ln.t(smp.x, smp.y) for smp in ln.samples]
            mid = ln.t(*anchor) if anchor is not None else (min(ts) + max(ts)) / 2
            half = spec["spacing"] / 2 / to_m
            for want in (mid - half, mid + half):
                best = min(ln.samples, key=lambda smp: abs(ln.t(smp.x, smp.y) - want))
                if best not in picks:
                    picks.append(best)
            note = f"pair {spec['spacing']} m apart on {why}"
        elif chosen:
            share = [count // len(chosen) + (1 if i < count % len(chosen) else 0) for i in range(len(chosen))]
            for ln, n in zip(chosen, share):
                picks += _spread(ln.samples, n, min_sp, key=lambda smp, ln=ln: ln.t(smp.x, smp.y))
            note = f"spread along {why}"
        else:
            note = why
    elif strategy == "corners":
        ranked = []
        for seg in segs:
            ranked += [seg[0], seg[-1]]
        ranked.sort(key=lambda smp: -_segment_length(next(sg for sg in segs if smp in sg)))
        for smp in ranked:
            if len(picks) >= count:
                break
            if all(math.hypot(smp.x - p.x, smp.y - p.y) >= min_sp for p in picks):
                picks.append(smp)
        note = "next to room corners, longest walls first"
    elif strategy == "beside_door":
        door = max(walls.openings, key=lambda o: o.length, default=None)
        if door is not None:
            picks = sorted(usable, key=lambda smp: _arc_dist(smp.s, door, walls.perimeter))[:1]
        note = "beside the widest opening"
    elif strategy == "at":
        p = Point(spec["x"], spec["y"])
        picks = sorted(usable, key=lambda smp: p.distance(Point(smp.x, smp.y)))[:1]
        note = "explicit point, snapped to the nearest usable wall"
    out = []
    face = style.face_offset_m / to_m
    lines = wall_lines(segs, to_m) if picks else []
    for smp in picks:
        line = next((ln for ln in lines if smp in ln.samples), None)
        if line is not None:  # one face line per wall, so symbols on a wall line up exactly
            off = statistics.median(q.x * line.nx + q.y * line.ny + q.wall_dist for q in line.samples)
            t = line.t(smp.x, smp.y)
            wx, wy = line.nx * off - line.ny * t, line.ny * off + line.nx * t
        else:
            wx, wy = smp.x + smp.nx * smp.wall_dist, smp.y + smp.ny * smp.wall_dist  # wall face
        target = (wx - smp.nx * face, wy - smp.ny * face)
        out.append({"target": target, "rotation": style.rotation_for(smp.nx, smp.ny), "note": note,
                    "wall_normal": [round(smp.nx, 3), round(smp.ny, 3)]})
    return out


def place_device(floor, overlay: Overlay, room_ref: str, device_type: str, rule, dry_run: bool = False) -> dict:
    """Place `device_type` in a room by rule, writing to the overlay unless `dry_run`."""
    room = floor.rooms.get(room_ref)
    dt = floor.catalogue.device_type(device_type)
    spec = parse_rule(rule)
    existing = [tuple(d["centre"]) for d in overlay.devices()
                if d.get("room") == room.room_id and "centre" in d]
    props = propose(floor, room, dt, spec, existing)
    result = {"room": room.room_id, "room_name": room.name, "unit": room.unit, "device_type": dt.key,
              "rule": rule_text(spec), "requested": int(spec["count"]), "proposed": len(props), "dry_run": dry_run,
              "placed": []}
    if len(props) < int(spec["count"]):
        result["warning"] = f"only {len(props)} of {spec['count']} positions fit the rule in {room.room_id} {room.name}"
    for p in props:
        if dry_run:
            result["placed"].append({"target": [round(v, 4) for v in p["target"]], "rotation": round(p["rotation"], 1),
                                     "note": p["note"]})
        else:
            rec = overlay.add_device(dt, p["target"], p["rotation"], room.room_id, rule_text(spec))
            rec["note"] = p["note"]
            result["placed"].append(rec)
    if not dry_run and props:
        overlay.save()
    return result


# ======================================================================= mirrored units
def mirror_unit(floor, overlay: Overlay, from_unit: str, to_unit: str, types: set[str] | None = None,
                dry_run: bool = False) -> dict:
    """Copy the overlay devices of one apartment unit onto its mirror-image unit.

    The pilot floors repeat one apartment type either side of the core, so a unit the engineer has
    reviewed can be copied instead of re-derived. Positions are mirrored across the axis
    found by the room finder; each symbol keeps its own (unmirrored) graphics and is
    turned to face its mirrored wall."""
    m = floor.mirror
    if m is None:
        raise ValueError("no mirror axis found on this floor")
    if {from_unit, to_unit} != set(m.sides):
        raise ValueError(f"units must be the two mirrored sides {m.sides}")
    out = {"from": from_unit, "to": to_unit, "axis": m.axis, "axis_value": round(m.value, 3), "dry_run": dry_run,
           "copied": [], "skipped": []}
    for d in overlay.devices():
        room = floor.rooms.get(d["room"]) if d.get("room") else None
        if room is None or room.unit != from_unit or (types and d.get("type") not in types):
            continue
        partner = m.partner(room.room_id)
        x, y = m.mirror(*d["centre"])
        target_room = floor.rooms.room_at(x, y, max_dist_m=0.6)
        if partner is None or target_room is None or target_room.room_id != partner:
            out["skipped"].append({"handle": d["handle"], "reason": "mirror image falls outside the partner room"})
            continue
        dt = floor.catalogue.device_type(d["type"])
        style = learn_wall_style(floor, dt.source_block)
        back = float(d["rotation"]) + style.back_angle_local
        back_m = (180.0 - back) if m.axis == "x" else -back
        rot = (back_m - style.back_angle_local) % 360
        if dry_run:
            out["copied"].append({"source": d["handle"], "type": d["type"], "room": partner,
                                  "target": [round(x, 4), round(y, 4)], "rotation": round(rot, 1)})
        else:
            rec = overlay.add_device(dt, (x, y), rot, partner, f"mirror of {d['handle']} ({from_unit}->{to_unit})")
            rec["source"] = d["handle"]
            out["copied"].append(rec)
    if not dry_run and out["copied"]:
        overlay.save()
    return out


# ======================================================================= circuits
def _wall_crossings(floor, a, b) -> int:
    rs = floor.rooms
    n = max(2, int(math.dist(a, b) / rs.res))
    crossings, inside = 0, False
    for k in range(n + 1):
        t = k / n
        r, c = rs.px(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        w = bool(rs.inside_raster(r, c) and rs.wall[r, c])
        if w and not inside:
            crossings += 1
        inside = w
    return crossings


def _resolve_point(floor, overlay: Overlay, ref: str) -> tuple[tuple[float, float], str]:
    for d in overlay.devices():
        if d["handle"] == ref:
            return tuple(d["centre"]), "overlay"
    s = floor.symbol(ref)
    if s is not None:
        return s.centre, "base"
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*", ref)
    if m:
        return (float(m.group(1)), float(m.group(2))), "point"
    raise KeyError(f"{ref!r} is neither an overlay device, a base symbol handle nor an 'x,y' point")


def circuit_run(floor, overlay: Overlay, devices: list[str], circuit_id: str, start: str | None = None,
                ring: bool = False, kind: str = "power", db: str | None = None, dry_run: bool = False) -> dict:
    """Draw an orthogonal run from `start` (e.g. the consumer unit) through `devices` in nearest-next order."""
    if not re.fullmatch(r"[A-Za-z0-9./_-]{1,16}", circuit_id):
        raise ValueError("circuit id must be 1-16 letters, digits or . / _ -")
    layer = {"power": "NFE-E-POWER-CCT", "lighting": "NFE-E-LIGHT-CCT"}[kind]
    pts = {ref: _resolve_point(floor, overlay, ref)[0] for ref in devices}
    order = []
    if start:
        cur = _resolve_point(floor, overlay, start)[0]
        path = [cur]
    else:
        first = devices[0]
        cur = pts[first]
        order.append(first)
        path = [cur]
    left = [d for d in devices if d not in order]
    while left:
        nxt = min(left, key=lambda d: math.dist(cur, pts[d]))
        order.append(nxt)
        left.remove(nxt)
        cur = pts[nxt]
        path.append(cur)
    if ring and len(path) > 2:
        path.append(path[0])
    route = [path[0]]
    crossings = 0
    for a, b in zip(path, path[1:]):
        options = [((b[0], a[1]),), ((a[0], b[1]),)]
        best = min(options, key=lambda o: _wall_crossings(floor, a, o[0]) + _wall_crossings(floor, o[0], b))
        crossings += _wall_crossings(floor, a, best[0]) + _wall_crossings(floor, best[0], b)
        route += [best[0], b]
    clean = [route[0]]
    for p in route[1:]:
        if math.dist(p, clean[-1]) > 1e-9:
            clean.append(p)
    length = sum(math.dist(a, b) for a, b in zip(clean, clean[1:])) * floor.to_m
    res = {"circuit": circuit_id, "kind": kind, "order": order, "points": [[round(x, 4), round(y, 4)] for x, y in clean],
           "length_m": round(length, 2), "wall_crossings": crossings, "dry_run": dry_run}
    if start and devices:
        r0 = floor.rooms.room_at(*path[0], max_dist_m=1.0)
        r1 = floor.rooms.room_at(*pts[devices[0]], max_dist_m=1.0)
        if r0 and r1 and r0.unit and r1.unit and r0.unit != r1.unit:
            res["warning"] = f"start is in unit {r0.unit} but the devices are in unit {r1.unit}"
    if dry_run:
        return res
    res["handle"] = overlay.add_run(clean, layer, {"circuit": circuit_id, "db": db, "devices": ",".join(order),
                                                    "length_m": round(length, 2)})
    seg = max(zip(clean, clean[1:]), key=lambda ab: math.dist(*ab))
    mid = ((seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2)
    res["label"] = overlay.add_label(circuit_id if not db else f"{db}/{circuit_id}", mid, fields={"circuit": circuit_id})
    for ref in order:
        overlay.set_attrib(ref, "NFE_CIRCUIT", circuit_id)
        if db:
            overlay.set_attrib(ref, "NFE_DB", db)
    overlay.save()
    return res
