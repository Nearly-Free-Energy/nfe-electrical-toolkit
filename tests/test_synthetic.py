"""Tests on a small synthetic drawing, so they run without any client drawing.

The drawing is one 4 x 4 m room in metres, a room label, and a SOCKET block like NFE's existing ones
drawn in millimetres with its graphics ~950 m from the block's base point.

The last tests cover what differs on Windows: text encodings, files held open by another program,
read-only input copies and finding LibreDWG. The LibreDWG test skips when LibreDWG is not installed,
unless NFE_REQUIRE_LIBREDWG is set (as in CI's Windows LibreDWG job).
"""
from __future__ import annotations

import csv
import json
import math
import os
import subprocess
from pathlib import Path

import ezdxf
import pytest

from nfe_electrical import cli, convert, mcp_server
from nfe_electrical.convert import ConversionError, find_tool
from nfe_electrical.floor import Floor
from nfe_electrical.overlay import Overlay
from nfe_electrical.placement import circuit_run, parse_rule, place_device
from nfe_electrical.rooms import dashed_chains
from nfe_electrical.render import plot_sheet
from nfe_electrical.rules import apply_rules, load_rules, normalise_standard, resolve_count
from nfe_electrical.units import detect_units
from nfe_electrical.workspace import Workspace, remove_workdir

FAR = (946933.0, -52586.0)  # where the pilot drawings' SOCKET block draws, in block units
SCALE = 0.00129


def _socket_block(doc):
    blk = doc.blocks.new("SOCKET")
    cx, cy = FAR
    blk.add_lwpolyline([(cx - 95, cy - 200), (cx + 95, cy - 200), (cx + 95, cy - 92), (cx - 95, cy - 92)],
                       dxfattribs={"layer": "sockets wall"})
    blk.add_line((cx, cy + 3), (cx, cy + 205), dxfattribs={"layer": "sockets wall"})
    blk.add_attdef("S", insert=(cx + 60, cy + 100), text="S", dxfattribs={"height": 80, "flags": 1})
    return blk


