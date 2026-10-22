"""Tests for what running the pilot rules on held-out floors showed up.

The synthetic drawing is one 4 x 4 m bedroom in metres with one socket, so the floor has no
mirrored units. Its small power sheet carries a stale copy of the plan viewport that is
larger than the real one but looks at empty model space, and a line pasted far off the
paper. A second sheet is named but empty.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from shapely.geometry import box

from nfe_electrical.apartments import detect_mirrored_units
from nfe_electrical.checks import check_sheets
from nfe_electrical.floor import Floor
from nfe_electrical.overlay import Overlay
from nfe_electrical.render import plot_sheet, sheet_page
from nfe_electrical.rooms import Room
from nfe_electrical.rules import apply_rules, load_rules
from nfe_electrical.units import detect_units

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "rules" / "bs7671" / "apartment_type_a_small_power.yaml"


def _drawing(path: Path) -> dict:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6
    for name in ("wall", "Text Room", "sockets wall"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True, dxfattribs={"layer": "wall"})
    msp.add_lwpolyline([(-0.2, -0.2), (4.2, -0.2), (4.2, 4.2), (-0.2, 4.2)], close=True, dxfattribs={"layer": "wall"})
    msp.add_text("Bedroom", height=0.15, dxfattribs={"layer": "Text Room"}).set_placement((1.5, 2.0))
    blk = doc.blocks.new("SOCKET")
    blk.add_lwpolyline([(-0.13, -0.07), (0.13, -0.07), (0.13, 0.07), (-0.13, 0.07)], close=True)
    blk.add_attdef("S", insert=(0.0, 0.1), text="S", dxfattribs={"height": 0.05})
    msp.add_blockref("SOCKET", (2.0, 0.15), dxfattribs={"layer": "sockets wall"}).add_auto_attribs({"S": "S"})
    lay = doc.layouts.new("SMALLPOWER")  # 420 x 297 mm paper, limits (0, 0)-(420, 297)
    lay.add_lwpolyline([(10, 10), (410, 10), (410, 287), (10, 287)], close=True)
    plan = lay.add_viewport(center=(150, 150), size=(200, 140), view_center_point=(2, 2), view_height=7)
    stale = lay.add_viewport(center=(150, 150), size=(201, 140), view_center_point=(500, 500), view_height=7)
    stray = lay.add_line((100, 3000), (120, 3000))
    doc.layouts.new("ICT LAYOUT")
    doc.saveas(path)
    return {"path": path, "plan": plan.dxf.handle, "stale": stale.dxf.handle, "stray": stray.dxf.handle}


@pytest.fixture(scope="module")
def drawing(tmp_path_factory):
    return _drawing(tmp_path_factory.mktemp("held_out") / "held_out.dxf")


@pytest.fixture(scope="module")
def floor(drawing, tmp_path_factory):
    return Floor.open(drawing["path"], tmp_path_factory.mktemp("held_out_work"), floor_id="T2")


def _room(room_id, room_type, x0, y0, x1, y1, service=False):
    poly = box(x0, y0, x1, y1)
    return Room(room_id=room_id, name=room_type.title(), labels=[room_type], room_type=room_type, service=service,
                polygon=poly, area_m2=poly.area, label_point=(poly.centroid.x, poly.centroid.y))


def _mirrored_pair(n, room_type, x0, y0, x1, y1, service=False):
    """A room and its mirror image across x = 10."""
    return [_room(f"R{n}a", room_type, x0, y0, x1, y1, service),
            _room(f"R{n}b", room_type, 20 - x1, y0, 20 - x0, y1, service)]


def test_plan_viewport_is_the_one_that_shows_the_drawing(floor, drawing):
    assert floor.plan_viewport.handle == drawing["plan"]
    assert floor.units.detected == "m" and floor.units.declared_ok
    assert [r.name for r in floor.rooms.rooms] == ["Bedroom"] and floor.symbols[0].on_plan
    issues = check_sheets(floor)["issues"]
    stale = [i for i in issues if i["check"] == "stale_viewport"]
    assert len(stale) == 1 and drawing["stale"] in stale[0]["message"]


def test_declared_units_are_kept_on_thin_evidence():
    # declared mm; only the plot scale and plan size of a viewport drawn for metres vote, both for m
    rep = detect_units(4, 1.5, 25.4, [], [], (-4.3, 99.5, 36.4, 128.6))
    assert rep.detected == "mm" and rep.declared_ok and "too few" in rep.note
    assert rep.confidence == 0.0


def test_mirror_needs_three_pairs_of_rooms_that_are_not_ducts():
    ducts = _mirrored_pair(1, "duct", 0, 0, 1, 1, True) + _mirrored_pair(2, "duct", 0, 5, 1, 6, True)
    assert detect_mirrored_units(ducts, 1.0) is None
    two = ducts + _mirrored_pair(3, "bedroom", 1, 2, 5, 6) + _mirrored_pair(4, "lounge", 2, 8, 8, 14)
    assert detect_mirrored_units(two, 1.0) is None
    m = detect_mirrored_units(two + _mirrored_pair(5, "wc", 5, 0, 7, 2), 1.0)
    assert m is not None and m.axis == "x" and abs(m.value - 10) < 1e-9 and m.sides == ("W", "E")
    assert {r.unit for r in two if r.room_id.endswith("a")} == {"W"}


def test_rules_say_when_their_unit_scope_matches_nothing(floor):
    assert floor.mirror is None
    ov = Overlay.open(floor, "t_scope", fresh=True)
    rep = apply_rules(floor, ov, load_rules(RULES, floor.catalogue), dry_run=True)
    assert rep["devices_placed"] == 0
    assert any("no mirrored units" in w for w in rep["warnings"])


def test_sheet_page_ignores_entities_off_the_paper(floor, drawing, tmp_path):
    sheet = next(s for s in floor.sheets if s.layout == "SMALLPOWER")
    page, off = sheet_page(floor, sheet)
    assert page == pytest.approx((0, 0, 420, 297))
    assert [o["handle"] for o in off] == [drawing["stray"]]
    res = plot_sheet(floor, tmp_path / "sheet.pdf", layout="SMALLPOWER")
    assert res["page_window"] == pytest.approx([0, 0, 420, 297]) and "outside the paper" in res["warning"]
    issues = check_sheets(floor)["issues"]
    assert any(i["check"] == "off_sheet" and i["sheet"] == "SMALLPOWER" for i in issues)


def test_empty_named_sheet_is_reported(floor):
    empty = [i["sheet"] for i in check_sheets(floor)["issues"] if i["check"] == "empty_sheet"]
    assert empty == ["ICT LAYOUT"]  # the default, unused "Layout1" is not reported


def test_demo_runs_on_a_floor_without_mirrored_units(drawing, tmp_path):
    work = tmp_path / "work"
    p = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "demo_f6.py"), "--drawing", str(drawing["path"]),
                        "--workdir", str(work), "--floor-id", "T3"], cwd=tmp_path, capture_output=True, text=True,
                       timeout=600)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    summary = json.loads((work / "T3" / "demo_summary.json").read_text(encoding="utf-8"))
    assert summary["devices_placed"] == 0 and summary["mirror_comparison"] is None and summary["circuit"] is None
    assert any("no mirrored units" in w for w in summary["rule_warnings"])
    assert "no mirrored units found" in (work / "T3" / "comparison.md").read_text(encoding="utf-8")
    assert all(Path(s["file"]).is_file() for s in summary["sheets"])
