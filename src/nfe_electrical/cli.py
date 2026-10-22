"""Command line: `nfe-elec <command> <drawing> --workdir DIR ...` (run with `python -I -m nfe_electrical.cli`)."""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys

from . import checks as checks_mod
from . import render as render_mod
from .compare import SOCKET_TYPES, compare_devices, comparison_markdown, mirror_consistency
from .floor import Floor
from .overlay import Overlay
from .placement import mirror_unit
from .rules import apply_rules, load_rules, preview_rules


def _open(args) -> Floor:
    return Floor.open(args.drawing, args.workdir, floor_id=args.floor_id, standard=args.standard)


def cmd_analyse(args) -> dict:
    f = _open(args)
    out = f.out_dir
    paths = f.write_reports()
    paths["rooms_json"] = f.rooms.write(out / "rooms.json")
    paths["rooms_png"] = render_mod.render_rooms(f, out / "rooms.png")
    res = checks_mod.run_all_checks(f)
    (out / "checks.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    (out / "checks.md").write_text(checks_mod.checks_markdown(res), encoding="utf-8")
    paths["device_counts_csv"] = checks_mod.write_device_counts_csv(res["device_counts"], out / "device_counts.csv")
    s = f.summary()
    return {"floor_id": f.floor_id, "units": s["units"]["detected"], "declared_units_ok": s["units"]["declared_ok"],
            "plotted_base": s["base"]["plotted"], "plan": s["plan"], "symbols_on_plan": s["symbols"]["on_plan"],
            "far_basepoint_blocks": s["block_offsets"]["far_basepoint_blocks"], "rooms": f.rooms.summary(),
            "check_issues": res["summary"], "files": paths, "timings": f.timings}


def cmd_rooms(args) -> dict:
    f = _open(args)
    return {"summary": f.rooms.summary(), "rooms_json": f.rooms.write(f.out_dir / "rooms.json"),
            "rooms_png": render_mod.render_rooms(f, f.out_dir / "rooms.png")}


def cmd_check(args) -> dict:
    f = _open(args)
    res = checks_mod.run_all_checks(f)
    (f.out_dir / "checks.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    (f.out_dir / "checks.md").write_text(checks_mod.checks_markdown(res), encoding="utf-8")
    return {"summary": res["summary"], "files": [str(f.out_dir / "checks.json"), str(f.out_dir / "checks.md")]}


def _placements(rep: dict) -> list[dict]:
    """Per room, what each rule placed: [x, y, rotation] of every device's drawn centre."""
    return [{"room": r["room"], "name": r["name"], "unit": r["unit"],
             "devices": [{"type": p["type"], "rule": p["rule"], "requested": p["requested"], "placed": p["placed"],
                          "at": [[round(i["target"][0], 3), round(i["target"][1], 3), round(i["rotation"], 1)]
                                 for i in p["items"]]} for p in r["placed"]]}
            for r in rep["rooms"]]


def cmd_apply_rules(args) -> dict:
    f = _open(args)
    ov = Overlay.open(f, args.overlay, fresh=args.fresh)
    rep = apply_rules(f, ov, load_rules(args.rules, f.catalogue), units=args.units, rooms=args.rooms,
                      dry_run=args.dry_run)
    return {"devices": rep["devices_placed"], "warnings": rep["warnings"], "rooms": _placements(rep),
            "overlay": str(ov.path), "dry_run": args.dry_run}


def cmd_mirror(args) -> dict:
    f = _open(args)
    ov = Overlay.open(f, args.overlay)
    res = mirror_unit(f, ov, args.from_unit, args.to_unit, dry_run=args.dry_run)
    return {"copied": len(res["copied"]), "skipped": res["skipped"], "overlay": str(ov.path)}


def cmd_compare(args) -> dict:
    f = _open(args)
    ov = Overlay.open(f, args.overlay)
    ids = [r.room_id for r in f.rooms.rooms if not args.units or r.unit in args.units]
    res = compare_devices(f, ov, ids, SOCKET_TYPES)
    print(comparison_markdown(res, mirror_consistency(f)))
    return res["totals"]


def cmd_render(args) -> dict:
    f = _open(args)
    ov = Overlay.open(f, args.overlay) if args.overlay else None
    ids = None
    if args.rooms or args.room:
        ids = [f.rooms.get(r).room_id for r in (args.rooms or [args.room])]
    elif args.unit:
        ids = [r.room_id for r in f.rooms.rooms if r.unit == args.unit]
    preview = None
    if args.rules:  # draw what the rules would add, on a copy of the overlay that is never saved
        base = ov or Overlay.open(f, "preview", fresh=True)
        ov, rep = preview_rules(f, base, load_rules(args.rules, f.catalogue), rooms=ids if not args.unit else None,
                                units=[args.unit] if args.unit else None)
        preview = {"rules": rep["rules"], "devices": rep["devices_placed"], "warnings": rep["warnings"],
                   "rooms": _placements(rep)}
    window = render_mod.room_window(f, ids) if ids else f.plan_window
    where = "_".join(args.rooms) if args.rooms else args.room or args.unit or "plan"
    out = args.out or str(f.workspace.floor_dir(f.floor_id, "renders") / f"{'preview' if preview else 'render'}_{where}.png")
    res = {"file": render_mod.render_crop(f, out, window=window, overlay=ov, room_ids=ids)}
    if preview:
        res["preview"] = preview
    return res


def cmd_plot_sheet(args) -> dict:
    f = _open(args)
    f.rooms
    ov = Overlay.open(f, args.overlay) if args.overlay else None
    layout = args.layout or render_mod.default_sheet(f, ov)
    out = args.out or str(f.workspace.floor_dir(f.floor_id, "sheets") / f"{f.floor_id}_{layout}_{args.mode}.pdf")
    return render_mod.plot_sheet(f, out, layout=layout, overlay=ov, mode=args.mode)


def cmd_validate(args) -> dict:
    f = _open(args)
    return Overlay.open(f, args.overlay).validate(libredwg=not args.no_libredwg)


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("ezdxf").setLevel(logging.WARNING)
    if isinstance(sys.stdout, io.TextIOWrapper):
        # `compare` prints room names; on Windows, output to a pipe or file would use the ANSI code page
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="nfe-elec", description="NFE electrical drawing toolkit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def floor_cmd(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("drawing", help="architect's .dwg or .dxf (copied into the workdir, never modified)")
        p.add_argument("--workdir", required=True, help="where inputs are copied and outputs written")
        p.add_argument("--floor-id", help="short id for the floor, e.g. F6")
        p.add_argument("--standard", default="BS 7671",
                       help="the job's governing standard (default BS 7671; or IEC 60364, US EAS 811-1:2014)")
        p.set_defaults(fn=fn)
        return p

    floor_cmd("analyse", cmd_analyse, "convert, normalise, find rooms and run all read-only checks")
    floor_cmd("rooms", cmd_rooms, "find rooms only")
    floor_cmd("check", cmd_check, "title block, legend and device-count checks")
    p = floor_cmd("apply-rules", cmd_apply_rules, "place devices from a YAML rules file into an overlay")
    p.add_argument("--rules", required=True)
    p.add_argument("--overlay", default="overlay")
    p.add_argument("--units", nargs="*")
    p.add_argument("--rooms", nargs="*")
    p.add_argument("--fresh", action="store_true", help="start the overlay from scratch")
    p.add_argument("--dry-run", action="store_true")
    p = floor_cmd("mirror", cmd_mirror, "copy one unit's overlay devices onto its mirror-image unit")
    p.add_argument("--overlay", default="overlay")
    p.add_argument("--from", dest="from_unit", required=True)
    p.add_argument("--to", dest="to_unit", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = floor_cmd("compare", cmd_compare, "compare overlay sockets with the engineer's existing ones")
    p.add_argument("--overlay", default="overlay")
    p.add_argument("--units", nargs="*")
    p = floor_cmd("render", cmd_render, "PNG/PDF of the overlay over the base")
    p.add_argument("--overlay")
    p.add_argument("--room")
    p.add_argument("--rooms", nargs="+", help="several rooms (ids or names)")
    p.add_argument("--unit")
    p.add_argument("--rules", help="preview a YAML rules file on the room(s), unit or (by default) the rule set's own "
                                   "scope, over --overlay if given; nothing is written")
    p.add_argument("--out", help="output .png or .pdf")
    p = floor_cmd("plot-sheet", cmd_plot_sheet, "plot a paper-space sheet at true size with the overlay (PDF)")
    p.add_argument("--layout", help="layout (sheet) name; default: the sheet matching the overlay's content")
    p.add_argument("--overlay")
    p.add_argument("--mode", choices=["issue", "review"], default="issue",
                   help="issue: overlay replaces the existing symbols it covers; review: overlay in magenta on top")
    p.add_argument("--out", help="output .pdf (or .png)")
    p = floor_cmd("validate", cmd_validate, "re-read the overlay with ezdxf and LibreDWG")
    p.add_argument("--overlay", default="overlay")
    p.add_argument("--no-libredwg", action="store_true")
    p = sub.add_parser("serve", help="run the MCP server on stdio")
    p.add_argument("--workdir")
    p.set_defaults(fn=None)

    args = ap.parse_args(argv)
    if args.cmd == "serve":
        from .mcp_server import main as serve
        serve(["--workdir", args.workdir] if args.workdir else [])
        return 0
    result = args.fn(args)
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