def _drawing(path, label="Bedroom", socket=True):
    """Write the synthetic drawing to `path` (DXF) and return the path."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6
    for name in ("wall", "Text Room", "sockets wall"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True, dxfattribs={"layer": "wall"})
    msp.add_lwpolyline([(-0.2, -0.2), (4.2, -0.2), (4.2, 4.2), (-0.2, 4.2)], close=True, dxfattribs={"layer": "wall"})
    msp.add_text(label, height=0.15, dxfattribs={"layer": "Text Room"}).set_placement((1.5, 2.0))
    if socket:
        _socket_block(doc)
        # one existing socket against the south wall, back (block -y) to the wall, drawn centre 0.265 m off it
        g_cy = FAR[1] + 2.5  # centre of the block's drawn extents (box -200..-92 and stem +3..+205)
        ref = msp.add_blockref("SOCKET", (2.0 - SCALE * FAR[0], 0.265 - SCALE * g_cy),
                               dxfattribs={"layer": "sockets wall", "xscale": SCALE, "yscale": SCALE})
        ref.add_auto_attribs({"S": "S"})
    lay = doc.layouts.new("SMALLPOWER")
    lay.add_viewport(center=(6, 4), size=(10, 7), view_center_point=(2, 2), view_height=7)
    doc.saveas(path)
    return path


@pytest.fixture(scope="module")
def floor(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("synthetic")
    return Floor.open(_drawing(tmp / "synthetic.dxf"), tmp / "work", floor_id="T1")


def test_reader_finds_symbol_by_drawn_centre(floor):
    sym = [s for s in floor.symbols if s.block == "SOCKET"]
    assert len(sym) == 1
    s = sym[0]
    assert math.dist(s.centre, (2.0, 0.265)) < 0.01      # drawn centre, not the insertion point
    assert s.offset > 1000                                  # the insertion point is over a kilometre away
    assert s.on_plan and s.device_type == "socket_twin_13a"
    assert floor.block_audit.far_blocks == 1


def test_units_and_base(floor):
    assert floor.units.detected == "m" and floor.units.declared_ok
    assert floor.base.plotted == "NATIVE" and "wall" in floor.base.wall_layers
    assert floor.plan_sheet.layout == "SMALLPOWER"


def test_room_found(floor):
    rooms = floor.rooms.rooms
    assert len(rooms) == 1 and rooms[0].name == "Bedroom"
    assert 15.0 < rooms[0].area_m2 < 16.2
    assert floor.symbols[0].room_id == rooms[0].room_id


def test_place_on_named_wall_faces_the_wall(floor):
    ov = Overlay.open(floor, "t_north", fresh=True)
    res = place_device(floor, ov, "Bedroom", "socket_twin_13a", "wall:north")
    assert len(res["placed"]) == 1
    p = res["placed"][0]
    assert abs(p["target"][1] - (4.0 - 0.265)) < 0.03       # learned 0.265 m off the north face
    assert abs(((p["rotation"] - 180) + 180) % 360 - 180) < 1  # back of the symbol turned to the wall
    assert p["centre_error_mm"] < 1.0
    val = ov.validate(libredwg=False)
    assert val["only_nfe_layers"] and val["ezdxf"]["audit_errors"] == 0 and val["ezdxf"]["counts_match"]
    assert set(val["model_space_layers"]) == {"NFE-E-POWER"}


def test_base_point_offset_is_corrected_without_recentring(floor):
    ov = Overlay.open(floor, "t_far", fresh=True, recentre_blocks=False)
    p = place_device(floor, ov, "Bedroom", "socket_twin_13a", "wall:east")["placed"][0]
    assert math.dist(p["insert"], p["target"]) > 1000        # inserted ~1.2 km away ...
    assert p["centre_error_mm"] < 1.0                         # ... yet drawn exactly on target
    item = ov.devices()[0]
    assert math.dist(item["centre"], p["target"]) < 0.001


def test_dry_run_writes_nothing(floor):
    ov = Overlay.open(floor, "t_dry", fresh=True)
    res = place_device(floor, ov, "Bedroom", "socket_twin_13a", "perimeter:3", dry_run=True)
    assert res["proposed"] == 3 and ov.devices() == []


def test_circuit_run_is_orthogonal(floor):
    ov = Overlay.open(floor, "t_cct", fresh=True)
    placed = place_device(floor, ov, "Bedroom", "socket_twin_13a", "perimeter:3")["placed"]
    res = circuit_run(floor, ov, [p["handle"] for p in placed], "C1", start="0.5,0.5")
    pts = res["points"]
    assert res["length_m"] > 0 and len(pts) >= 3
    assert all(abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9 for a, b in zip(pts, pts[1:]))
    assert all(d.get("attribs", {}).get("NFE_CIRCUIT") == "C1" for d in ov.devices())


def test_sheet_plot_with_overlay(floor, tmp_path):
    ov = Overlay.open(floor, "t_sheet", fresh=True)
    place_device(floor, ov, "Bedroom", "socket_twin_13a", "wall:north")
    res = plot_sheet(floor, tmp_path / "sheet.pdf", overlay=ov, mode="issue")
    assert (tmp_path / "sheet.pdf").stat().st_size > 1000
    assert res["overlay_entities"] == 1 and res["existing_symbols_hidden"] == 1   # the old socket is replaced
    assert res["layout"] == "SMALLPOWER"


def test_rule_sets_must_match_the_job_standard(floor, tmp_path):
    assert normalise_standard("bs7671") == "BS 7671" and normalise_standard(None) == "BS 7671"
    rules_file = tmp_path / "iec.yaml"
    rules_file.write_text("name: test\nstandard: IEC 60364\nrooms:\n  bedroom:\n"
                          "    - {type: socket_twin_13a, count: 1, place: perimeter}\n", encoding="utf-8")
    rules = load_rules(rules_file, floor.catalogue)
    assert floor.standard == "BS 7671"
    with pytest.raises(ValueError, match="governed by BS 7671"):
        apply_rules(floor, Overlay.open(floor, "t_std", fresh=True), rules)
    (tmp_path / "none.yaml").write_text("name: x\nrooms: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="which standard"):
        load_rules(tmp_path / "none.yaml", floor.catalogue)


def test_parse_rule_and_counts():
    assert parse_rule("perimeter:3")["count"] == 3
    spec = parse_rule("pair:bed_head")
    assert spec["strategy"] == "pair" and spec["wall"] == "bed_head" and spec["count"] == 2
    assert parse_rule({"strategy": "wall", "wall": ["media_wall", "facing_lounge"]})["wall"][1] == "facing_lounge"
    with pytest.raises(ValueError):
        parse_rule("wall:sideways")
    assert resolve_count({"per_m2": 8, "min": 2, "max": 6}, 30.0) == 4
    assert resolve_count({"per_m2": 8, "min": 2, "max": 6}, 5.0) == 2


def test_units_detects_mislabelled_millimetres():
    # declared metres (6) but drawn in mm: 1500 units per paper inch, 270 mm symbols, 150 mm text, 41 m plan
    rep = detect_units(6, 1500.0, 25.4, [270.0, 300.0], [150.0], (0, 0, 40800, 29000))
    assert rep.detected == "mm" and not rep.declared_ok and rep.confidence == 1.0


def test_dashed_lines_are_recognised():
    doc = ezdxf.new()
    msp = doc.modelspace()
    dashes = [msp.add_line((x, 0), (x + 0.15, 0)) for x in [i * 0.3 for i in range(10)]]
    solid = msp.add_line((0, 1), (3, 1))
    short_pair = [msp.add_line((0, 2), (0.2, 2)), msp.add_line((0.5, 2), (0.7, 2))]
    found = dashed_chains(dashes + [solid] + short_pair, 1.0)
    assert {id(d) for d in dashes} <= found
    assert id(solid) not in found and not ({id(s) for s in short_pair} & found)


# ---------------------------------------------------------------- dry runs, previews and checks
def _rules(tmp_path, body: str):
    path = tmp_path / "rules.yaml"
    path.write_text("name: test\nstandard: BS 7671\n" + body)
    return path


def _targets(report: dict) -> list:
    return [i["target"] for r in report["rooms"] for p in r["placed"] for i in p["items"]]


def test_dry_run_places_exactly_as_a_write_would(floor, tmp_path):
    # three twins fill the north wall, so a fourth socket has to go elsewhere; a dry run that ignored
    # what the earlier rule proposed would put it on top of one of them
    rules = load_rules(_rules(tmp_path, "defaults: {min_spacing: 0.8}\nrooms:\n  bedroom:\n"
                              "    - {type: socket_twin_13a, count: 3, place: wall, wall: north}\n"
                              "    - {type: socket_twin_usb, count: 1, place: wall, wall: [north, east]}\n"),
                       floor.catalogue)
    dry_ov = Overlay.open(floor, "t_dry_rules", fresh=True)
    dry = apply_rules(floor, dry_ov, rules, dry_run=True)
    real = apply_rules(floor, Overlay.open(floor, "t_real_rules", fresh=True), rules)
    pts = _targets(dry)
    assert len(pts) == 4 and pts == _targets(real)
    assert min(math.dist(a, b) for k, a in enumerate(pts) for b in pts[k + 1:]) >= 0.8 - 1e-6
    assert dry_ov.devices() == [] and not dry_ov.path.exists()
    assert not any("handle" in i for r in dry["rooms"] for p in r["placed"] for i in p["items"])


def test_a_rule_set_that_fails_part_way_writes_nothing(floor, tmp_path):
    rules = load_rules(_rules(tmp_path, "rooms:\n  bedroom:\n"
                              "    - {type: socket_twin_13a, count: 2, place: wall, wall: north}\n"
                              "    - {type: socket_twin_high_level, count: 1, place: wall, wall: south}\n"),
                       floor.catalogue)  # the drawing has no HIGHLEVEL block to copy
    ov = Overlay.open(floor, "t_all_or_nothing", fresh=True)
    with pytest.raises(KeyError, match="HIGHLEVEL"):
        apply_rules(floor, ov, rules)
    assert ov.devices() == [] and not ov.path.exists()  # the first rule's sockets are not kept either


def test_cli_dry_run_lists_positions_and_render_previews_rules(floor, tmp_path, capsys):
    rules = _rules(tmp_path, "rooms:\n  bedroom:\n    - {type: socket_twin_13a, count: 2, place: perimeter}\n")
    work = tmp_path / "work"
    common = [str(floor.source), "--workdir", str(work), "--floor-id", "P1"]
    cli.main(["apply-rules", *common, "--rules", str(rules), "--overlay", "plan", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["devices"] == 2 and len(out["rooms"][0]["devices"][0]["at"]) == 2
    png = tmp_path / "preview.png"
    cli.main(["render", *common, "--overlay", "plan", "--rules", str(rules), "--out", str(png)])
    res = json.loads(capsys.readouterr().out)
    assert res["preview"]["devices"] == 2 and png.stat().st_size > 1000
    assert not list((work / "P1" / "overlays").glob("*.dxf"))  # neither wrote an overlay


def test_mcp_render_review_previews_a_rule_set(floor, tmp_path, monkeypatch):
    rules = _rules(tmp_path, "rooms:\n  bedroom:\n    - {type: socket_twin_13a, count: 2, place: perimeter}\n")
    monkeypatch.setitem(mcp_server.STATE["floors"], floor.floor_id, floor)
    info = json.loads(mcp_server.render_review(floor.floor_id, overlay="t_mcp_preview", rules_path=str(rules),
                                               fmt="pdf")[0])
    assert info["preview"]["devices"] == 2 and Path(info["file"]).stat().st_size > 1000
    assert not (floor.out_dir / "overlays" / "t_mcp_preview.dxf").exists()


def test_title_block_scale_is_checked_against_the_plot(floor, tmp_path):
    from nfe_electrical.checks import check_title_blocks
    measured = floor.plan_sheet.scale_ratio(floor.to_m)
    for stated, flagged in ((f"1:{measured / 2.5:.0f}", True), (f"1:{measured:.0f} @ A1", False), ("NTS", True)):
        doc = ezdxf.readfile(str(_drawing(tmp_path / "tb.dxf")))
        blk = doc.blocks.new("TB")
        for text, y in (("DRAWING NO", 20), ("E-201", 15), ("SCALE", 10), (stated, 5)):
            blk.add_text(text, height=2.5).set_placement((0, y))
        doc.layouts.get("SMALLPOWER").add_blockref("TB", (300, 0))
        doc.saveas(tmp_path / "tb.dxf")
        f = Floor.open(tmp_path / "tb.dxf", tmp_path / f"work{len(stated)}{flagged}", floor_id="TB")
        found = [i["message"] for i in check_title_blocks(f)["issues"] if i["check"] == "scale"]
        assert bool(found) == flagged, (stated, found)


def test_renders_show_existing_symbols_in_blue(floor, tmp_path):
    import numpy as np
    from PIL import Image
    from nfe_electrical.render import EXISTING, render_crop, room_window
    blue = np.array([int(EXISTING[k:k + 2], 16) for k in (1, 3, 5)])
    window = room_window(floor, [floor.rooms.rooms[0].room_id])

    def blue_pixels(path) -> int:  # the legend's blue patch is in both renders
        px = np.asarray(Image.open(path).convert("RGB")).reshape(-1, 3).astype(int)
        return int((np.abs(px - blue).sum(axis=1) < 150).sum())  # grey and white are 270+ away
    shown = blue_pixels(render_crop(floor, tmp_path / "a.png", window=window, existing_categories=("power",)))
    hidden = blue_pixels(render_crop(floor, tmp_path / "b.png", window=window, existing_categories=()))
    assert shown > hidden + 100


# ---------------------------------------------------------------- Windows and other platforms
def test_reports_are_utf8(tmp_path):
    # Ω is outside cp1252, the code page Python 3.11-3.14 writes text files in on most Windows machines
    src = _drawing(tmp_path / "plan.dxf", label="Bedroom Ω", socket=False)
    cli.main(["analyse", str(src), "--workdir", str(tmp_path / "work"), "--floor-id", "U1"])
    out = tmp_path / "work" / "U1"
    assert "R01 Bedroom Ω" in (out / "checks.md").read_text(encoding="utf-8")  # "... has no socket outlets"
    with open(out / "device_counts.csv", newline="", encoding="utf-8-sig") as fh:  # with a BOM, for Excel
        assert next(csv.DictReader(fh))["name"] == "Bedroom Ω"


def test_overlay_open_in_another_program(floor, monkeypatch):
    ov = Overlay.open(floor, "t_open", fresh=True)
    ov.save()
    monkeypatch.setitem(mcp_server.STATE["overlays"], (floor.floor_id, "t_open"), ov)
    with monkeypatch.context() as m, open(ov.path, "rb"):  # held open, as a CAD program holds a drawing
        if os.name != "nt":  # only Windows refuses to replace a file that is open; pretend elsewhere
            def refuse(self, target):
                raise PermissionError(13, "Permission denied")
            m.setattr(Path, "replace", refuse)
        with pytest.raises(PermissionError, match="Close it and try again"):
            place_device(floor, ov, "Bedroom", "socket_twin_13a", "wall:north")
    assert ov.save_failed and ov.path.with_suffix(".tmp.dxf").is_file()  # the new version is kept
    again = mcp_server._overlay(floor, "t_open")  # the MCP server starts again from the saved file ...
    assert again is not ov and again.devices() == []
    place_device(floor, again, "Bedroom", "socket_twin_13a", "wall:north")  # ... so repeating the step adds one socket
    assert not again.save_failed and len(Overlay.open(floor, "t_open").devices()) == 1


def test_work_folder_with_read_only_inputs_can_be_removed(tmp_path):
    ws = Workspace(tmp_path / "work")
    copy = ws.import_drawing(_drawing(tmp_path / "plan.dxf"), "T3")
    assert not os.access(copy, os.W_OK)  # the input copy is read-only, which stops shutil.rmtree on Windows
    remove_workdir(ws.root)
    assert not ws.root.exists()
    with pytest.raises(ValueError, match="does not look like a work folder"):
        remove_workdir(tmp_path)


def test_find_tool_ignores_the_current_folder(tmp_path, monkeypatch):
    # shutil.which on Windows looks in the current folder first: a folder of drawings must not supply dwg2dxf
    exe = "dwg2dxf.exe" if os.name == "nt" else "dwg2dxf"
    (tmp_path / exe).write_bytes(b"")
    (tmp_path / exe).chmod(0o755)
    (tmp_path / "bin").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("NFE_DWG2DXF", raising=False)
    monkeypatch.setattr(convert, "_COMMON_PATHS", ())
    with pytest.raises(ConversionError, match="not found"):
        find_tool("dwg2dxf")


def test_find_tool_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "_WINDOWS", True)  # Windows lookup rules, checked on every platform
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    tool = tmp_path / "LibreDWG" / "dwg2dxf.exe"
    tool.parent.mkdir()
    tool.write_bytes(b"")
    monkeypatch.setenv("NFE_DWG2DXF", f'"{tool.with_suffix("")}"')  # quoted and without .exe, as cmd's `set` leaves it
    assert find_tool("dwg2dxf") == str(tool)
    monkeypatch.setenv("NFE_DWG2DXF", str(tmp_path / "missing.exe"))
    with pytest.raises(ConversionError, match="NFE_DWG2DXF is set"):
        find_tool("dwg2dxf")
    monkeypatch.delenv("NFE_DWG2DXF")
    with pytest.raises(ConversionError, match="win64.zip"):  # the install hint
        find_tool("dwg2dxf")
    unzipped = tmp_path / "AppData" / "Programs" / "LibreDWG" / "dwg2dxf.exe"  # where the README unzips it
    unzipped.parent.mkdir(parents=True)
    unzipped.write_bytes(b"")
    assert find_tool("dwg2dxf") == str(unzipped)
    on_path = tmp_path / "bin" / "dwg2dxf.exe"
    on_path.parent.mkdir()
    on_path.write_bytes(b"")
    on_path.chmod(0o755)
    assert find_tool("dwg2dxf") == str(on_path)  # PATH comes first


def test_dwg_through_libredwg(tmp_path):
    try:
        dxf2dwg = find_tool("dxf2dwg")
        find_tool("dwg2dxf")
    except ConversionError as exc:
        if os.environ.get("NFE_REQUIRE_LIBREDWG"):
            raise
        pytest.skip(str(exc))
    dwg = tmp_path / "synthetic.dwg"
    subprocess.run([dxf2dwg, "-y", "-o", str(dwg), str(_drawing(tmp_path / "synthetic.dxf"))], cwd=tmp_path,
                   capture_output=True, timeout=300, check=True)
    floor = Floor.open(dwg, tmp_path / "work", floor_id="T2")  # read back through dwg2dxf
    assert floor.conversion is not None and not floor.conversion.cached
    sym = [s for s in floor.symbols if s.block == "SOCKET"]
    assert len(sym) == 1 and math.dist(sym[0].centre, (2.0, 0.265)) < 0.01
    assert floor.units.detected == "m" and [r.name for r in floor.rooms.rooms] == ["Bedroom"]
    ov = Overlay.open(floor, "t_dwg", fresh=True)
    place_device(floor, ov, "Bedroom", "socket_twin_13a", "perimeter:3")
    lib = ov.validate(libredwg=True)["libredwg"]  # dxf2dwg -> dwgread -> dwg2dxf on the overlay
    assert lib["ran"] and lib["inserts_match"] and lib["attribs_match"]
