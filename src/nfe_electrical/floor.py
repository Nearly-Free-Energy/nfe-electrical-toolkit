"""A floor: one received drawing, converted, loaded and normalised.

`Floor.open()` is the reader/normaliser in one call:
  1. copy the drawing into the workspace and convert it with LibreDWG `dwg2dxf`
  2. load the DXF with ezdxf (strict, falling back to recover mode)
  3. rebuild sheet views from viewport geometry (viewport flags arrive zeroed)
  4. index electrical symbols by block name and drawn graphic centre
  5. pick the plan window (main viewport showing the most symbols)
  6. detect the real units, whatever $INSUNITS says
  7. pick the plotted architectural base copy
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import recover

from . import __version__
from .basepick import BaseReport, pick_base
from .catalogue import Catalogue, default_catalogue
from .convert import Conversion, convert_dwg, sha256_file
from .sheets import SheetView, ViewportView, rebuild_sheet_views
from .symbols import BlockAudit, BlockGeometryCache, Symbol, block_offset_audit, index_symbols
from .units import UnitsReport, detect_units
from .workspace import Workspace, check_id, slug

DISCIPLINE_GROUPS = {"lighting": ("lighting", "switch"), "small_power": ("power", "board"), "ict_fire": ("fire", "ict")}


class Floor:
    def __init__(self, floor_id: str, workspace: Workspace, source: Path, dxf_path: Path,
                 conversion: Conversion | None, catalogue: Catalogue, standard: str | None = None):
        from .rules import normalise_standard
        self.floor_id = check_id(floor_id, "floor id")
        self.standard = normalise_standard(standard)  # the job's governing standard
        self.workspace = workspace
        self.source = source
        self.dxf_path = dxf_path
        self.conversion = conversion
        self.catalogue = catalogue
        self.timings: dict[str, float] = {}
        self.load_report: dict = {}
        self._rooms = None
        self._mirror = None
        t0 = time.perf_counter()
        self.doc = self._load()
        self.timings["load_dxf_s"] = round(time.perf_counter() - t0, 2)
        self.msp = self.doc.modelspace()
        self._analyse()

    # ------------------------------------------------------------ opening
    @classmethod
    def open(cls, drawing: str | Path, workdir: str | Path, floor_id: str | None = None,
             catalogue: Catalogue | None = None, standard: str | None = None) -> "Floor":
        ws = Workspace(workdir)
        drawing = Path(drawing).expanduser()
        fid = floor_id or slug(drawing.stem)
        local = ws.import_drawing(drawing, fid)
        conv = None
        t0 = time.perf_counter()
        if local.suffix.lower() == ".dwg":
            conv = convert_dwg(local, ws.cache_dir)
            dxf = Path(conv.dxf)
        else:
            dxf = local
        floor = cls(fid, ws, local, dxf, conv, catalogue or default_catalogue(), standard=standard)
        floor.timings["convert_dwg_s"] = conv.seconds if conv and not conv.cached else 0.0
        floor.timings["open_total_s"] = round(time.perf_counter() - t0, 2)
        return floor

    def _load(self):
        try:
            doc = ezdxf.readfile(str(self.dxf_path))
            auditor = doc.audit()
            mode = "strict"
        except ezdxf.DXFStructureError:
            doc, auditor = recover.readfile(str(self.dxf_path))
            mode = "recover"
        self.load_report = {"mode": mode, "audit_errors": len(auditor.errors), "audit_fixes": len(auditor.fixes),
                            "dxfversion": doc.dxfversion}
        return doc

    # ------------------------------------------------------------ analysis
    def _analyse(self) -> None:
        t0 = time.perf_counter()
        self.sheets: list[SheetView] = rebuild_sheet_views(self.doc)
        self.geometry = BlockGeometryCache(self.doc)
        self.symbols: list[Symbol] = index_symbols(self.doc, self.catalogue, self.geometry)
        self.timings["index_symbols_s"] = round(time.perf_counter() - t0, 2)

        t1 = time.perf_counter()
        self.plan_sheet, self.plan_viewport = self._pick_plan()
        self.plan_window = self.plan_viewport.window
        for s in self.symbols:
            s.on_plan = self.plan_viewport.contains(*s.centre)
        frozen = set(self.plan_viewport.frozen_layers)
        self.layers_on = {ly.dxf.name for ly in self.doc.layers if ly.is_on() and not ly.is_frozen()}
        self.visible_layers = self.layers_on - frozen

        label_heights = []
        for t in self.msp.query("TEXT MTEXT"):
            if t.dxf.get("layer") in self.catalogue.label_layers and self.plan_viewport.contains(t.dxf.insert.x, t.dxf.insert.y):
                h = t.dxf.char_height if t.dxftype() == "MTEXT" else t.dxf.height
                if h > 0:
                    label_heights.append(h)
        sizes = []
        for s in self.symbols:
            g = self.geometry.get(s.block)
            if s.on_plan and g and s.known:
                sizes.append(max(g.size) * abs(s.xscale))
        self.units: UnitsReport = detect_units(
            int(self.doc.header.get("$INSUNITS", 0)), self.plan_viewport.scale, self.plan_sheet.paper_unit_mm,
            sizes, label_heights, self.plan_window)
        self.to_m = self.units.to_m

        self.base: BaseReport = pick_base(self.doc, self.catalogue, self.plan_window, self.visible_layers)
        self.block_audit: BlockAudit = block_offset_audit(self.symbols, self.geometry, self.to_m)
        self._sheet_disciplines()
        self.timings["normalise_s"] = round(time.perf_counter() - t1, 2)

    def _pick_plan(self) -> tuple[SheetView, ViewportView]:
        best = None
        for sh in self.sheets:
            vp = sh.main
            if vp is None:
                continue
            n = sum(1 for s in self.symbols if vp.contains(*s.centre))
            key = (n, vp.paper_area)
            if best is None or key > best[0]:
                best = (key, sh, vp)
        if best is None:
            raise ValueError("no paper-space sheet with a usable viewport found")
        return best[1], best[2]

    def _sheet_disciplines(self) -> None:
        for sh in self.sheets:
            vp = sh.main
            if vp is None:
                sh.discipline = "no viewport"
                continue
            frozen = set(vp.frozen_layers)
            cats = Counter(s.category for s in self.symbols
                           if vp.contains(*s.centre) and s.layer in self.layers_on and s.layer not in frozen)
            sh.visible_categories = dict(cats.most_common())
            score = {d: sum(cats.get(c, 0) for c in group) for d, group in DISCIPLINE_GROUPS.items()}
            total = sum(score.values())
            if total == 0:
                sh.discipline = "empty"
                continue
            top = max(score, key=score.get)
            sh.discipline = top if score[top] >= 0.6 * total else "mixed"

    # ------------------------------------------------------------ helpers
    def m(self, value_m: float) -> float:
        """Metres -> drawing units."""
        return value_m / self.to_m

    def symbols_on_plan(self, categories: set[str] | None = None, types: set[str] | None = None) -> list[Symbol]:
        return [s for s in self.symbols if s.on_plan and (categories is None or s.category in categories)
                and (types is None or s.device_type in types)]

    def resolve_block(self, name: str) -> str | None:
        """The drawing's own name for a catalogue block, ignoring bound-xref prefixes; the most-used
        one wins if several blocks strip to the same name."""
        if name in self.doc.blocks:
            return name
        from .catalogue import strip_bound_prefix
        cands = [b.name for b in self.doc.blocks if strip_bound_prefix(b.name) == name]
        if not cands:
            return None
        used = {c: sum(1 for s in self.symbols if s.block == c) for c in cands}
        return max(cands, key=lambda c: used[c])

    def symbol(self, handle: str) -> Symbol | None:
        for s in self.symbols:
            if s.handle == handle:
                return s
        return None

    @property
    def out_dir(self) -> Path:
        return self.workspace.floor_dir(self.floor_id)

    @property
    def rooms(self):
        if self._rooms is None:
            from .apartments import detect_mirrored_units
            from .rooms import find_rooms
            t0 = time.perf_counter()
            self._rooms = find_rooms(self)
            self._mirror = detect_mirrored_units(self._rooms.rooms, self.to_m)
            self.timings["find_rooms_s"] = round(time.perf_counter() - t0, 2)
            self._rooms.assign_symbols(self.symbols)
        return self._rooms

    @property
    def mirror(self):
        """Mirrored apartment units (None if the floor has no clear mirror axis)."""
        self.rooms
        return self._mirror

    # ------------------------------------------------------------ reports
    def summary(self) -> dict:
        on = self.symbols_on_plan()
        ratio = self.plan_sheet.scale_ratio(self.to_m)
        return {
            "floor_id": self.floor_id, "toolkit_version": __version__, "governing_standard": self.standard,
            "source": {"drawing": self.source.name, "sha256": sha256_file(self.source),
                       "conversion": self.conversion.as_dict() if self.conversion else None,
                       "load": self.load_report},
            "units": self.units.as_dict(),
            "plan": {"sheet": self.plan_sheet.layout, "viewport": self.plan_viewport.handle,
                     "window": [round(v, 3) for v in self.plan_window],
                     "window_size_m": [round((self.plan_window[2] - self.plan_window[0]) * self.to_m, 2),
                                       round((self.plan_window[3] - self.plan_window[1]) * self.to_m, 2)],
                     "plot_scale": f"1:{ratio:.0f}" if ratio else None},
            "sheets": [s.as_dict() for s in self.sheets],
            "base": self.base.as_dict(),
            "symbols": {"total_indexed": len(self.symbols), "on_plan": len(on),
                        "by_category": dict(Counter(s.category for s in on).most_common()),
                        "by_type": dict(Counter(s.device_type for s in on).most_common()),
                        "unknown_blocks": sorted({s.block for s in on if not s.known})},
            "block_offsets": {"far_basepoint_blocks": self.block_audit.far_blocks,
                              "inserts_affected": self.block_audit.far_inserts,
                              "electrical_blocks": self.block_audit.total_blocks,
                              "electrical_inserts": self.block_audit.total_inserts},
            "timings": self.timings,
        }

    def write_reports(self) -> dict[str, str]:
        out = self.out_dir
        paths = {"floor": out / "floor.json", "symbols": out / "symbols.json", "block_audit": out / "block_audit.json"}
        paths["floor"].write_text(json.dumps(self.summary(), indent=1, default=str), encoding="utf-8")
        paths["symbols"].write_text(json.dumps([s.as_dict(self.to_m) for s in self.symbols], indent=0, default=str),
                                    encoding="utf-8")
        paths["block_audit"].write_text(json.dumps(self.block_audit.as_dict(), indent=1, default=str), encoding="utf-8")
        return {k: str(v) for k, v in paths.items()}
