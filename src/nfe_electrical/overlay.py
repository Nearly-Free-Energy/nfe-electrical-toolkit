"""The overlay DXF: NFE's own work, kept apart from the architect's drawing.

Everything the toolkit writes goes into `<floor>/overlays/<name>.dxf`, on NFE layers
only, in the base drawing's coordinates and real units, so the file can be attached
to the untouched base as an xref (or opened next to it) and a bad run costs only a
deleted file. Symbols are copies of NFE's existing blocks, renamed `NFE_<block>`, with
their geometry moved to layer 0 so the NFE layer controls colour and visibility.
By default each copy is also re-centred on its drawn geometry (the library clean-up the
research recommends); placement corrects for any base-point offset either way.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.addons import Importer
from ezdxf.math import Matrix44, Vec3

from . import __version__
from .catalogue import DeviceType, strip_bound_prefix
from .convert import find_tool, replace_file
from .workspace import check_id

APPID = "NFE_TOOLKIT"


def overlay_block_name(source: str) -> str:
    return "NFE_" + re.sub(r"[^A-Za-z0-9_-]+", "_", strip_bound_prefix(source)).strip("_")


class Overlay:
    def __init__(self, floor, name: str, path: Path, doc, recentre_blocks: bool = True):
        self.floor = floor
        self.name = name
        self.path = path
        self.doc = doc
        self.msp = doc.modelspace()
        self.recentre_blocks = recentre_blocks
        self.save_failed = False  # True when the last save could not replace the file (memory and file differ)
        self._geom: dict[str, tuple[float, float]] = {}

    # ------------------------------------------------------------ files
    @classmethod
    def open(cls, floor, name: str = "overlay", recentre_blocks: bool = True, fresh: bool = False) -> "Overlay":
        check_id(name, "overlay name")
        path = floor.workspace.floor_dir(floor.floor_id, "overlays") / f"{name}.dxf"
        if path.exists() and not fresh:
            doc = ezdxf.readfile(str(path))
        else:
            doc = cls._new_doc(floor)
        ov = cls(floor, name, path, doc, recentre_blocks)
        ov._ensure_layers()
        return ov

    @staticmethod
    def _new_doc(floor):
        doc = ezdxf.new("R2018", setup=True)
        doc.header["$INSUNITS"] = floor.units.insunits
        doc.header["$MEASUREMENT"] = 1
        doc.appids.add(APPID)
        cv = doc.header.custom_vars
        cv.append("NFE_TOOLKIT_VERSION", __version__)
        cv.append("NFE_FLOOR", floor.floor_id)
        cv.append("NFE_STANDARD", floor.standard)
        cv.append("NFE_SOURCE_DRAWING", floor.source.name)
        if floor.conversion:
            cv.append("NFE_SOURCE_SHA256", floor.conversion.sha256)
        cv.append("NFE_UNITS", f"{floor.units.detected} (declared {floor.units.declared})")
        cv.append("NFE_PLOTTED_BASE", floor.base.plotted)
        cv.append("NFE_NOTE", "Overlay only. Attach to the untouched architect's drawing at 0,0,0, scale 1.")
        return doc

    def _ensure_layers(self) -> None:
        for name, spec in self.floor.catalogue.nfe_layers.items():
            if name not in self.doc.layers:
                layer = self.doc.layers.add(name, color=int(spec.get("color", 7)))
                layer.description = spec.get("description", "")
                if spec.get("plot") is False:
                    layer.dxf.plot = 0
        if APPID not in self.doc.appids:
            self.doc.appids.add(APPID)

    def save(self) -> str:
        self._purge_foreign_layers()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.dxf")
        self.doc.saveas(str(tmp))
        try:
            replace_file(tmp, self.path)  # on Windows this stops with a clear message while a CAD program has it open
        except PermissionError:
            self.save_failed = True
            raise
        self.save_failed = False
        return str(self.path)

    def _purge_foreign_layers(self) -> None:
        used = {e.dxf.get("layer", "0") for e in self.msp}
        for blk in self.doc.blocks:
            used |= {e.dxf.get("layer", "0") for e in blk}
        keep = set(self.floor.catalogue.nfe_layers) | {"0", "Defpoints"}
        for layer in list(self.doc.layers):
            name = layer.dxf.name
            if name not in keep and name not in used:
                self.doc.layers.remove(name)

    # ------------------------------------------------------------ blocks
    def ensure_block(self, source: str) -> str:
        """Copy an NFE symbol from the base drawing into the overlay (once) as NFE_<name>."""
        target = overlay_block_name(source)
        if target in self.doc.blocks:
            return target
        resolved = self.floor.resolve_block(source)
        if resolved is None:
            raise KeyError(f"block {source!r} is not defined in {self.floor.source.name}")
        source = resolved
        imp = Importer(self.floor.doc, self.doc)
        imp.import_block(source, rename=False)
        imp.finalize()
        self.doc.blocks.rename_block(source, target)
        blk = self.doc.blocks.get(target)
        geom = [e for e in blk if e.dxftype() != "ATTDEF"]
        ext = bbox.extents(geom, fast=True)
        cx, cy = (ext.center.x, ext.center.y) if ext.has_data else (0.0, 0.0)
        size = max(ext.size.x, ext.size.y) if ext.has_data else 1.0
        for e in blk:
            e.dxf.layer = "0"
            if e.dxf.hasattr("color"):
                e.dxf.color = 256  # BYLAYER: on layer 0 inside a block, the insert's layer decides
            if self.recentre_blocks:
                e.translate(-cx, -cy, 0)
        tags = {e.dxf.tag for e in blk if e.dxftype() == "ATTDEF"}
        for k, tag in enumerate(self.floor.catalogue.nfe_attributes):
            if tag not in tags:
                blk.add_attdef(tag, insert=(0, -k * size * 0.12), text="",
                               dxfattribs={"height": size * 0.1, "flags": 1, "layer": "0"})
        blk.block.description = f"NFE overlay copy of {source!r} from {self.floor.source.name}"
        return target

    def block_centre(self, name: str) -> tuple[float, float]:
        """Drawn centre of an overlay block, relative to its base point (block units)."""
        if name not in self._geom:
            blk = self.doc.blocks.get(name)
            ext = bbox.extents([e for e in blk if e.dxftype() != "ATTDEF"], fast=True)
            bp = blk.block.dxf.base_point
            self._geom[name] = (ext.center.x - bp.x, ext.center.y - bp.y)
        return self._geom[name]

    # ------------------------------------------------------------ writing
    def _xdata(self, entity, fields: dict) -> None:
        tags = [(1000, f"{k}={v}"[:250]) for k, v in fields.items() if v not in (None, "")]
        entity.set_xdata(APPID, tags)

    def add_device(self, dt: DeviceType, target: tuple[float, float], rotation_deg: float, room_id: str | None,
                   rule: str, scale: float | None = None) -> dict:
        """Insert a device so that its *drawn centre* lands on `target`, whatever its base point."""
        name = self.ensure_block(dt.source_block)
        s = scale or self.floor_scale(dt.source_block)
        cx, cy = self.block_centre(name)
        a = math.radians(rotation_deg)
        ox, oy = s * cx, s * cy  # base-point offset, scaled then rotated into the drawing
        insert = (target[0] - (ox * math.cos(a) - oy * math.sin(a)), target[1] - (ox * math.sin(a) + oy * math.cos(a)))
        ref = self.msp.add_blockref(name, insert, dxfattribs={"layer": dt.layer, "xscale": s, "yscale": s,
                                                               "rotation": rotation_deg % 360})
        values = dict(dt.attribs)
        values.update({"NFE_TYPE": dt.key, "NFE_ROOM": room_id or "", "NFE_RULE": rule[:120],
                       "NFE_MOUNT_MM": "" if dt.height_mm is None else str(dt.height_mm)})
        ref.add_auto_attribs(values)
        self._xdata(ref, {"kind": "device", "type": dt.key, "room": room_id, "rule": rule, "source_block": dt.source_block,
                          "by": f"nfe-electrical-toolkit {__version__}",
                          "at": _dt.datetime.now().isoformat(timespec="seconds")})
        err = self._centre_error(ref, target, a) * self.floor.to_m
        return {"handle": ref.dxf.handle, "type": dt.key, "block": name, "layer": dt.layer, "room": room_id,
                "target": [round(target[0], 4), round(target[1], 4)], "insert": [round(insert[0], 4), round(insert[1], 4)],
                "rotation": round(rotation_deg % 360, 2), "scale": s, "centre_error_mm": round(err * 1000, 2),
                "base_point_offset_m": round(math.hypot(ox, oy) * self.floor.to_m, 3)}

    @staticmethod
    def _centre_error(ref, target, angle_rad: float) -> float:
        """Independent check of where the symbol is drawn: bounding-box centre of the insert's
        exploded geometry, measured with the rotation undone about the target."""
        ents = [v for v in ref.virtual_entities() if v.dxftype() != "ATTDEF"]
        if abs(math.sin(2 * angle_rad)) > 1e-9:  # not a quarter turn: un-rotate before taking the box
            back = Matrix44.chain(Matrix44.translate(-target[0], -target[1], 0), Matrix44.z_rotate(-angle_rad),
                                  Matrix44.translate(target[0], target[1], 0))
            for v in ents:
                try:
                    v.transform(back)
                except Exception:
                    pass
        c = bbox.extents(ents, fast=True).center
        return math.dist((c.x, c.y), target)

    def floor_scale(self, source_block: str) -> float:
        """The insert scale the engineers use for this block on this floor (blocks are drawn in mm)."""
        source_block = self.floor.resolve_block(source_block) or source_block
        for row in self.floor.block_audit.blocks:
            if row["block"] == source_block:
                return float(row["typical_insert_scale"])
        return 0.001 / self.floor.to_m

    def add_run(self, points: list[tuple[float, float]], layer: str, fields: dict) -> str:
        pl = self.msp.add_lwpolyline(points, dxfattribs={"layer": layer})
        self._xdata(pl, {"kind": "run", **fields})
        return pl.dxf.handle

    def add_label(self, text: str, at: tuple[float, float], height_m: float = 0.15, fields: dict | None = None) -> str:
        t = self.msp.add_text(text, height=height_m / self.floor.to_m, dxfattribs={"layer": "NFE-E-ANNO"})
        t.set_placement(at)
        self._xdata(t, {"kind": "label", **(fields or {})})
        return t.dxf.handle

    def set_attrib(self, handle: str, tag: str, value: str) -> bool:
        e = self.doc.entitydb.get(handle)
        if e is None or e.dxftype() != "INSERT":
            return False
        for a in e.attribs:
            if a.dxf.tag == tag:
                a.dxf.text = value
                return True
        return False

    def remove(self, handles: list[str]) -> list[str]:
        gone = []
        for h in handles:
            e = self.doc.entitydb.get(h)
            if e is not None and e.dxf.owner == self.msp.layout_key:
                self.msp.delete_entity(e)
                gone.append(h)
        return gone

    # ------------------------------------------------------------ reading
    def items(self) -> list[dict]:
        out = []
        for e in self.msp:
            if not e.has_xdata(APPID):
                continue
            fields = dict(str(v).split("=", 1) for c, v in e.get_xdata(APPID) if c == 1000 and "=" in str(v))
            rec = {"handle": e.dxf.handle, "entity": e.dxftype(), "layer": e.dxf.layer, **fields}
            if e.dxftype() == "INSERT":
                name = e.dxf.name
                cx, cy = self.block_centre(name)
                c = e.matrix44().transform(Vec3(cx + self.doc.blocks.get(name).block.dxf.base_point.x,
                                                cy + self.doc.blocks.get(name).block.dxf.base_point.y, 0))
                rec.update({"block": name, "centre": [round(c.x, 4), round(c.y, 4)],
                            "rotation": round(e.dxf.rotation, 2),
                            "attribs": {a.dxf.tag: a.dxf.text for a in e.attribs if a.dxf.text}})
            elif e.dxftype() == "LWPOLYLINE":
                rec["points"] = [[round(p[0], 4), round(p[1], 4)] for p in e.get_points("xy")]
            out.append(rec)
        return out

    def devices(self) -> list[dict]:
        return [i for i in self.items() if i.get("kind") == "device"]

    # ------------------------------------------------------------ checks
    def validate(self, libredwg: bool = True) -> dict:
        """Re-read the saved overlay strictly with ezdxf and (optionally) LibreDWG, and compare counts."""
        self.save()
        nfe = set(self.floor.catalogue.nfe_layers)
        src = _counts(self.doc)
        doc2 = ezdxf.readfile(str(self.path))
        aud = doc2.audit()
        back = _counts(doc2)
        msp_layers = Counter(e.dxf.get("layer", "0") for e in doc2.modelspace())
        res = {
            "path": str(self.path), "dxfversion": doc2.dxfversion, "insunits": doc2.header.get("$INSUNITS"),
            "ezdxf": {"audit_errors": len(aud.errors), "audit_fixes": len(aud.fixes), "counts_match": src == back,
                      "counts": back},
            "only_nfe_layers": all(ly in nfe for ly in msp_layers), "model_space_layers": dict(msp_layers),
            "table_layers": sorted(l.dxf.name for l in doc2.layers),
        }
        if libredwg:
            res["libredwg"] = self._libredwg_roundtrip(back)
        return res

    def _libredwg_roundtrip(self, expected: dict) -> dict:
        """dxf2dwg (R2000) -> dwgread -> dwg2dxf -> ezdxf: a smoke test that LibreDWG reads the overlay.

        The DWG is a throwaway test file in a temporary directory, not a deliverable."""
        try:
            dxf2dwg, dwgread, dwg2dxf = find_tool("dxf2dwg"), find_tool("dwgread"), find_tool("dwg2dxf")
        except Exception as exc:  # LibreDWG not installed
            return {"ran": False, "reason": str(exc)}
        out = {"ran": True}
        # ignore_cleanup_errors: on Windows a virus scanner may still be holding the test DWG
        with tempfile.TemporaryDirectory(prefix="nfe-overlay-check-", ignore_cleanup_errors=True) as td:
            tdir = Path(td)
            src = tdir / "overlay.dxf"
            src.write_bytes(self.path.read_bytes())
            dwg, back = tdir / "overlay_r2000.dwg", tdir / "overlay_back.dxf"
            steps = [("dxf2dwg", [dxf2dwg, "-y", "-o", str(dwg), str(src)]),
                     ("dwgread", [dwgread, str(dwg)]),
                     ("dwg2dxf", [dwg2dxf, "-y", "-o", str(back), str(dwg)])]
            for label, cmd in steps:
                try:
                    p = subprocess.run(cmd, cwd=td, capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    out[label] = {"exit": "timeout"}
                    return out
                log = (p.stdout + p.stderr).decode("utf-8", "replace").splitlines()
                out[label] = {"exit": p.returncode, "errors": sum(1 for ln in log if ln.startswith("ERROR")),
                              "warnings": sum(1 for ln in log if ln.startswith("Warning"))}
                if p.returncode != 0 and label != "dwgread":
                    return out
            try:
                d3 = ezdxf.readfile(str(back))
                got = _counts(d3)
                out["reread_counts"] = got
                # LibreDWG writes attributes out as separate ATTRIB records; compare the rest.
                strip = lambda c: {k: v for k, v in c.items() if k not in ("ATTRIB", "SEQEND")}
                out["model_space_entities_match"] = strip(got["model_space"]) == strip(expected["model_space"])
                out["inserts_match"] = got["inserts"] == expected["inserts"]
                out["attribs_match"] = got["attribs"] == expected["attribs"]
            except Exception as exc:
                out["reread_error"] = repr(exc)[:300]
        return out

    def summary(self) -> dict:
        items = self.items()
        return {"overlay": self.name, "path": str(self.path),
                "devices": dict(Counter(i.get("type") for i in items if i.get("kind") == "device").most_common()),
                "runs": sum(1 for i in items if i.get("kind") == "run"),
                "labels": sum(1 for i in items if i.get("kind") == "label")}


def _counts(doc) -> dict:
    msp = doc.modelspace()
    return {
        "model_space": dict(Counter(e.dxftype() for e in msp)),
        "inserts": dict(Counter(e.dxf.name for e in msp.query("INSERT"))),
        "attribs": sum(len(e.attribs) for e in msp.query("INSERT")),
        "blocks": sorted(b.name for b in doc.blocks if not b.name.startswith("*")),
    }
